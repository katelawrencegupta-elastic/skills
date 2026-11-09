# Serverless analyzer

Three skills that share one K360 fetch. The same folders run in Claude and in Cursor. Install with [INSTALL.md](INSTALL.md), or run `python3 install.py` from this folder.

`<skill>` in the commands below is the directory that contains that skill's `SKILL.md`. After `install.py` links the folders, that is `~/.claude/skills/<name>` or `~/.cursor/skills/<name>`. From this repo it is `serverless_analyzer/<name>`.

A script finds the other two skills beside its own folder, then in `~/.claude/skills` and `~/.cursor/skills`, then in a project `.claude/skills` or `.cursor/skills`. `K360_ORG_SKILL_DIR`, `K360_ACCOUNT_SKILL_DIR`, and `DIAG_SKILL_DIR` override that search. Each skill keeps its own copy of `scripts/skill_paths.py`, and those three copies stay the same.

| Order | Skill | What it produces | Stops before |
|---|---|---|---|
| 1 | [k360-org-report](k360-org-report/SKILL.md) | Exec or full organization report, plus the org JSON the other two reuse | An account summary |
| 2 | [k360-account-analysis](k360-account-analysis/SKILL.md) | One customer's footprint, ECU, storage, burndown, and projections | Diagnostic bundles and a Serverless price |
| 3 | [diag-serverless-consolidation](diag-serverless-consolidation/SKILL.md) | A Serverless quote from diagnostic bulk and measured retention | Using stored GB ÷ assumed retention as the quoted ingest |

Run each command from the project that holds `.env`. Artifacts land in `artifacts/<customer>_<date>/`.

## k360-org-report

Fetches one Elastic Cloud organization from K360 and writes an exec report or a full report. The exec report condenses the footprint and keeps the projection and the Serverless split. The full report gives each section its own block. Both can copy Search and Observability estimator workbooks and upload a PDF.

`python <skill>/scripts/run_org_report.py --org "ORG NAME" --report exec`

| Script | What it does |
|---|---|
| `run_org_report.py` | One pipeline: fetch, slim JSON, estimator copies, HTML and PDF |
| `fetch_org.py` | Queries K360 and writes the compact org JSON. One fetch is reused with `--reuse` |
| `report_mode.py` | Shared `exec` and `full` names for the PDF, HTML, and canvas |
| `emit_canvas_data.py` | Writes the small canvas and summary JSON so the full org JSON stays closed |
| `push_estimator.py` | Copies the golden estimator workbooks, fills them, and can upload the PDF |
| `render_org_pdf.py` | Renders the exec or full HTML and PDF and uploads the PDF |
| `emit_estimator_import.py` | Writes Apps Script JSON for a manual estimator fill |
| `sheets_auth.py` | One-time Sheets and Drive sign-in. The token stays in `~/.config` |
| `skill_paths.py` | Finds this skill's siblings for the current run. The same file is in the other two skills |

References: `exec-report.md` and `full-report.md` are the canvas outlines. `data-model.md` describes the fetch. `methodology.md` is the Serverless split. `taxonomy.json` classifies SKUs. `setup.md` is the Sheets sign-in. `EstimatorImport.gs` is the Apps Script fallback.

## k360-account-analysis

Writes the account summary for one customer: platform mix, cloud and instance footprint, versions, ECU, storage, solution mix, ML and inference, a burndown check, and 3- and 6-month projections. Pass `--skip-diags --skip-sizing`. Omitting either flag hands the command to the diagnostic consolidation.

`python <skill>/scripts/run_account_analysis.py --org "ORG NAME" --skip-diags --skip-sizing`

| Script | What it does |
|---|---|
| `run_account_analysis.py` | Fetches or reuses the org report, fills infra, analyzes, and writes the account PDF |
| `k360_infra.py` | One Elasticsearch call for cloud, instance, and region detail |
| `analyze.py` | Turns the org report and infra into the account JSON. No network |
| `report_md.py` | Renders the markdown account report |
| `render_pdf.py` | Renders account and consolidation HTML, then prints PDF with Chrome |
| `upload_pdfs.py` | Uploads those PDFs into the customer's Drive folder |
| `skill_paths.py` | Finds `k360-org-report` and `diag-serverless-consolidation` for this run. The same file is in the other two skills |

References: `setup.md` is the machine setup. `output-schema.md` is the account JSON. `canvas-layout.md` is the page order. `recommendation-rules.md` is how the recommendations are chosen.

## diag-serverless-consolidation

Prices Observability and Security from Elasticsearch diagnostic bundles. Daily GB is bulk on the write indices. Retention is the measured age, written as a whole month. Search disk is local store, and Search ingest stays the K360 rate scaled by that store. The estimator read-back is the dollar. A second estimate, `compressioncalc_accounted_for`, is bulk × 0.60 and does not replace the bulk quote.

`python <skill>/scripts/run_consolidation.py --org "ORG NAME" --reuse <org_report.json> --diags-dir <bundles>`

| Script | What it does |
|---|---|
| `run_consolidation.py` | Reuses the org report, reads bundles, fills the estimators, and writes the consolidation PDFs |
| `admin_diags.py` | Pulls or parses diagnostic zips and writes `sizing_from_diags.json` |
| `vpn_check.py` | Opens a TLS connection to the admin host before a pull. The API key is not sent |
| `diag_account.py` | Builds the diagnostics copy of the account. Ingest and retention come from the bundles |
| `size_serverless.py` | Copies the golden Observability and Security sheets, writes the cells, and reads the dollars back |
| `es3_estimate.py` | Prices a Search slice with the es3-estimator library |
| `consolidation.py` | Builds the savings, deployment-count plan, metered rates, and quote integrity from the read-backs |
| `quote_checks.py` | Checks the sheet echo, coverage, retention, and published totals. No network |
| `diag_inputs.py` | Prints 2–5 project packs and per-deployment inputs. It does not quote dollars |
| `privacy.py` | Sets private file modes and warns when `.env` or `artifacts/` would be committed |
| `test_diag_quote.py` | Offline unit tests. No Google account and no sheets |
| `skill_paths.py` | Finds `k360-org-report` and `k360-account-analysis` for this run. The same file is in the other two skills |

References: `calculations.md` is the arithmetic. `sizing-cells.md` is the estimator cell map.

## How a quote is assembled

`k360-org-report` writes `<customer>_<date>_org_report.json`. `k360-account-analysis` can reuse that file for the summary. `diag-serverless-consolidation` reuses it again, then replaces ingest and retention with the diagnostic measurements. Observability monthly is D32. Security monthly is E74. Search is the es3 on-demand profile. The account PDF stays in the customer folder. Measured-retention consolidation PDFs go in `full_retention/`. 30-day PDFs go in `30d/`.
