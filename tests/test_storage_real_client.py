"""Storage tests using the REAL google-cloud-storage client.

The client talks to a small local stand-in for the Cloud Storage API, so
these tests check what our code really sends, with no cloud account needed.
"""

import json

import httpx
import pytest
from google.api_core.exceptions import PreconditionFailed
from google.auth.credentials import AnonymousCredentials
from google.cloud import storage as gcs

from conftest import json_response
from fake_gcs_server import FakeGcsServer
from quake_pipeline import nodes, storage, verify
from quake_pipeline.schemas import PipelineRequest

RUN = "20261117T051500Z-3fa9c2d1"


@pytest.fixture
def gcs_server(monkeypatch):
    server = FakeGcsServer().start()
    client = gcs.Client(
        project="test-project",
        credentials=AnonymousCredentials(),
        client_options={"api_endpoint": server.url},
    )
    monkeypatch.setattr(storage, "_client", client)
    yield server
    server.stop()


def uploads(server):
    return [r for r in server.requests if r[0] == "POST"]


def test_upload_sends_the_create_only_precondition(gcs_server):
    storage.upload(f"raw/{RUN}.geojson", b'{"a": 1}', "application/geo+json")

    (method, path, query), = uploads(gcs_server)
    assert path == "/upload/storage/v1/b/test-bucket/o"
    assert query["ifGenerationMatch"] == ["0"]
    assert "predefinedAcl" not in query
    stored = gcs_server.objects[f"raw/{RUN}.geojson"]
    assert stored == {"data": b'{"a": 1}', "content_type": "application/geo+json"}


def test_existing_object_is_never_overwritten(gcs_server):
    storage.upload(f"raw/{RUN}.geojson", b"original", "application/geo+json")
    with pytest.raises(PreconditionFailed):
        storage.upload(f"raw/{RUN}.geojson", b"replacement", "application/geo+json")
    assert gcs_server.objects[f"raw/{RUN}.geojson"]["data"] == b"original"


def test_missing_objects_read_as_none(gcs_server):
    assert storage.read_raw(RUN) is None
    assert storage.read_processed(RUN) is None
    assert storage.list_run_ids() == []
    assert storage.latest_processed() is None


def test_full_pipeline_and_verify_through_the_real_client(
    gcs_server, set_transport, feed_bytes
):
    set_transport(httpx.MockTransport(lambda r: json_response(feed_bytes)))
    ingested = nodes.run_ingest(PipelineRequest(min_magnitude=4.5))
    _, processed = nodes.run_transform(ingested)
    result, object_name = nodes.run_publish("Three earthquakes matched.", processed)

    # Exactly one raw and one processed object, bytes intact.
    assert sorted(gcs_server.objects) == [object_name, ingested.raw_object]
    assert gcs_server.objects[ingested.raw_object]["data"] == feed_bytes
    assert gcs_server.objects[object_name]["content_type"] == (
        "application/json; charset=utf-8"
    )
    stored = json.loads(gcs_server.objects[object_name]["data"])
    assert stored["run_id"] == result.run_id and stored["schema_version"] == 2

    # Read back through the real client.
    assert storage.list_run_ids() == [result.run_id]
    assert storage.latest_processed() == result
    assert storage.read_raw(result.run_id) == feed_bytes
    report = verify.verify_run(result.run_id)
    assert report.ok, report

    # The pipeline only ever creates and reads. It never deletes, never
    # touches access control, and works without permission to read bucket
    # settings (the stand-in server answers 403 for those, like real GCS
    # does for the least-privilege roles in the README).
    methods = {method for method, _, _ in gcs_server.requests}
    assert methods <= {"GET", "POST"}
    for method, path, query in gcs_server.requests:
        assert "acl" not in path.lower()
        assert "predefinedAcl" not in query
        if method == "POST":
            assert query["ifGenerationMatch"] == ["0"]
