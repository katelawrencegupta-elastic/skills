/**
 * Estimator Import (no GCP OAuth) — paste into Extensions → Apps Script
 * on a *copy* of the Observability estimator. A golden template is refused.
 *
 * Usage:
 * 1. Run fetch_org.py → org JSON includes offload.apps_script_payload
 * 2. python scripts/emit_estimator_import.py --org-json <folder>/<key>_org_report.json --kind o11y
 * 3. Copy the JSON, open the target spreadsheet
 * 4. Menu: Estimator Import → Import JSON from clipboard
 *    (fallback: paste JSON into sheet AgentImport!A1, then Import from AgentImport!A1)
 *
 * JSON shape:
 *   { "o11y": { "sheet_tab": "Observability Estimator", "layout": "1.22",
 *               "cloud": { "provider": "AWS", "region": "us-west-2", "cell": "G8" },
 *               "cells": { "G23": 962, ... } } }
 * Or a single block: { "sheet_tab": "...", "layout": "1.22", "cells": { ... } }
 *
 * Checks before any cell is written:
 * - Every block carries a `layout` tag, and it equals the last version on this
 *   workbook's `Version Control` tab. A missing tag or another version stops the
 *   import. The Search (ES3) block has no layout yet, so it is not imported.
 * - The region cell (`cloud.cell`) is written only when this workbook lists an
 *   option for the org's own provider and region. Otherwise the sheet keeps its
 *   value and the log says `region_match: none`.
 * A cell whose value is null is written blank, so no template sample stays.
 */

var MENU = "Estimator Import";
var VERSION_TAB = "Version Control";
var REFERENCE_TAB = "Estimator_Reference";
// Golden templates are copied, never filled.
var GOLDEN_IDS = [
  "1Jsi5cMIDGfaP-S1_xqDcGeMYJSgklcBbu4dSJAEjlA0",
  "1tUdwJbTmd4helFOzEHMi_gpHw7qu_aCgprjlqmHh2hs",
  "1b91Uoa2e4vBrep_7I4RoY_8Mr19poqaUgWl7LqhIOqY",
];

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu(MENU)
    .addItem("Import JSON from clipboard", "importFromClipboard")
    .addItem("Import from AgentImport!A1", "importFromAgentSheet")
    .addItem("Show last import log", "showLastLog")
    .addToUi();
}

function importFromClipboard() {
  var ui = SpreadsheetApp.getUi();
  var res = ui.prompt(
    MENU,
    "Paste the apps_script_payload JSON (or its o11y block), then OK.",
    ui.ButtonSet.OK_CANCEL
  );
  if (res.getSelectedButton() !== ui.Button.OK) return;
  applyPayload_(res.getResponseText());
}

function importFromAgentSheet() {
  var ss = SpreadsheetApp.getActive();
  var sh = ss.getSheetByName("AgentImport");
  if (!sh) {
    if (GOLDEN_IDS.indexOf(ss.getId()) >= 0) {
      SpreadsheetApp.getUi().alert("This is a golden estimator template. Make a copy and import there.");
      return;
    }
    sh = ss.insertSheet("AgentImport");
    sh.getRange("A1").setValue('{"cells":{}}');
    sh.getRange("A2").setValue("Paste full JSON in A1, then use menu → Import from AgentImport!A1");
    SpreadsheetApp.getUi().alert("Created AgentImport tab — paste JSON into A1 and re-run.");
    return;
  }
  applyPayload_(String(sh.getRange("A1").getValue() || ""));
}

function showLastLog() {
  var props = PropertiesService.getDocumentProperties();
  SpreadsheetApp.getUi().alert(props.getProperty("last_import_log") || "No imports yet.");
}

/** "v1.22" or "1.22" → 1.22. A sheet prints 1.20 as "1.2", so versions compare as numbers. */
function versionNumber_(text) {
  var m = /^v?(\d+(?:\.\d+)?)$/i.exec(String(text === null || text === undefined ? "" : text).trim());
  return m ? parseFloat(m[1]) : null;
}

/** Last filled cell of the Version Control tab's first column: the workbook's own version. */
function sheetVersion_(ss) {
  var sh = ss.getSheetByName(VERSION_TAB);
  if (!sh) return null;
  var rows = sh.getRange("A1:A300").getDisplayValues();
  var last = null;
  for (var i = 0; i < rows.length; i++) {
    var text = String(rows[i][0] || "").trim();
    if (text) last = text;
  }
  return last;
}

/** Why a block must not be written to a workbook of this version. null means it may. */
function layoutProblem_(job, sheetVersion) {
  if (job.layout === null || job.layout === undefined || String(job.layout).trim() === "") {
    return (
      job.sheet_tab +
      ": the payload has no layout tag, so its cells cannot be checked against this sheet. " +
      "Emit it again with emit_estimator_import.py."
    );
  }
  var want = versionNumber_(job.layout);
  var have = versionNumber_(sheetVersion);
  if (want === null) {
    return job.sheet_tab + ": the payload layout tag '" + job.layout + "' is not a version.";
  }
  if (have === null) {
    return job.sheet_tab + ": the " + VERSION_TAB + " tab gave no readable version (" + sheetVersion + ").";
  }
  if (Math.abs(want - have) > 1e-9) {
    return (
      job.sheet_tab +
      ": the payload was built for estimator v" +
      job.layout +
      " and this sheet reads " +
      sheetVersion +
      "."
    );
  }
  return null;
}

function norm_(text) {
  return String(text || "")
    .toLowerCase()
    .replace(/[^a-z0-9]/g, "");
}

/** eastus2 and us-east-2 are the same Azure region. */
function regionAliases_(region) {
  var r = norm_(String(region || "").toLowerCase().replace(/^(aws|gcp|azure)-/, ""));
  var out = r ? [r] : [];
  var compass = "east|west|north|south|central|southeast|northeast";
  var places =
    "us|europe|asia|india|australia|uk|japan|brazil|canada|germany|france|norway|sweden|" +
    "switzerland|uae|korea|africa|qatar|poland|italy|israel|spain";
  var m = new RegExp("^(" + compass + ")(" + places + ")(\\d*)$").exec(r);
  if (m) out.push(m[2] + m[1] + m[3]);
  m = new RegExp("^(" + places + ")(" + compass + ")(\\d*)$").exec(r);
  if (m) out.push(m[2] + m[1] + m[3]);
  return out;
}

/** The option for this provider and region, or null. Never another cloud's or another region's option. */
function matchRegion_(provider, region, options) {
  var prov = String(provider || "").trim().toUpperCase();
  var want = regionAliases_(region);
  if (!prov || !want.length) return null;
  for (var i = 0; i < options.length; i++) {
    var m = /^(AWS|AZURE|GCP)\s*:?\s*([a-z0-9\-]+)/i.exec(options[i]);
    if (!m || m[1].toUpperCase() !== prov) continue;
    if (want.indexOf(norm_(m[2])) >= 0) return options[i];
  }
  return null;
}

/** Every "PROVIDER: region (Name)" string on the reference tab. */
function regionOptions_(ss) {
  var sh = ss.getSheetByName(REFERENCE_TAB);
  if (!sh) return [];
  var rows = sh.getRange("A1:M450").getDisplayValues();
  var out = [];
  for (var r = 0; r < rows.length; r++) {
    for (var c = 0; c < rows[r].length; c++) {
      var text = String(rows[r][c] || "").trim();
      if (/^(AWS|AZURE|GCP)\s*:?\s*[a-z0-9\-]+/i.test(text) && out.indexOf(text) < 0) out.push(text);
    }
  }
  return out;
}

function applyPayload_(raw) {
  var ui = SpreadsheetApp.getUi();
  raw = String(raw || "").trim();
  if (!raw) {
    ui.alert("Empty JSON.");
    return;
  }
  var payload;
  try {
    payload = JSON.parse(raw);
  } catch (e) {
    ui.alert("Invalid JSON: " + e);
    return;
  }

  var ss = SpreadsheetApp.getActive();
  if (GOLDEN_IDS.indexOf(ss.getId()) >= 0) {
    ui.alert("This is a golden estimator template. Nothing was written. Make a copy and import there.");
    return;
  }
  var tabs = ss.getSheets().map(function (s) {
    return s.getName();
  });
  var jobs = normalizeJobs_(payload, tabs);
  if (!jobs.length) {
    ui.alert(
      "No matching sheet tabs.\nOpen a copy of the Observability Estimator workbook.\nTabs here: " +
        tabs.join(", ")
    );
    return;
  }

  // Every block is checked before any cell is written.
  var version = sheetVersion_(ss);
  var problems = [];
  jobs.forEach(function (job) {
    var problem = layoutProblem_(job, version);
    if (problem) problems.push(problem);
  });
  if (problems.length) {
    ui.alert("Layout mismatch. Nothing was written.\n" + problems.join("\n"));
    return;
  }

  var log = [];
  jobs.forEach(function (job) {
    var sh = ss.getSheetByName(job.sheet_tab);
    var written = 0;
    var cloud = job.cloud || {};
    var keys = Object.keys(job.cells || {});
    keys.forEach(function (a1) {
      if (cloud.cell && a1 === cloud.cell) return; // the region cell is matched below
      var v = job.cells[a1];
      sh.getRange(a1).setValue(v === null || v === undefined ? "" : v);
      written++;
    });
    var line = job.sheet_tab + ": wrote " + written + " cells";
    if (cloud.cell) {
      var option = matchRegion_(cloud.provider, cloud.region, regionOptions_(ss));
      if (option) {
        sh.getRange(cloud.cell).setValue(option);
        line += "; region " + cloud.cell + " = " + option;
      } else {
        line +=
          "; region " +
          cloud.cell +
          " not written, no option for " +
          cloud.provider +
          " " +
          cloud.region +
          " (region_match: none)";
      }
    }
    log.push(line);
  });

  var msg = log.join("\n");
  PropertiesService.getDocumentProperties().setProperty(
    "last_import_log",
    new Date().toISOString() + "\n" + msg
  );
  ui.alert("Import complete.\n" + msg);
}

/** Pick the blocks that match tabs in this workbook. Each keeps its layout tag and cloud. */
function normalizeJobs_(payload, tabs) {
  var jobs = [];
  function add(block) {
    if (!block || !block.cells) return;
    var tab = block.sheet_tab;
    if (tab && tabs.indexOf(tab) >= 0) {
      jobs.push({ sheet_tab: tab, cells: block.cells, layout: block.layout, cloud: block.cloud });
      return;
    }
    // Fuzzy: single-job payload with cells only
    if (!tab && payload.cells) {
      // prefer Estimator / Observability Estimator if present
      var guess =
        tabs.indexOf("Observability Estimator") >= 0
          ? "Observability Estimator"
          : tabs.indexOf("Estimator") >= 0
            ? "Estimator"
            : tabs[0];
      jobs.push({ sheet_tab: guess, cells: payload.cells, layout: payload.layout, cloud: payload.cloud });
    }
  }

  if (payload.es3 || payload.o11y) {
    add(payload.es3);
    add(payload.o11y);
  } else {
    add(payload);
  }
  return jobs;
}
