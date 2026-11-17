import http.client
import json
import re
import threading
from pathlib import Path

import pytest

import serve
from conftest import store_copy
from quake_pipeline import storage

WEB_DIR = Path(serve.__file__).resolve().parent / "web"
STATIC = ["/", "/app.js", "/style.css", "/vendor/chart.umd.js"]
RUN_A = "20261117T051500Z-3fa9c2d1"
SCRIPT = "<script>alert(1)</script>"


@pytest.fixture
def server():
    httpd = serve.make_server("127.0.0.1", 0)
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def run(fake_gcs, publish_run):
    """One real published run, with markup in the summary."""
    return publish_run(summary=SCRIPT)


def call(server, method, path, host=None):
    port = server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.putrequest(method, path, skip_host=True)
    conn.putheader("Host", host if host is not None else f"127.0.0.1:{port}")
    conn.endheaders()
    response = conn.getresponse()
    body = response.read()
    conn.close()
    return response, body


def assert_security_headers(response):
    csp = response.getheader("Content-Security-Policy")
    assert csp == (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src 'self'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'"
    )
    assert response.getheader("X-Content-Type-Options") == "nosniff"
    assert response.getheader("Referrer-Policy") == "no-referrer"
    assert response.getheader("Cache-Control") == "no-store"
    assert "charset=utf-8" in response.getheader("Content-Type")
    assert "Python" not in (response.getheader("Server") or "")


# --- static files ----------------------------------------------------------


@pytest.mark.parametrize("path", STATIC)
def test_static_routes(server, path):
    response, body = call(server, "GET", path)
    assert response.status == 200
    assert len(body) > 100
    assert_security_headers(response)


def test_content_types(server):
    types = {p: call(server, "GET", p)[0].getheader("Content-Type") for p in STATIC}
    assert types["/"].startswith("text/html")
    assert types["/app.js"].startswith("text/javascript")
    assert types["/style.css"].startswith("text/css")
    assert types["/vendor/chart.umd.js"].startswith("text/javascript")


def test_query_strings_are_ignored_for_routing(server):
    assert call(server, "GET", "/?x=../../etc/passwd")[0].status == 200


# --- API -------------------------------------------------------------------


def test_latest_and_run_endpoints_return_the_result(server, run):
    for path in ("/api/latest", f"/api/runs/{run.run_id}"):
        response, body = call(server, "GET", path)
        assert response.status == 200
        assert response.getheader("Content-Type").startswith("application/json")
        assert_security_headers(response)
        data = json.loads(body)
        assert data["run_id"] == run.run_id
        assert data["summary"] == SCRIPT  # stored and served as plain data
        assert data["schema_version"] == 2


def test_runs_endpoint_lists_newest_first(server, run, fake_gcs):
    store_copy(fake_gcs, run, "20270101T000000Z-00000000")
    response, body = call(server, "GET", "/api/runs")
    assert response.status == 200
    assert json.loads(body) == {"runs": ["20270101T000000Z-00000000", run.run_id]}
    assert_security_headers(response)


def test_runs_endpoint_is_empty_before_the_first_run(server, fake_gcs):
    response, body = call(server, "GET", "/api/runs")
    assert (response.status, json.loads(body)) == (200, {"runs": []})
    response, body = call(server, "GET", "/api/latest")
    assert (response.status, json.loads(body)) == (404, {"error": "no_result"})


def test_verify_endpoint(server, run, fake_gcs):
    response, body = call(server, "GET", f"/api/verify/{run.run_id}")
    assert response.status == 200
    report = json.loads(body)
    assert report["ok"] is True and report["run_id"] == run.run_id
    assert_security_headers(response)

    fake_gcs.objects[run.lineage.raw_object] += b"tampered"
    report = json.loads(call(server, "GET", f"/api/verify/{run.run_id}")[1])
    assert report["ok"] is False


@pytest.mark.parametrize("prefix", ["/api/runs/", "/api/verify/"])
def test_unknown_run_is_404(server, fake_gcs, prefix):
    response, body = call(server, "GET", prefix + RUN_A)
    assert (response.status, json.loads(body)) == (404, {"error": "no_such_run"})
    assert_security_headers(response)


@pytest.mark.parametrize("prefix", ["/api/runs/", "/api/verify/"])
@pytest.mark.parametrize(
    "bad",
    ["", "x", "../../quake_pipeline/.env", "..%2F..%2Fsecret", RUN_A + "/",
     RUN_A + "/../x", RUN_A.upper(), RUN_A + "%00", "%2e%2e", "latest"],
)
def test_bad_run_ids_never_reach_storage(server, monkeypatch, prefix, bad):
    def boom(*args, **kwargs):
        raise AssertionError("storage must not be called for an invalid run id")

    monkeypatch.setattr(storage, "_get_client", boom)
    response, body = call(server, "GET", prefix + bad)
    assert (response.status, json.loads(body)) == (404, {"error": "not_found"})


@pytest.mark.parametrize(
    "path",
    ["/nope", "/../quake_pipeline/.env", "/vendor/../../quake_pipeline/.env",
     "/%2e%2e/quake_pipeline/.env", "/app.js/../../serve.py", "//etc/passwd",
     "/index.html", "/vendor/", "/api", "/api/", "/API/latest", "/serve.py",
     "/vendor/README.md", "/app.js%00.html"],
)
def test_unknown_and_traversal_paths_are_404(server, path):
    response, body = call(server, "GET", path)
    assert response.status == 404
    assert json.loads(body) == {"error": "not_found"}
    assert_security_headers(response)


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD", "FOO"])
@pytest.mark.parametrize("path", ["/", "/api/latest"])
def test_other_methods_are_405(server, method, path):
    response, _ = call(server, method, path)
    assert response.status == 405
    assert response.getheader("Allow") == "GET"
    assert_security_headers(response)


@pytest.mark.parametrize("path", ["/api/latest", "/api/runs", f"/api/runs/{RUN_A}",
                                  f"/api/verify/{RUN_A}"])
def test_api_errors_are_generic(server, monkeypatch, path):
    def boom():
        raise RuntimeError("secret-internal-detail")

    monkeypatch.setattr(storage, "_get_client", boom)
    response, body = call(server, "GET", path)
    assert response.status == 500
    assert json.loads(body) == {"error": "internal_error"}
    assert b"secret-internal-detail" not in body and b"Traceback" not in body
    assert_security_headers(response)


# --- Host header (DNS rebinding defence) ------------------------------------


def test_requests_for_this_machine_are_served(server):
    port = server.server_address[1]
    for host in (f"127.0.0.1:{port}", f"localhost:{port}"):
        assert call(server, "GET", "/", host=host)[0].status == 200


@pytest.mark.parametrize(
    "host", ["evil.example.com", "evil.example.com:8080", "127.0.0.1", "localhost:1",
             "", "127.0.0.1.evil.example.com:8080"],
)
@pytest.mark.parametrize("path", ["/", "/api/latest", "/api/runs"])
def test_requests_for_other_hosts_are_refused(server, host, path):
    response, body = call(server, "GET", path, host=host)
    assert response.status == 403
    assert json.loads(body) == {"error": "forbidden_host"}
    assert_security_headers(response)


def test_server_binds_to_loopback_only():
    assert serve.HOST == "127.0.0.1"
    httpd = serve.make_server("127.0.0.1", 0)
    assert httpd.server_address[0] == "127.0.0.1"
    httpd.server_close()


# --- static checks on the front-end files ----------------------------------

FORBIDDEN = ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write",
             "eval(", "new Function", "setAttribute(\"on", "javascript:"]


def front_end_files():
    return sorted(WEB_DIR.glob("*.js")) + sorted(WEB_DIR.glob("*.html"))


def test_front_end_has_no_dangerous_sinks():
    files = front_end_files()
    assert len(files) >= 2
    for path in files:
        text = path.read_text(encoding="utf-8")
        for word in FORBIDDEN:
            assert word not in text, f"{word} found in {path.name}"


def test_html_has_no_inline_script_style_or_handlers():
    for path in WEB_DIR.glob("*.html"):
        text = path.read_text(encoding="utf-8")
        for attrs, body in re.findall(r"<script\b([^>]*)>(.*?)</script>", text, re.S | re.I):
            assert "src=" in attrs and body.strip() == "", "inline <script> body"
        assert not re.search(r"<style\b", text, re.I)
        assert not re.search(r"\sstyle\s*=", text, re.I)
        assert not re.search(r"\son[a-z]+\s*=", text, re.I)
        assert "http://" not in text and "https://" not in text  # no CDN tags


def test_front_end_only_loads_same_origin_resources():
    for path in list(WEB_DIR.glob("*.js")) + list(WEB_DIR.glob("*.css")):
        text = path.read_text(encoding="utf-8")
        assert "http://" not in text and "https://" not in text, path.name
        assert "@import" not in text and "url(" not in text, path.name


def test_viewer_refuses_to_start_without_a_bucket_and_says_why(monkeypatch):
    monkeypatch.delenv("GCS_BUCKET")
    with pytest.raises(SystemExit) as stop:
        serve.main()
    assert "GCS_BUCKET is not set" in str(stop.value)
    assert "tools/demo.py" in str(stop.value)
