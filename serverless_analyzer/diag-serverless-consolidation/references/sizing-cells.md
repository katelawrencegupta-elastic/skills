# Serverless estimator copies

The account summary starts sizing when `platform.ech_share_pct > 50`. A consolidation run sizes from the diagnostic bundles whether or not that share is above 50. `--force-sizing` and `--skip-sizing` still apply.

## Folder + naming
- The compression-applied set is retired (see calculations.md). No `compression_applied` folders or `compression-applied` names are written.
- Local: `~/serverless_analyzer_artifacts/<customer_slug>_<YYYY-MM-DD>/` · Drive: folder with the **same name** in My Drive (found-or-created). `SERVERLESS_ANALYZER_ARTIFACTS` overrides the local directory.
- Estimate sheets live directly in `generated_estimates`, under the customer folder on Drive and under the artifact folder locally. Deployment-plan sheets live in `generated_estimates/plan_sheets`. Every PDF is in the customer folder root, on Drive and under the artifact folder: the account PDF, the appendix, and the consolidation PDF. There are no `full_retention` or `30d` folders. An estimate or a consolidation PDF that an earlier run filed under a `full_retention` or `30d` folder is moved up, on disk and on Drive, and the folder is removed once it is empty. An estimate sheet is named `<customer>_<date>_<search|observability|security>_full retention_<bulk>_<region>_serverless_estimate`. The region is the cloud region that sheet was scoped to. A solution that spans regions is one sheet per region. There is no second fleet-wide sheet for that solution.
- The consolidation PDF is `<customer>_<date>_full retention_bulk_serverless_consolidation_plan`. A K360 backup uses `k360` in the basis slot. The account PDF is `<customer>_<date>_Account_analysis`. Same-named PDFs are updated in place. Deployment-plan sheets are named `<run_key>_Observability_plan_` or `<run_key>_Security_plan_` and stay in `plan_sheets`.
- Copies: `<customer>_<date>_observability_full retention_bulk_<region>_serverless_estimate`, and the same pattern for security and search. Each solution with a workload is written once per region, at the average index retention rounded up to a whole month. There is no 30-day sheet and no second measured-retention copy. A solution with no workload is skipped. Search is a copy of the Search estimator sheet (the es3-estimator CSV report, uploaded as a Google Sheet, only with `--search-notebook`), on the measured local store. Search disk is not scaled by the month.
- Same-named copies in the folder are **reused** on re-runs (cells rewritten); `--fresh` forces new copies. Golden templates are never written to.
- **Layout check.** Every copy, new or reused, is checked before a cell is written (`verify_template`). The labels beside the cells below must be in place (`TEMPLATE_ANCHORS` in `k360-org-report/scripts/estimator_layout.py`, which `quote_checks.py` re-exports), and the last version on the `Version Control` tab must be readable and at least Observability 1.22 or Security 2.6. A miss raises `TemplateMismatch`, trashes a copy made by that call, exits `size_serverless.py` with status 3, and stops `run_consolidation.py`. When a new estimator version moves a row, change the cell map, `TEMPLATE_ANCHORS`, and this file together.

## Golden templates (fixed — always copy these, never edit in place)
| Kind | Template | Override |
|------|----------|----------|
| Observability (v1.22) | [1tUdwJbTmd4helFOzEHMi_gpHw7qu_aCgprjlqmHh2hs](https://docs.google.com/spreadsheets/d/1tUdwJbTmd4helFOzEHMi_gpHw7qu_aCgprjlqmHh2hs/edit) | `O11Y_SHEET_ID` / `--o11y-id` |
| Security (v2.6) | [1b91Uoa2e4vBrep_7I4RoY_8Mr19poqaUgWl7LqhIOqY](https://docs.google.com/spreadsheets/d/1b91Uoa2e4vBrep_7I4RoY_8Mr19poqaUgWl7LqhIOqY/edit?usp=drive_link) | `SECURITY_SHEET_ID` / `--security-id` |
| Search (v2.3) | [13OdvASrBHTVqfe1VMdp8gXyHrMZ7cPeyvtKMK7A9VpY](https://docs.google.com/spreadsheets/d/13OdvASrBHTVqfe1VMdp8gXyHrMZ7cPeyvtKMK7A9VpY/edit) | `SEARCH_SHEET_ID` / `--search-id`. Performant total (G110) is the quotable $ |
| Search, notebook path | es3-estimator `estimate_serverless_pricing` (clone via `ES3_ESTIMATOR_DIR`, venv via `ES3_ESTIMATOR_PY`) | `--search-notebook`. Performant profile is the quotable $ |

Overrides accept a bare id or a full `docs.google.com/spreadsheets/d/<id>/…` URL. The consolidation-savings analysis (`consolidation.py`) prices with the Observability template's `Estimator_Reference` ladders (imported from the parent skill) and reconciles against the copied sheet's read-back.

## Consolidated inputs (`sizing.consolidated`)
The K360 scenario is produced only when `--k360-ingest` is explicitly requested. It sets `daily_raw_gb = stored_gb / (retention_months × 30.416)` per solution slice and is an assumption-based comparison, never the automatic quote. K360 does not provide a raw ingest total. If a slice has ECU but no telemetry, GB is prorated by ECU share (`gb_estimated_from_ecu_share: true`). A slice with no ECU and no GB is **skipped** unless `--always-copy`.

The default quote does not back-calculate. `daily_raw_gb` is the sum of measured bulk GB/day, and `retention_months` is the store-weighted average retention across indexes, rounded up to a whole month and clamped to 1–36 for the sheet. With no index counted it is the ingest-weighted delete age the bundles measured, and with no retention ascertained at all there is no month: `retention_months` is absent, `--force-months` is not passed, and the Observability and Security sheets are not filled. A row without a delete phase or data-stream retention has no age and no weight. A regional project uses that region's measured bulk rate when `architecture.groups[].daily_raw_gb` is set. Search disk is the local diagnostic store (`size_in_bytes`). Searchable snapshots stay out of that store. A deployment with no bundle adds no ingest or stored GB to the estimate.

## Observability — tab `Observability Estimator`
| Cell | Value | Source |
|------|-------|--------|
| G4 | `Complete` | feature tier, from the account's `sizing.feature_tier` (default `Complete`; dropdown: Complete / Logs Essentials) |
| G5 | `Premium` | support (Limited / Base / Enhanced / Premium) — like-for-like with ECH Enterprise |
| G8 | region option | matched dropdown string, e.g. `AWS: us-east-1 (Virginia)` |
| G11 | retention months | The average index retention, rounded up to a whole month, for everything except TSDS metrics. `retention_months_measured` is the size-weighted record, not this cell. `--retention-months` is the K360 backup only. `--retention-days` replaces the average |
| G12 | retention months | The same rounded-up month, on the TSDS metrics row. `run_consolidation.py` always passes `--force-months`, so G12 equals G11 on every orchestrated run and the measured metrics age is a record, not this cell. Only a direct `size_serverless.py` run with no `--force-months` writes the metrics indices' own rounded-up age here |
| G15 | `No` | synthetics |
| G19 | monthly GB | Option 1, everything except TSDS metrics: (logs + traces GB/day) × 730 ÷ 24. Diagnostic bulk is metered, so it goes here. `0` on the K360 backup |
| G20 | monthly GB | Option 1, TSDS metrics: metrics GB/day × 730 ÷ 24. `0` on the K360 backup |
| G23 | `0` | Option 2 logs. Off on a diagnostic run. The K360 backup writes the measured logs plus traces here, from the product mix. With no product mix it stops: no 90/10 split is invented |
| G24 | `0%` | k8s share of Option 2 logs |
| G25 | `0` | Option 2 metrics. Off on a diagnostic run. The K360 backup writes the measured metrics here, from the product mix |
| G26 | `0` | traces/min. Measured trace GB/day is in G19 |
| G27 | `100%` | trace sampling |
| G33 | `No` | Cross-Project Search add-on |

Read-back, unformatted: `Estimator_Reference!C2` + `Estimator_Reference-Metrics!C2` the GB/day the two ladders price (the echo on a diagnostic run) · `G28` total raw GB/day (the echo on the K360 backup) · `G29` metered GB/day · `D38` monthly steady state · `E38` annual year 1 · `F38` annual steady state (estimator v1.22). The sheet echo is accepted when it is within the tolerance, 0.1% of the bulk written to that sheet and at least 0.02 GB/day. A wider miss is recorded and Observability is left unquoted. The written bulk stays on the solution block.

## Security — tab `Security Estimator`
| Cell | Value | Source |
|------|-------|--------|
| G4 | SIEM raw GB/day | `daily_raw_gb` of the security slice ÷ the sheet's expansion (G5 ÷ G4 on the copy, 1.66), so G5 is the measured GB/day. The K360 backup writes `daily_raw_gb` as is |
| G6 | retention months | The average index retention, rounded up to a whole month. `retention_months_measured` is the size-weighted record. `--security-retention-months` is the K360 backup only |
| G7 | region option | matched dropdown string, e.g. `AZURE: us-east-2 (Virginia)` |
| G8 | `Complete` | tier (Complete / Essentials) |
| G9 | `0` | Elastic Defend endpoints (unknown from K360) |
| G21 | `Premium` | support (Limited / Base / Enhanced / Premium) |

Cleared on the copy, before the read-back: G11, G12, G14–G18, G20, G23–G25, G27–G34. A numeric cell is set to 0. A dropdown that contains `No` is set to `No`. A dropdown that cannot accept either is left alone. The golden template is not edited. E74 is the monthly quote only when parsed E73 is 0. A non-zero E73, including a parenthesized credit, leaves Security out of the quote. E73 is not subtracted from E74 by hand.
Read-back, unformatted: `G5` metered GB/day · `E74` monthly total · `E61` ingest · `E62` retention · `E69` support · `E73` CPS add-on inside that total · `D36` / `E36` / `F36` annual Y1 / Y2 / Y3. The headline writes the rounded-up index average, so E74 is the monthly quote. E74 − E61 − E62 − E69 − E73 is `other_addons_monthly`: a non-zero amount there blocks the quote the same way a non-zero E73 does. Y2 is the steady annual. There is no separate monthly cell above the year row.

## Region matching (`size_serverless.match_region`)
Dropdown strings are scraped live from the copy's `Estimator_Reference` tab (any cell matching `^(AWS|AZURE|GCP)`), so template version drift is tolerated. K360 region (`us-east-1`, `eastus2`, `europe-west1`) is normalised (strip dashes) and compared; Azure compass aliases handled (`eastus2` ⇄ `us-east-2`). Result recorded as `region_match: "<option> (exact|alias|provider_default|nearest|none)"`. A region that is not listed is priced as AWS `us-east-1`, Azure `eastus`, or GCP `us-east1`, recorded as `provider_default`. The Security sheet spells those Azure and GCP defaults `us-east-1`. The deployment's own region stays on the quote. Region fidelity warns and the quote is still produced; the read-back is for the region named, not the one the deployments run in. `none` is only when that cloud has no default on the list, and it leaves that solution unquoted, including Search. Another cloud is never written.

## Search (estimator sheet, v2.3)
The default. A copy of the Search template is filled on tab `Estimator`, column C, and read back, as for Observability and Security. `search_sheet.py` owns the fill. Layout is checked first against `TEMPLATE_ANCHORS["search"]` in `k360-org-report/scripts/estimator_layout.py` (labels in column B and E; the workbook has no Version Control tab, so labels are the whole check).

| Cell | Input |
| --- | --- |
| C5, C6, C7, C8 | Provider, region text (`eastus (Virginia)`), support level, workload `text` |
| C12 | Stored GB (the search slice). C13 and C14 stay blank |
| C19, C20, C21, C22 | Peak QPS, complexity, share of the 730-hour month, minimum VCU (0) |
| C24, C25, C26, C27 | The same for non-peak |
| C30 | Idle minimum VCU (0) |
| C33, C34, C35 | Ingest docs/s, average document bytes, active hours |
| C37, C40:C45, C48 | Data out, cross-project search, fast refresh: all off (0, 0, No) |

Every input is written, zeros included, so no template default survives. Read-back: C53 must say `Yes` (a region the sheet cannot price is rejected). Columns F, G and H are on-demand, performant and high availability; rows 103 to 110 are search, ingest, retained data, data out, CPS, subtotal, support and total per month, and 89, 96 and 98 are peak VCU, idle VCU and VCU-hours. The quote is G110. `annual_year1` is G110 × 12.

The sheet only offers query complexity `simple` (100 ms), `moderate` (500 ms) and `complex` (5,000 ms). A measured query time follows the sheet's Documentation rule: up to 250 ms is `simple`, 250 to 500 `moderate`, over 500 `complex`. The pick is written to `assumptions`. The notebook path uses the measured milliseconds, so the two paths can differ where query time is far from those three points. The deployment-count bars, each region and the search-power profiles all read Search sheet copies: a plan project is a copy in `plan_sheets`, named `<run_key>_Search_plan_…`, and a project that recurs on several bars is one copy. A plan project takes its own region's workload, scaled by its share of that region's Search store, so the floor (one project per region) gets exactly the inputs the headline sheet got. A region with no proxy-log workload leaves the bars unpriced rather than borrowing the org-wide sum. A region with no sheet read-back is left unquoted; it is never re-priced on the notebook.

## Search (es3-estimator, `--search-notebook`)
`es3_estimate.py` calls `estimate_serverless_pricing` from the notebook's library. Inputs: text workload, `disk_storage_gb` = the search slice's stored GB, and the regional workload below. Search workload is exact per region. QPS, query complexity and the peak and non-peak hour shares come from the service proxy request logs (`logging-*:service-proxy-requests-*`, read with `PROXY_LOGS_URL` and `PROXY_LOGS_API_KEY`, from `.env` or the environment) for that region's own Search deployments, over the last `--proxy-days` complete UTC days (default 7). Only `external` requests are counted, and Metricbeat, `Mozilla` and `fac/` user agents are dropped. A request is a search when its action is `search` or its path has `_search`, `_msearch`, `_count` or `_pit`. Peak is weekdays 13:00 to 23:00 UTC and everything else is non-peak, so idle is 0. QPS is the 95th percentile of per-minute peak QPS in each window, rounded up. Complexity is the median hourly p90 latency: up to 250 ms simple, 250 to 500 moderate, over 500 complex. Ingest comes from the diagnostic bundles: bytes/sec is the measured bulk GB/day, the average document is local primary store bytes over primary documents, and active hours are the whole 730-hour month. Nothing is scaled by a store share. A region whose proxy logs return no traffic, or whose bundles show no bulk or no documents, is left unquoted and named in `limits` (`search_proxy_workload_missing`). With `--search-notebook`, every Search figure (headline, regions, deployment-count bars, profiles) is priced on this library, with the same per-region inputs as the sheet path. Billing pace never scales Search workload. Support Premium maps to the notebook's Enterprise (15%). Provider and region are matched to the library's price table. A region that is not in that table is priced as AWS `us-east-1`, Azure `eastus`, or GCP `us-east1`. A cloud with no default in the table leaves Search unquoted.

Read-back (`estimators.search.readback`): `monthly_steady_state`, `annual_year1`, `annual_steady_state` from the **performant** profile, plus search / ingest / storage / support dollars and provisioned vs idle search VCU. On-demand and high availability are in the markdown and in `profiles`. They are not the quote. Performant is the profile this skill quotes. The es3-estimator README says more than 98% of customers run Performant, so it is the setting the model has the most data for. On-demand and high availability are shown beside it; the README says on-demand can come out too high and high availability too low because of the training data. `QUOTED_SEARCH_PROFILE` in `es3_estimate.py` sets the quoted profile, and a run whose estimator returns no numeric total for it leaves Search unquoted.

The consolidation headline uses the performant total for Search plus the Observability and Security sheet read-backs. EIS is billed inference ECU and tokens for eligible Search deployments; ML-node ECU is kept separate and is not attributed from Jina SKU co-occurrence. When a solution runs in more than one region, there is one sizing per project, named `<customer>_<date>_<solution>_full retention_bulk_<region>_serverless_estimate`. Search is a Search estimator sheet for that region's store and its own proxy-log workload. Observability and Security are a sheet filled with that region's GB/day. The report links each sizing, and the headline is the sum of those read-backs plus EIS. Deployments, the tail, and fleet rows are ECU and GB, not a second price.
