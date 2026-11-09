/**
 * Estimator Import (no GCP OAuth) — paste into Extensions → Apps Script
 * on a *copy* of ES3 or Observability estimator (or any sheet you can edit).
 *
 * Usage:
 * 1. Run fetch_org.py → org JSON includes offload.apps_script_payload
 * 2. Or: python scripts/emit_estimator_import.py --org-json /tmp/org_report.json --kind o11y
 * 3. Copy the JSON, open the target spreadsheet
 * 4. Menu: Estimator Import → Import JSON from clipboard
 *    (fallback: paste JSON into sheet AgentImport!A1, then Import from AgentImport!A1)
 *
 * JSON shape:
 *   { "es3": { "sheet_tab": "Estimator", "cells": { "C19": 50, ... } },
 *     "o11y": { "sheet_tab": "Observability Estimator", "cells": { "G21": 962, ... } } }
 * Or a single map: { "sheet_tab": "Estimator", "cells": { ... } }
 */

var MENU = "Estimator Import";

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
    "Paste the apps_script_payload JSON (or es3/o11y subset), then OK.",
    ui.ButtonSet.OK_CANCEL
  );
  if (res.getSelectedButton() !== ui.Button.OK) return;
  applyPayload_(res.getResponseText());
}

function importFromAgentSheet() {
  var ss = SpreadsheetApp.getActive();
  var sh = ss.getSheetByName("AgentImport");
  if (!sh) {
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
  var tabs = ss.getSheets().map(function (s) {
    return s.getName();
  });
  var jobs = normalizeJobs_(payload, tabs);
  if (!jobs.length) {
    ui.alert(
      "No matching sheet tabs.\nOpen the ES3 Estimator or Observability Estimator workbook.\nTabs here: " +
        tabs.join(", ")
    );
    return;
  }

  var log = [];
  jobs.forEach(function (job) {
    var sh = ss.getSheetByName(job.sheet_tab);
    var written = 0;
    var keys = Object.keys(job.cells || {});
    keys.forEach(function (a1) {
      var v = job.cells[a1];
      if (v === null || v === undefined || v === "") return;
      sh.getRange(a1).setValue(v);
      written++;
    });
    log.push(job.sheet_tab + ": wrote " + written + " cells");
  });

  var msg = log.join("\n");
  PropertiesService.getDocumentProperties().setProperty(
    "last_import_log",
    new Date().toISOString() + "\n" + msg
  );
  ui.alert("Import complete.\n" + msg);
}

/** Pick es3 and/or o11y blocks that match tabs in this workbook. */
function normalizeJobs_(payload, tabs) {
  var jobs = [];
  function add(block) {
    if (!block || !block.cells) return;
    var tab = block.sheet_tab;
    if (tab && tabs.indexOf(tab) >= 0) {
      jobs.push({ sheet_tab: tab, cells: block.cells });
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
      jobs.push({ sheet_tab: guess, cells: payload.cells });
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
