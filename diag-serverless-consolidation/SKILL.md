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
  days), a deployment-count plan at 0/25/50/75/100%, the
  blended metered ingest rate for each active deployment, with $0.50 rows
  marked in red, a search-power chart for on-demand, performant, and high
  availability that is not added to the quote, 2–5 project packs,
  and a gap against the K360 stored-GB method.
  Does not use stored GB ÷ assumed retention as the quoted ingest. Triggers:
  diag consolidation, diagnostic bundles, serverless consolidation report,
  price serverless from diags, per-cluster serverless, 2-5 projects, ingest gap.
---

# Diagnostic Serverless consolidation

Quote Serverless from diagnostic measurements. The K360 account summary is a different skill (`k360-account-analysis`) and is not rerun here except to reuse its org report.

## Hard rules
1. **Observability and Security ingest are diagnostic bulk.** Daily GB is `bulk_gb_day`: the sum of `index.primaries.bulk.est_bytes_per_day` on write indices, in decimal GB. Retention is that row's `retention_days`, and only a value above 0 enters the weight. A row with no measured age is named and left out. A solution with bulk and no measured age does not get a sheet. Stored GB, data_pivot growth, stored GB ÷ 3 months, and node `total_size_in_bytes` ÷ JVM uptime are not this ingest. Search disk is local `size_in_bytes`. `partial-*` and the snapshot portion of `total_data_set_size_in_bytes` stay out of store and out of the retention weight. Search ingest docs/s is the K360 org rate times local search store ÷ org store. On one region, query rate stays the org window. When Search is split by region, query rate is scaled by that region's share of search store. Say so when quoting Search. Do not replace Search ingest with `bulk_gb_day`.
2. **The estimator read-back is the dollar.** Observability monthly is `D32`. Security monthly is `E74` (month-12 total). Search is the es3-estimator on-demand profile. The ladder in `consolidation.py` is a check, not the quote. Never edit a golden template.
3. **Match the estimator to the solution.** Observability clusters go on the Observability sheet. Security on the Security sheet. Search on es3. Do not price an Observability fleet with the Security or Search estimator.
4. **Show the arithmetic** for every quoted project: cluster count, Σ bulk, Σ(bulk × days), weighted days, months written to the sheet, the cells written, and the read-back. The formula is in [calculations.md](references/calculations.md). The month written is always a whole number: round the measured months up, then clamp to 1–36. Never write a fraction. Every consolidation run writes two PDFs from the same bulk. The rounded PDF uses that whole month. The 30-day PDF writes 1 month, because 30 / 30.416 is under 1. Each PDF links to a sheet left at that scenario's month.
5. **Size the active month only.** A deployment is in the quote when it has billing ECU in the latest month of the lookback. That month can be partial. The sizing count, stored GB, and GB/day include only those deployments. A deployment with no bill that month is out. Its last telemetry snapshot is not added to stored GB and is not turned into GB/day. When that data now reports on an active sibling, the sibling's own row is the size. A diagnostic bundle for an inactive deployment is named and not priced. An active deployment with no bundle is named and adds no ingest. ECU/mo on the sizing row is the last complete month for the active deployments. `short_uptime` (JVM uptime under 2 days) is named. The bulk rate still comes from the index write phase, not that uptime.
6. Read `*_brief.json` for the pipeline quote. Do not open `*_account.json`, `*_diags_account.json`, the org report, or infra. `sizing_from_diags.json` is the allowed diagnostic table. Sheets work goes through `size_serverless.py`, not the browser.

## Run the quoted consolidation
```bash
SCR=~/.cursor/skills/k360-account-analysis/scripts
cd <project with .env>          # KIBANA_URL, ELASTIC_API_KEY, FOUND_ADMIN_API_KEY

# Reuse the account-summary fetch when that folder already exists.
python $SCR/run_account_analysis.py --org "ORG NAME" \
  --reuse artifacts/<customer>_<date>/<customer>_<date>_org_report.json

# Bundles already on disk. Does not call the admin API.
python $SCR/run_account_analysis.py --org "ORG NAME" \
  --reuse artifacts/<customer>_<date>/<customer>_<date>_org_report.json \
  --diags-dir artifacts/<org_id>_diags_<date>
```
Do not pass `--skip-diags`, `--skip-sizing`, or `--k360-ingest` unless the user asked for the K360 backup as a separate document. Diagnostics are the quote. The Aviatrix VPN tunnel must be up for a fresh admin pull (`openvpn` process). `--skip-vpn-check` pulls anyway.

Pipeline after the reused fetch: `admin_diags.py` + `diag_account.py` → `size_serverless.py` → `consolidation.py` → consolidation PDF. ECH ECU and the contract rate stay on the K360 bill. Serverless stays at estimator list.

## After the script
Read `*_brief.json`. Quote only when `ingest_source` is `diagnostics`. Build `<slug>-serverless-consolidation-savings.canvas.tsx` from `savings`, `quotes`, `fleet`, `contract_rate`, `notes`, and `diagnostics_coverage`. Page order is the account skill's [canvas-layout.md](../k360-account-analysis/references/canvas-layout.md) §11. Chat is the consolidation bullets plus estimator links (≤6).

Confirm the sheet inputs before quoting:
- Observability: G4 `Complete`, G5 `Premium`, G8 the account region, G11 = measured months rounded up, G21 = 90% of Σ bulk, G23 = 10%, G14 `No`. Dollar = D32 only when unformatted G26 is within 0.51 GB/day of the bulk written. A wider miss leaves Observability out. The written bulk stays on the solution block.
- Security: G4 = Σ bulk, G6 = measured months rounded up, G8 `Complete`, G21 `Premium`. Add-ons are cleared on the copy. Dollar = E74 only when parsed E73 is 0. A non-zero E73, including a parenthesized credit, leaves Security out. Do not subtract E73 from E74.
- 30-day PDF: same ingest cells, G11 or G6 = 1, on its own `_30d` sheet copy. Leave that copy at 1 month. The rounded PDF's sheet stays at the rounded measured month. Do not point either PDF at the other scenario's sheet. Search is not repriced for the 30-day PDF.
- Search: disk = Σ local `store_gb`. Ingest docs/s = K360 org docs/s × local search store ÷ org store. On one region, query rate is the org window. Dollar = on-demand `monthly_steady_state`. Performant and high availability stay on the search-power chart and are not added to the total.

If `diagnostics_coverage.inactive_excluded` is above zero, say how many deployments had no bill in the active month and how many GB of leftover snapshot were left out. If `snapshot_gb` is above zero, say how many searchable-snapshot GB were left out of store. If `k360_fallback` is above zero, those are active deployments with no bundle. They add no ingest. If `retention_unmeasured` is non-empty, name those deployments and leave them out of the weighted month. A regional G26 miss or a non-zero regional E73 leaves that whole solution out, including regions that read back. Until the per-region sheets succeed, a multi-region total is a split of the one sheet by local store, so a region with bulk and no local bytes contributes $0.

## Deployment-count plan

When the quote is Observability from diagnostics, page 1 of the consolidation PDF has a bar for cutting the active deployment count by 0, 25, 50, 75, and 100%.

- 0% is one project per active Observability deployment. A deployment with no bulk is $0 and does not open a sheet.
- 25, 50, and 75% keep `round(active × (1 − pct/100))` projects. Python rounds halves to even, so 75% of 2 deployments is 0 projects. Pack by local `store_gb`, largest into the lightest bin. Price on each group's Σ bulk.
- 100% is one project. Do not round that count to zero. It does not open a sheet. The bar reuses the headline Observability read-back. After a regional pass that headline is one sheet per region.
- The 0/25/50/75 bars are sums of D32. On the rounded PDF, retention for each project is that group's measured months, rounded up. A project with bulk and no measured month omits the whole rounded bar set. On the 30-day PDF, every project with bulk is written as 1 month, including a project with no measured age.
- `--skip-deployment-plan` skips the bars. The headline quote still runs. A failed sheet pass leaves the bars off and puts that on the PDF note.

## Metered ingest rate

The consolidation PDF lists every active deployment's raw GB/day, metered GB/day, and blended ingest rate. The formula and the band prices are in [calculations.md](references/calculations.md). The rate is what that deployment pays per metered GB when it is its own project. It is not D32. D32 stays the monthly quote. A deployment with no bulk is listed and has no rate. A deployment at $0.50 per metered GB is marked in red. That is a deployment whose logs stream and metrics stream both sit in the first 50 metered GB/day.

## Search-power projection

The consolidation PDF charts three search-power settings on the sized stored GB and the measured query rate: on-demand, performant, and high availability. High availability is the high-demand setting. The dollars come from the es3 estimator. They are not added to Observability D32 or Security E74. When Search is the solution being quoted, on-demand remains the quote and the other two settings stay on this chart.

## Further analysis (only when asked)
Per-deployment prices, a 2–5 project pack, or a gap against K360 stored-GB pricing. Inputs come from `diag_inputs.py`, then each group is written to a **copy** of the matching estimator and read back. Restore the consolidated cells afterward if you borrowed the working copy.

```bash
python ~/.cursor/skills/diag-serverless-consolidation/scripts/diag_inputs.py \
  --diags-json artifacts/<org_id>_diags_<date>/sizing_from_diags.json \
  --projects 1,2,3,4,5 --each
```

Packs are greedy on diagnostic `store_gb` (largest into the lightest bin). The price still uses Σ `bulk_gb_day` and the bulk-weighted retention, not the pack's stored GB. Even packs are the expensive way to use N projects. Show each project's arithmetic next to its read-back.

`--each` prices the active deployments. A bundle for a deployment with no bill in the latest month stays out. A deployment at or above 50 GB/day stays its own project. Every deployment under 50 GB/day is pooled into one project and priced on that pool's Σ bulk, so the combined ingest can leave the top ingest tier. Do not price those small deployments one by one.

Each of those projects gets its own copy of the matching estimator. Do not borrow the single consolidated sheet. Observability cells are G4 `Complete`, G5 `Premium`, G8 the account region, G11 the month written, G14 `No`, G21 = 90% of that project's bulk, G23 = 10%. Accept the sheet when G26 is within 0.51 GB/day of that project's bulk. The split-fleet dollar is the sum of the D32 read-backs. When that is the quote the user asked to publish, the consolidation PDF headline is that sum. Deployments with no bundle stay out of it.

When the user sets a retention month, pass `--retention-months N` and write that whole number on every project. Bulk stays measured. Show the measured months beside N. The 30-day read stays at 1 month either way. Reread every D32 and rebuild the PDF. A project whose written month was already N keeps the same read-back.

The K360 backup (stored GB ÷ assumed retention) is a different quantity. Compare it only in a gap section: same clusters, ingest gap and retention gap stated separately. Do not blend the two into one quote. K360 has no raw ingest total. `partial-*` index growth is a searchable-snapshot mount, not ingest.

## References
- Arithmetic, cells, and the gap split → [calculations.md](references/calculations.md)
- Full cell map → [sizing-cells.md](../k360-account-analysis/references/sizing-cells.md)
- Setup (VPN, admin key, Sheets venv) → [setup.md](../k360-account-analysis/references/setup.md)
