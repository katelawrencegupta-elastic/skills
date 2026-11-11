# Diagnostic consolidation arithmetic

`DAYS_PER_MONTH` = 30.416. Every quoted project uses rows from `sizing_from_diags.json` for deployments that billed in the latest lookback month.

## Active deployments

A deployment is active when its billing ECU in the latest lookback month is above zero. The month is named, including when it is partial. Deployments with no bill that month are counted and then left out of the sizing. Their last telemetry snapshot is not added to stored GB and is not converted to GB/day. A sibling that is still active keeps its own current size. The old snapshot is not added on top.

ECU/mo on the sizing row is the last complete month, summed over the active deployments. A partial final month does not shrink that bill.

An active deployment with no bundle adds no ingest. Its current stored GB can stay. A diagnostic bundle whose deployment is inactive is not priced.

## One project, or one pack

```
bulk_gb_day = Σ bulk_gb_day
weighted_days = Σ (bulk_gb_day × retention_days) / bulk_gb_day
months = weighted_days / 30.416
```

The sheet retention is always a whole month. Snap a remainder under 0.001 month down to the integer, round up, then clamp to 1–36. 365 / 30.416 is 12.000263 and writes 12. A real extra day, about 0.033 month, still rounds up. The estimator prices retention only on the integers 1–36, so a fractional month leaves that line at zero. Show both the measured months and the whole month written. Only rows with `retention_days` above 0 enter the weight. A row with no measured age is named and left out. If the rows that have bulk have no measured age, the month stays unset and the sheet is not filled. A solution with no stored GB and no bulk is skipped because it has no workload. That skip is not an unmeasured retention month.

Every consolidation run writes two PDFs from the same bulk.

| PDF | Sheet month | File |
|-----|-------------|------|
| Rounded | measured months, rounded up, clamped to 1–36 | `<key>_full retention_bulk_serverless_consolidation_plan` |
| 30-day | Observability and Security written as 1. Search disk is measured store × (1 / written months), on its own 30-day sheet. | `<key>_30d_bulk_serverless_consolidation_plan` |

The quote window is `--lookback-days`, default 30, maximum 365. ECH monthly ECU is the total of those billed days ÷ the number of days × 30.416. Ingest written to the estimators is diagnostic bulk × (window daily average ÷ complete-month daily average). The sheet read-back is the quote. The cover chart title names that window. When a contract rate exists, ECH is drawn at that rate and serverless stays at list. `savings` remains the list read-back against ECH at the contract rate. A missing daily series for a window of 30 days or less falls back to `last_30d_daily_avg`. The account fetch stays the trailing year. What you pay for today keeps the fetched year as Full lookback, split by tier, and adds the quote-window usage total as its own bar.

Each PDF links to its own estimator copy, left at that month. Bulk estimate sheets live in `generated_estimates/full_retention` or `generated_estimates/30d`. Compression-applied estimate sheets live in `generated_estimates/compression_applied/full_retention` or `generated_estimates/compression_applied/30d`. An estimate already under `generated_estimates` is moved into the folder its name calls for. Deployment-plan sheets live in `generated_estimates/plan_sheets`. Measured-retention PDFs are uploaded to `full_retention`. 30-day PDFs are uploaded to `30d`. The account PDF stays in the customer folder. The rounded sheet is not rewritten to 1 after the 30-day read. If the measured ceiling is already 1, the two read-backs match. Still write both PDFs. The deployment-count bars are priced again at the scenario's month: measured months rounded up on the rounded PDF, and 1 month on every project on the 30-day PDF.

When the user sets a retention month, that whole month replaces the headline (`--retention-months`, G11 or G6). Bulk GB/day stays the measured rate. The measured months stay in the arithmetic next to the month written. The 30-day read stays at 1 month.

`retention_days` is the size-weighted ILM delete age, else data-stream retention, else the oldest index age, kept to 3 decimals. The weight uses local primary bytes only. Searchable-snapshot mounts (`partial-*`) and the snapshot portion of `total_data_set_size_in_bytes` stay out of store and out of that weight. `bulk_gb_day` is the sum of `index.primaries.bulk.est_bytes_per_day` on write indices, divided by 1e9. A write index is the last backing index of a data stream, or an index an alias marks `is_write_index`. Failure-store indices are not write indices. For each of those indices the bytes per day are `primaries.bulk.total_size_in_bytes × 86,400 / write_phase_sec`, truncated to a whole byte. That is `IndexStatsDocument::calculate` in esdiag `indices_stats/processor.rs`. `write_phase_sec` is whole seconds from creation until rollover, or the index age while the current ILM action is `rollover`. A missing or zero write phase contributes 0. Rolled indices are not added. This is the bulk request size over the write window. The indexing rate is the same window over `primaries.store.size_in_bytes` and is not this bulk rate. It is not stored dataset growth and not node `total_size_in_bytes` divided by JVM uptime.

A row with `uptime_days_min` < 2 has a short node uptime. Keep it in the sum and name it. The bulk rate is still the write-phase estimate.

## compressioncalc_accounted_for

This is a second estimate. It does not replace `bulk_gb_day`.

The priced ratio keeps 60% of bulk. On-disk bytes, a per-index cap, and a median do not change it. A missing on-disk rate does not zero the estimate. esdiag `bulk.compression_ratio` is bulk bytes ÷ store bytes, and `bulk.storage_ratio` is store bytes ÷ bulk bytes. Neither one is this 0.60.

```
compression_ratio = 0.60
compressioncalc_accounted_for = bulk_gb_day × 0.60
```

`compression_ratio` is 0.60 whenever a solution has a workload. 105 GB/day of bulk is 63 GB/day. The same write indices as `bulk_gb_day` supply that bulk, including a system write index that store itself leaves out. When no solution has a workload, the ratio is unset and `compressioncalc_accounted_for` is 0.

Observability and Security sheets for this set write that GB/day. Logs stay 90% and metrics 10% of it. Security takes all of it. Retention is weighted on this GB/day. G26 is accepted when it is within 0.51 GB/day of the accounted GB written, not the raw bulk. Search is re-estimated. Its disk is local store × 0.60. Org store is scaled by the same factor, so the ingest share stays put. Query rate does not scale. The 30-day compression sheet also scales that disk by 1 / written months.

The rounded and 30-day PDFs are `<key>_full retention_compression-applied_serverless_consolidation_plan` and `<key>_30d_compression-applied_serverless_consolidation_plan`. The full-retention sheets are in `generated_estimates/compression_applied/full_retention`. The 30-day sheets are in `generated_estimates/compression_applied/30d`. The 30-day sheet writes 1 month. This set does not price the deployment-count plan.

## Which estimator

| Solution | Inputs | Quote cell |
|----------|--------|------------|
| Observability | G21 logs = 0.90 × bulk_gb_day, G23 metrics = 0.10 × bulk_gb_day, G22 0%, G24 0, G25 100%, G11 months, G4 Complete, G5 Premium, G8 region, G14 No, G18 0 | D32 monthly, E32 year 1, F32 steady annual |
| Security | G4 = bulk_gb_day (all of it), G6 months, G7 region, G8 Complete, G21 Premium | E74 monthly |
| Search | disk = Σ local `store_gb`. Ingest docs/s is the K360 org rate × local search store ÷ org store. On one region, query rate stays the org window. A regional split scales query rate by that region's share of search store. es3 on-demand | `monthly_steady_state` |

The 90/10 logs/metrics split is only the Observability sheet. Security takes the full bulk rate on G4.

G26 on the Observability sheet displays as an integer. Read it unformatted. Accept it when it is within 0.51 GB/day of the bulk written to that sheet, including when local store is 0. A wider miss records both numbers and leaves that solution unquoted. The written bulk stays on the solution block.

Security E74 is the monthly quote only when parsed E73 is 0. A blank or unreadable E73 blocks, and so does a non-zero value. A parenthesized credit stays negative. Call that check only after a Security sheet was read. A region that was never sized is a missing sheet, not a blank E73. E73 is not subtracted from E74. Add-on inputs are cleared on the copy. The golden template is not edited.

A regional estimate is written only when that region's projected monthly sizing is more than $10. Observability and Security project from that region's bulk and retention. Search projects that region's share of the solution ballpark. A region at or under $10 is left out and does not count as a missing read-back. A region with bulk and no measured age has no month of its own. If another region supplied the solution month, that region is written at the solution month. A solution with no measured age anywhere is not written. One regional G26 miss, a blank regional E73, or a non-zero regional E73 leaves that whole Observability or Security solution out, including regions whose own read-back was accepted. Regional dollars use one mode. When every region has its own accepted read-back, the headline is the sum of those read-backs. Otherwise the headline stays the one consolidated read-back and every region, including Search, is a stored-GB share of that total. A region's own read-back is not added to another region's share. Year 1 follows the same mode. A region with bulk and no local bytes contributes $0 on a store split.

## Per deployment

A deployment at or above 50 GB/day is its own project. Every deployment under 50 GB/day is one pooled project. 50 GB/day is the top of the first Observability ingest tier, so a deployment below that, priced alone, never leaves the top $/GB band. The pool is priced on its Σ bulk and bulk-weighted retention, which lets the combined ingest step into cheaper tiers.

## Deployment-count plan

The ceiling is one serverless project per ECH deployment, aligned to that deployment's feature. The floor is one project per feature that exists in each region (Observability, Search, or Security). A region and feature with no deployments adds nothing. Each later count is round(previous × 0.75), half-to-even. If that round does not decrease, subtract 1. A result at or under the floor is the floor, and the series stops. Labels are Ceiling, −25% on each middle step, and Floor.

A bar's monthly is the sum of its projects. Search is the on-demand es3 read-back. Observability is the D32 read-back for that group's region and provider. The headline Observability read-back is reused only when the ceiling is already one project. A floor of 1 reached by cutting is priced as that packed group.

Packing stays inside one region and one feature. The ceiling is one group per deployment. At or under the number of region-and-feature pairs, each pair is one group. Between those, extra projects above the floor go to the largest pairs. On the rounded PDF, retention on a project is that group's measured months, rounded up. On the 30-day PDF, every project is written as 1 month and Search disk scales to that month. Bulk does not change. On the rounded PDF, a project with bulk and no measured month omits the whole bar set.

When a contract rate exists, the chart draws serverless at list and that ECH bill on every step. The amount under the project count is the ECH bill minus serverless at list. Serverless is not drawn at the contract rate on this chart, the cover, the monthly build-up, the usage chart, or the search-power appendix.

## Deployment consolidation analysis across metered ingest rates

This is the Observability ingest rate for one deployment priced alone. It is not a D32 quote and it does not include retention or support.

```
metered_gb_day = bulk_gb_day × 1.595
logs = 0.90 × metered_gb_day
metrics = 0.10 × metered_gb_day
rate = (tier(logs) + tier(metrics)) / metered_gb_day
```

`tier` walks these bands. The price is dollars per metered GB. Each stream starts at the top.

| Metered GB/day | $/GB |
|----------------|-----:|
| 0–50 | 0.50 |
| 50–100 | 0.325 |
| 100–200 | 0.225 |
| 200–500 | 0.15 |
| 500–1,000 | 0.105 |
| 1,000–2,000 | 0.10 |
| 2,000–5,000 | 0.095 |
| 5,000+ | 0.0925 |

A deployment whose logs stream and metrics stream are both inside the first 50 GB/day has a rate of $0.50. The consolidation PDF marks that row in red. A deployment with no bulk has no rate. The list is every active deployment, largest metered volume first.

## Search-power projection

On-demand, performant, and high availability are priced with the es3 estimator when Search was estimated. High availability is the highly-performant setting. Show monthly and steady annual (monthly × 12) for each. When Search was not estimated, `search_power` is absent and the Appendix does not include the chart. These dollars stay at estimator list and are not added to D32 or E74. The contract rate is not applied. When the quoted solution is Search, the quote stays the on-demand read-back. The chart, when present, is in Appendix A of serverless_consolidation_analysis_appendix, after the input arithmetic and the metered-rate tables. The quote integrity table follows it. Appendix B follows Appendix A in that same document.

## Quote integrity

The score is the mean of the checks that apply. A check with no inputs is n/a and stays out of the mean. Pass is separate from the score. This is not a serverless bill.

| Check | Score | Pass |
|-------|-------|------|
| Sheet echo | Mean of the G26 component and the E73 component, for each solution that has a workload and a sheet. G26 component = 1 − min(\|G26 − written bulk\| / 0.51, 1). A blank G26 scores 0. E73 scores 1 when the parsed amount is 0, and 0 when it is blank or \|amount\| > 0.005. | G26 is within 0.51 GB/day, and parsed E73 is 0. A gap of exactly 0.51 scores 0 and still passes. A sheet that was never read is left out. |
| Bundle coverage | matched deployments / active deployments. When the active count is absent, the denominator is matched bundles plus bundles that fell back. | Every active deployment has a bundle, and the fallback count is 0. |
| Measured retention | 1 − (bulk with no measured age / Σ bulk) | No unmeasured bulk remains in the weighted month. |
| Direct read-back | solutions on their own read-back / solutions with a workload | Every solution with a workload is quotable from its own read-back. One region counts. `per_region` counts. A store split does not. A rejection does not. |
| Line agreement | 1 − min(\|sum of quote lines − headline\| / headline, 1). Both $0 scores 1. | The lines and the headline agree to the cent. |
| Year agreement | For each quote line, 1 − min(\|steady annual − 12 × unrounded monthly\| / \|steady annual\|, 1), then the mean. Both sides are rounded to cents first. A line with no steady annual uses its year-1 figure. | The steady annual equals 12 × the unrounded monthly, to the cent. When the sheet is written as 1 month, year 1 also equals that steady annual, to the cent. A longer keep may ramp, so year 1 can sit below the steady annual. |

The detail prints both cents and the gap. A missing monthly or a missing steady annual on a quote line scores 0 for that line. The overall result passes when the score is 0.90 or higher, and also when every scored check passes. A check below its own bar stays flagged and its detail still names the gap. Bundle coverage below 1.0 names each deployment that has no bundle.

Same formulas inside each project. Each project is its own estimator copy. The month written is the project's measured months rounded up. Observability and Security projects also get a 30-day read at 1 month on the same bulk. Search does not. Its only price is the full-retention read-back, and the 30-day total keeps that figure. Sum of the read-backs is the split-fleet price, once at the measured month and once at 30 days. Compare each sum with the matching one-project read-back. The difference is the cost of staying split. A user-set retention month replaces the headline month on every project, including the pool, and does not change Σ bulk.

## 2–5 projects

1. Sort rows by `store_gb` descending.
2. Place each row into the pack with the smallest `store_gb` so far.
3. Price each pack with the formulas above, not with `store_gb / (3 × 30.416)`.

State the pack's stored GB only as a label for what was grouped. The ingest input is the pack's Σ bulk.

## Gap against K360

K360 stored GB ÷ assumed retention is a backup, used when the user asks for the comparison or when `--k360-ingest` was passed. It is not a second diag quote.

Split the dollar gap into:

- **Ingest.** Same retention, the two daily rates. Diagnostic bulk versus stored GB ÷ retention (or versus cloud-tracker dataset growth, when that was the other tab).
- **Retention.** Same daily rate, diagnostic weighted days versus the flat assumed months.

Most of a large gap is the ingest measure. Bulk request bytes over a node's uptime run well above the growth of `index.dataset_size_bytes`. Stored size on the two sides often agrees; that does not make the daily rates interchangeable.

Do not count `partial-*` (searchable snapshots) as ingest. Do count `.ds-*` data streams. Exclude `.ds-.` and `.ds-ilm`.

## What not to quote

- Stored GB ÷ (3 × 30.416) labeled as a diagnostic price.
- A ladder result from `price_o11y_serverless` in place of D32 or E74.
- A pack whose GB came from `data_pivot` or the March telemetry snapshot while the label says diagnostic.
- One blended monthly number that adds the diag quote and the K360 backup.

## Appendix B

serverless_consolidation_analysis_appendix prints these formulas as Appendix B, on its own page after Appendix A. The numbers are the full-retention bulk quote. Each row is the value, the formula, the inputs used on that run, and the result already in the report. A row is left out when that value was not calculated. D32, E74, and the es3 on-demand total stay read-backs. The page does not reprice them.
