// Builds the run report from the local API.
//
// Every piece of data is written with textContent, so text from the API or
// the model can only ever show up as text, never as markup or script.
"use strict";

const GROUP_NAMES = {
  day: "day",
  region: "region",
  magnitude_band: "magnitude band",
};
const STATUS = {
  pass: { icon: "✓", label: "Pass" },
  warn: { icon: "!", label: "Warning" },
  fail: { icon: "✕", label: "Fail" },
};
const RUN_ID_PATTERN = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z-([0-9a-f]{8})$/;

let chart = null;
let drawChart = null;
let currentData = null;

// --- small helpers ---------------------------------------------------------

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) {
    node.textContent = String(text);
  }
  if (className) {
    node.className = className;
  }
  return node;
}

function append(parent) {
  for (let i = 1; i < arguments.length; i += 1) {
    parent.appendChild(arguments[i]);
  }
  return parent;
}

function num(value) {
  return value === null || value === undefined ? "n/a" : value.toLocaleString();
}

function fixed(value, digits) {
  return value === null || value === undefined ? "n/a" : value.toFixed(digits);
}

function formatTime(iso) {
  if (!iso) {
    return "n/a";
  }
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? String(iso) : date.toLocaleString();
}

function runLabel(runId) {
  const m = RUN_ID_PATTERN.exec(runId);
  if (!m) {
    return runId;
  }
  const date = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]));
  return date.toLocaleString() + "  ·  " + m[7];
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function showStatus(message, isError) {
  const status = document.getElementById("status");
  status.textContent = message;
  status.className = isError ? "status error" : "status";
  status.hidden = false;
}

function section(title) {
  const box = el("section", null, "card");
  box.appendChild(el("h2", title));
  return box;
}

async function getJson(path) {
  const response = await fetch(path);
  if (response.status === 404) {
    return null;
  }
  if (!response.ok) {
    throw new Error("Unexpected status " + response.status);
  }
  return response.json();
}

// --- page sections ---------------------------------------------------------

function buildTiles(data) {
  const stats = data.stats;
  const tiles = [
    ["Matching earthquakes", num(stats.total_events),
      "of " + num(data.quality.feed_count) + " events in the feed"],
    ["Strongest", stats.max_magnitude == null ? "n/a" : "M " + fixed(stats.max_magnitude, 1),
      stats.strongest_place || "No matching events"],
    ["Average magnitude", fixed(stats.avg_magnitude, 2),
      "across matching earthquakes"],
    ["Data quality", qualityHeadline(data.quality.checks),
      data.quality.checks.length + " checks run"],
  ];
  const row = el("div", null, "tiles");
  tiles.forEach(function (tile) {
    append(row, append(el("div", null, "tile"),
      el("div", tile[0], "tile-label"),
      el("div", tile[1], "tile-value"),
      el("div", tile[2], "tile-note")));
  });
  return row;
}

function qualityHeadline(checks) {
  const fails = checks.filter(function (c) { return c.status === "fail"; }).length;
  const warns = checks.filter(function (c) { return c.status === "warn"; }).length;
  if (fails) {
    return fails + " failed";
  }
  if (warns) {
    return warns + (warns === 1 ? " warning" : " warnings");
  }
  return "All passed";
}

function buildRequest(data) {
  const request = data.request;
  const box = section("What was asked");
  append(box, el("p",
    "Earthquakes of magnitude " + request.min_magnitude + " and above, past " +
    request.timeframe + ", grouped by " +
    (GROUP_NAMES[request.group_by] || request.group_by) + ".", "lead"));
  return box;
}

function buildSummary(data) {
  const box = section("Summary");
  append(box,
    el("p", data.summary, "summary"),
    el("p", data.summary_source === "model"
      ? "Written by the AI model from the totals below. It never saw the raw data."
      : "Written by the pipeline itself because the AI model returned no usable text.",
    "note"));
  return box;
}

function buildChart(data) {
  const horizontal = data.request.group_by === "region";
  const box = section("Earthquakes per " +
    (GROUP_NAMES[data.request.group_by] || "group"));
  const wrap = el("div", null, horizontal ? "chart-wrap tall" : "chart-wrap");
  const canvas = document.createElement("canvas");
  canvas.setAttribute("role", "img");
  canvas.setAttribute("aria-label",
    "Bar chart of earthquake count per group. The same numbers are in the table below.");
  wrap.appendChild(canvas);
  box.appendChild(wrap);

  // Chart.js measures its container, so it must be drawn after the section
  // is on the page. render() calls this once everything is attached.
  drawChart = function () {
    drawBars(canvas, data, horizontal);
  };
  return box;
}

function drawBars(canvas, data, horizontal) {
  const groups = data.groups;
  const ink = cssVar("--muted");
  const grid = cssVar("--grid");
  const countAxis = {
    beginAtZero: true,
    ticks: { precision: 0, color: ink },
    grid: { color: grid },
    border: { display: false },
  };
  const labelAxis = {
    ticks: { color: ink },
    grid: { display: false },
    border: { color: cssVar("--axis") },
  };
  chart = new Chart(canvas, {
    type: "bar",
    data: {
      labels: groups.map(function (g) { return g.label; }),
      datasets: [{
        label: "Earthquakes",
        data: groups.map(function (g) { return g.count; }),
        backgroundColor: cssVar("--series"),
        hoverBackgroundColor: cssVar("--series-hover"),
        maxBarThickness: 24,
        borderRadius: 4,
      }],
    },
    options: {
      indexAxis: horizontal ? "y" : "x",
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            afterLabel: function (item) {
              const g = groups[item.dataIndex];
              return "Avg magnitude " + fixed(g.avg_magnitude, 2) +
                ", max " + fixed(g.max_magnitude, 1);
            },
          },
        },
      },
      scales: horizontal
        ? { x: countAxis, y: labelAxis }
        : { x: labelAxis, y: countAxis },
    },
  });
}

function buildTable(data) {
  const box = section("Details");
  const table = el("table");
  const headRow = el("tr");
  ["Group", "Earthquakes", "Avg magnitude", "Max magnitude", "Avg depth (km)"]
    .forEach(function (title, index) {
      const th = el("th", title, index === 0 ? "" : "num");
      th.setAttribute("scope", "col");
      headRow.appendChild(th);
    });
  table.appendChild(append(el("thead"), headRow));
  const tbody = el("tbody");
  data.groups.forEach(function (g) {
    append(tbody, append(el("tr"),
      el("td", g.label),
      el("td", num(g.count), "num"),
      el("td", fixed(g.avg_magnitude, 2), "num"),
      el("td", fixed(g.max_magnitude, 1), "num"),
      el("td", fixed(g.avg_depth_km, 1), "num")));
  });
  table.appendChild(tbody);
  box.appendChild(append(el("div", null, "table-wrap"), table));
  if (data.request.group_by === "region") {
    box.appendChild(el("p",
      "Showing the regions with the most earthquakes (up to 15).", "note"));
  }
  return box;
}

function buildCheckList(checks) {
  const list = el("ul", null, "checks");
  checks.forEach(function (check) {
    const status = STATUS[check.status] || STATUS.warn;
    const badge = append(el("span", null, "badge " + check.status),
      el("span", status.icon, "badge-icon"),
      el("span", status.label));
    append(list, append(el("li"),
      badge,
      append(el("div"),
        el("div", check.name, "check-name"),
        el("div", check.detail, "check-detail"))));
  });
  return list;
}

function buildQuality(data) {
  const q = data.quality;
  const box = section("Data quality");
  box.appendChild(el("p",
    "Where every row from the feed went. The numbers on the right add up " +
    "to the number on the left.", "note intro"));
  const funnel = el("dl", null, "funnel");
  [
    ["Events in the feed", q.feed_count],
    ["Unreadable or out of range", q.skipped_invalid],
    ["Duplicates", q.duplicates_removed],
    ["Not earthquakes (blasts, explosions...)", q.non_earthquake],
    ["No magnitude", q.missing_magnitude],
    ["Below the requested magnitude", q.below_threshold],
    ["Matched the request", q.matched],
  ].forEach(function (row, index) {
    const first = index === 0;
    const last = index === 6;
    const cls = first || last ? "strong" : "";
    append(funnel,
      el("dt", (first ? "" : last ? "= " : "− ") + row[0], cls),
      el("dd", num(row[1]), cls));
  });
  append(box, funnel, buildCheckList(q.checks));
  return box;
}

function buildLineage(data) {
  const box = section("Lineage and integrity");
  box.appendChild(el("p",
    "The exact raw file and code this result came from. Verify re-downloads " +
    "the raw file, checks its fingerprint, and recomputes every number.",
    "note intro"));
  const list = el("dl", null, "facts");
  [
    ["Run id", data.run_id, true],
    ["Source", data.source.name, false],
    ["Source address", data.source.url, true],
    ["Downloaded", formatTime(data.source.fetched_at), false],
    ["Feed built by USGS", formatTime(data.lineage.feed_generated_at), false],
    ["Raw file", data.lineage.raw_object + "  (" + num(data.lineage.raw_bytes) + " bytes)", true],
    ["Raw file SHA-256", data.lineage.raw_sha256, true],
    ["Pipeline version", data.lineage.pipeline_version, false],
    ["AI model", data.lineage.model, false],
    ["Report written", formatTime(data.generated_at), false],
    ["Time taken", "download " + num(data.timings_ms.fetch) + " ms, transform " +
      num(data.timings_ms.transform) + " ms", false],
  ].forEach(function (row) {
    append(list, el("dt", row[0]), el("dd", row[1], row[2] ? "mono" : ""));
  });
  box.appendChild(list);

  const button = el("button", "Verify this run", "button");
  button.type = "button";
  const output = el("div", null, "verify-output");
  output.setAttribute("aria-live", "polite");
  button.addEventListener("click", function () {
    runVerify(data.run_id, button, output);
  });
  append(box, button, output);
  return box;
}

async function runVerify(runId, button, output) {
  button.disabled = true;
  output.replaceChildren(el("p", "Re-downloading the raw file and recomputing...", "note"));
  try {
    const report = await getJson("/api/verify/" + runId);
    if (!report) {
      throw new Error("Run not found");
    }
    output.replaceChildren(
      el("p", report.ok
        ? "Verified. The published numbers match the raw file."
        : "Verification failed. Do not rely on this result.",
      report.ok ? "verdict ok" : "verdict bad"),
      buildCheckList(report.checks));
  } catch (err) {
    output.replaceChildren(el("p",
      "Could not verify this run. Check the terminal running the viewer.",
      "status error"));
  }
  button.disabled = false;
}

// --- render and load -------------------------------------------------------

function render(data) {
  currentData = data;
  const content = document.getElementById("content");
  if (chart) {
    chart.destroy();
    chart = null;
  }
  drawChart = null;
  content.replaceChildren();
  document.getElementById("status").hidden = true;

  append(content, buildTiles(data), buildRequest(data), buildSummary(data));
  if (data.groups.length) {
    append(content, buildChart(data), buildTable(data));
  } else {
    const empty = section("Nothing to chart");
    empty.appendChild(el("p",
      "No earthquakes matched this request. Try a lower magnitude or a " +
      "longer timeframe.", "lead"));
    content.appendChild(empty);
  }
  append(content, buildQuality(data), buildLineage(data));
  if (drawChart) {
    drawChart();
  }
}

async function loadRun(runId) {
  try {
    const data = await getJson("/api/runs/" + runId);
    if (!data) {
      showStatus("That run could not be found. Pick another one.", true);
      return;
    }
    render(data);
  } catch (err) {
    showStatus("Could not load that run. Check the terminal running the " +
      "viewer for details.", true);
  }
}

async function start() {
  const select = document.getElementById("run-select");
  try {
    const listing = await getJson("/api/runs");
    const runs = listing ? listing.runs : [];
    if (!runs.length) {
      showStatus("No runs yet. Run the pipeline first, then refresh this page.",
        false);
      return;
    }
    runs.forEach(function (runId) {
      const option = el("option", runLabel(runId));
      option.value = runId;
      select.appendChild(option);
    });
    select.disabled = false;
    select.addEventListener("change", function () {
      loadRun(select.value);
    });
    await loadRun(runs[0]);
  } catch (err) {
    showStatus("Could not load the list of runs. Check the terminal running " +
      "the viewer for details.", true);
  }
}

// Redraw the chart in the right colors if the system theme changes.
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change",
  function () {
    if (currentData) {
      render(currentData);
    }
  });

start();
