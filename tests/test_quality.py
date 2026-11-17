import pytest

from conftest import collection_of, feature, ms
from quake_pipeline import config, nodes
from quake_pipeline.quality import DataQualityError, build_quality
from quake_pipeline.regions import region_of


def checks_by_name(processed) -> dict:
    return {c["name"]: c for c in processed["quality"]["checks"]}


def test_every_row_is_accounted_for(ingest_feed):
    _, processed = nodes.run_transform(ingest_feed(min_magnitude=2.5))
    q = processed["quality"]
    assert (q["feed_count"], q["valid_records"], q["skipped_invalid"]) == (8, 7, 1)
    assert (q["duplicates_removed"], q["non_earthquake"], q["missing_magnitude"]) == (0, 1, 1)
    assert (q["below_threshold"], q["matched"], q["missing_depth"]) == (1, 4, 0)
    assert q["feed_count"] == q["valid_records"] + q["skipped_invalid"]
    assert q["valid_records"] == (
        q["duplicates_removed"] + q["non_earthquake"] + q["missing_magnitude"]
        + q["below_threshold"] + q["matched"]
    )


def test_check_statuses_on_the_fixture_feed(ingest_feed):
    _, processed = nodes.run_transform(ingest_feed(min_magnitude=0))
    checks = checks_by_name(processed)
    assert set(checks) == {
        "Records are readable", "Feed is complete", "Feed is fresh",
        "No duplicate events", "Every row is accounted for", "Depth is recorded",
    }
    # 1 of 8 unreadable is above the 1% warning line, below the 20% stop line.
    assert checks["Records are readable"]["status"] == "warn"
    assert "1 of 8" in checks["Records are readable"]["detail"]
    assert checks["Feed is complete"]["status"] == "pass"
    assert checks["Feed is fresh"]["status"] == "pass"
    assert "900 seconds" in checks["Feed is fresh"]["detail"]
    assert checks["No duplicate events"]["status"] == "pass"
    assert checks["Every row is accounted for"]["status"] == "pass"
    assert checks["Depth is recorded"]["status"] == "warn"  # event g has no depth


def test_clean_feed_passes_every_check(ingest_feed):
    features = [feature(f"q{i}", 5.0, "A, B", ms(10), 5) for i in range(3)]
    _, processed = nodes.run_transform(ingest_feed(collection_of(features)))
    assert {c["status"] for c in processed["quality"]["checks"]} == {"pass"}


def test_duplicates_are_removed_and_reported(ingest_feed):
    features = [
        feature("same", 5.0, "A, B", ms(10), 5),
        feature("same", 5.0, "A, B", ms(10), 5),
        feature("other", 6.0, "A, B", ms(10), 5),
    ]
    summary_input, processed = nodes.run_transform(
        ingest_feed(collection_of(features), min_magnitude=0)
    )
    assert summary_input.stats.total_events == 2
    assert processed["quality"]["duplicates_removed"] == 1
    assert checks_by_name(processed)["No duplicate events"]["status"] == "warn"


def test_stale_feed_is_flagged(ingest_feed):
    collection = collection_of([feature("a", 5.0, "A, B", ms(10), 5)])
    collection["metadata"]["generated"] = ms(17, 3)  # 2h15m before the fetch
    _, processed = nodes.run_transform(ingest_feed(collection))
    assert checks_by_name(processed)["Feed is fresh"]["status"] == "warn"


def test_missing_or_wrong_feed_metadata_is_flagged(ingest_feed):
    collection = collection_of([feature("a", 5.0, "A, B", ms(10), 5)])
    collection["metadata"] = {"count": 99}
    _, processed = nodes.run_transform(ingest_feed(collection))
    checks = checks_by_name(processed)
    assert checks["Feed is complete"]["status"] == "warn"  # declares 99, has 1
    assert checks["Feed is fresh"]["status"] == "warn"     # no generated time

    del collection["metadata"]
    _, processed = nodes.run_transform(ingest_feed(collection))
    assert checks_by_name(processed)["Feed is complete"]["status"] == "warn"


def test_counts_that_do_not_reconcile_stop_the_run(ingest_feed):
    ingested = ingest_feed()
    with pytest.raises(DataQualityError, match="do not reconcile"):
        build_quality(
            ingested, duplicates_removed=0, non_earthquake=0, missing_magnitude=0,
            below_threshold=0, matched=1, missing_depth=0,
        )
    tampered = ingested.model_copy(update={"skipped_invalid": 5})
    with pytest.raises(DataQualityError):
        nodes.run_transform(tampered)


def test_gate_thresholds_are_sane():
    assert 0 < config.INVALID_RATIO_WARN < config.INVALID_RATIO_FAIL < 1


# --- regions ---------------------------------------------------------------


@pytest.mark.parametrize(
    "place,expected",
    [
        ("29 km WNW of Valdez, Alaska", "Alaska"),
        ("5 km NW of The Geysers, CA", "California"),
        ("3 km S of Town, California", "California"),
        ("10 km S of Ensenada, B.C., MX", "Mexico"),
        ("12 km E of Ponce, Puerto Rico", "Puerto Rico"),
        ("148 km WSW of Tual, Indonesia", "Indonesia"),
        ("south of the Fiji Islands", "Fiji Islands"),
        ("off the coast of Oregon", "Oregon"),
        ("off the west coast of northern Sumatra", "northern Sumatra"),
        ("Mid-Indian Ridge", "Mid-Indian Ridge"),
        ("South Sandwich Islands region", "South Sandwich Islands region"),
        ("Town, <b>Bold</b>", "bBold/b"),
        ("", "Unknown"),
        ("   ", "Unknown"),
        (None, "Unknown"),
        (12345, "Unknown"),
        ("A, " + "x" * 500, "x" * 80),
    ],
)
def test_region_of(place, expected):
    assert region_of(place) == expected
