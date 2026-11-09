---
name: k360-account-analysis
description: >-
  K360 account summary for one Elastic Cloud customer: ECH vs Serverless
  platform mix, cloud/instance/region detail, deployments by version and over
  time, ECU totals (trailing year, monthly peak, daily average), storage, ECU
  by Observability / Search / Security, ML/vector/AI spend, ≤5 performance
  recs, ≤5 cost-savings recs, a burndown check, and 3- and 6-month ECU
  projections from that year's complete-month pace. Does not pull diagnostic
  bundles and does not quote a Serverless consolidation. That report is
  diag-serverless-consolidation. Triggers: account summary, account analysis,
  account footprint, ECH footprint, "analyze account X", burndown, ECU projection.
---

# K360 account summary

One command → folder of artifacts → account canvas + ≤6 chat bullets. This skill stops at the K360 footprint. It does not price Serverless.

## Hard rules
1. **One pipeline run** per request. The script resolves the K360 name first and exits if it is not unique, so a fuzzy name does not fetch the wrong customer.
2. **Read `*_brief.json` only** (the command prints `brief <path>`). Do not open `*_account.json`, `*_org_report.json`, `*_infra.json`, `sizing.json`, diagnostic zips, or xlsx.
3. Pass **`--skip-diags --skip-sizing`**. Do not download diagnostic bundles. Do not copy estimator sheets. Do not quote a Serverless dollar amount, a consolidation saving, or an ingest GB/day.
4. Chat: ≤6 bullets from `brief.chat_bullets`, plus local PDF links in `brief.links`. Do not add a Serverless quote.
5. A request to price Serverless, to consolidate deployments, or to use diagnostic bundles is the `diag-serverless-consolidation` skill. Finish this summary first only when the user asked for both. Do not start that work inside this skill.

## Run
```bash
SCR=~/.cursor/skills/k360-account-analysis/scripts
cd <project with .env>          # KIBANA_URL, ELASTIC_API_KEY

python $SCR/run_account_analysis.py --org "ORG NAME" --skip-diags --skip-sizing
python $SCR/run_account_analysis.py --org "ORG NAME" --skip-diags --skip-sizing \
  --reuse /tmp/account_org_report.json
```
Every run uses the trailing year (`--days` defaults to 365). Do not shorten the window. The 3- and 6-month projections use that year's complete-month ECU average (partial months excluded), not the last 30 days. Contract dollars on the projection are included only when ARR and a commit pace exist: ARR ÷ (commit ECU/day × 365).

A command that omits `--skip-diags` or `--skip-sizing` hands off to `diag-serverless-consolidation/scripts/run_consolidation.py`. That is the Serverless quote, not this summary.

Flags kept for this skill: `--days N` · `--skip-workload` · `--skip-pdf` · `--artifacts-root` · `--date` · `--reuse`. Leave `--force-sizing`, `--k360-ingest`, and `--diags-dir` unused.

Pipeline: `fetch_org.py` (parent skill, daily grain) → if `cloud-tracker-telemetry` has no stored GB, fill it from `cloud-tracker-data_pivot` → `k360_infra.py` → `analyze.py` → `report_md.py` → `render_pdf.py`. Sizing and diagnostic capture do not run.

## Artifacts — `artifacts/<customer>_<date>/`
| File | Role |
|------|------|
| `<key>_brief.json` | The only file to read |
| `<key>_account.json` | Slim analysis — do not load |
| `<key>_account_report.md` | Full report — do not load |
| `<key>_Account_analysis.pdf` (+ `.html`) | Account summary (§0–10; no consolidation PDF) |
| `<key>_org_report.json`, `<key>_infra.json` | Raw inputs — do not load. The consolidation skill reuses the org report |

K360 does not provide a raw ingest total. Stored GB in this summary is telemetry size, not a Serverless ingest rate.

## Report sections
0 Snapshot and architecture (version, region) · Topology on its own page · 1 Platform (+ 1-AZ warning) · 2 Versions & ECU · 3–4 Burndown and deployments over time, including the 3- and 6-month projection · 5 Storage · 6 ECU by solution · 7 ML/vectors/AI · 8 Recommendations (≤5) · 9 cost recs · 10 sizing notes that stay qualitative. No §11.

§7 includes `ai_ml.token_meter` when inference was billed: model, million tokens, ECU, ECU per million. The model id is the SKU suffix. A chat SKU with no suffix has a blank model name. Quote the brief meter; do not recompute it.

## After the script (agent)
Read `*_brief.json` once. Build the account canvas from `platform`, `ecu`, `versions`, `storage`, `burndown`, `projections`, `recommendations`, and `ai_ml.token_meter`. Quote `projections.horizons` for the 3- and 6-month ECU, remaining balance, and contract dollars. Do not recompute the pace. The architecture table uses `platform.top_deployments` (version, region, zones). The topology is its own section: group those deployments by region and availability zone, then show `nodes` (RAM × node count). Do not build a consolidation canvas, even if `consolidation_canvas_file` is set. Chat is `chat_bullets` plus PDF links (≤6).

## References (open only when needed)
- Page order and PDF rules → [canvas-layout.md](references/canvas-layout.md)
- Rec thresholds → [recommendation-rules.md](references/recommendation-rules.md)
- `account.json` schema → [output-schema.md](references/output-schema.md)
- Setup → [setup.md](references/setup.md)
