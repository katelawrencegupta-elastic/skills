---
name: diag-serverless-consolidation
description: >-
  Diagnostic-to-Serverless consolidation report for one Elastic Cloud account.
  Prices Observability and Security from Elasticsearch diagnostic bundles
  (measured bulk GB/day, retention written as one year) through the matching estimator
  read-back, and Search from local store on es3. Produces one consolidation PDF
  as a one-year (12-month retention) estimate, with recent ECH spend on page 1,
  a deployment-count plan, per-cluster costs, a raw bulk ingest page, and an appendix.
  Triggers: diag consolidation, diagnostic bundles, serverless consolidation report,
  price serverless from diags, per-cluster serverless, ingest gap.
---

# Diagnostic Serverless consolidation

Quote Serverless from diagnostic measurements. Reuse the K360 org report. Do not rerun that summary.

## Run

`<skill>` is the directory that contains this file.

```bash
cd <project with .env>

python <skill>/scripts/run_consolidation.py --org "ORG NAME" \
  --reuse serverless_analyzer/diag_artifacts/<customer>_<date>/<customer>_<date>_org_report.json

python <skill>/scripts/run_consolidation.py --org "ORG NAME" \
  --reuse serverless_analyzer/diag_artifacts/<customer>_<date>/<customer>_<date>_org_report.json \
  --diags-dir serverless_analyzer/diag_artifacts/<org_id>_diags_<date>

python <skill>/scripts/run_consolidation.py --org "ORG NAME" \
  --org-id ID1 --org-id ID2 --org-id ID3
```

`--exclude-deployment` removes a deployment from billing, bulk, store, counts, the plan, and every estimate. Monitoring-named deployments are excluded unless `--keep-monitoring` is set. Do not pass `--skip-diags`, `--skip-sizing`, or `--k360-ingest` unless the user asked for the K360 backup. `--lookback-days` defaults to 90 and cannot exceed 365. `--ech-scope priced` compares only deployments that received a serverless price. `--unmeasured exclude` leaves an active deployment with no bundle off both sides.

## Read the brief

The command prints `brief <path>`. That file is `<run_key>_consolidation_brief.json`. Do not open the account JSON, the org report, or the sheet.

Quote only when `ingest_source` is `diagnostics`.

Read from the brief: `savings`, `quotes`, `fleet`, `contract_rate`, `ech_scope`, `ech_recent_months`, `estimate_months`, `notes`, `limits`, `region_matches`, `per_cluster_plan`, `diagnostics_coverage`, `excluded_deployments`, `method`, `deployment_plan`, `metered_rates`, `estimators`, `links`, `integrity`, `search_power`.

`method.uncommitted_changes` true means the checkout was dirty. Two runs are comparable only when `method` matches.

## Chat output contract

`chat_lines` prints at most six bullets. Say "not verified" when the serverless monthly is missing, any integrity check failed, or the run is a draft. Otherwise give the serverless monthly. Then name every failed check. Then the top three `limits` by severity. Then the Drive and PDF links. Do not add a seventh bullet.

## Gates

Confirm these from the brief. Do not open the sheet.

- Observability is quoted only when `estimators.observability.echo_gate.accepted` is true. The monthly is D38.
- Security is quoted only when E73 is 0 and `readback.other_addons_monthly` is 0. The monthly is E74.
- Search stays on the quote when EIS is incomplete. The cover says Search is quoted without EIS, and inference billed on ECH is context, not a price.
- A rejected region drops that whole solution, including Search.
- `integrity.passed` is false when any scored check fails. Do not call a failed quote verified.
- When the billing window is unstable, the comparison is the latest complete month. Do not call it a 3-month average.
- When no serverless price came back, show the ECH bill and say there is no comparison.
- Page 1 says "Retention measured at N months; the estimate prices 12." The headline stays the 12-month read-back.

## Hard rules

1. Observability and Security ingest are diagnostic bulk, written as metered, and the written month is 12.
2. The estimator read-back is the dollar: Observability D38, Security E74, Search the es3 performant profile.
3. Match the estimator to the solution.
4. Every serverless estimate is a one-year estimate, one sheet per solution per region, with no second fleet-wide sheet.
5. Size the last 90 billed days unless `--lookback-days` says otherwise.
6. One estimate per title; `--fresh` replaces that title only after the new copy matches the expected layout.
7. Read the printed brief only.

## References

Open these only to change a formula or the PDF, not to write a quote.

- Arithmetic → [calculations.md](references/calculations.md)
- Cell map → [sizing-cells.md](references/sizing-cells.md)
- PDF page order → [canvas-layout.md](../k360-account-analysis/references/canvas-layout.md)
- Setup → [setup.md](../k360-account-analysis/references/setup.md)
