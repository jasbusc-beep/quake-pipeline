"""Pydantic models. Every hand-off between pipeline steps is one of these.

Optional fields default to None because ADK leaves out None values when it
passes a model from one step to the next.
"""

from typing import Literal

from pydantic import BaseModel, Field

from . import config


class PipelineRequest(BaseModel):
    """The only thing the planning agent is allowed to produce."""

    timeframe: Literal["hour", "day", "week", "month"] = "week"
    min_magnitude: float = Field(default=2.5, ge=0, le=10)
    group_by: Literal["day", "region", "magnitude_band"] = "day"


class QuakeRecord(BaseModel):
    """One event, trimmed to the fields we use, with plausibility bounds."""

    id: str = Field(min_length=1, max_length=config.MAX_ID_CHARS)
    mag: float | None = Field(
        default=None,
        ge=config.MAG_RANGE[0],
        le=config.MAG_RANGE[1],
        allow_inf_nan=False,
    )
    place: str | None = Field(default=None, max_length=config.MAX_PLACE_CHARS)
    time_ms: int = Field(ge=0, le=config.TIME_MS_MAX)
    depth_km: float | None = Field(
        default=None,
        ge=config.DEPTH_KM_RANGE[0],
        le=config.DEPTH_KM_RANGE[1],
        allow_inf_nan=False,
    )
    event_type: str | None = Field(
        default=None, max_length=config.MAX_EVENT_TYPE_CHARS
    )


class IngestResult(BaseModel):
    run_id: str
    request: PipelineRequest
    source_url: str
    feed_level: str
    fetched_at: str  # ISO 8601 UTC
    fetch_ms: int
    raw_object: str
    raw_sha256: str
    raw_bytes: int
    feed_count: int  # features in the file
    feed_declared_count: int | None = None  # what the feed says it contains
    feed_generated_at: str | None = None  # when USGS built the feed
    skipped_invalid: int
    records: list[QuakeRecord]


class GroupStat(BaseModel):
    label: str
    count: int
    avg_magnitude: float
    max_magnitude: float
    avg_depth_km: float | None = None


class OverallStats(BaseModel):
    total_events: int
    avg_magnitude: float | None = None
    max_magnitude: float | None = None
    strongest_place: str | None = None
    strongest_time: str | None = None


class SummaryInput(BaseModel):
    """The ONLY thing the summary agent receives. No raw records."""

    request: PipelineRequest
    stats: OverallStats
    groups: list[GroupStat]


class Check(BaseModel):
    """One pass / warn / fail line in a quality or integrity report."""

    name: str
    status: Literal["pass", "warn", "fail"]
    detail: str


class DataQuality(BaseModel):
    """Where every row went, plus the checks that were run on the data.

    feed_count = valid_records + skipped_invalid
    valid_records = duplicates_removed + non_earthquake + missing_magnitude
                    + below_threshold + matched
    """

    feed_count: int
    valid_records: int
    skipped_invalid: int
    duplicates_removed: int
    non_earthquake: int
    missing_magnitude: int
    below_threshold: int
    matched: int
    missing_depth: int  # among matched events
    checks: list[Check]


class Lineage(BaseModel):
    """Ties the result to the exact raw file and code that produced it."""

    raw_object: str
    raw_sha256: str
    raw_bytes: int
    feed_level: str
    feed_generated_at: str | None = None
    pipeline_version: str
    model: str


class Source(BaseModel):
    name: str
    url: str
    fetched_at: str


class ProcessedResult(BaseModel):
    """The file written to GCS and read by the viewer."""

    schema_version: Literal[2] = 2
    run_id: str
    generated_at: str
    request: PipelineRequest
    source: Source
    lineage: Lineage
    quality: DataQuality
    stats: OverallStats
    groups: list[GroupStat]
    summary: str
    summary_source: Literal["model", "fallback"]
    timings_ms: dict[str, int]


class VerifyReport(BaseModel):
    """Result of re-checking a stored run against its raw file."""

    run_id: str
    ok: bool
    checks: list[Check]
