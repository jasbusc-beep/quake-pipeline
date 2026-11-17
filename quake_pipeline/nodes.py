"""The three deterministic steps: ingest, transform, publish.

These are plain Python functions. The model never picks a URL, a bucket,
an object name or file contents. All of that is decided here in code.

Each step is split in two: a run_* function that holds the logic and can be
called directly (by tests and by the verify command), and a thin ADK node
that wraps it.
"""

import hashlib
import json
import secrets
import time
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
import pandas as pd
from google.adk import Event
from google.adk.workflow import RetryConfig, node
from pydantic import ValidationError

from . import config, storage
from .logs import log_event
from .quality import build_quality, enforce_ingest_gate
from .regions import region_of
from .sanitize import clean_label, clean_summary
from .schemas import (
    GroupStat,
    IngestResult,
    Lineage,
    OverallStats,
    PipelineRequest,
    ProcessedResult,
    QuakeRecord,
    Source,
    SummaryInput,
)

# Tests swap this for an httpx.MockTransport. In real runs it stays None.
HTTP_TRANSPORT = None

BAND_ORDER = ["Under 2.5", "2.5-4.4", "4.5-5.9", "6.0 and up"]


class TransientUpstreamError(Exception):
    """USGS answered 429 or 5xx, or was too slow. Worth retrying."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


# --- ingest ----------------------------------------------------------------


def select_feed_level(min_magnitude: float) -> str:
    """Pick the smallest USGS feed that still contains everything we need."""
    for level in ("4.5", "2.5", "1.0"):
        if float(level) <= min_magnitude:
            return level
    return "all"


def build_feed_url(level: str, timeframe: str) -> str:
    """Build the USGS URL from allow-listed parts only."""
    if level not in config.ALLOWED_LEVELS:
        raise ValueError("Feed level is not on the allow-list")
    if timeframe not in config.ALLOWED_TIMEFRAMES:
        raise ValueError("Timeframe is not on the allow-list")
    url = config.USGS_URL_TEMPLATE.format(level=level, timeframe=timeframe)
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc != config.USGS_HOST:
        raise ValueError("Refusing to fetch a URL outside the USGS host")
    return url


def new_run_id() -> str:
    return f"{_utc_now():%Y%m%dT%H%M%SZ}-{secrets.token_hex(4)}"


def fetch_feed(url: str) -> bytes:
    """Download the feed with timeouts, no redirects and a size cap.

    The size cap counts decompressed bytes, so a small compressed response
    that expands into a huge one is still stopped.
    """
    start = time.monotonic()
    with httpx.Client(
        timeout=config.HTTP_TIMEOUT_SECONDS,
        follow_redirects=False,
        headers={
            "User-Agent": config.USER_AGENT,
            "Accept": "application/geo+json, application/json",
        },
        transport=HTTP_TRANSPORT,
    ) as client:
        with client.stream("GET", url) as response:
            status = response.status_code
            if status == 429 or status >= 500:
                raise TransientUpstreamError(f"USGS returned status {status}")
            if status != 200:
                raise ValueError(f"USGS returned unexpected status {status}")
            content_type = response.headers.get("content-type", "")
            if "json" not in content_type.lower():
                raise ValueError("USGS response is not JSON")
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > config.MAX_RESPONSE_BYTES:
                    raise ValueError("USGS response is larger than the size cap")
                if time.monotonic() - start > config.MAX_DOWNLOAD_SECONDS:
                    raise TransientUpstreamError("USGS download took too long")
    return bytes(body)


def parse_feed(raw: bytes) -> tuple[list, dict]:
    """Check the body is a GeoJSON FeatureCollection.

    Returns (features, metadata). Metadata is {} if the feed has none.
    """
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError) as err:
        # Bad JSON, bad UTF-8, or nesting deep enough to exhaust the stack.
        raise ValueError("USGS response is not valid JSON") from err
    if (
        not isinstance(data, dict)
        or data.get("type") != "FeatureCollection"
        or not isinstance(data.get("features"), list)
    ):
        raise ValueError("USGS response is not a GeoJSON FeatureCollection")
    metadata = data.get("metadata")
    return data["features"], metadata if isinstance(metadata, dict) else {}


def _plain_int(value) -> int | None:
    """An int that is really an int (JSON true/false are not counts)."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def feed_declared_count(metadata: dict) -> int | None:
    count = _plain_int(metadata.get("count"))
    return count if count is not None and count >= 0 else None


def feed_generated_at(metadata: dict) -> str | None:
    generated = _plain_int(metadata.get("generated"))
    if generated is None or not 0 <= generated <= config.TIME_MS_MAX:
        return None
    moment = datetime.fromtimestamp(generated / 1000, tz=UTC)
    return moment.isoformat(timespec="seconds")


def to_records(features: list) -> tuple[list[QuakeRecord], int]:
    """Keep only the fields we use. Returns (records, number skipped).

    A feature is skipped if it is malformed or has a value outside the
    plausible range (see config). Skips are counted, never silent.
    """
    records, skipped = [], 0
    for feature in features:
        try:
            props = feature["properties"]
            place = props.get("place")
            if isinstance(place, str):
                place = place[: config.MAX_PLACE_CHARS]
            records.append(
                QuakeRecord(
                    id=feature["id"],
                    mag=props.get("mag"),
                    place=place,
                    time_ms=props["time"],
                    depth_km=feature["geometry"]["coordinates"][2],
                    event_type=props.get("type"),
                )
            )
        except (ValidationError, KeyError, TypeError, IndexError, AttributeError):
            skipped += 1
    return records, skipped


def run_ingest(request: PipelineRequest) -> IngestResult:
    # Fail fast: stop before any network call if the bucket is not configured.
    config.get_bucket_name()
    request = PipelineRequest.model_validate(request)
    level = select_feed_level(request.min_magnitude)
    url = build_feed_url(level, request.timeframe)

    start = time.monotonic()
    raw = fetch_feed(url)
    fetch_ms = _elapsed_ms(start)
    fetched_at = _utc_now().isoformat(timespec="seconds")

    features, metadata = parse_feed(raw)
    records, skipped = to_records(features)

    # Land the raw file first, so a run that fails the quality gate below
    # still leaves the evidence behind for whoever investigates.
    run_id = new_run_id()  # a fresh id on every attempt
    raw_object = storage.raw_object_name(run_id)
    storage.upload(raw_object, raw, "application/geo+json")
    raw_sha256 = hashlib.sha256(raw).hexdigest()
    log_event(
        "ingest.landed",
        run_id=run_id,
        raw_object=raw_object,
        raw_bytes=len(raw),
        raw_sha256=raw_sha256,
        feed_count=len(features),
        skipped_invalid=skipped,
        fetch_ms=fetch_ms,
    )

    enforce_ingest_gate(skipped, len(features))

    return IngestResult(
        run_id=run_id,
        request=request,
        source_url=url,
        feed_level=level,
        fetched_at=fetched_at,
        fetch_ms=fetch_ms,
        raw_object=raw_object,
        raw_sha256=raw_sha256,
        raw_bytes=len(raw),
        feed_count=len(features),
        feed_declared_count=feed_declared_count(metadata),
        feed_generated_at=feed_generated_at(metadata),
        skipped_invalid=skipped,
        records=records,
    )


# Up to 3 attempts. ADK waits longer after each failure (1s, then about 2s)
# with some randomness, so retries do not hammer a struggling server.
@node(
    retry_config=RetryConfig(
        max_attempts=3,
        initial_delay=1.0,
        exceptions=[httpx.TransportError, TransientUpstreamError],
    )
)
def ingest(node_input: PipelineRequest) -> IngestResult:
    return run_ingest(node_input)


# --- transform -------------------------------------------------------------


def band_of(mag: float) -> str:
    if mag < 2.5:
        return BAND_ORDER[0]
    if mag < 4.5:
        return BAND_ORDER[1]
    if mag < 6.0:
        return BAND_ORDER[2]
    return BAND_ORDER[3]


def _round_or_none(value) -> float | None:
    return None if pd.isna(value) else round(float(value), 2)


def _group_table(df: pd.DataFrame, group_by: str) -> pd.DataFrame:
    table = (
        df.groupby(group_by)
        .agg(
            count=("mag", "size"),
            avg_magnitude=("mag", "mean"),
            max_magnitude=("mag", "max"),
            avg_depth_km=("depth_km", "mean"),
        )
        .reset_index()
        .rename(columns={group_by: "label"})
    )
    if group_by == "day":
        return table.sort_values("label")
    if group_by == "magnitude_band":
        return table.sort_values("label", key=lambda s: s.map(BAND_ORDER.index))
    # region: biggest groups first (name breaks ties), keep the top few
    return table.sort_values(["count", "label"], ascending=[False, True]).head(
        config.TOP_GROUPS
    )


def run_transform(ingested: IngestResult) -> tuple[SummaryInput, dict]:
    """Filter, derive, group and aggregate.

    Returns (what the summary agent may see, what publish needs).
    The same input always gives the same output, which is what lets the
    verify command recompute a stored result and compare.
    """
    ingested = IngestResult.model_validate(ingested)
    request = ingested.request

    columns = list(QuakeRecord.model_fields)
    df = pd.DataFrame([r.model_dump() for r in ingested.records], columns=columns)
    df["mag"] = pd.to_numeric(df["mag"])
    df["depth_km"] = pd.to_numeric(df["depth_km"])

    # Each filter step is counted so every row can be accounted for.
    before = len(df)
    df = df.drop_duplicates(subset="id", keep="first")
    duplicates_removed = before - len(df)

    is_quake = df["event_type"].map(lambda t: t == "earthquake").astype(bool)
    non_earthquake = int((~is_quake).sum())
    df = df[is_quake]

    has_mag = df["mag"].notna()
    missing_magnitude = int((~has_mag).sum())
    df = df[has_mag]

    big_enough = df["mag"] >= request.min_magnitude
    below_threshold = int((~big_enough).sum())
    df = df[big_enough].copy()

    groups: list[GroupStat] = []
    if df.empty:
        stats = OverallStats(total_events=0)
    else:
        when = pd.to_datetime(df["time_ms"], unit="ms", utc=True)
        df["day"] = when.dt.strftime("%Y-%m-%d")
        df["region"] = df["place"].map(region_of)
        df["magnitude_band"] = df["mag"].map(band_of)

        for row in _group_table(df, request.group_by).itertuples(index=False):
            groups.append(
                GroupStat(
                    label=clean_label(row.label),
                    count=int(row.count),
                    avg_magnitude=round(float(row.avg_magnitude), 2),
                    max_magnitude=round(float(row.max_magnitude), 2),
                    avg_depth_km=_round_or_none(row.avg_depth_km),
                )
            )

        strongest = df.loc[df["mag"].idxmax()]
        stats = OverallStats(
            total_events=len(df),
            avg_magnitude=round(float(df["mag"].mean()), 2),
            max_magnitude=round(float(df["mag"].max()), 2),
            strongest_place=clean_label(strongest["place"]),
            strongest_time=when.loc[strongest.name].isoformat(timespec="seconds"),
        )

    quality = build_quality(
        ingested,
        duplicates_removed=duplicates_removed,
        non_earthquake=non_earthquake,
        missing_magnitude=missing_magnitude,
        below_threshold=below_threshold,
        matched=len(df),
        missing_depth=int(df["depth_km"].isna().sum()),
    )
    lineage = Lineage(
        raw_object=ingested.raw_object,
        raw_sha256=ingested.raw_sha256,
        raw_bytes=ingested.raw_bytes,
        feed_level=ingested.feed_level,
        feed_generated_at=ingested.feed_generated_at,
        pipeline_version=config.PIPELINE_VERSION,
        model=config.GEMINI_MODEL,
    )

    summary_input = SummaryInput(request=request, stats=stats, groups=groups)
    processed = {
        "run_id": ingested.run_id,
        "request": request.model_dump(mode="json"),
        "source_url": ingested.source_url,
        "fetched_at": ingested.fetched_at,
        "fetch_ms": ingested.fetch_ms,
        "lineage": lineage.model_dump(mode="json"),
        "quality": quality.model_dump(mode="json"),
        "stats": stats.model_dump(mode="json"),
        "groups": [g.model_dump(mode="json") for g in groups],
    }
    return summary_input, processed


def transform(node_input: IngestResult):
    start = time.monotonic()
    summary_input, processed = run_transform(node_input)
    processed["transform_ms"] = _elapsed_ms(start)
    quality = processed["quality"]
    log_event(
        "transform.done",
        run_id=processed["run_id"],
        matched=quality["matched"],
        groups=len(processed["groups"]),
        warnings=[c["name"] for c in quality["checks"] if c["status"] != "pass"],
        transform_ms=processed["transform_ms"],
    )
    # Only the aggregates go to the summary agent. Raw records stop here.
    return Event(output=summary_input, state={"processed": processed})


# --- publish ---------------------------------------------------------------


def fallback_summary(processed: dict) -> str:
    total = processed["stats"]["total_events"]
    request = processed["request"]
    return (
        f"{total} earthquakes of magnitude {request['min_magnitude']} or "
        f"higher were recorded in the past {request['timeframe']}."
    )


def run_publish(summary_text, processed: dict) -> tuple[ProcessedResult, str]:
    cleaned = clean_summary(summary_text)
    result = ProcessedResult(
        run_id=processed["run_id"],
        generated_at=_utc_now().isoformat(timespec="seconds"),
        request=processed["request"],
        source=Source(
            name=config.SOURCE_NAME,
            url=processed["source_url"],
            fetched_at=processed["fetched_at"],
        ),
        lineage=processed["lineage"],
        quality=processed["quality"],
        stats=processed["stats"],
        groups=processed["groups"],
        summary=cleaned or fallback_summary(processed),
        summary_source="model" if cleaned else "fallback",
        timings_ms={
            "fetch": processed.get("fetch_ms", 0),
            "transform": processed.get("transform_ms", 0),
        },
    )
    object_name = storage.processed_object_name(result.run_id)
    storage.upload(
        object_name,
        result.model_dump_json(indent=2),
        "application/json; charset=utf-8",
    )
    log_event(
        "publish.done",
        run_id=result.run_id,
        processed_object=object_name,
        total_events=result.stats.total_events,
        groups=len(result.groups),
        summary_source=result.summary_source,
    )
    return result, object_name


def publish(node_input: str, processed: dict):
    result, object_name = run_publish(node_input, processed)
    return Event(
        message=(
            f"Pipeline complete. {result.stats.total_events} events, "
            f"{len(result.groups)} groups. Saved {object_name}. "
            "Run: python viewer/serve.py"
        ),
        output=object_name,
    )
