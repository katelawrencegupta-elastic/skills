# Diagnostic consolidation arithmetic

`DAYS_PER_MONTH` = 30.416. Every quoted project uses rows from `sizing_from_diags.json` for deployments that billed in the quote window, the last 90 billed days by default.

## Active deployments

A deployment is on the quote when its billing ECU in the quote window is above zero. The window is `--lookback-days`, 90 by default. The window ends on the last day with a positive total. Trailing days with no bill are left out. A partial calendar month is not the window. Deployments with no bill in that window are counted and then left out of the sizing. Their last telemetry snapshot is not added to stored GB and is not converted to GB/day. A sibling that billed in the window keeps its own current size. The old snapshot is not added on top.

ECU/mo on the sizing row is the last complete month, summed over that roster. An open month does not shrink that row. The cover ECU is that same window, expressed as a month.

A fresh fetch saves the exact roster for 30 and for 90 days (`chart.billed_windows`): every deployment with a bill on any day in the window. An org report fetched before that carries only the 30-day roster. For any other window the roster is rebuilt: a deployment with its own daily series counts when that series is above zero in the window, and any other deployment counts when a calendar month the window touches has ECU. That rebuild can include a deployment whose last bill was earlier in the first month. Fetch again for the exact set.

An active deployment with no bundle is not sized (`--unmeasured exclude`, the default): no ingest, no stored GB, and its K360 size is kept only as `k360_gb`. A decommissioned deployment, billed in the window and gone from the admin console, is treated the same way under every mode. A diagnostic bundle whose deployment is inactive is not priced.

`--unmeasured k360` prices a no-bundle deployment from K360 instead and labels it `k360_fallback`. Observability and Security take `stored GB ÷ (--retention-months × 30.416) × expansion`, where the expansion is 1.595 for Observability and 1.66 for Security: stored GB ÷ retention is a raw rate, and bulk is written as metered. Its retention is that assumed month. Search keeps its K360 stored GB as disk. It still counts as a deployment without a bundle, so Bundle coverage stays flagged. `--unmeasured fail` stops the run and names every active deployment with no bundle.

## ECH comparison scope

K360 bills the org by day and each deployment by month. A deployment's part of a day is its share of that calendar month:

```
part(dep, day)   = org_cost(day) × dep_cost(month of day) ÷ Σ org_cost(days in that month)
window(dep)      = Σ part(dep, day) over the quote window
as a month       = window ÷ window_days × 30.416
```

Four parts add up to the window (`ech_scope` on the consolidation):

| Part | What it is |
|------|------------|
| `priced_mo` | ECH deployments that got a serverless price |
| `unpriced_mo` | ECH deployments on the roster that did not, each named in `unpriced` with a reason |
| `serverless_mo` | existing serverless projects |
| `other_mo` | lines with no deployment name, and deployments off the roster |

A deployment is priced when its solution has an accepted read-back and it was measured: a matched bundle with bulk above zero for Observability and Security, stored GB above zero for Search, or a `k360_fallback` row. The reasons are: no diagnostic bundle; decommissioned; the solution has no accepted estimator read-back; its bundle measured no bulk; no stored GB to price.

`--ech-scope priced`, the default, sets ECH to `priced_mo`. `--ech-scope all` sets it to the whole window less `serverless_mo`, or the whole window with `--keep-serverless-spend`. `savings.ech_list_mo` is that figure and `basis.ech_window_ecu` is the whole window. The usage rows under "What if data is kept longer, or the fleet is busier" scale the scoped figure by whole-org month ratios. Each row of "Where ECH spend sits" is that deployment's own part of the window (`ech_scope.by_deployment_mo`), and its fixed overhead is scaled to the same part. EIS on a Search project counts only deployments that were sized. When the month shares add up to more than the window, the three named parts are scaled down together so they never exceed it. An account with no per-deployment months (written before they were saved) has no `ech_scope`, and the whole window is compared.

## One project, or one pack

```
bulk_gb_day = Σ bulk_gb_day
weighted_days = Σ (bulk_gb_day × retention_days) / bulk_gb_day
months = weighted_days / 30.416
```

The sheet retention is always a whole month. Snap a remainder under 0.001 month down to the integer, round up, then clamp to 1–36. 365 / 30.416 is 12.000263 and writes 12. A real extra day, about 0.033 month, still rounds up. The estimator prices retention only on the integers 1–36, so a fractional month leaves that line at zero. Show both the measured months and the whole month written. Only rows with `retention_days` above 0 enter the weight. A row with no measured age is named and left out. If the rows that have bulk have no measured age, the month stays unset and the sheet is not filled. A solution with no stored GB and no bulk is skipped because it has no workload. That skip is not an unmeasured retention month.

Every consolidation run writes one PDF: `<key>_full retention_bulk_serverless_consolidation_plan`, with each sheet at its measured months, rounded up and clamped to 1–36. There is no 30-day read. The earlier second PDF wrote Observability and Security as 1 month and divided Search disk by the written month; a Search fleet's bulk-weighted month could swing from 1 to 36 on one cluster, which moved that Search price many times over, so the read was removed. Search disk is the measured local store on every sheet and every plan bar.

The quote window is `--lookback-days`, default 90, maximum 365. ECH monthly ECU is the total of those billed days ÷ the number of days × 30.416. Billed days run from the org's first positive day in the fetch to its last: days before the first bill and days after the last one are not billed days. An org with fewer billed days than the window asks for is priced on the days it has, and the roster follows the same days. `ecu.quote_lookback_requested` is what was asked and `ecu.quote_lookback_days` is what was priced. With no daily series there is no window: `quote_lookback_days` is null, ECH is the complete-month average, and there is no `ech_scope`. The consolidation run stops on an org report with no daily series (one fetched at week grain by `run_org_report.py`).

ECH dollars are the K360 billing `cost` field, summed as billed. Each billing row carries `quantity`, `rate`, and `cost`, and `cost` is the ECU for that row, so the sum is the ECU total and 1 ECU is $1. No ARR ÷ (commit ECU/day × 365) rate is applied to it. That implied rate came out above $1 per ECU on real accounts and put the ECH bill above the ARR itself. ARR and commit stay on `contract_rate` for reference, with `implied_usd_per_ecu` null and `basis` `k360_cost`. `savings` is `ech_list_mo` (ECH billed cost for the month), `serverless_mo`, and `list_mo` = `ech_list_mo` − `serverless_mo`, with `list_yr` and `list_pct`. A negative `list_mo` is an increase. Ingest written to the estimators is diagnostic bulk as measured. Bulk is a rate at capture, so the billing window does not scale it: `ecu.quote_ingest_factor` is 1.0. `ecu.quote_usage_factor`, window daily average ÷ complete-month daily average, scales ECH ECU only. On the K360 backup, stored-GB ingest is × that factor, and the backup is priced on the same window and the same comparison scope. Search docs/s is a K360 rate: org docs/s × `ecu.quote_usage_factor` (both runs), then × local search store ÷ org store. Org store there is `storage.ingest_share_store_gb`: the sized store plus the K360 size of every deployment left unsized, because the org rate includes their ingest. The sheet read-back is the quote. The cover chart title names that window. ECH is drawn as billed cost and serverless at list. A missing daily series for a window of 30 days or less falls back to `last_30d_daily_avg`. The account fetch stays the trailing year. What you pay for today keeps the fetched year as Full lookback, split by tier, and adds the quote-window usage total as its own bar.

The PDF links to its estimator copies, left at that month. Estimate sheets live directly in `generated_estimates`. Deployment-plan sheets live in `generated_estimates/plan_sheets`. The consolidation PDF is uploaded to the customer folder root, beside the account PDF and the appendix. There are no `full_retention` or `30d` folders. An estimate or a consolidation PDF that an earlier run filed under a `full_retention` or `30d` folder is moved up, on disk and on Drive, and the folder is removed once it is empty. The deployment-count bars are priced at each project's measured months, rounded up.

When the user sets a retention month, that whole month replaces the headline (`--retention-months`, G11 or G6). Bulk GB/day stays the measured rate. The measured months stay in the arithmetic next to the month written.

`retention_days` is the size-weighted ILM delete age, else data-stream retention, else the oldest index age, kept to 3 decimals. The weight uses local primary bytes only. Searchable-snapshot mounts (`partial-*`) and the snapshot portion of `total_data_set_size_in_bytes` stay out of store and out of that weight. `bulk_gb_day` is the sum of `index.primaries.bulk.est_bytes_per_day` on write indices, divided by 1e9. A write index is the last backing index of a data stream, or an index an alias marks `is_write_index`. Failure-store indices are not write indices. For each of those indices the bytes per day are `primaries.bulk.total_size_in_bytes × 86,400 / write_phase_sec`, truncated to a whole byte. That is `IndexStatsDocument::calculate` in esdiag `indices_stats/processor.rs`. `write_phase_sec` is whole seconds from creation until rollover, or the index age while the current ILM action is `rollover`. A missing or zero write phase contributes 0. A write index in its first day is rated together with the generations before it, Σ bulk bytes × 86,400 ÷ Σ write-phase seconds, until the window reaches a day. An older generation is used only while ILM has it in the hot phase and its bulk-to-store ratio is at least half the write index's, because a moved shard has lost its counter. Otherwise rolled indices are not added. A data stream with no ILM dates takes the write index age as its write phase. Dated indices written by name (`app-2026.11.12`) with no data stream or write alias are rated on the newest index over its age, when that index is at most 32 days old. `bulk_fallback_gb_day` is the bulk from those two cases. `bulk_short_window_gb_day` is the bulk still rated over less than a day. This is the bulk request size over the write window. The indexing rate is the same window over `primaries.store.size_in_bytes` and is not this bulk rate. It is not stored dataset growth and not node `total_size_in_bytes` divided by JVM uptime.

A row with `uptime_days_min` < 2 has a short node uptime. Keep it in the sum and name it. The bulk rate is still the write-phase estimate.

## Measured split

Each rated series is one kind, from its data stream or index name: `metrics-*`, `metricbeat-*`, and any index whose `index.mode` is `time_series` are metrics; `traces-*` are traces; everything else is logs; `logs-kubernetes.*` is the Kubernetes part of logs. The bundle row carries `bulk_mix` (`logs`, `k8s_logs`, `metrics`, `traces`, in GB/day; logs + metrics + traces = `bulk_gb_day`) and `retention_days_metrics`, the size-weighted delete age of the metrics indices.

A group's split is the sum of its deployments' splits, each scaled to that deployment's written GB/day, so the proportions are kept. On the metered basis the Observability sheet takes logs plus traces on G19 and metrics on G20 (see Metered basis). On the raw basis it takes logs plus traces on G23, the Kubernetes share of logs on G24, and metrics on G25. G12 is the metrics month, rounded up like G11. The fleet sheet, each regional sheet, and each deployment-plan sheet use the split of the deployments in that sheet. A row parsed before the split existed is 90% logs and 10% metrics, and `mix_basis` on the solution block says `measured`, `assumed 90/10`, or `mixed`.

## Retention with no delete policy

An index is aged by its ILM delete phase, else its data stream's retention. An index covered by neither is taken at its own age (the bundle's capture time minus its creation time) and joins the size-weighted average at that age. Before this, that store was left out of the average, and when no index had a policy the single oldest index set the month, however small it was. `retention_known_share` on a bundle row is the share of local store whose age a policy or data stream sets. `retention_basis` is `policy` (all of it), `blended` (some of it at its own age) or `observed` (none of it). The blended age is how long that data has been kept so far, not how long it will be kept, so it is a floor for it. When more than 10% of the sized store is blended, the notes say so (`diagnostics.retention_blended_share`).

## Bulk rate after a restart

The bulk counter starts at zero when a primary shard starts on a node, and the write phase runs from the index's creation, so a shard that restarted or moved has counted fewer bytes than the write phase's length implies. `analyze_bundle` reads each index's latest primary recovery start from `recovery.json`. Where the counter is younger than 90% of the write phase, it adds what the rate would be if the same bytes were spread over the counter's own age: `bulk_restart_gb_day` on the row and `diagnostics.bulk_restart_gb_day` on the quote. The quoted `bulk_gb_day` stays esdiag's figure. The notes say so when the extra is more than 5% of the quoted bulk. Replica copies count bytes too, so this is an estimate of the size of the effect and not a corrected rate.

## Unrated ingest

Bulk GB/day is summed over write indices that have a write phase. An index outside that sum is listed when its primary `bulk.total_size_in_bytes` is above 0, so it took bulk writes since its shards started, and it is a user index. Two kinds are listed:

- A write index with no write phase that no fallback rates: an alias write index that ILM does not manage.
- An index that is not in a data stream, has no alias with a write index, was never rolled over by ILM (`lifecycle_date_millis` is not after `index_creation_date_millis`), and is not in a dated family that was rated. A long-lived index written by name is the usual case: its bulk counter started when its shard last started, and that time is not in the bundle.

A rolled generation is not listed, because the current write index rates its series. System indices and `partial-*` mounts are not listed.

Each bundle row carries `unrated_indices`, `unrated_store_gb`, `unrated_bulk_gb`, and the five largest in `unrated_top`. `unrated_bulk_gb` is the bulk counter since shard start. It is a volume, not a rate, and it is not added to `bulk_gb_day`. `sizing.diagnostics.unrated` sums the matched active deployments and gives `share_of_store`. The quote does not change. The figure says how much write activity the rate did not see.

## Metered basis

`bulk_gb_day` is `primaries.bulk.total_size_in_bytes` over time: the size of the bulk requests as they reach the primary shards. Ingest pipelines have already run, and nothing is compressed yet. The estimators define metered ingest the same way ("uncompressed, normalized, fully-enriched data size, after ingest pipelines are run"), and they turn a raw GB/day into metered GB/day by multiplying logs and SIEM data by 1.66. Writing bulk into the raw-data cells applied that factor to data that already carried it.

So a diagnostic run writes bulk as metered (`ingest_basis` `metered` on each estimator entry):

```
Observability  G19 = (logs + traces GB/day) × 730 ÷ 24     monthly GB, everything except TSDS metrics
               G20 = metrics GB/day × 730 ÷ 24              monthly GB, TSDS metrics
               G23, G25, G26 = 0 and G24 = 0%               Option 2 is off
Security       G4  = bulk GB/day ÷ expansion                expansion = G5 ÷ G4 on the copy, 1.66 on v2.6
```

The sheet's ladders take `G19 ÷ 730 × 24` and `G20 ÷ 730 × 24`, so they price the measured GB/day with no expansion. Kubernetes logs need no separate share on this basis, because their higher factor is an expansion too. The echo that gates the quote is the GB/day the sheet priced: `Estimator_Reference!C2` + `Estimator_Reference-Metrics!C2` for Observability and G5 for Security, each within the tolerance, 0.1% of the bulk written to that sheet and at least 0.02 GB/day. A Security miss clears the monthly, so the sheet is not quoted.

The K360 backup (`--k360-ingest`) has no measured rate. It keeps Option 2 and G4 as raw GB/day, and its echo is G28. The ladder that decides the $10 regional line and the metered-rate table use a factor of 1.0 on diagnostic bulk and 1.595 on a raw rate (`consolidation.metered_expansion`).

Bulk already carries the metadata overhead: the shipper's fields and the pipeline enrichment are in the bulk request by the time it reaches the shard. That is the overhead the sheets' expansion factor adds to a raw rate, which is why no factor is applied here (confirmed by the skill owner, 2026-11-12). A useful spot check remains: for a customer already on Serverless, billed ingest GB/day should sit close to the bulk figure from a diagnostic of the same workload.

## Compression-applied set (retired)

The run no longer produces a compression-applied set (`RUN_COMPRESSION_SET` is False in `run_consolidation.py`). It wrote bulk × 0.60 into the raw-data cells, where the sheets multiply by 1.66. 0.60 × 1.66 is 0.996, so it was the metered quote within 0.4% under another name. Bulk now goes into the metered cells directly. `compressioncalc_accounted_for` is absent from the brief, and no `compression_applied` folders or `compression-applied` file names are written. No 0.60 constant remains in the quote or the report.

## Which estimator

| Solution | Inputs | Quote cell |
|----------|--------|------------|
| Observability | G19 = (logs + traces) × 730 ÷ 24, G20 = metrics × 730 ÷ 24, G23 0, G24 0%, G25 0, G26 0, G27 100%, G11 months, G12 metrics months, G4 Complete, G5 Premium, G8 region, G15 No, G33 No. The split is the measured one, or 90% logs and 10% metrics when the bundle rows carry none | D38 monthly, E38 year 1, F38 steady annual |
| Security | G4 = bulk_gb_day ÷ the sheet's expansion (all of it), G6 months, G7 region, G8 Complete, G21 Premium | E74 monthly, or month 36 above 12 months |
| Search | disk = Σ local `store_gb`. Ingest docs/s is the K360 org rate × local search store ÷ org store. On one region, query rate stays the org window. A regional split scales query rate by that region's share of search store. es3 performant | `monthly_steady_state` |

The 90/10 logs/metrics split is only the Observability sheet. Security takes the full bulk rate on G4.

The echo is the GB/day the sheet priced (see Metered basis). Read it unformatted. Accept it when it is within the tolerance, 0.1% of the bulk written to that sheet and at least 0.02 GB/day, including when local store is 0. The earlier flat 0.51 GB/day could never fail a sheet under 0.51 GB/day and let a 5% error through at 10 GB/day. A wider miss records both numbers and leaves that solution unquoted. The written bulk stays on the solution block.

Security E74 is the month-12 statement. It is the monthly quote up to 12 months of retention. Above 12 months the sheet's retained GB is still filling at month 12, so the quote is month 36 on the reference tab (`Estimator_Reference!CB254`) and the steady annual is 12 × that month. If month 36 cannot be read above 12 months, Security is not quoted. E74 also sums the endpoint, cloud, workflow, LLM, and SOAR lines: E74 − E61 − E62 − E69 − E73 must be 0, and a non-zero amount blocks the quote like a non-zero E73. Either figure is the quote only when parsed E73 is 0. A blank or unreadable E73 blocks, and so does a non-zero value. A parenthesized credit stays negative. Call that check only after a Security sheet was read. A region that was never sized is a missing sheet, not a blank E73. E73 is not subtracted from E74. Add-on inputs are cleared on the copy. The golden template is not edited.

A regional Observability estimate is written only when the Observability ladder on that region's bulk and retention is more than $10. Search is written only when that region's share of the solution ballpark is more than $10. Security is written whenever the region has bulk and a month, and it stays in the quote only when E74 is more than $10. A region at or under $10 is left out and does not count as a missing read-back. A region with bulk and no measured age has no month of its own. If another region supplied the solution month, that region is written at the solution month. A solution with no measured age anywhere is not written. One regional echo miss, a blank regional E73, or a non-zero regional E73 leaves that whole Observability or Security solution out, including regions whose own read-back was accepted. Regional dollars use one mode. When every included region has its own accepted read-back, the headline is the sum of those read-backs. A missing read-back leaves that solution out. A stored-GB split is used only when the fleet total is already a quotable read-back. A region's own read-back is not added to another region's share. Year 1 follows the same mode. A region with bulk and no local bytes contributes $0 on a store split.

## Per deployment

A deployment at or above 50 GB/day is its own project. Every deployment under 50 GB/day is one pooled project. 50 GB/day is the top of the first Observability ingest tier, so a deployment below that, priced alone, never leaves the top $/GB band. The pool is priced on its Σ bulk and bulk-weighted retention, which lets the combined ingest step into cheaper tiers.

## Deployment-count plan

The ceiling is the cover count: one serverless project per deployment that billed in the quote window, aligned to that deployment's feature. The cover count and the ceiling are the same deployments. The floor is one project per feature that exists in each region (Observability, Search, or Security). A region and feature with no deployments adds nothing. Each later count is round(previous × 0.75), half-to-even. If that round does not decrease, subtract 1. A result at or under the floor is the floor, and the series stops. Labels are Ceiling, −25% on each middle step, and Floor.

A bar's monthly is the sum of its projects. Search is the performant es3 read-back. A Search project with no performant total leaves the bars unpriced. Search disk on a bar is that project's measured store, so a project's disk does not change when it merges into another. The es3 model is not additive, so a merged Search project can still price above the projects it replaced, and a bar can step up. The floor differs from the headline by Search EIS and by any region left out under $10. Observability is the D38 read-back for that group's region and provider. Security is the E74 read-back, and only when E73 is 0. The headline Observability read-back is reused only when the ceiling is already one project, and that bar is labeled Ceiling. A floor of 1 reached by cutting is priced as that packed group.

Packing stays inside one region and one feature. The ceiling is one group per deployment. At or under the number of region-and-feature pairs, each pair is one group. Between those, extra projects above the floor go to the largest pairs. Retention on a project is that group's measured months, rounded up. A project with bulk and no measured month omits the whole bar set.

The chart draws serverless at list and the ECH billed cost on every step. The amount under the project count is the ECH bill minus serverless at list. No contract rate is applied to either side on this chart, the cover, the monthly build-up, the usage chart, or the search-power appendix.

## Raw bulk ingest by deployment

Own page in the consolidation PDF, after "Where ECH spend sits". It lists every sized deployment's raw bulk GB/day and the arithmetic from bundle to estimator input. Appendix B repeats the formulas under "Raw bulk ingest". It is not a quote. The estimator read-back stays the dollar.

```
bytes_per_day(index) = primaries.bulk.total_size_in_bytes × 86,400 ÷ write_phase_sec   (whole bytes)
bundle_gb_day(dep)   = Σ write indices bytes_per_day ÷ 1e9
quoted_gb_day(dep)   = bundle_gb_day                          (ecu.quote_ingest_factor = 1.0; only the K360 backup is × ecu.quote_usage_factor)
fleet                = Σ deployments, by solution; a deployment with no bundle adds 0
share(dep)           = quoted_gb_day ÷ fleet quoted
logs / metrics / traces = the measured split of Observability bulk (0.90 / 0.10 / 0 when no split was measured)
metered_gb_day       = bulk                                   (× 1.595 only on a raw K360 rate)
days_in_store(dep)   = stored_gb ÷ bundle_gb_day              (indicative: stored GB is compressed, bulk is not)
```

`write_phase_sec` is creation until rollover, or the index age while the ILM action is rollover. Write indices are the last index of each data stream and any alias marked `is_write_index`. A missing or zero write phase is 0. Days in store compares the local primary store with ingest: a value far below retention days means retained data sits outside that store (searchable snapshots) or the stored-GB snapshot is older than the bundle. A node up under 2 days measures the rate over a short window, so that row is shaded.

## Deployment consolidation analysis across metered ingest rates

This is the Observability ingest rate for one deployment priced alone. It is a band walk. Diagnostic bulk is metered, so the walk applies no expansion. A raw K360 rate is × 1.595 first. It is not a D38 quote and it does not include retention or support.

```
metered_gb_day = bulk_gb_day            (× 1.595 only on a raw K360 rate)
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

On-demand, performant, and high availability are priced with the es3 estimator when Search was estimated. High availability is the notebook's third search-power band. Show monthly and steady annual (monthly × 12) for each. When Search was not estimated, `search_power` is absent and the Appendix does not include the chart. These dollars stay at estimator list and are not added to D38 or E74. When the quoted solution is Search, the quote stays the on-demand read-back. The chart, when present, is in Appendix A of serverless_consolidation_analysis_appendix, after the input arithmetic and the metered-rate tables. The quote integrity table follows it. Appendix B follows Appendix A in that same document.

## Quote integrity

The score is the mean of the checks that apply. A check with no inputs is n/a and stays out of the mean. Pass is every scored check passing its own bar, 100% of them. The score does not decide it. This is not a serverless bill.

| Check | Score | Pass |
|-------|-------|------|
| Sheet echo | Mean of the components that apply: the Observability echo (Estimator_Reference C2 sums), the Security echo (G5), and the Security E73 component, for every sheet that was read, fleet or regional. echo component = 1 − min(\|echo − written bulk\| ÷ tolerance, 1). A blank echo scores 0. E73 scores 1 when the parsed amount is 0, and 0 when it is blank or \|amount\| > 0.005. | each sheet echo is within the tolerance, and parsed E73 is 0. A gap of exactly the tolerance scores 0 and still passes. A sheet that was never read is left out. |
| Bundle coverage | matched deployments / active ECH deployments (a serverless project on the roster is not counted). When the active count is absent, the denominator is matched bundles plus bundles that fell back. | Every active deployment has a bundle, and the fallback count is 0. |
| Measured retention | 1 − (bulk with no measured age / Σ bulk) | No unmeasured bulk remains in the weighted month. |
| Rated ingest | 1 − (store in unrated indices / local store of matched active deployments). n/a when the bundles were parsed before the count existed. | Unrated store is 5% of local store or less. |
| Estimator monotone | 1 − (steps of the deployment-count plan that cost more than the step before ÷ steps). n/a with fewer than two priced steps. | No step rises more than 2% plus $5 above the one before it. Merging projects removes a project's fixed ingest and minimum search power, so a step that rises means an estimator's total did not fall with the fleet. The read-back stays the dollar; this flags it. |
| Priced share | `priced_mo` ÷ (`priced_mo` + `unpriced_mo`): the share of the ECH deployments' billed cost that belongs to deployments with a serverless price. n/a when the account has no per-deployment months. | The share is at least `--min-priced-ecu-pct`, 90% unless set. |
| Direct read-back | solutions on their own read-back / solutions with a workload | Every solution with a workload is quotable from its own read-back. One region counts. `per_region` counts. A store split does not. A rejection does not. |
| Line agreement | 1 − min(\|sum of quote lines − headline\| / headline, 1). Both $0 scores 1. | The lines and the headline agree to the cent. |
| Year agreement | For each quote line, 1 − min(\|steady annual − 12 × unrounded monthly\| / \|steady annual\|, 1), then the mean. Both sides are rounded to cents first. A line with no steady annual uses its year-1 figure. | The steady annual equals 12 × the unrounded monthly, to the cent. When the sheet is written as 1 month, year 1 also equals that steady annual, to the cent. A longer keep may ramp, so year 1 can sit below the steady annual. |

The detail prints both cents and the gap. A missing monthly or a missing steady annual on a quote line scores 0 for that line. The overall result passes only when every scored check passes. One check below its own bar fails the quote, and its detail names the gap. A check can pass its bar and score under 1, so a passing quote can show a score below 1.000. Bundle coverage below 1.0 names each deployment that has no bundle.

## 2–5 projects

1. Sort rows by `store_gb` descending.
2. Place each row into the pack with the smallest `store_gb` so far.
3. Price each pack with the formulas above, not with `store_gb / (3 × 30.416)`.

State the pack's stored GB only as a label for what was grouped. The ingest input is the pack's Σ bulk.

The 2–5 project pack prints inputs only. It does not price Search a second time. Sum of the read-backs is the split-fleet price at the measured month. Compare that sum with the one-project read-back. The difference is the cost of staying split. A user-set retention month replaces the headline month on every project, including the pool, and does not change Σ bulk.

## Gap against K360

K360 stored GB ÷ assumed retention is a backup, used when the user asks for the comparison or when `--k360-ingest` was passed. It is not a second diag quote.

Split the dollar gap into:

- **Ingest.** Same retention, the two daily rates. Diagnostic bulk versus stored GB ÷ retention (or versus cloud-tracker dataset growth, when that was the other tab).
- **Retention.** Same daily rate, diagnostic weighted days versus the flat assumed months.

Most of a large gap is the ingest measure. Bulk request bytes over a node's uptime run well above the growth of `index.dataset_size_bytes`. Stored size on the two sides often agrees; that does not make the daily rates interchangeable.

Do not count `partial-*` (searchable snapshots) as ingest. Do count `.ds-*` data streams. Exclude `.ds-.` and `.ds-ilm`.

## What not to quote

- Stored GB ÷ (3 × 30.416) labeled as a diagnostic price.
- A ladder result from `price_o11y_serverless` in place of D38 or E74.
- A pack whose GB came from `data_pivot` or the March telemetry snapshot while the label says diagnostic.
- One blended monthly number that adds the diag quote and the K360 backup.

## Appendix B

serverless_consolidation_analysis_appendix prints these formulas as Appendix B, on its own page after Appendix A. The numbers are the full-retention bulk quote. Each row is the value, the formula, the inputs used on that run, and the result already in the report. A row is left out when that value was not calculated. D38, E74, and the es3 performant total stay read-backs. The page does not reprice them.
