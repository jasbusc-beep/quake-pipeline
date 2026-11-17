import hashlib
import itertools
import json

import httpx
import pytest

from conftest import collection_of, feature, json_response, ms
from quake_pipeline import config, nodes
from quake_pipeline.quality import DataQualityError
from quake_pipeline.schemas import PipelineRequest

REQUEST = PipelineRequest(timeframe="week", min_magnitude=4.5, group_by="day")


# --- URL builder -----------------------------------------------------------


@pytest.mark.parametrize(
    "level,timeframe",
    list(itertools.product(sorted(config.ALLOWED_LEVELS), sorted(config.ALLOWED_TIMEFRAMES))),
)
def test_url_builder_allowed(level, timeframe):
    assert nodes.build_feed_url(level, timeframe) == (
        "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/"
        f"{level}_{timeframe}.geojson"
    )


@pytest.mark.parametrize(
    "level,timeframe",
    [
        ("significant", "week"),
        ("4.5", "year"),
        ("../../x", "week"),
        ("4.5", "week.geojson?x=http://example.com/"),
        ("all@example.com/", "day"),
        ("", ""),
        (4.5, "week"),
    ],
)
def test_url_builder_rejects_everything_else(level, timeframe):
    with pytest.raises(ValueError):
        nodes.build_feed_url(level, timeframe)


@pytest.mark.parametrize(
    "min_magnitude,expected",
    [(0, "all"), (0.99, "all"), (1.0, "1.0"), (2.4, "1.0"), (2.5, "2.5"),
     (4.49, "2.5"), (4.5, "4.5"), (9, "4.5"), (10, "4.5")],
)
def test_feed_level_selection(min_magnitude, expected):
    assert nodes.select_feed_level(min_magnitude) == expected


def test_run_ids_match_the_pattern_and_are_unique():
    ids = {nodes.new_run_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(config.RUN_ID_RE.fullmatch(i) for i in ids)


# --- ingest ----------------------------------------------------------------


def respond(status=200, content=b"", content_type="application/json; charset=utf-8"):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status, content=content, headers={"content-type": content_type})

    return httpx.MockTransport(handler), seen


def test_ingest_happy_path(fake_gcs, set_transport, feed_bytes):
    transport, seen = respond(content=feed_bytes)
    set_transport(transport)

    result = nodes.run_ingest(REQUEST)

    assert len(seen) == 1
    assert str(seen[0].url) == (
        "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_week.geojson"
    )
    assert seen[0].method == "GET"
    assert "quake-pipeline" in seen[0].headers["user-agent"]
    assert config.RUN_ID_RE.fullmatch(result.run_id)
    assert result.raw_object == f"raw/{result.run_id}.geojson"
    assert fake_gcs.objects[result.raw_object] == feed_bytes  # landed unchanged
    assert result.raw_sha256 == hashlib.sha256(feed_bytes).hexdigest()
    assert result.raw_bytes == len(feed_bytes)
    assert result.feed_level == "4.5"
    assert result.fetched_at == "2026-11-17T05:15:00+00:00"
    assert result.feed_generated_at == "2026-11-17T05:00:00+00:00"
    assert (result.feed_count, result.feed_declared_count) == (8, 8)
    assert result.skipped_invalid == 1  # the broken feature
    assert len(result.records) == 7
    first = result.records[0]
    assert (first.id, first.mag, first.depth_km, first.event_type) == (
        "a", 5.0, 10, "earthquake",
    )


@pytest.mark.parametrize("status", [503, 500, 429])
def test_ingest_retryable_status(fake_gcs, set_transport, status):
    set_transport(respond(status=status)[0])
    with pytest.raises(nodes.TransientUpstreamError):
        nodes.run_ingest(REQUEST)
    assert fake_gcs.objects == {}


@pytest.mark.parametrize("status", [404, 302, 301, 403, 204])
def test_ingest_other_status_is_not_retried(fake_gcs, set_transport, status):
    set_transport(respond(status=status)[0])
    with pytest.raises(ValueError, match="unexpected status"):
        nodes.run_ingest(REQUEST)
    assert fake_gcs.objects == {}


def test_ingest_wrong_content_type(fake_gcs, set_transport, feed_bytes):
    set_transport(respond(content=feed_bytes, content_type="text/html")[0])
    with pytest.raises(ValueError, match="not JSON"):
        nodes.run_ingest(REQUEST)
    assert fake_gcs.objects == {}


def test_ingest_oversized_body(fake_gcs, set_transport, monkeypatch, feed_bytes):
    monkeypatch.setattr(config, "MAX_RESPONSE_BYTES", 100)
    set_transport(respond(content=feed_bytes)[0])
    with pytest.raises(ValueError, match="size cap"):
        nodes.run_ingest(REQUEST)
    assert fake_gcs.objects == {}


def test_ingest_download_time_budget(fake_gcs, set_transport, monkeypatch, feed_bytes):
    monkeypatch.setattr(config, "MAX_DOWNLOAD_SECONDS", -1)
    set_transport(respond(content=feed_bytes)[0])
    with pytest.raises(nodes.TransientUpstreamError, match="too long"):
        nodes.run_ingest(REQUEST)
    assert fake_gcs.objects == {}


@pytest.mark.parametrize(
    "body",
    [
        b"{not json",
        b"\xff\xfe\x00bad utf8",
        b"[1, 2, 3]",
        b'"just a string"',
        b'{"type": "Feature"}',
        b'{"type": "FeatureCollection"}',
        b'{"type": "FeatureCollection", "features": "nope"}',
        b"[" * 100_000,  # nesting deep enough to exhaust the parser's stack
    ],
)
def test_ingest_malformed_body(fake_gcs, set_transport, body):
    set_transport(respond(content=body)[0])
    with pytest.raises(ValueError):
        nodes.run_ingest(REQUEST)
    assert fake_gcs.objects == {}


def test_ingest_does_not_follow_redirects(fake_gcs, set_transport):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://example.com/"})

    set_transport(httpx.MockTransport(handler))
    with pytest.raises(ValueError):
        nodes.run_ingest(REQUEST)
    assert len(calls) == 1


def test_network_errors_are_the_retryable_type(fake_gcs, set_transport):
    def handler(request):
        raise httpx.ConnectError("no route", request=request)

    set_transport(httpx.MockTransport(handler))
    with pytest.raises(httpx.TransportError):
        nodes.run_ingest(REQUEST)


# --- record validation -----------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        feature("x", 99.0, "P, Q", ms(10), 5),            # impossible magnitude
        feature("x", -9.0, "P, Q", ms(10), 5),
        feature("x", float("nan"), "P, Q", ms(10), 5),    # NaN is not a number
        feature("x", float("inf"), "P, Q", ms(10), 5),
        feature("x", "big", "P, Q", ms(10), 5),
        feature("x", 5.0, "P, Q", -1, 5),                 # time before 1970
        feature("x", 5.0, "P, Q", 10**18, 5),             # time far in the future
        feature("x", 5.0, "P, Q", "yesterday", 5),
        feature("x", 5.0, "P, Q", ms(10), 99999),         # impossible depth
        feature("x", 5.0, "P, Q", ms(10), float("inf")),
        feature("", 5.0, "P, Q", ms(10), 5),              # empty id
        feature("i" * 65, 5.0, "P, Q", ms(10), 5),        # absurd id
        feature(None, 5.0, "P, Q", ms(10), 5),
        feature("x", 5.0, 12345, ms(10), 5),              # place is not text
        {"id": "x", "properties": None, "geometry": None},
        {"id": "x", "properties": {"time": ms(10)}, "geometry": {"coordinates": [1, 2]}},
        "not even an object",
        None,
        42,
    ],
)
def test_bad_records_are_skipped_and_counted(bad):
    good = feature("ok", 5.0, "P, Q", ms(10), 5)
    records, skipped = nodes.to_records([good, bad])
    assert [r.id for r in records] == ["ok"]
    assert skipped == 1


def test_long_place_is_truncated_not_dropped():
    records, skipped = nodes.to_records([feature("a", 5.0, "x" * 5000, ms(10), 5)])
    assert skipped == 0
    assert len(records[0].place) == config.MAX_PLACE_CHARS


def test_feed_metadata_is_optional_and_validated():
    assert nodes.feed_declared_count({}) is None
    assert nodes.feed_declared_count({"count": -1}) is None
    assert nodes.feed_declared_count({"count": True}) is None
    assert nodes.feed_declared_count({"count": "7"}) is None
    assert nodes.feed_declared_count({"count": 7}) == 7
    assert nodes.feed_generated_at({}) is None
    assert nodes.feed_generated_at({"generated": "now"}) is None
    assert nodes.feed_generated_at({"generated": 10**20}) is None
    assert nodes.feed_generated_at({"generated": ms(17, 5)}) == "2026-11-17T05:00:00+00:00"
    features, metadata = nodes.parse_feed(b'{"type":"FeatureCollection","features":[],"metadata":[1]}')
    assert (features, metadata) == ([], {})


# --- hard quality gate -----------------------------------------------------


def test_mostly_unreadable_feed_stops_the_run_but_keeps_the_evidence(
    fake_gcs, set_transport
):
    broken = [{"id": f"b{i}", "properties": {}} for i in range(3)]
    body = json.dumps(collection_of(broken + [feature("a", 5.0, "P, Q", ms(10), 5)])).encode()
    set_transport(httpx.MockTransport(lambda r: json_response(body)))

    with pytest.raises(DataQualityError, match="3 of 4"):
        nodes.run_ingest(REQUEST)

    # The raw file was still landed so the failure can be investigated.
    assert list(fake_gcs.objects.values()) == [body]
    assert all(name.startswith("raw/") for name in fake_gcs.objects)


def test_quality_gate_error_is_not_a_retryable_type():
    assert not issubclass(DataQualityError, (httpx.TransportError, nodes.TransientUpstreamError))


def test_empty_feed_is_not_a_quality_failure(ingest_feed):
    result = ingest_feed(collection_of([]), timeframe="hour")
    assert (result.feed_count, result.skipped_invalid, result.records) == (0, 0, [])


@pytest.mark.parametrize("bucket", [None, "Not A Bucket"])
def test_missing_or_invalid_bucket_stops_before_any_network_call(
    fake_gcs, set_transport, monkeypatch, bucket
):
    calls = []
    set_transport(httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200)))
    if bucket is None:
        monkeypatch.delenv("GCS_BUCKET")
    else:
        monkeypatch.setenv("GCS_BUCKET", bucket)
    with pytest.raises(RuntimeError, match="GCS_BUCKET"):
        nodes.run_ingest(REQUEST)
    assert calls == [] and fake_gcs.objects == {}
