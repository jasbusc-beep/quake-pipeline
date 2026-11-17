"""Re-check a stored run against its raw file.

    python -m quake_pipeline.verify              # newest run
    python -m quake_pipeline.verify <run_id>     # a specific run

It downloads the raw file, confirms its SHA-256 matches the one recorded at
ingest time, then re-runs the transform and confirms the stored numbers
come out the same. Exit code 0 means every check passed.

What this proves: the published numbers really do come from that raw file,
and neither file has been corrupted or edited since.
What it does not prove: that the summary sentence is accurate (a model
wrote it), or that USGS's data was right in the first place.
"""

import hashlib
import sys

from google.api_core.exceptions import GoogleAPIError
from google.auth.exceptions import GoogleAuthError

from . import config, nodes, storage
from .sanitize import clean_summary
from .schemas import Check, IngestResult, ProcessedResult, VerifyReport


def _check(name: str, ok: bool, good: str, bad: str) -> Check:
    return Check(name=name, status="pass" if ok else "fail", detail=good if ok else bad)


def _recompute(processed: ProcessedResult, raw: bytes) -> list[Check]:
    """Re-run the deterministic steps on the raw file and compare."""
    try:
        features, metadata = nodes.parse_feed(raw)
        records, skipped = nodes.to_records(features)
        ingested = IngestResult(
            run_id=processed.run_id,
            request=processed.request,
            source_url=processed.source.url,
            feed_level=processed.lineage.feed_level,
            fetched_at=processed.source.fetched_at,
            fetch_ms=0,
            raw_object=processed.lineage.raw_object,
            raw_sha256=hashlib.sha256(raw).hexdigest(),
            raw_bytes=len(raw),
            feed_count=len(features),
            feed_declared_count=nodes.feed_declared_count(metadata),
            feed_generated_at=nodes.feed_generated_at(metadata),
            skipped_invalid=skipped,
            records=records,
        )
        _, recomputed = nodes.run_transform(ingested)
    except ValueError as err:
        return [
            Check(
                name="Numbers can be recomputed",
                status="fail",
                detail=f"The raw file could not be reprocessed: {err}",
            )
        ]
    stored = processed.model_dump(mode="json")
    return [
        _check(
            "Totals match the raw file",
            recomputed["stats"] == stored["stats"],
            f"Recomputed {recomputed['stats']['total_events']} matching events "
            "and the same overall statistics.",
            "Recomputed overall statistics differ from the stored ones.",
        ),
        _check(
            "Groups match the raw file",
            recomputed["groups"] == stored["groups"],
            f"All {len(recomputed['groups'])} groups recomputed identically.",
            "Recomputed groups differ from the stored ones.",
        ),
        _check(
            "Quality report matches the raw file",
            recomputed["quality"] == stored["quality"],
            "Row counts and quality checks recomputed identically.",
            "Recomputed row counts or quality checks differ from the stored ones.",
        ),
    ]


def verify_run(run_id: str | None = None) -> VerifyReport | None:
    """Verify one run (newest if run_id is None). None if it does not exist."""
    if run_id is None:
        run_ids = storage.list_run_ids(limit=1)
        if not run_ids:
            return None
        run_id = run_ids[0]
    processed = storage.read_processed(run_id)
    if processed is None:
        return None

    checks = [
        _check(
            "Result names its own run",
            processed.run_id == run_id
            and processed.lineage.raw_object == storage.raw_object_name(run_id),
            "The result's run id and raw file name match where it is stored.",
            "The result's run id or raw file name does not match where it is stored.",
        ),
        _check(
            "Summary is plain bounded text",
            processed.summary == clean_summary(processed.summary)
            and 0 < len(processed.summary) <= config.MAX_SUMMARY_CHARS,
            f"{len(processed.summary)} characters, no control characters.",
            "The stored summary is empty, too long or contains control characters.",
        ),
    ]

    same_version = processed.lineage.pipeline_version == config.PIPELINE_VERSION
    checks.append(
        Check(
            name="Same pipeline version",
            status="pass" if same_version else "warn",
            detail=(
                f"Produced by pipeline {processed.lineage.pipeline_version}, "
                f"checked with {config.PIPELINE_VERSION}."
                + ("" if same_version else " Differences below may be code changes.")
            ),
        )
    )

    raw = storage.read_raw(run_id)
    if raw is None:
        checks.append(
            Check(
                name="Raw file exists",
                status="fail",
                detail="The raw file for this run is missing from the bucket.",
            )
        )
    else:
        actual = hashlib.sha256(raw).hexdigest()
        checks.append(
            _check(
                "Raw file is unchanged",
                actual == processed.lineage.raw_sha256
                and len(raw) == processed.lineage.raw_bytes,
                f"SHA-256 {actual[:16]}... matches the hash recorded at ingest.",
                "The raw file's SHA-256 or size differs from what was recorded "
                "at ingest.",
            )
        )
        checks.extend(_recompute(processed, raw))

    ok = not any(check.status == "fail" for check in checks)
    return VerifyReport(run_id=run_id, ok=ok, checks=checks)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) > 1:
        print("Usage: python -m quake_pipeline.verify [run_id]")
        return 2
    try:
        report = verify_run(args[0] if args else None)
    except ValueError as err:
        print(f"Error: {err}")
        return 2
    except (RuntimeError, GoogleAuthError, GoogleAPIError) as err:
        print(f"Could not reach the bucket ({type(err).__name__}).")
        print("Run this to see what is missing: python -m quake_pipeline.doctor")
        return 2
    if report is None:
        print("No such run. Run the pipeline first.")
        return 1
    print(f"Run {report.run_id}")
    for check in report.checks:
        print(f"  [{check.status.upper():4}] {check.name}: {check.detail}")
    print("VERIFIED" if report.ok else "VERIFICATION FAILED")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
