"""Security rules written as tests, so breaking one fails the build."""

import ast
import hashlib
import re
from pathlib import Path

import pytest

from quake_pipeline import agents, config
from quake_pipeline.agent import root_agent

REPO = Path(__file__).resolve().parent.parent
SOURCE_FILES = sorted((REPO / "quake_pipeline").glob("*.py")) + [
    REPO / "viewer" / "serve.py",
    REPO / "tools" / "demo.py",
]

BANNED_IMPORTS = {"subprocess", "pickle", "marshal", "shelve", "os.system", "ctypes"}
BANNED_CALLS = {"eval", "exec", "compile", "__import__"}
BANNED_STORAGE_WORDS = ["make_public", "make_private", "generate_signed_url", ".acl",
                        "delete", "create_bucket", "set_iam_policy", "from_service_account"]


def _code_only(path: Path) -> str:
    """The file's code with comments and docstrings removed."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (
            isinstance(body, list) and body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body[0] = ast.Pass()
    return ast.unparse(tree)


def test_source_files_were_found():
    names = {p.name for p in SOURCE_FILES}
    assert {"agent.py", "agents.py", "nodes.py", "storage.py", "serve.py"} <= names


@pytest.mark.parametrize("path", SOURCE_FILES, ids=lambda p: p.name)
def test_no_dangerous_imports_or_calls(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in BANNED_IMPORTS, alias.name
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in BANNED_IMPORTS, node.module
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in BANNED_CALLS, node.func.id
        elif isinstance(node, ast.Attribute):
            assert f"os.{node.attr}" != "os.system"


def test_storage_code_only_creates_and_reads():
    code = _code_only(REPO / "quake_pipeline" / "storage.py")
    for word in BANNED_STORAGE_WORDS:
        assert word not in code, word
    assert code.count("if_generation_match=0") == 1
    assert "upload_from_string" in code


def test_deprecated_and_over_powered_adk_pieces_are_not_used():
    for path in SOURCE_FILES:
        text = path.read_text(encoding="utf-8")
        for word in ("SequentialAgent", "ParallelAgent", "LoopAgent", "GCSToolset",
                     "code_executor", "allow_origins"):
            assert word not in text, f"{word} in {path.name}"


def test_agents_have_no_tools_and_no_helpers():
    for agent in (agents.plan_agent, agents.summary_agent):
        assert agent.tools == []
        assert agent.sub_agents == []
        assert agent.code_executor is None
    assert agents.plan_agent.output_schema.__name__ == "PipelineRequest"
    assert agents.summary_agent.input_schema.__name__ == "SummaryInput"
    assert agents.summary_agent.output_key == "summary_text"


def test_graph_is_the_fixed_five_step_line():
    edges = [(e.from_node.name, e.to_node.name) for e in root_agent.graph.edges]
    assert edges == [
        ("__START__", "plan_agent"),
        ("plan_agent", "ingest"),
        ("ingest", "transform"),
        ("transform", "summary_agent"),
        ("summary_agent", "publish"),
    ]


def test_only_usgs_is_reachable():
    assert config.USGS_HOST == "earthquake.usgs.gov"
    assert config.USGS_URL_TEMPLATE.startswith("https://earthquake.usgs.gov/")
    assert config.ALLOWED_TIMEFRAMES == {"hour", "day", "week", "month"}
    assert config.ALLOWED_LEVELS == {"all", "1.0", "2.5", "4.5"}
    urls = set()
    for path in SOURCE_FILES:
        urls |= set(re.findall(r"https?://[^\s\"')]+", path.read_text(encoding="utf-8")))
    assert all(
        u.startswith(("https://earthquake.usgs.gov/", "http://127.0.0.1", "http://{HOST}", "http://{serve.HOST}"))
        for u in urls
    ), urls


def test_limits_are_the_documented_values():
    assert config.HTTP_TIMEOUT_SECONDS == 10
    assert config.MAX_RESPONSE_BYTES == 20 * 1024 * 1024
    assert config.MAX_SUMMARY_CHARS == 600
    assert config.MAX_LABEL_CHARS == 80
    assert config.TOP_GROUPS == 15


def test_every_dependency_is_pinned_exactly():
    lines = [
        line.strip()
        for line in (REPO / "requirements.txt").read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert len(lines) >= 7
    for line in lines:
        assert re.fullmatch(r"[A-Za-z0-9_.-]+==[0-9][A-Za-z0-9_.]*", line), line
    assert "google-adk==2.14.1" in lines


def test_gitignore_protects_secrets():
    ignored = (REPO / ".gitignore").read_text().split()
    for entry in (".env", ".venv/", ".adk/", "__pycache__/", "*.pyc"):
        assert entry in ignored


def test_env_example_holds_placeholders_only():
    text = (REPO / ".env.example").read_text()
    assert "GCS_BUCKET=your-unique-bucket-name" in text
    assert "GOOGLE_API_KEY=replace-with-your-key" in text
    assert not re.search(r"AIza[0-9A-Za-z_-]{20,}", text)  # shape of a real Google key


def test_no_secrets_or_key_files_in_the_repo():
    skip = {".venv", ".git", "__pycache__", ".pytest_cache", ".ruff_cache", "vendor"}
    for path in REPO.rglob("*"):
        if not path.is_file() or skip & set(path.relative_to(REPO).parts):
            continue
        if path.name == ".env":
            continue  # local only and git-ignored; checked above
        assert path.suffix not in {".pem", ".p12", ".key"}, path
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        assert not re.search(r"AIza[0-9A-Za-z_-]{35}", text), path
        assert '"private_key"' not in text or path.name == "test_policy.py", path
        assert "BEGIN PRIVATE KEY" not in text or path.name == "test_policy.py", path


def test_vendored_chart_library_matches_its_recorded_hash():
    vendor = REPO / "viewer" / "web" / "vendor"
    actual = hashlib.sha256((vendor / "chart.umd.js").read_bytes()).hexdigest()
    readme = (vendor / "README.md").read_text()
    assert actual in readme
    assert "4.6.1" in readme
    assert "Chart.js v4.6.1" in (vendor / "chart.umd.js").read_text(encoding="utf-8")[:200]


def test_logging_never_includes_payloads(fake_gcs, publish_run, caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="quake_pipeline"):
        publish_run(summary="A secret-looking summary sentence.")
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "ingest.landed" in logged and "publish.done" in logged
    assert "Somewhere, Alaska" not in logged          # no raw API text
    assert "secret-looking summary" not in logged     # no model text
    assert "FeatureCollection" not in logged
    for record in caplog.records:
        assert isinstance(__import__("json").loads(record.getMessage()), dict)


def test_importing_the_package_has_no_side_effects_and_needs_no_settings():
    """So the doctor, the tests and the viewer can load before setup is done."""
    import os
    import subprocess  # noqa: S404 - test only: runs our own interpreter
    import sys

    env = {k: v for k, v in os.environ.items() if k != "GCS_BUCKET"}
    env["PYTHONPATH"] = str(REPO)
    code = "import quake_pipeline; print(quake_pipeline.agent.root_agent.name)"
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], env=env, cwd="/", capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr[-500:]
    assert done.stdout.strip() == "quake_pipeline"


def test_model_calls_have_a_time_limit_and_a_retry_cap():
    """Without these, an overloaded model makes the run hang with no error."""
    for agent in (agents.plan_agent, agents.summary_agent):
        model = agent.model
        assert model.model == config.GEMINI_MODEL
        assert model.retry_options.attempts == config.MODEL_MAX_ATTEMPTS == 3
        assert model.client_kwargs["http_options"].timeout == 60_000
    assert agents.plan_agent.model is not agents.summary_agent.model
