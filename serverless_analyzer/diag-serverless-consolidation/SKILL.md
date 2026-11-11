---
name: diag-serverless-consolidation
description: >-
  Diagnostic-to-Serverless consolidation report for one Elastic Cloud account.
  Prices Observability and Security from Elasticsearch diagnostic bundles:
  bulk GB/day and measured retention, through the matching estimator
  read-back. Search disk is local store. Search ingest docs/s stays the
  K360 rate scaled by that store. Sizes deployments with a bill in the
  latest month.
  Leaves out a decommissioned deployment and does not carry its last
  snapshot. Covers one project per solution, per-deployment projects that
  pool ingest under 50 GB/day, a whole-month retention rounded up, a 30-day
  read on the same bulk, two consolidation PDFs (rounded month and 30
  days), a deployment-count plan from one project per ECH deployment, cut 25% at a time until one project per feature per region, the
  blended metered ingest rate for each active deployment, with $0.50 rows
  marked in red, a search-power chart when Search was estimated, not added to the quote,
  Appendix B with each calculated value and its formula, 2–5 project packs,
  and a gap against the K360 stored-GB method.
  Does not use stored GB ÷ assumed retention as the quoted ingest. Triggers:
  diag consolidation, diagnostic bundles, serverless consolidation report,
  price serverless from diags, per-cluster serverless, 2-5 projects, ingest gap.
---

# Diagnostic Serverless consolidation

Quote Serverless from diagnostic measurements. The K360 account summary is a different skill (`k360-account-analysis`). Reuse its org report. Do not rerun that summary.

A fast model launches the one command below and writes the chat bullets from the printed brief. A stronger model continues in that same thread when a G26 or E73 gate fails, a solution is split by region, or the user asks for the 2–5 project pack. One run. The sheets and the search-power model run inside the command.

## Hard rules
1. **Observability and Security ingest are diagnostic bulk.** Daily GB is `bulk_gb_day`. Retention is `retention_days`, and only a value above 0 enters the weight. A row with no measured age is named and left out. Stored GB, stored GB ÷ months, and node size ÷ uptime are not this ingest. Search disk is local store. Search ingest docs/s is the K360 org rate times local search store ÷ org store. On one region, query rate stays the org window. When Search is split by region, query rate is scaled by that region's share of search store. Say so when quoting Search.
2. **The estimator read-back is the dollar.** Observability monthly is D32. Security monthly is E74. Search is the es3 on-demand profile. The ladder is a check. Never edit a golden template.
3. **Match the estimator to the solution.** Observability on the Observability sheet, Security on the Security sheet, Search on es3.
4. **The month written is a whole number.** Snap a remainder under 0.001 month down, round up, then clamp to 1–36. 365 days writes 12. The run writes two PDFs from the same bulk: the rounded month, and 30 days written as 1 month. `compressioncalc_accounted_for` is bulk × 0.60, priced the same way, and does not replace the bulk quote. Each solution with a workload gets four estimate sheets: bulk and compression-applied, at the measured month and at 30 days. A solution with no workload is skipped. Bulk sheets go in `generated_estimates/full_retention` or `generated_estimates/30d`. Compression-applied sheets go in `generated_estimates/compression_applied/full_retention` or `generated_estimates/compression_applied/30d`. An estimate already under `generated_estimates` is moved into the folder its name calls for, on disk and on Drive. Plan sheets stay in `generated_estimates/plan_sheets`. An estimate sheet is named `<customer>_<date>_<solution>_<full retention|30d>_<bulk|compression-applied|k360>_<region>_serverless_estimate`. The region is the cloud region that sheet was scoped to. A solution that spans regions is one sheet per region, and only when that region's projected sizing is more than $10/mo. Observability and Security project from that region's bulk and retention. Search projects that region's share of the solution ballpark. A region at or under $10/mo is left out of the quote. There is no second fleet-wide sheet for that solution. Search disk on the 30-day sheet is measured store × (1 / written months). Search disk on a compression-applied sheet is measured store × 0.60. Ingest share stays put. Query rate does not scale.
5. **Size the active month only.** A deployment is in the quote when it has billing ECU in the latest month. A deployment with no bill that month is out, including its last snapshot. An active deployment with no bundle adds no ingest.
6. **Read the printed brief only.** The command prints `brief <path>`. That file is `<run_key>_consolidation_brief.json`. Do not open `*_account.json`, `*_diags_account.json`, the org report, infra, `sizing.json`, `calculations.md`, `canvas-layout.md`, or `render_pdf.py`. The PDF is the full report. When Chrome is missing, the HTML file in `links` is the report.

## Run
`<skill>` is the directory that contains this file. The same directory works for Claude and Cursor.

```bash
cd <project with .env>          # KIBANA_URL, ELASTIC_API_KEY, FOUND_ADMIN_API_KEY

# Reuse the account-summary fetch when that folder already exists.
python <skill>/scripts/run_consolidation.py --org "ORG NAME" \
  --reuse serverless_analyzer/diag_artifacts/<customer>_<date>/<customer>_<date>_org_report.json

# Bundles already on disk. Does not call the admin API.
python <skill>/scripts/run_consolidation.py --org "ORG NAME" \
  --reuse serverless_analyzer/diag_artifacts/<customer>_<date>/<customer>_<date>_org_report.json \
  --diags-dir serverless_analyzer/diag_artifacts/<org_id>_diags_<date>

# Several cloud orgs share the customer name. Pass every id.
# The contract pivot is the id with the highest commit. Billing, storage,
# workload, and diagnostic bundles cover every id.
python <skill>/scripts/run_consolidation.py --org "ORG NAME" \
  --org-id ID1 --org-id ID2 --org-id ID3
```
Do not pass `--skip-diags`, `--skip-sizing`, or `--k360-ingest` unless the user asked for the K360 backup as a separate document. Diagnostics are the quote. A fresh admin pull checks TLS to the pinned admin host before it sends the API key. Connect the Aviatrix VPN tunnel when that check fails. `--xlsx` is off unless the user asked for local workbooks. Pass `--skip-deployment-plan` only when the user asked to skip the bars.

`--lookback-days` defaults to 30 and cannot exceed 365. That window sets ECH monthly ECU and the GB/day written into the estimators. The account summary still fetches the trailing year. Pass `--lookback-days 365` for a year. A value outside 1–365 stops the run.

## After the script
Quote only when `ingest_source` is `diagnostics`. Build `<slug>-serverless-consolidation-savings.canvas.tsx` from the brief: `savings`, `quotes`, `fleet`, `contract_rate`, `notes`, `diagnostics_coverage`, `compressioncalc_accounted_for`, `thirty_day`, `deployment_plan`, `metered_rates`, `search_power`, `integrity`, `estimators`, and `links`. Chat is at most six bullets plus those links. Use the HTML path when a link has no PDF.

**Cover.** Page 1 is the last 30 days of billed ECU unless `--lookback-days` says otherwise. The chart title names that window: the estimates use the total of those billed days, expressed as a month (that total ÷ the number of days × 30.416). ECH is that month at the contract rate. The page 1 chart draws that ECH bar and serverless at list. It does not project serverless at the contract rate. The page 1 reduction is ECH at the contract rate minus serverless at list, and the chart labels the rate. The deployment-count chart draws serverless at list and the ECH bill on every step. The amount under the project count is that ECH bill minus serverless at list. Serverless is not drawn at the contract rate anywhere in the report. `savings` in the brief stays the list read-back against contract ECH. The estimators are filled with diagnostic bulk × (that pace ÷ the complete-month pace), so D32 and E74 are the same window. `savings` is that read-back. The first row under “What if data is kept longer, or the fleet is busier” is the same window. The complete-month average and the quietest month scale that read-back and are not re-estimated.

**What you pay for today.** The monthly build-up has three bars when a quote lookback is set. Full lookback is the fetched year, split by tier, at the contract rate. The middle bar is the quote-window usage total and is not split by tier. Serverless at list is the estimator at that window. The year bar is labeled Full lookback.

**Report.** Consolidation headings are not numbered. Each heading is the title of that section. Appendix A and Appendix B are one document, `serverless_consolidation_analysis_appendix`, in the run folder. The consolidation PDFs do not repeat them. Appendix A holds the input arithmetic, the metered-rate tables, the search-power chart when Search was estimated, and the quote integrity score. Appendix B lists each calculated value with its formula, the inputs used, and the result already on the full-retention bulk quote. A row is omitted when that value was not calculated. Sheet dollars on Appendix B stay read-backs. Omit the Monthly search-power estimate when `search_power` is absent. Search-power dollars stay at list. Do not price the Observability store as Search. Fixed overhead in the report is the active deployments on the quote window, as a share of their ECU. The full-lookback overhead includes deployments with no bill in the latest month, so the two figures differ. The smallest clusters' ECU is their whole bill. Their fixed overhead is a separate figure, and the merge saving is a quarter of their bill. Bar charts have a top face and a side face. The usage chart under “What if data is kept longer, or the fleet is busier” is full width. Consolidation type is larger than the account report.

**Deployment-count page.** Required on the canvas when `deployment_plan` has rows, and already printed on both bulk PDFs after the cover. Draw every bar in `deployment_plan`, in order: `label`, `projects`, `monthly`. The count varies. The ceiling is every ECH deployment, one serverless project each, aligned to that deployment's feature. The floor is one project per feature that exists in each region (Observability, Search, or Security). Do not invent a project for a region and feature with no deployments, and do not merge features or regions. Each later bar is round(previous × 0.75). round is half-to-even. If that round does not decrease, subtract 1. A result at or under the floor is the floor, and the series stops. Labels are Ceiling, −25% on each middle step, and Floor. Search on a bar is the on-demand estimator for that project. Observability is the D32 read-back. The Observability headline is reused only when the ceiling is already one project. A floor of 1 reached by cutting is priced as that packed group. Appendix B writes that arithmetic with the prior count filled in. Draw `savings.ech_contract_mo` as an ECH bar on every step when that value exists, beside serverless at list. The figure under the project count is that ECH bill minus serverless at list. Do not draw serverless at the contract rate. Do not reprice the bars. An empty `deployment_plan` means the page was not priced: say so, and do not draw stand-in bars. The compressioncalc PDFs do not get this page. The 30-day PDF prices the same series at 1 month; those dollars stay on that PDF.

Confirm the gates from the brief. Do not open the sheet.
- Observability: D32 only when `estimators.observability.g26_gate.accepted` is true. A miss leaves Observability out. The cells on that estimator are what was written.
- Security: E74 only when E73 is 0. A blank, unreadable, or non-zero E73 leaves Security out. A region that was never sized is a missing sheet. No workload skips Security.
- 30-day dollars are `thirty_day`. Each solution with a workload has its own 30-day sheet and its own compression-applied sheet at both retention windows. Search disk on the 30-day sheet is the measured store scaled to 1 month. When the written month is already 1, that sheet matches the full-retention Search dollars. Search-power profiles stay off the quote total, and the Appendix A chart is omitted when `search_power` is absent. Appendix B is in `serverless_consolidation_analysis_appendix` and uses the full-retention bulk quote. A metered row with `at_half_dollar` true is the $0.50 row.
- Give `compressioncalc_accounted_for` beside the bulk quote when the brief has it. Do not replace D32 or E74 with it.
- `integrity`: give the score, whether it passed, and every check in `failed`. A score of 0.90 or higher passes. A check in `failed` stays flagged. It scores the written inputs, the sheet echo, and the published totals. It is not a serverless bill.
- If `diagnostics_coverage.inactive_excluded` is above zero, say how many deployments had no bill in the active month. If `snapshot_gb` is above zero, say how many searchable-snapshot GB were left out. If `k360_fallback` is above zero, those active deployments had no bundle and add no ingest. If `retention_unmeasured` is non-empty, name those deployments.
- Regional dollars are one mode. When a solution spans regions, the headline is the sum of the regional read-backs and there is no second fleet-wide sheet. Each estimator name contains the region it was scoped to. A single-region solution is one sheet, and that name still contains its region. A rejected region leaves that whole solution out.

## Further analysis (only when asked)
A 2–5 project pack, per-deployment prices, or a gap against K360 stored-GB pricing. The K360 backup is a different quantity. Compare it only in a gap section. Do not blend the two into one quote.

```bash
python <skill>/scripts/diag_inputs.py \
  --diags-json serverless_analyzer/diag_artifacts/<org_id>_diags_<date>/sizing_from_diags.json \
  --projects 1,2,3,4,5 --each
```

When the user sets a retention month, pass `--retention-months N` on the consolidation command, keep bulk measured, and leave the 30-day read at 1 month.

## References
Open these only to change a formula or the PDF, not to write a quote.
- Arithmetic → [calculations.md](references/calculations.md)
- Cell map → [sizing-cells.md](references/sizing-cells.md)
- PDF page order → [canvas-layout.md](../k360-account-analysis/references/canvas-layout.md)
- Setup → [setup.md](../k360-account-analysis/references/setup.md)
