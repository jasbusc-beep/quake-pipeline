# Future Improvements

Quake Pipeline works end to end today: it ingests from the USGS API,
transforms, stores raw and processed files in a private bucket, and shows a
verifiable report. This document covers what I would change to take it from
a solid class project to something a team could run in production.

Each item follows the same shape: where it stands today, what I would
change, and why it matters.

## Priorities at a glance

| # | Improvement | Area | Effort | Why it is first in line |
| --- | --- | --- | --- | --- |
| 1 | Run on a schedule in the cloud | Scheduling | Medium | Today a person has to start every run |
| 2 | Alert when a run fails or a quality check warns | Monitoring | Low | Failures are only visible if someone looks |
| 3 | Dedicated service account and sign-in on the viewer | Security | Medium | Required before anyone else uses it |
| 4 | Load only new events and keep history in BigQuery | Scalability, cost | High | Each run re-downloads the whole feed |
| 5 | Evaluate the AI summary against the numbers | Data quality | Medium | The summary is the one output not yet verified |

## Scheduling

**Today.** A run starts when someone types a command.

**Improvement.** Package the pipeline in a container, run it as a Cloud Run
job, and trigger it with Cloud Scheduler (for example hourly). Each run
already gets a unique id and never overwrites an earlier one, so scheduled
runs are safe to repeat.

**Why it matters.** Data that only updates when a person remembers to run it
is not a pipeline anyone can depend on.

## Monitoring and alerting

**Today.** Each step writes one structured JSON log line with the run id,
row counts and timings. Quality checks are stored with every result. Nobody
is notified of anything.

**Improvement.** Send the logs to Cloud Logging, build a small dashboard
(runs per day, failure rate, rows in and out, run time), and alert on three
things: a failed run, a quality check that warns, and no successful run in
the expected window. Add OpenTelemetry tracing so slow steps are obvious.

**Why it matters.** The structured logs make this cheap to add. The most
dangerous failure is the quiet one, where the schedule stops and the page
keeps showing old numbers.

## Error handling

**Today.** Network errors and USGS 429 or 5xx responses are retried up to
three times, with a growing, randomized wait between attempts. Bad input, a failed quality gate or a failed schema check stops
the run with a clear message. A raw file is saved before the quality gate so
there is evidence to inspect.

**Improvements.**

- Honor the `Retry-After` header on a 429, so the wait matches what the
  source asks for.
- Retry the AI calls on transient errors, and fall back to a second model
  if the first is unavailable. Today the summary has a safe non-AI fallback,
  but the planning step does not.
- Move failed runs to a "needs attention" list with the reason, so they can
  be replayed from the saved raw file instead of re-fetched.
- If the planner cannot understand the request, ask the user a clarifying
  question instead of failing.

**Why it matters.** Most failures in data pipelines are temporary. Handling
them well is the difference between a 2 a.m. page and a log line.

## Scalability

**Today.** Each run downloads a full USGS feed (up to about 10 MB for a
month), holds it in memory, and processes it with pandas on one machine.
That is the right size for this data.

**Improvements.**

- **Incremental loads.** Remember the newest event already loaded and only
  fetch what changed, using the USGS query API with a start time.
- **A warehouse for history.** Load cleaned events into a BigQuery table
  partitioned by day. The feeds only go back 30 days, so today there is no
  way to ask about last year.
- **Bigger data.** If volume grew by orders of magnitude, write Parquet
  instead of JSON and move the transform to BigQuery SQL or Dataflow. The
  step boundaries would not change, because each step already hands off a
  typed, validated object.
- **Parallel work.** ADK graphs support parallel branches, so several
  timeframes or regions could be ingested at once.

**Why it matters.** The current design is deliberately simple. These are the
changes that would let it grow without a rewrite.

## Data quality

**Today.** Every record is validated against plausible ranges, duplicates
are removed, every row is accounted for, and six quality checks are
published with each result. A feed that is more than 20% unreadable stops
the run.

**Improvements.**

- **Check the AI summary.** Automatically compare every number in the
  summary sentence against the computed totals, and reject or flag a summary
  that states a number that is not in the data. This is the one output that
  is not verified today.
- **Detect unusual values, not just invalid ones.** Compare today's event
  count against the recent average and warn on a sudden drop or spike, which
  can mean a broken source rather than a quiet day.
- **True regions.** Region currently comes from USGS's free-text place
  name, so "Japan" and "Japan region" are separate groups. Using each
  event's latitude and longitude with a country boundary dataset would give
  exact countries.
- **Handle revisions.** USGS updates magnitudes after the fact. Tracking the
  `updated` field would show when a past result is out of date.
- **A written data contract** for the processed file, with a version number
  and a compatibility test, so downstream users know what they can rely on.

**Why it matters.** People make decisions from dashboards. A number that
looks right and is wrong is worse than an error.

## Security

**Today.** The AI agents have no tools and cannot choose URLs or touch
storage. The bucket is private with create-only uploads. The viewer only
answers on this machine. Dependencies are pinned and scanned. Details are in
the README.

**Improvements.**

- **A dedicated service account** for the pipeline with only the two bucket
  roles, using Workload Identity instead of a person's login.
- **Sign-in and roles on the viewer** before hosting it for anyone else,
  for example Identity-Aware Proxy in front of Cloud Run.
- **Secret Manager** for the model key instead of a local file, with
  rotation.
- **Stronger tamper evidence.** `verify` detects a changed raw file or
  result today. Turning on bucket object versioning and a retention policy,
  and signing each result, would also cover someone with write access
  replacing both files.
- **Model guardrails.** Add Gemini safety settings and Model Armor to
  screen inputs and outputs, as a second layer behind the schema checks.
- **Supply chain.** Install with `--require-hashes`, pin GitHub Actions by
  commit, and produce a software bill of materials on each release.
- **Audit trail.** Turn on Cloud Audit Logs for the bucket so every read
  and write is attributed to an identity.

**Why it matters.** The current controls fit one person running it locally.
Each of these is what a security review would ask for before more people or
more sensitive data were involved.

## Testing

**Today.** 302 automated tests with 97% coverage, including the full graph
with a stub model, the real storage client against a local stand-in for the
storage API, and the security rules themselves written as tests.

**Improvements.**

- **A scheduled integration test** that runs the real pipeline against a
  real test bucket and the real model, so breaking changes in USGS, Google
  Cloud or Gemini are caught within a day.
- **Model evaluation.** A fixed set of example requests with expected
  plans, run on every model or prompt change, using ADK's evaluation tools.
  This also makes it safe to try a cheaper model.
- **Automated browser tests** (Playwright) for the viewer. It was checked
  by hand in every state, which does not protect against future changes.
- **Property-based tests** (Hypothesis) that generate random feeds to look
  for inputs nobody thought of.
- **Run the CI workflow on more than one Python version.**

**Why it matters.** The parts that cannot be tested offline (the real model
and the real cloud) are exactly where outside changes will break things.

## Cost

**Today.** Close to free. Two short model calls per run, a few megabytes of
storage, no servers left running.

**Improvements.**

- **Storage lifecycle rules.** Move raw files to a cheaper storage class
  after 30 days and delete them after a set period. Compress raw files
  before upload.
- **Skip the model when it is not needed.** Cache the plan for a repeated
  question, and use the built-in fallback summary for routine scheduled
  runs.
- **Use the smallest model that passes the evaluation set**, and record
  token counts per run so cost is visible.
- **A budget alert** on the project.

**Why it matters.** Costs here are small, but AI and storage costs both grow
quietly with usage. It is cheaper to set limits before scale than after.

## Features I would add next

- **More data sources through the same design**, for example NOAA tsunami
  alerts joined to large earthquakes. The allow-list approach extends to any
  fixed set of trusted hosts.
- **A map view** of events alongside the chart.
- **Ask follow-up questions** about a stored run, answered from the
  processed file only.
- **Compare two runs** side by side, for example this week against last
  week.
