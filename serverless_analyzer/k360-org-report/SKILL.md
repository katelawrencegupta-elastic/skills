---
name: k360-org-report
description: >-
  K360 org report parent: run exec (short) or full (expanded §1–10) via
  --report flag. Infra, storage, search/ingest, multi-project serverless
  savings, ECU/burndown, product mix, AI/ML. Triggers: org report, exec
  report, full report, cloud tracker, burndown, serverless savings, offload.
---

# K360 Org Report (parent)

One skill, two deliverables — pick with **`--report`**.

| Flag | Deliverable | Canvas | PDF |
|------|-------------|--------|-----|
| `--report exec` (default) | Exec report | condensed §1–8 + §9–10 | `*_Exec_report.pdf` |
| `--report full` | Full report | expanded §1–10 | `*_Full_report.pdf` |

Details: [exec-report.md](references/exec-report.md) · [full-report.md](references/full-report.md)

## Hard rules (token / model / runs)
1. **Pass `--report exec|full` on every run** (default `exec` if omitted).
2. **One K360 fetch** — never re-query unless `fetch_org.py` fails. Prefer `--reuse`.
3. **Never `Read` the full `*_org_report.json`**. Use `*_canvas.json`, `*_offload.json`, `*_summary.json` only.
4. **One pipeline shell** — no mid-run skill exploration, no subagents, no TodoWrite.
5. **Never** fill Sheets via Chrome/AppleScript. Never write to a golden estimator template.
6. Chat **≤5 bullets** from `*_summary.json` → `chat_bullets_hint`. The last bullet is the caveats. Say every caveat. Methodology/setup refs only if blocked.

## Run
`<skill>` is the directory that contains this file. The same directory works for Claude and Cursor.

```bash
cd <project with .env>

# Exec (short)
python <skill>/scripts/run_org_report.py --org "ORG_NAME" --report exec

# Full (expanded)
python <skill>/scripts/run_org_report.py --org "ORG_NAME" --report full

# Same fetch, other flavor. The pipeline prints this path as "reuse":
python <skill>/scripts/run_org_report.py --org "ORG_NAME" --report full \
  --reuse serverless_analyzer/diag_artifacts/<customer>_<date>/<customer>_<date>_org_report.json
```

`.env`: `KIBANA_URL` (https, no username or password in it), `ELASTIC_API_KEY`. Other flags: `--org-id ID` (repeat when one customer name covers several cloud orgs) · `--skip-pdf` · `--skip-push` · `--skip-workload` · `--days N`.

`--org` is the exact organization name, in any case, or the start of exactly one name. The fetch stops when several organizations start with it and lists them; pass the exact name or `--org-id`.

`--reuse` takes the org JSON in that customer's artifacts folder. The run stops when the file's organization name is not `--org` and does not start with it, or when `--org-id` is not in the file. A file fetched more than 7 days ago prints a warning with its fetch date. The fetch writes to a private temporary folder and the pipeline copies it into the artifacts folder. There is no shared `/tmp` file.

**Pipeline:** fetch → `serverless_analyzer/diag_artifacts/<customer>_<date>/` → slim JSON → Observability estimator copy (Drive+xlsx) → HTML+PDF (named by `--report`) → Drive PDF upload. When Chrome is missing, the HTML file is the report and the PDF upload is skipped. Folders are mode 0700 and files 0600.

## After the script (agent only)
1. Open the matching child doc (`exec-report.md` or `full-report.md`).
2. **Canvas** — use `canvas_file_hint` from the pipeline JSON / `*_summary.json`; embed slim JSON only.
3. **Chat** — ≤5 bullets from `*_summary.json` (+ folder / Drive URL).

## Numbers
- ECU is the K360 billing `cost` field as billed. 1 ECU is $1. No ARR ÷ commit rate is applied.
- **ECU / mo** is lookback ECU ÷ (`--days` ÷ 30.416). Use `ecu_mo` (canvas top level and each item). §10 ECH $/mo uses the same divisor, so the two agree on a page. `ecu_monthly_avg_calendar` and each item's `ecu_monthly_avg` are the mean of calendar-month totals (13 months for 365 days) and are not the report figure.
- Solution families (`families`) partition ECU. They sum to the org total. A deployment whose use case is security is Security.
- `truncated` lists any aggregation that held more than 300 deployments. Say so when it is not empty: totals are low.

## Artifacts (same folder for both)
`serverless_analyzer/diag_artifacts/<customer>_<date>/` holds the Observability estimator copy, chosen `*_Exec_report` or `*_Full_report` PDF, HTML, org JSON, canvas/offload/summary, `artifacts.json`. Both PDFs may coexist in the folder. `--artifacts-root` overrides that directory.

The Observability copy is checked against `estimator_layout.py` before any cell is written. Its region cell is written only when the copy lists an option for the org's own provider and region; otherwise the copy keeps the template's region and `artifacts.json` records `region_match: "none"`. The Search (ES3) estimator is **not filled**: no ES3 layout is recorded in `estimator_layout.py`, so a copy cannot be checked. `artifacts.json` → `es3_not_filled` says so. The Search price in §10 is the Python model.

## §10 (required)
`offload.full_split` lines + totals; $/yr + 3yr; hybrid secondary; never recommend all-ES3. → [methodology.md](references/methodology.md)

Only ECH deployments with stored GB are compared. `full_split.not_priced` names every other deployment with its ECU/mo and reason (`no stored GB in telemetry`, `already serverless`, `no bill in the last 90 days of the range`). They are on neither side: `full_ech_mo`, the saving, $/yr and 3-yr cover `priced_n` deployments. Always print how many deployments and how much ECU/mo were not compared (`not_priced_n`, `not_priced_ecu_mo`). Never show a deployment priced at $0.

## Storage
Each row in `storage.by_deployment` has its own snapshot day (`as_of`). `storage.stale` lists snapshots more than 45 days older than the range end; say so when it is not empty. `storage.not_billing` lists deployments with a snapshot and no bill in the last 90 days of the range; they are not in org stored GB.

## ML + vectors
One section from slim `ml` / `ai`. No invented model ids when only `vector_mappings`: `ml.vectors.model_counts` and `ml.vectors.unnamed_model` are in the canvas JSON. Chat model ids are the SKU suffix; a chat SKU with no suffix has no model name. `quantity` is millions of tokens.
