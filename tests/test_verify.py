import json

from conftest import collection_of, feature, ms, store_copy
from quake_pipeline import config, verify

RUN_A = "20261117T051500Z-3fa9c2d1"


def statuses(report) -> dict:
    return {c.name: c.status for c in report.checks}


def test_a_clean_run_verifies(fake_gcs, publish_run):
    result = publish_run()
    report = verify.verify_run(result.run_id)
    assert report.run_id == result.run_id
    assert report.ok
    assert statuses(report) == {
        "Result names its own run": "pass",
        "Summary is plain bounded text": "pass",
        "Same pipeline version": "pass",
        "Raw file is unchanged": "pass",
        "Totals match the raw file": "pass",
        "Groups match the raw file": "pass",
        "Quality report matches the raw file": "pass",
    }


def test_verify_works_for_every_grouping_and_for_empty_results(fake_gcs, publish_run):
    for request in (
        {"group_by": "day", "min_magnitude": 0},
        {"group_by": "region", "min_magnitude": 0},
        {"group_by": "magnitude_band", "min_magnitude": 2.5},
        {"group_by": "day", "min_magnitude": 9},
    ):
        assert verify.verify_run(publish_run(**request).run_id).ok, request


def test_newest_run_is_the_default(fake_gcs, publish_run):
    result = publish_run()
    assert verify.verify_run().run_id == result.run_id


def test_changed_raw_file_is_detected(fake_gcs, publish_run):
    result = publish_run()
    collection = collection_of([feature("z", 9.0, "Fake, Place", ms(10), 5)])
    fake_gcs.objects[result.lineage.raw_object] = json.dumps(collection).encode()

    report = verify.verify_run(result.run_id)
    assert not report.ok
    assert statuses(report)["Raw file is unchanged"] == "fail"
    assert statuses(report)["Totals match the raw file"] == "fail"


def test_one_changed_byte_in_the_raw_file_is_detected(fake_gcs, publish_run):
    result = publish_run()
    fake_gcs.objects[result.lineage.raw_object] += b" "
    report = verify.verify_run(result.run_id)
    assert statuses(report)["Raw file is unchanged"] == "fail"
    # The data itself is the same, so the numbers still recompute.
    assert statuses(report)["Totals match the raw file"] == "pass"
    assert not report.ok


def test_edited_numbers_in_the_result_are_detected(fake_gcs, publish_run):
    result = publish_run()
    name = f"processed/{result.run_id}.json"
    edited = json.loads(fake_gcs.objects[name])
    edited["stats"]["total_events"] = 999
    edited["groups"][0]["count"] = 500
    edited["quality"]["matched"] = 999
    fake_gcs.objects[name] = json.dumps(edited).encode()

    report = verify.verify_run(result.run_id)
    assert not report.ok
    found = statuses(report)
    assert found["Raw file is unchanged"] == "pass"
    assert found["Totals match the raw file"] == "fail"
    assert found["Groups match the raw file"] == "fail"
    assert found["Quality report matches the raw file"] == "fail"


def test_unreadable_raw_file_fails_cleanly(fake_gcs, publish_run):
    result = publish_run()
    fake_gcs.objects[result.lineage.raw_object] = b"not json at all"
    report = verify.verify_run(result.run_id)
    assert not report.ok
    assert statuses(report)["Numbers can be recomputed"] == "fail"


def test_missing_raw_file_is_detected(fake_gcs, publish_run):
    result = publish_run()
    del fake_gcs.objects[result.lineage.raw_object]
    report = verify.verify_run(result.run_id)
    assert not report.ok
    assert statuses(report)["Raw file exists"] == "fail"


def test_result_stored_under_the_wrong_name_is_detected(fake_gcs, publish_run):
    result = publish_run()
    fake_gcs.objects[f"processed/{RUN_A}.json"] = fake_gcs.objects[
        f"processed/{result.run_id}.json"
    ]
    fake_gcs.objects[f"raw/{RUN_A}.geojson"] = fake_gcs.objects[result.lineage.raw_object]
    report = verify.verify_run(RUN_A)
    assert statuses(report)["Result names its own run"] == "fail"
    assert not report.ok


def test_other_pipeline_version_is_a_warning_not_a_failure(fake_gcs, publish_run, monkeypatch):
    result = publish_run()
    monkeypatch.setattr(config, "PIPELINE_VERSION", "9.9.9")
    report = verify.verify_run(result.run_id)
    assert statuses(report)["Same pipeline version"] == "warn"
    assert report.ok


def test_unknown_run_and_empty_bucket(fake_gcs, publish_run):
    assert verify.verify_run() is None
    assert verify.verify_run(RUN_A) is None


def test_command_line(fake_gcs, publish_run, capsys):
    assert verify.main([]) == 1  # nothing to verify yet
    assert "No such run" in capsys.readouterr().out

    result = publish_run()
    store_copy(fake_gcs, result, RUN_A)

    assert verify.main([result.run_id]) == 0
    out = capsys.readouterr().out
    assert f"Run {result.run_id}" in out and "[PASS]" in out
    assert out.strip().endswith("VERIFIED")

    fake_gcs.objects[result.lineage.raw_object] += b"x"
    assert verify.main([result.run_id]) == 1
    out = capsys.readouterr().out
    assert "[FAIL]" in out and "VERIFICATION FAILED" in out

    assert verify.main(["../../etc/passwd"]) == 2
    assert "Invalid run id" in capsys.readouterr().out
    assert verify.main(["a", "b"]) == 2


def test_command_line_explains_setup_problems_without_a_traceback(monkeypatch, capsys):
    from google.auth.exceptions import DefaultCredentialsError

    from quake_pipeline import storage

    def no_login():
        raise DefaultCredentialsError("internal detail")

    monkeypatch.setattr(storage, "_get_client", no_login)
    assert verify.main([]) == 2
    out = capsys.readouterr().out
    assert "quake_pipeline.doctor" in out and "internal detail" not in out
