# Common tasks. Run "make help" to list them.
PY := .venv/bin/python
# Python 3.11 or newer. Override if needed: make install PYTHON=python3.12
PYTHON ?= python3
QUERY ?= earthquakes above 4.5 this week by day

.PHONY: help install test lint security guard check doctor demo run view verify

help:  ## Show this list
	@grep -E "^[a-z]+:.*##" Makefile | sed -E "s/:.*## /  -  /"

install:  ## Create the virtual environment and install pinned packages
	@$(PYTHON) -c 'import sys; sys.exit(sys.version_info < (3, 11))' || \
		(echo "Python 3.11 or newer is needed. Try: make install PYTHON=python3.12"; exit 1)
	$(PYTHON) -m venv .venv
	$(PY) -m pip install -r requirements.lock

test:  ## Run the tests with coverage (no network, no cloud, no model)
	$(PY) -m pytest --cov --cov-report=term-missing:skip-covered

lint:  ## Check code style and common bug patterns
	$(PY) -m ruff check .

security:  ## Scan dependencies and code for known security problems
	$(PY) -m pip_audit -r requirements.lock
	$(PY) -m bandit -q -r quake_pipeline viewer tools

guard:  ## Fail if banned patterns appear in the code
	@! grep -rn "SequentialAgent\|GCSToolset\|innerHTML\|eval(" quake_pipeline viewer tools
	@echo "guard: no banned patterns"

check: lint test security guard  ## Everything CI runs

doctor:  ## Check this machine is set up to run the pipeline
	$(PY) -m quake_pipeline.doctor

demo:  ## See the report with live data: no cloud account, no AI model
	$(PY) tools/demo.py

run:  ## Run the pipeline. Example: make run QUERY="quakes above 6 this month by region"
	.venv/bin/adk run quake_pipeline "$(QUERY)"

view:  ## Start the local viewer at http://127.0.0.1:8080
	$(PY) viewer/serve.py

verify:  ## Re-check the newest run against its raw file
	$(PY) -m quake_pipeline.verify
