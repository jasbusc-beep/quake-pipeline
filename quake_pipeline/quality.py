"""Data quality: account for every row and check the feed looks healthy.

Every check is computed from the ingest result alone (no clock, no network),
so the verify command can recompute it later and get the same answer.
"""

from datetime import datetime

from . import config
from .schemas import Check, DataQuality, IngestResult


class DataQualityError(ValueError):
    """The data failed a hard quality gate. The run stops. Not retried."""


def invalid_ratio(skipped: int, feed_count: int) -> float:
    return skipped / feed_count if feed_count else 0.0


def enforce_ingest_gate(skipped: int, feed_count: int) -> None:
    """Stop the run if too much of the feed is unreadable (schema drift)."""
    ratio = invalid_ratio(skipped, feed_count)
    if ratio > config.INVALID_RATIO_FAIL:
        raise DataQualityError(
            f"{skipped} of {feed_count} records failed validation "
            f"({ratio:.0%}). The feed format may have changed."
        )


def _schema_check(ingested: IngestResult) -> Check:
    skipped, total = ingested.skipped_invalid, ingested.feed_count
    ratio = invalid_ratio(skipped, total)
    status = "warn" if ratio > config.INVALID_RATIO_WARN else "pass"
    return Check(
        name="Records are readable",
        status=status,
        detail=f"{skipped} of {total} records failed validation ({ratio:.1%}).",
    )


def _declared_count_check(ingested: IngestResult) -> Check:
    declared, actual = ingested.feed_declared_count, ingested.feed_count
    if declared is None:
        return Check(
            name="Feed is complete",
            status="warn",
            detail="The feed did not say how many records it holds.",
        )
    return Check(
        name="Feed is complete",
        status="pass" if declared == actual else "warn",
        detail=f"Feed declares {declared} records and contains {actual}.",
    )


def _freshness_check(ingested: IngestResult) -> Check:
    if ingested.feed_generated_at is None:
        return Check(
            name="Feed is fresh",
            status="warn",
            detail="The feed did not say when it was generated.",
        )
    generated = datetime.fromisoformat(ingested.feed_generated_at)
    fetched = datetime.fromisoformat(ingested.fetched_at)
    age = int((fetched - generated).total_seconds())
    return Check(
        name="Feed is fresh",
        status="pass" if age <= config.FEED_STALE_SECONDS else "warn",
        detail=f"USGS generated the feed {max(age, 0)} seconds before download.",
    )


def build_quality(
    ingested: IngestResult,
    *,
    duplicates_removed: int,
    non_earthquake: int,
    missing_magnitude: int,
    below_threshold: int,
    matched: int,
    missing_depth: int,
) -> DataQuality:
    valid = len(ingested.records)
    accounted = (
        duplicates_removed + non_earthquake + missing_magnitude
        + below_threshold + matched
    )
    balanced = (
        accounted == valid
        and valid + ingested.skipped_invalid == ingested.feed_count
    )
    if not balanced:
        # Rows went missing between steps. Never publish numbers like that.
        raise DataQualityError(
            f"Row counts do not reconcile: feed {ingested.feed_count}, "
            f"valid {valid}, accounted for {accounted}."
        )
    checks = [
        _schema_check(ingested),
        _declared_count_check(ingested),
        _freshness_check(ingested),
        Check(
            name="No duplicate events",
            status="pass" if duplicates_removed == 0 else "warn",
            detail=f"{duplicates_removed} duplicate event ids removed.",
        ),
        Check(
            name="Every row is accounted for",
            status="pass",
            detail=(
                f"{ingested.feed_count} in feed = {ingested.skipped_invalid} "
                f"unreadable + {duplicates_removed} duplicate + "
                f"{non_earthquake} not earthquakes + {missing_magnitude} "
                f"no magnitude + {below_threshold} below threshold + "
                f"{matched} matched."
            ),
        ),
        Check(
            name="Depth is recorded",
            status="pass" if missing_depth == 0 else "warn",
            detail=f"{missing_depth} of {matched} matched events have no depth.",
        ),
    ]
    return DataQuality(
        feed_count=ingested.feed_count,
        valid_records=valid,
        skipped_invalid=ingested.skipped_invalid,
        duplicates_removed=duplicates_removed,
        non_earthquake=non_earthquake,
        missing_magnitude=missing_magnitude,
        below_threshold=below_threshold,
        matched=matched,
        missing_depth=missing_depth,
        checks=checks,
    )
