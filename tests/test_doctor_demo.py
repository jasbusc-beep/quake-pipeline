import importlib.util
from pathlib import Path

import httpx
import pytest
from google.api_core.exceptions import Forbidden
from google.auth.exceptions import DefaultCredentialsError

from conftest import json_response
from quake_pipeline import doctor, storage, verify

REPO = Path(__file__).resolve().parent.parent


# --- doctor ----------------------------------------------------------------


@pytest.fixture
def healthy(monkeypatch, fake_gcs, set_transport, feed_bytes):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key-not-real")
    monkeypatch.setattr(doctor.google.auth, "default", lambda: (object(), "proj"))
    set_transport(httpx.MockTransport(lambda r: json_response(feed_bytes)))


def test_doctor_passes_on_a_healthy_setup(healthy, capsys):
    assert doctor.main() == 0
    out = capsys.readouterr().out
    assert out.count("[PASS]") == len(doctor.CHECKS) and "[FAIL]" not in out
    assert "Ready to run." in out
    assert "test-key-not-real" not in out  # never prints the key


def test_doctor_reports_each_problem_with_a_fix(healthy, monkeypatch, capsys, set_transport):
    monkeypatch.delenv("GOOGLE_API_KEY")
    monkeypatch.setenv("GCS_BUCKET", "Not A Bucket")

    def no_login():
        raise DefaultCredentialsError("none")

    def denied(*args, **kwargs):
        raise Forbidden("secret-detail-from-google")

    monkeypatch.setattr(doctor.google.auth, "default", no_login)
    monkeypatch.setattr(storage, "list_run_ids", denied)
    set_transport(httpx.MockTransport(lambda r: httpx.Response(503)))

    assert doctor.main() == 1
    out = capsys.readouterr().out
    assert out.count("[FAIL]") == 5 and "[PASS] Python version" in out
    assert "gcloud auth application-default login" in out
    assert "GOOGLE_API_KEY" in out
    assert "not a valid bucket name" in out
    assert "Could not read the bucket (Forbidden)" in out
    assert "Could not reach the USGS feed" in out
    assert "secret-detail-from-google" not in out
    assert "5 thing(s) to fix first." in out


def test_doctor_accepts_the_google_cloud_model_path(healthy, monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY")
    assert doctor.check_model_access()[0] is False
    monkeypatch.setenv("GOOGLE_GENAI_USE_ENTERPRISE", "True")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    assert doctor.check_model_access()[0] is True


def test_doctor_only_reads(healthy, fake_gcs):
    doctor.main()
    assert fake_gcs.upload_calls == []


# --- demo mode -------------------------------------------------------------


def load_demo():
    spec = importlib.util.spec_from_file_location("demo", REPO / "tools" / "demo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_demo_builds_real_verifiable_runs_in_memory(monkeypatch, set_transport, feed_bytes):
    demo = load_demo()
    monkeypatch.setattr(storage, "_client", None)
    set_transport(httpx.MockTransport(lambda r: json_response(feed_bytes)))

    client = demo.build_demo_store()

    run_ids = storage.list_run_ids()
    assert len(run_ids) == len(demo.DEMO_REQUESTS) == 3
    assert len(client.objects) == 6  # one raw and one processed file per run
    for run_id in run_ids:
        result = storage.read_processed(run_id)
        assert result.summary_source == "fallback"  # no model in demo mode
        assert verify.verify_run(run_id).ok
    assert {storage.read_processed(r).request.group_by for r in run_ids} == {
        "day", "region", "magnitude_band",
    }
    monkeypatch.setattr(storage, "_client", None)


def test_demo_storage_is_create_only():
    demo = load_demo()
    blob = demo.MemoryClient().blob("x")
    blob.upload_from_string(b"one", if_generation_match=0)
    with pytest.raises(Exception):
        blob.upload_from_string(b"two", if_generation_match=0)
    assert blob.download_as_bytes() == b"one"
