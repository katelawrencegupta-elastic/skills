# Serverless estimator copies (ECH > 50 % rule)

Trigger: `platform.ech_share_pct > 50` (ECU-weighted, ECH vs serverless projects). Override with `--force-sizing` / `--skip-sizing`.

## Folder + naming
- The compression-applied set is retired (see calculations.md). No `compression_applied` folders or `compression-applied` names are written.
- Local: `serverless_analyzer/diag_artifacts/<customer_slug>_<YYYY-MM-DD>/` · Drive: folder with the **same name** in My Drive (found-or-created).
- Bulk estimate sheets live in `generated_estimates/full_retention` or `generated_estimates/30d`, under the customer folder on Drive and under the artifact folder locally. An estimate already under `generated_estimates` is moved into the folder its name calls for. Deployment-plan sheets live in `generated_estimates/plan_sheets`. Measured-retention PDFs go in `full_retention` and 30-day PDFs go in `30d`, on Drive and under the artifact folder. The account PDF stays in the customer folder. An estimate sheet is named `<customer>_<date>_<search|observability|security>_<full retention|30d>_<bulk>_<region>_serverless_estimate`. The region is the cloud region that sheet was scoped to. A solution that spans regions is one sheet per region. There is no second fleet-wide sheet for that solution.
- A consolidation PDF is `<customer>_<date>_<full retention|30d>_<bulk>_serverless_consolidation_plan`. A K360 backup uses `k360` in the basis slot. The account PDF is `<customer>_<date>_Account_analysis`. Same-named PDFs are updated in place. Deployment-plan sheets are named `<run_key>_Observability_plan_` or `<run_key>_Security_plan_` and stay in `plan_sheets`.
- Copies: `<customer>_<date>_observability_full retention_bulk_<region>_serverless_estimate`, and the same pattern for security and search. Each solution with a workload is written twice: bulk at full retention and at 30 days. A solution with no workload is skipped. Search is the es3-estimator CSV report, uploaded as a Google Sheet. The 30-day Search sheet scales disk by 1 / written months. Org store scales by the same factor, so the ingest share stays put.
- Same-named copies in the folder are **reused** on re-runs (cells rewritten); `--fresh` forces new copies. Golden templates are never written to.
- **Layout check.** Every copy, new or reused, is checked before a cell is written (`verify_template`). The labels beside the cells below must be in place (`TEMPLATE_ANCHORS` in `quote_checks.py`), and the last version on the `Version Control` tab must be readable and at least Observability 1.22 or Security 2.6. A miss raises `TemplateMismatch`, trashes a copy made by that call, exits `size_serverless.py` with status 3, and stops `run_consolidation.py`. When a new estimator version moves a row, change the cell map, `TEMPLATE_ANCHORS`, and this file together.

## Golden templates (fixed — always copy these, never edit in place)
| Kind | Template | Override |
|------|----------|----------|
| Observability (v1.22) | [1tUdwJbTmd4helFOzEHMi_gpHw7qu_aCgprjlqmHh2hs](https://docs.google.com/spreadsheets/d/1tUdwJbTmd4helFOzEHMi_gpHw7qu_aCgprjlqmHh2hs/edit) | `O11Y_SHEET_ID` / `--o11y-id` |
| Security (v2.6) | [1b91Uoa2e4vBrep_7I4RoY_8Mr19poqaUgWl7LqhIOqY](https://docs.google.com/spreadsheets/d/1b91Uoa2e4vBrep_7I4RoY_8Mr19poqaUgWl7LqhIOqY/edit?usp=drive_link) | `SECURITY_SHEET_ID` / `--security-id` |
| Search | es3-estimator `estimate_serverless_pricing` (clone via `ES3_ESTIMATOR_DIR`, venv via `ES3_ESTIMATOR_PY`) | performant profile is the quotable $ |

Overrides accept a bare id or a full `docs.google.com/spreadsheets/d/<id>/…` URL. The consolidation-savings analysis (`consolidation.py`) prices with the Observability template's `Estimator_Reference` ladders (imported from the parent skill) and reconciles against the copied sheet's read-back.

## Consolidated inputs (`sizing.consolidated`)
The K360 backup, used by `--k360-ingest` and when no bundles are available, sets `daily_raw_gb = stored_gb / (retention_months × 30.416)` per solution slice (GB from ECH telemetry, grouped by `solutions.by_deployment`). K360 does not provide a raw ingest total. If a slice has ECU but no telemetry, GB is prorated by ECU share (`gb_estimated_from_ecu_share: true`). A slice with no ECU and no GB is **skipped** unless `--always-copy`.

The default quote does not back-calculate. `daily_raw_gb` is the sum of measured bulk GB/day, and `retention_months` is the ingest-weighted diagnostic retention, rounded up to a whole month and clamped to 1–36 for the sheet. Only rows with a measured age above 0 days enter that weight. A solution with bulk and no measured retention leaves the month unset and does not get a sheet. A regional project uses that region's measured bulk rate when `architecture.groups[].daily_raw_gb` is set. Search disk is the local diagnostic store (`size_in_bytes`). Searchable snapshots stay out of that store. K360 does not provide a raw ingest total. `--k360-ingest` writes a second set of sheets suffixed `_from_k360` whose GB/day is stored GB ÷ assumed retention. The sidecar for that backup is `sizing_from_k360.json`. A deployment with no bundle adds no ingest.

## Observability — tab `Observability Estimator`
| Cell | Value | Source |
|------|-------|--------|
| G4 | `Complete` | feature tier (dropdown: Complete / Logs Essentials) |
| G5 | `Premium` | support (Limited / Base / Enhanced / Premium) — like-for-like with ECH Enterprise |
| G8 | region option | matched dropdown string, e.g. `AWS: us-east-1 (Virginia)` |
| G11 | retention months | measured retention, rounded up (1–36), for everything except TSDS metrics. `--retention-months` is the K360 backup only |
| G12 | retention months | TSDS metrics retention: the measured delete age of the metrics indices, rounded up (1–36). G11's month when no metrics age was measured, and on the 30-day read |
| G15 | `No` | synthetics |
| G19 | monthly GB | Option 1, everything except TSDS metrics: (logs + traces GB/day) × 730 ÷ 24. Diagnostic bulk is metered, so it goes here. `0` on the K360 backup |
| G20 | monthly GB | Option 1, TSDS metrics: metrics GB/day × 730 ÷ 24. `0` on the K360 backup |
| G23 | `0` | Option 2 logs. Off on a diagnostic run. The K360 backup writes 90 % of `daily_raw_gb` here |
| G24 | `0%` | k8s share of Option 2 logs |
| G25 | `0` | Option 2 metrics. Off on a diagnostic run. The K360 backup writes 10 % of `daily_raw_gb` here |
| G26 | `0` | traces/min. Measured trace GB/day is in G19 |
| G27 | `100%` | trace sampling |
| G33 | `No` | Cross-Project Search add-on |

Read-back, unformatted: `Estimator_Reference!C2` + `Estimator_Reference-Metrics!C2` the GB/day the two ladders price (the echo on a diagnostic run) · `G28` total raw GB/day (the echo on the K360 backup) · `G29` metered GB/day · `D38` monthly steady state · `E38` annual year 1 · `F38` annual steady state (estimator v1.22). The sheet echo is accepted when it is within 0.51 GB/day of the bulk written to that sheet. A wider miss is recorded and Observability is left unquoted. The written bulk stays on the solution block.

## Security — tab `Security Estimator`
| Cell | Value | Source |
|------|-------|--------|
| G4 | SIEM raw GB/day | `daily_raw_gb` of the security slice ÷ the sheet's expansion (G5 ÷ G4 on the copy, 1.66), so G5 is the measured GB/day. The K360 backup writes `daily_raw_gb` as is |
| G6 | retention months | measured retention, rounded up (1–36). `--security-retention-months` is the K360 backup only |
| G7 | region option | matched dropdown string, e.g. `AZURE: us-east-2 (Virginia)` |
| G8 | `Complete` | tier (Complete / Essentials) |
| G9 | `0` | Elastic Defend endpoints (unknown from K360) |
| G21 | `Premium` | support (Limited / Base / Enhanced / Premium) |

Cleared on the copy, before the read-back: G11, G12, G14–G18, G20, G23–G25, G27–G34. A numeric cell is set to 0. A dropdown that contains `No` is set to `No`. A dropdown that cannot accept either is left alone. The golden template is not edited. E74 is the monthly quote only when parsed E73 is 0. A non-zero E73, including a parenthesized credit, leaves Security out of the quote. E73 is not subtracted from E74 by hand.
Read-back, unformatted: `G5` metered GB/day · `E74` month-12 total · `E61` ingest · `E62` retention · `E69` support · `E73` CPS add-on inside that total · `Estimator_Reference!CB254` month-36 total · `D36` / `E36` / `F36` annual Y1 / Y2 / Y3. Retained GB on the sheet starts at zero in month 1 and fills for the retention period, so E74 is the monthly quote up to 12 months of retention. Above 12 months the quote is the month-36 total, and the steady annual is 12 × that month (`steady_basis` on the read-back says which). E74 − E61 − E62 − E69 − E73 is `other_addons_monthly`: a non-zero amount there blocks the quote the same way a non-zero E73 does. Y2 is the steady annual. There is no separate monthly cell above the year row.

## Region matching (`size_serverless.match_region`)
Dropdown strings are scraped live from the copy's `Estimator_Reference` tab (any cell matching `^(AWS|AZURE|GCP)`), so template version drift is tolerated. K360 region (`us-east-1`, `eastus2`, `europe-west1`) is normalised (strip dashes) and compared; Azure compass aliases handled (`eastus2` ⇄ `us-east-2`). Result recorded as `region_match: "<option> (exact|alias|provider_default|none)"` — **call out `provider_default`** in chat.

## Search (es3-estimator)
`es3_estimate.py` calls `estimate_serverless_pricing` from the notebook's library. Inputs: text workload, `disk_storage_gb` = the search slice's stored GB, peak and non-peak QPS with measured query time (ms) and hour share, ingest docs/s scaled by search stored-GB share. Support Premium maps to the notebook's Enterprise (15%). Provider and region are matched to the library's price table; a miss is recorded as `provider_default`.

Read-back (`estimators.search.readback`): `monthly_steady_state`, `annual_year1`, `annual_steady_state` from the **performant** profile, plus search / ingest / storage / support dollars and provisioned vs idle search VCU. On-demand and high availability are in the markdown and in `profiles`. They are not the quote. Performant is the profile this skill quotes. The es3-estimator README says more than 98% of customers run Performant, so it is the setting the model has the most data for. On-demand and high availability are shown beside it; the README says on-demand can come out too high and high availability too low because of the training data. `QUOTED_SEARCH_PROFILE` in `es3_estimate.py` sets the quoted profile, and a run whose estimator returns no numeric total for it leaves Search unquoted.

The consolidation headline uses the performant total for Search plus the Observability and Security sheet read-backs. When Search deployments bill jina embeddings, the ML-node ECU on those deployments is added to the EIS line at list, along with the billed inference tokens. When a solution runs in more than one region, there is one sizing per project, named `<key>_<Solution>_Serverless_<region>`. Search is an es3 sheet for that region's GB and query share. Observability and Security are a sheet filled with that region's GB/day. The report links each sizing, and the headline is the sum of those read-backs plus EIS. Deployments, the tail, and fleet rows are ECU and GB, not a second price.
