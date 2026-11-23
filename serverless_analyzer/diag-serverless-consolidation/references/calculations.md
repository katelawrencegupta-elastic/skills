# Diagnostic consolidation arithmetic

`DAYS_PER_MONTH` = 30.416. `HOURS_PER_MONTH` = 730, and Observability monthly GB is GB/day × 730 ÷ 24. 730 ÷ 24 is 30.4167, so that monthly GB and 30.416 differ in the fourth decimal. Two raw-to-metered expansions exist and they are not the same number. `O11Y_METER_FACTOR` = 1.595 is the Observability blend and is applied only to a K360 raw rate, never to diagnostic bulk. `SECURITY_EXPANSION` = 1.66 is the Security sheet's own factor; the quote reads the live ratio G5 ÷ G4 from the copy rather than trusting that constant, so a template that changes it is still priced correctly. The 1.66 constant is used only by `diag_inputs.py`, which prints inputs and does not quote dollars. Every quoted project uses rows from `sizing_from_diags.json` for deployments that billed in the quote window, the last 90 billed days by default. An unmeasured age is left out of the recorded weighted age and never blocks a quote.

## Active deployments

A deployment is on the quote when its billing ECU in the quote window is above zero. The window is `--lookback-days`, 90 by default. The window ends on the last day with a positive total. Trailing days with no bill are left out. A partial calendar month is not the window. Deployments with no bill in that window are counted and then left out of the sizing. Their last telemetry snapshot is not added to stored GB and is not converted to GB/day. A sibling that billed in the window keeps its own current size. The old snapshot is not added on top.

ECU/mo on the sizing row is the last complete month, summed over that roster. An open month does not shrink that row. The cover ECU is that same window, expressed as a month.

A fresh fetch saves the exact roster for 30 and for 90 days (`chart.billed_windows`): every deployment with a bill on any day in the window. An org report fetched before that carries only the 30-day roster. For any other window the roster is rebuilt: a deployment with its own daily series counts when that series is above zero in the window, and any other deployment counts when a calendar month the window touches has ECU. That rebuild can include a deployment whose last bill was earlier in the first month. Fetch again for the exact set.

An active deployment with no bundle is not sized (`--unmeasured exclude`, the default): no ingest or stored GB enters the diagnostic estimate. Its K360 size is kept only as `k360_gb` context and cannot affect estimator inputs. A decommissioned deployment, billed in the window and gone from the admin console, is treated the same way under every mode. A diagnostic bundle whose deployment is inactive is not priced.

An active deployment without a diagnostic bundle is excluded from both sides of the comparison. `--unmeasured fail` stops the run and names every active deployment with no bundle. The separate `--k360-ingest` scenario is opt-in only and is labeled as an assumed scenario; it is never the automatic quote.

## ECH comparison scope

K360 daily deployment cost is paginated from Cloud Tracker using the same billing `cost` field as the org daily total. Each day's deployment costs must reconcile to org billing within tolerance before allocation is used:

```
window(dep)      = Σ daily_deployment_cost(dep, day) over the quote window
as a month       = window ÷ window_days × 30.416
```

If the daily deployment composite is incomplete, does not reconcile, or does not align to org days, the scope and savings comparison are unavailable. There is no monthly-share fallback.

Four parts add up to the window (`ech_scope` on the consolidation):

| Part | What it is |
|------|------------|
| `priced_mo` | ECH deployments that got a serverless price |
| `unpriced_mo` | ECH deployments on the roster that did not, each named in `unpriced` with a reason |
| `serverless_mo` | existing serverless projects |
| `other_mo` | lines with no deployment name, and deployments off the roster |

A deployment is priced when its solution has an accepted read-back and it was measured: a matched bundle with bulk above zero for Observability and Security, or diagnostic stored GB above zero for Search. The reasons are: no diagnostic bundle; decommissioned; the solution has no accepted estimator read-back; its bundle measured no bulk; no stored GB to price.

`--ech-scope priced`, the default, sets ECH to `priced_mo`. An account written before these flags existed carries no `sizing.quote_options`, and is compared the way those runs were: the whole window, existing serverless spend included. A fresh run always records the flags it used. `--ech-scope all` sets it to the whole window less `serverless_mo`, or the whole window with `--keep-serverless-spend`. `savings.ech_list_mo` is that figure and `basis.ech_window_ecu` is the whole window. On the diagnostic quote, the historical usage rows under "What if data is kept longer, or the fleet is busier" scale the scoped figure by whole-org month ratios. The separate K360 scenario shows its quote-window estimate only and has no quietest-month or complete-month-average rows. Each row of "Where ECH spend sits" is that deployment's own part of the window (`ech_scope.by_deployment_mo`), and its fixed overhead is scaled to the same part. ECH deployment allocation uses `billing.daily_deployment_costs`, the exact Cloud Tracker daily cost by deployment. The source composite must complete and its deployment totals must reconcile to org daily totals; if absent, incomplete, or unreconciled, the deployment-scoped comparison is unavailable and the priced-share integrity check fails. The serverless estimate stays the spreadsheet or notebook read-back, and page 1 still sets it beside the last complete months of billed cost. Monthly shares are never used to allocate daily org spend. Search EIS counts only sized ECH Search deployments represented in the accepted Search estimate; existing Serverless projects are excluded from the migration quote. EIS uses complete per-deployment daily billing summed over the exact quote window and normalized by `30.416 / quote_window_days`. If daily EIS and ML-node data are incomplete or do not reconcile to complete fetched-period totals, EIS is not added and the EIS integrity check fails. The Search read-back stays on the quote. Fetched-period pace proxies are never used. EIS is billed inference ECU/tokens; ML-node ECU is reported separately and not attributed to Jina co-occurrence.

## ML nodes carried to serverless

No serverless project bills an ML node, so the ML capacity ECH paid for is carried to the serverless total as an EIS estimate and is not dropped (`ml_eis_carryover`, and `savings.ml_eis_mo`). The line is the quote-window ML-node ECU billed on ECH, priced at 1 ECU = $1 before support. It is a placeholder for the same ML work moving to EIS inference or project VCUs, not an estimator read-back. The ECH side keeps the ML nodes it billed. The line is added to `serverless.total_mo` and held constant in the sensitivity rows, like EIS. It is not in the estimator total the integrity checks and the plan bars reconcile to. The read does not depend on Search: an Observability or Security fleet with ML nodes and no Search read-back carries the same capacity. It covers every deployment in the comparison — the priced set under `--ech-scope priced`, every ECH deployment on the roster under `all` — not only the ones the Search estimate represents. `search.ml_node_ecu_mo` stays the Search deployments' own share, for the EIS narrative, and is not the amount carried.

It uses the same gate as EIS: a complete filtered composite, and a daily series that covers the exact quote window and reconciles to the fetched-period totals, normalized by `30.416 / quote_window_days`. A deployment whose composite total is zero and that has no daily row billed no AI/ML in the fetched interval and counts as zero, the same way an absent composite bucket does. If the telemetry is incomplete or does not reconcile, nothing is carried, the notes say the serverless total is understated, and `estimation_limits` carries `ml_node_ecu_unverified`. A line that cannot be verified would invent a price, so none is added. The ML read needs the deployment-scoped ECH comparison, because that comparison names the deployments.

## Regions Serverless is offered in

The supported regions are the Elastic Cloud Serverless regions page, https://www.elastic.co/docs/deploy-manage/deploy/elastic-cloud/regions, kept as one table in `serverless_regions.py` with the date it was read. It lists a limited number of AWS, Azure and GCP regions and says more will follow, so refresh the table when the page changes.

An ECH deployment whose provider and region are not on that page is not quoted on Serverless. `apply_diagnostics` marks it `ingest_source` `unsupported_region`: no ingest and no stored GB go to the estimators, it is not a serverless project in the plan or the per-region sheets, and its bundle is not counted as unmatched. Its workload continues on ECH. The comparison leaves its billed cost out under every `--ech-scope`, `ech_scope.unpriced` lists it with `stays_on_ech`, and `ech_scope.stays_on_ech_mo` and `stays_on_ech_deployments` report it on its own. It is not counted as a deployment the quote failed to price, so it does not lower Priced share and is not named in that check. The cover callout, the notes, `region_support` in the brief and the `region_not_on_serverless` limit say which deployments, which regions and what they bill on ECH. A deployment with no known provider or region is not treated as unsupported.

A US deployment is the exception. The US has no data-residency constraint, and staying on one provider keeps a marketplace purchase valid, so a US region the page does not list is quoted in the nearest offered region of the same provider (`US_SWAPS` in `serverless_regions.py`: Azure `southcentralus` to `centralus`, for example). The deployment is sized and priced as normal in that region and stays in the comparison. The cover callout "Region change assumed", the notes (`REGION CHANGE ASSUMED`), `region_support.swapped` in the brief and the `region_swapped` limit say which deployments moved from and to which region and what they bill on ECH. The swap is an assumption: a serverless project cannot change region after it is created, and the latency, data-transfer and cutover work is not in the estimate. A non-US region is never swapped, because the country may matter, so that workload stays on ECH.

Every project is priced on the provider of the deployments in it, taken from each deployment's own cloud record, never on one account-wide provider. A multi-cloud account has no single provider, and pricing an AWS or GCP region against Azure rates returns the Azure default or no price at all (`serverless_regions.deployment_providers`, `group_provider` and `solution_provider`).

A region the page lists but an estimator sheet does not is a different case. It is still priced at that provider's default and flagged by the Region fidelity check.

## Source precedence and estimation limits

For deployments with a matched diagnostic bundle, diagnostics take precedence for local primary store, bulk GB/day, retention ages, product-derived Observability mix, and the aggregates derived from those fields. `field_sources` on deployment rows and `sizing.source_precedence` make the source choice explicit. Cloud Tracker remains authoritative for billed ECU/cost, the billing-window active roster, Search workload telemetry, cloud region/provider/tier metadata, and deployment classification. The Search ingest-share denominator is the sum of diagnostic local stores. Cloud Tracker stored GB on an unmeasured deployment is retained for context only and cannot change any diagnostic estimator input.

`diagnostics.estimation_limits` lists missing bundles, unmeasured retention, short diagnostic uptime, bulk without a measured ILM write phase, unrated ingest, assumed Observability mix, EIS quote-pace proxy use, and incomplete EIS totals that were omitted. Reports surface these limits with the quote. A limit is not silently converted to a diagnostic measurement.

A step that ran degraded is a limit too, not a line on the terminal. `run_consolidation.py` records one whenever a warning changes what the quote contains, so the brief, the chat bullets and the PDF all carry it:

| Code | What ran degraded |
|---|---|
| `admin_list_unavailable` | The admin deployment list could not be read, so a billed deployment with no bundle counts as missing a diagnostic rather than decommissioned |
| `diagnostic_pull_failed` | A diagnostic pull failed for an organization; deployments in it may have no bundle |
| `stale_diagnostic_bundles` | No fresh diagnostics could be pulled, so the quote was measured from zips already on disk |
| `diagnostic_download_failed` | Downloads failed after retries and `--allow-missing-diags` kept the run going |
| `sheets_venv_missing` | No estimator copy was filled, so the quote has no sheet read-back |
| `sizing_step_failed` | The estimator sizing step failed; the document keeps the measured inputs and is a draft |
| `regional_sizing_failed` | Per-region sizing failed, so each region is a stored-GB share of one fleet read-back |
| `deployment_plan_failed` | The deployment-count plan was not priced: no bars and no per-cluster split |
| `es3_estimator_missing` / `es3_estimator_failed` | The es3 interpreter is not installed, or it ran and failed. A Search region went unpriced for an environment reason, and the two are told apart |
| `consolidation_failed` | Building the consolidation raised; no quote was produced |
| `ml_node_ecu_unverified` | ML-node ECU was not carried to the serverless total |
| `retention_month_fallback` | No retention could be ascertained, so the sheets took the 30-day default, written as month 1 |
| `search_proxy_workload_missing` | Search in a region was not priced: the proxy logs returned no external traffic for its Search deployments, the logs could not be read after retries, or the bundles show no bulk or documents. The note names the region and the reason. Other regions still price |
| `region_swapped` | US ECH deployments are quoted in a different region of the same provider because Serverless is not offered in their own |
| `region_not_on_serverless` | ECH deployments in a region Elastic Cloud Serverless is not offered in stay on ECH: no serverless price, and their ECH bill is out of the comparison |
| `billing_reconciliation_outside_window` | Daily deployment billing missed org billing on some days, but fewer than 5% of the quote window's days. The ECH comparison was kept (medium severity) |
| `estimate_dedupe_failed`, `pdf_export_failed`, `pdf_upload_failed` | Delivery steps; the quote itself is unaffected |

A run marked DRAFT always names why: a test asserts that no path sets `meta.draft` without recording a limit.

## One project, or one pack

```
bulk_gb_day = Σ bulk_gb_day
weighted_days = Σ (bulk_gb_day × retention_days) / bulk_gb_day
months = weighted_days / 30.416
```

The written month is the store-weighted average retention across indexes with local primary store, rounded up with `whole_months` (`index_retention_months`). Each index counts once. Its age is the ILM delete age, else the data-stream retention, else 30 days. Deployments carry `retention_index_days_sum` and `retention_index_count`, and the fleet mean divides the sums so a large deployment is not one vote. That integer is written on every Observability and Security sheet, including each regional sheet and each deployment-plan sheet. When no index was counted, the written month is the ingest-weighted delete age the bundles measured, rounded up (`retention_month_basis` `measured_age`). When no retention can be ascertained at all, it is `RETENTION_FALLBACK_DAYS`, 30 days, written as month 1 (`fallback_30d`), and that fallback is a warning: the Retention month check scores 0, `estimation_limits` carries `retention_month_fallback` at high severity, and the notes say the month was not measured. It is never a 12-month assumption. The size-weighted age stays `retention_months_measured` and does not enter the price. A solution with no bulk is skipped. Security monthly at the written month is E74.

Every consolidation run writes one PDF: `<key>_full retention_bulk_serverless_consolidation_plan`, with every Observability and Security sheet at that rounded-up index average. There is no 30-day read and no second measured-retention sheet. Search disk is the measured local store on every sheet and every plan bar, and is not scaled by the month.

The quote window is `--lookback-days`, default 90, maximum 365. ECH monthly ECU is the total of those billed days ÷ the number of days × 30.416. Billed days run from the org's first positive day in the fetch to its last: days before the first bill and days after the last one are not billed days. An org with fewer billed days than the window asks for is priced on the days it has, and the roster follows the same days. `ecu.quote_lookback_requested` is what was asked and `ecu.quote_lookback_days` is what was priced. With no daily series there is no window: `quote_lookback_days` is null, ECH is the complete-month average, and there is no `ech_scope`. The consolidation run stops on an org report with no daily series (one fetched at week grain by `run_org_report.py`).

ECH dollars are the K360 billing `cost` field, summed as billed. Each billing row carries `quantity`, `rate`, and `cost`, and `cost` is the ECU for that row, so the sum is the ECU total and 1 ECU is $1. No ARR ÷ (commit ECU/day × 365) rate is applied to it. That implied rate came out above $1 per ECU on real accounts and put the ECH bill above the ARR itself. ARR and commit stay on `contract_rate` for reference, with `implied_usd_per_ecu` null and `basis` `k360_cost`. `savings` is `ech_list_mo` (ECH billed cost for the month), `serverless_mo`, and `list_mo` = `ech_list_mo` − `serverless_mo`, with `list_yr` and `list_pct`. A negative `list_mo` is an increase. Ingest written to the diagnostic estimators is diagnostic bulk as measured. Bulk is a rate at capture, so the billing window does not scale it: `ecu.quote_ingest_factor` is 1.0. `ecu.quote_usage_factor`, window daily average ÷ complete-month daily average, scales ECH ECU only. The separately requested K360 backup may use stored-GB ingest × that factor; it cannot alter the diagnostic estimate. Search QPS comes from the service proxy request logs and docs/s from the diagnostics, and neither is ever multiplied by a billing pace factor. A region uses its own measured workload. A packed what-if project splits the org-wide workload by diagnostic search store ÷ diagnostic org store. The sheet read-back is the quote. The cover chart title names that window. Page 1 also draws `ech_recent_months`: the last 3 complete calendar months of the same billing `cost` (the monthly total from `ecu.monthly`, partial months left out) and their average, beside the serverless estimate at that month; the difference row is the average minus `serverless_mo`. ECH is drawn as billed cost and serverless at list. A missing daily series for a window of 30 days or less falls back to `last_30d_daily_avg`. The account fetch stays the trailing year. What you pay for today keeps the fetched year as Full lookback, split by tier, and adds the quote-window usage total as its own bar.

The PDF links to its estimator copies, left at that month. Estimate sheets live directly in `generated_estimates`. Deployment-plan sheets live in `generated_estimates/plan_sheets`. The consolidation PDF is uploaded to the customer folder root, beside the account PDF and the appendix. There are no `full_retention` or `30d` folders. An estimate or a consolidation PDF that an earlier run filed under a `full_retention` or `30d` folder is moved up, on disk and on Drive, and the folder is removed once it is empty. The deployment-count bars are priced at that same month.

`--retention-months` is the K360 backup only. The diagnostic quote writes the rounded-up index average on G11, G12, and G6. `--retention-days` replaces that average with the named age, still stored as a whole month. Bulk GB/day stays the measured rate. `retention_months_measured` stays in the arithmetic next to the month written. When daily deployment billing is unavailable, page 1 compares the priced-only monthly ECH bill with the serverless read-back and does not invent a daily split.

`retention_days` is the size-weighted ILM delete age, else data-stream retention, else the 30-day default for indices without configured retention, kept to 3 decimals. The default is marked assumed and does not use historical index age as a retention setting. The weight uses local primary bytes only. Searchable-snapshot mounts (`partial-*`) and the snapshot portion of `total_data_set_size_in_bytes` stay out of store and out of that weight. `bulk_gb_day` is the sum of `index.primaries.bulk.est_bytes_per_day` on write indices, divided by 1e9. A write index is the last backing index of a data stream, or an index an alias marks `is_write_index`. Failure-store indices are not write indices. For each of those indices the bytes per day are `primaries.bulk.total_size_in_bytes × 86,400 / write_phase_sec`, truncated to a whole byte. That is `IndexStatsDocument::calculate` in esdiag `indices_stats/processor.rs`. `write_phase_sec` is whole seconds from creation until rollover, or the index age while the current ILM action is `rollover`. A missing or zero write phase contributes 0. A write index in its first day is rated together with the generations before it, Σ bulk bytes × 86,400 ÷ Σ write-phase seconds, until the window reaches a day. An older generation is used only while ILM has it in the hot phase and its bulk-to-store ratio is at least half the write index's, because a moved shard has lost its counter. Otherwise rolled indices are not added. A data stream with no ILM dates takes the write index age as its write phase. Dated indices written by name (`app-2026.11.12`) with no data stream or write alias are rated on the newest index over its age, when that index is at most 32 days old. `bulk_fallback_gb_day` is the bulk from those two cases. `bulk_short_window_gb_day` is the bulk still rated over less than a day. This is the bulk request size over the write window. The indexing rate is the same window over `primaries.store.size_in_bytes` and is not this bulk rate. It is not stored dataset growth and not node `total_size_in_bytes` divided by JVM uptime.

A row with `uptime_days_min` < 2 has a short node uptime. Keep it in the sum and name it. The bulk rate is still the write-phase estimate.

## Measured split

Each rated series is one kind, from its data stream or index name: `metrics-*`, `metricbeat-*`, and any index whose `index.mode` is `time_series` are metrics; `traces-*` are traces; everything else is logs; `logs-kubernetes.*` is the Kubernetes part of logs. The bundle row carries `bulk_mix` (`logs`, `k8s_logs`, `metrics`, `traces`, in GB/day; logs + metrics + traces = `bulk_gb_day`) and `retention_days_metrics`, the size-weighted delete age of the metrics indices.

A group's split is the sum of its deployments' product-derived splits, each scaled to that deployment's written GB/day, so the proportions are kept. On the metered basis the Observability sheet takes logs plus traces on G19 and metrics on G20 (see Metered basis). On the raw basis it takes logs plus traces on G23, the Kubernetes share of logs on G24, and metrics on G25. G12 is the metrics month, rounded up like G11. The fleet sheet, each regional sheet, and each deployment-plan sheet use the split of the deployments in that sheet. If a diagnostic bundle has no product classification, sizing stops instead of applying a synthetic split.

## Retention with no delete policy

An index is aged by its ILM delete phase, else its data stream's retention. An index covered by neither uses the explicit 30-day assumed default, rather than its historical index age. `retention_known_share` on a bundle row is the share of local store whose age a policy or data stream sets, and `retention_assumed_share` is the rest.

`retention_basis` is `policy`, `blended`, or `assumed`, decided by `retention_assumed_share` against `RETENTION_ASSUMED_MAX_SHARE`, 10%:

| `retention_assumed_share` | `retention_basis` | Counts toward `retention_policy_bulk_gb_day` |
|---|---|---|
| 0 | `policy` | yes |
| above 0, at or under 10% | `policy` | yes, and the row is named as still carrying an assumption |
| above 10%, under 100% | `blended` | no |
| 100% | `assumed` | no |

One unconfigured index does not drop a configured cluster's whole bulk out of the measured-retention total. A row inside the threshold is still listed in `diagnostics.retention_30d_assumed` with `within_threshold` true and its own share, its bulk is on `diagnostics.retention_policy_within_threshold_gb_day`, and the notes name each one, so counting it as policy-aged does not hide what it assumed. When more than 10% of the sized store has no policy, the notes also report that share and the 30-day assumption (`diagnostics.retention_blended_share`).

## Bulk rate after a restart

The bulk counter starts at zero when a primary shard starts on a node, and the write phase runs from the index's creation, so a shard that restarted or moved has counted fewer bytes than the write phase's length implies. `analyze_bundle` reads each index's latest primary recovery start from `recovery.json`. Where the counter is younger than 90% of the write phase, it adds what the rate would be if the same bytes were spread over the counter's own age: `bulk_restart_gb_day` on the row and `diagnostics.bulk_restart_gb_day` on the quote. The quoted `bulk_gb_day` stays the write-phase rate. The notes say so when the extra is more than 5% of the quoted bulk. Replica copies count bytes too, so this is an estimate of the size of the effect and not a corrected rate.

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
Security       G4  = bulk GB/day ÷ expansion                expansion is read live as G5 ÷ G4 on the copy (1.66 on v2.6)
```

The sheet's ladders take `G19 ÷ 730 × 24` and `G20 ÷ 730 × 24`, so they price the measured GB/day with no expansion. Kubernetes logs need no separate share on this basis, because their higher factor is an expansion too. The echo that gates the quote is the GB/day the sheet priced: `Estimator_Reference!C2` + `Estimator_Reference-Metrics!C2` for Observability and G5 for Security, each within the tolerance, 0.1% of the bulk written to that sheet and at least 0.02 GB/day. A Security miss clears the monthly, so the sheet is not quoted.

The K360 backup (`--k360-ingest`) has no measured rate. It keeps Option 2 and G4 as raw GB/day, and its echo is G28. The metered-rate table uses a factor of 1.0 on diagnostic bulk and 1.595 on a raw rate (`consolidation.metered_expansion`).

Bulk already carries the metadata overhead: the shipper's fields and the pipeline enrichment are in the bulk request by the time it reaches the shard. That is the overhead the sheets' expansion factor adds to a raw rate, which is why no factor is applied here (confirmed by the skill owner, 2026-11-12). A useful spot check remains: for a customer already on Serverless, billed ingest GB/day should sit close to the bulk figure from a diagnostic of the same workload.

## Compression-applied set (retired)

The compression-applied quote path is retired. Bulk goes into the metered cells directly. No `compression_applied` folders or `compression-applied` file names are written, and a new bundle does not carry a second ingest field.

## Which estimator

| Solution | Inputs | Quote cell |
|----------|--------|------------|
| Observability | G19 = (product-classified logs + traces) × 730 ÷ 24, G20 = product-classified metrics × 730 ÷ 24, G23 0, G24 0%, G25 0, G26 0, G27 100%, G11, G12 metrics retention, G4 Complete, G5 Premium, G8 region, G15 No, G33 No | D38 monthly, E38 year 1, F38 steady annual |
| Security | G4 = Security-row bulk ÷ the copy's own G5 ÷ G4 (1.66 on v2.6), G6 = the written month, G7 region, G8 Complete, G21 Premium | E74 monthly |
| Search | disk = Σ diagnostic local `store_gb`. QPS, complexity and hour shares are read from the service proxy request logs for that region's Search deployments, and docs/s from the diagnostics' bulk rate over 730 h (see sizing-cells.md). Nothing is scaled. K360 stored GB for an unmeasured deployment is excluded. A packed what-if project takes its region's own workload, scaled by its share of that region's Search store. Search sheet performant (G110); es3 performant only with `--search-notebook` | `monthly_steady_state` |

Product classification is required for Observability split inputs. Security takes the full bulk rate on G4.

On a diagnostic Observability run the echo is two cells. G19 must match the written logs plus traces, and G20 must match the written metrics. Each cell is accepted within 0.1% of what was written to that cell, at least 0.02 GB/day. A blank cell fails that cell. A swapped mix with the same total fails. Security stays one echo, G5 against the written bulk, with the same tolerance. The K360 backup echoes G28, the total raw GB/day. Read each figure unformatted. A miss records both numbers and leaves that solution unquoted. The written bulk stays on the solution block.

Security E74 is the monthly quote. The headline writes the rounded-up index average, so E74 is the steady month. E74 also sums the endpoint, cloud, workflow, LLM, and SOAR lines: E74 − E61 − E62 − E69 − E73 must be 0, and a non-zero amount blocks the quote like a non-zero E73. Either figure is the quote only when parsed E73 is 0. A blank or unreadable E73 blocks, and so does a non-zero value. A parenthesized credit stays negative. Call that check only after a Security sheet was read. A region that was never sized is a missing sheet, not a blank E73. E73 is not subtracted from E74. Add-on inputs are cleared on the copy. The golden template is not edited.

Every eligible region is written to its matching estimator, regardless of projected or read-back monthly price. Observability and Security require regional bulk and a retention month; Search requires positive measured local store and proxy-log workload for that region. The estimator read-back determines the regional amount. A region without eligible inputs is not sized; an eligible region without an accepted read-back is a quote-integrity failure and is not silently omitted. A region with bulk is written at the rounded-up index average whether or not it has a size-weighted age. The measured age stays on `retention_months_measured`. One regional echo miss, a blank regional E73, or a non-zero regional E73 leaves that whole Observability or Security solution out, including regions whose own read-back was accepted. Regional dollars use one mode. When every included region has its own accepted read-back, the headline is the sum of those read-backs. A region that is not on the estimator list is still accepted: it is priced as AWS us-east-1, Azure eastus, or GCP us-east1, never another cloud, and the match is `provider_default`. That read-back is the regional amount, the notes and the cover name the substitute, and Region fidelity warns. It does not fail the quote. A missing read-back leaves that solution out. A stored-GB split is used only when the fleet total is already a quotable read-back. A region's own read-back is not added to another region's share. Year 1 follows the same mode. A region with bulk and no local bytes contributes $0 on a store split.

## Per deployment

A deployment at or above 50 GB/day is its own project. Every deployment under 50 GB/day is one pooled project. 50 GB/day is the top of the first Observability ingest tier, so a deployment below that, priced alone, never leaves the top $/GB band. The pool is priced on its Σ bulk and bulk-weighted retention, which lets the combined ingest step into cheaper tiers.

## Deployment-count plan

The ceiling is the cover count: one serverless project per deployment that billed in the quote window, aligned to that deployment's feature. The cover count and the ceiling are the same deployments. The floor is one project per feature that exists in each region (Observability, Search, or Security). A region and feature with no deployments adds nothing. Each later count is round(previous × 0.75), half-to-even. If that round does not decrease, subtract 1. A result at or under the floor is the floor, and the series stops. Labels are Ceiling, −25% on each middle step, and Floor.

A bar's monthly is the sum of its projects. Search is the performant read-back (G110) of that project's Search estimator sheet, the es3 notebook only with `--search-notebook`, plus the same Search EIS-with-support dollar the cover uses. A project gets its region's own workload, scaled by its share of that region's Search store, so the floor prices exactly what the headline priced. That dollar is billed inference for the sized Search deployments, so packing does not change it and every bar that prices Search carries it once. A Search project with no performant total leaves the bars unpriced. Search disk on a bar is that project's measured store, so a project's disk does not change when it merges into another. The Search model is not additive, so a merged Search project can still price above the projects it replaced, and a bar can step up. Every eligible region is written, including one under $10. Observability is the D38 read-back for that group's region and provider. Security is the E74 read-back, and only when E73 is 0. The headline Observability read-back is reused only when the ceiling is already one project, and that bar is labeled Ceiling. A floor of 1 reached by cutting is priced as that packed group.

Packing stays inside one region and one feature. The ceiling is one group per deployment. At or under the number of region-and-feature pairs, each pair is one group. Between those, extra projects above the floor go to the largest pairs. Retention on a project is the rounded-up index average. The size-weighted age stays a record. A project with bulk and no measured age is still priced.

The chart draws serverless at list and the ECH billed cost on every step. The amount under the project count is the ECH bill minus serverless at list. No contract rate is applied to either side on this chart, the cover, the monthly build-up, the usage chart, or the search-power appendix.

## Raw bulk ingest by deployment

Own page in the consolidation PDF, after "Where ECH spend sits". It lists every sized deployment's raw bulk GB/day and the arithmetic from bundle to estimator input. Appendix B repeats the formulas under "Raw bulk ingest". It is not a quote. The estimator read-back stays the dollar.

```
bytes_per_day(index) = primaries.bulk.total_size_in_bytes × 86,400 ÷ write_phase_sec   (whole bytes)
bundle_gb_day(dep)   = Σ write indices bytes_per_day ÷ 1e9
quoted_gb_day(dep)   = bundle_gb_day                          (ecu.quote_ingest_factor = 1.0; only the K360 backup is × ecu.quote_usage_factor)
fleet                = Σ deployments, by solution; a deployment with no bundle adds 0
share(dep)           = quoted_gb_day ÷ fleet quoted
logs / metrics / traces = the measured split of Observability bulk (no split leaves the $/GB empty)
metered_gb_day       = bulk                                   (× 1.595 only on a raw K360 rate)
days_in_store(dep)   = stored_gb ÷ bundle_gb_day              (indicative: stored GB is compressed, bulk is not)
```

`write_phase_sec` is creation until rollover, or the index age while the ILM action is rollover. Write indices are the last index of each data stream and any alias marked `is_write_index`. A missing or zero write phase is 0. Days in store compares the local primary store with ingest: a value far below retention days means retained data sits outside that store (searchable snapshots) or the stored-GB snapshot is older than the bundle. A node up under 2 days measures the rate over a short window, so that row is shaded.

## Deployment consolidation analysis across metered ingest rates

This is the Observability ingest rate for one deployment priced alone. It is a band walk. Diagnostic bulk is metered, so the walk applies no expansion. A raw K360 rate is × 1.595 first. It is not a D38 quote and it does not include retention or support.

```
metered_gb_day = bulk_gb_day            (× 1.595 only on a raw K360 rate)
logs, metrics = the measured split of metered_gb_day
rate = (tier(logs) + tier(metrics)) / metered_gb_day   when a split was measured
rate is empty when the split is missing, and the row is not the $0.50 row
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

On-demand, performant, and high availability are read from columns F, G and H of the Search estimator sheet when Search was estimated (from the es3 notebook with `--search-notebook`). High availability is the third search-power band. Show monthly and steady annual (monthly × 12) for each. When Search was not estimated, `search_power` is absent and the Appendix does not include the chart. These dollars stay at estimator list and are not added to D38 or E74. When the quoted solution is Search, the quote stays the performant read-back. The chart, when present, is in Appendix A of serverless_consolidation_analysis_appendix, after the input arithmetic and the metered-rate tables. The quote integrity table follows it. Appendix B follows Appendix A in that same document.

## Confidence

Two different questions were answered by one number. `integrity.passed` says whether the quote assembled correctly. `integrity.score`, the unweighted mean of every scored check, was also carrying how well the fleet could be measured — and a mean is the wrong instrument for that, because the one check that undermines a headline is averaged away by the many that pass trivially. A fleet priced at an unmeasured default month scored 0.869 next to the same fleet priced at a month that was stated.

`integrity.confidence` is that second question on its own: how much of the quoted price rests on measurement rather than assumption. It is the weighted mean of the evidence checks, each weighted by how much it moves the dollar (`CONFIDENCE_WEIGHTS`). The correctness checks are not in it — the sheet echo, the line and year totals, plan consistency and the rest say a number is wrong, which is a pass or a fail and not a degree.

| Weight | Checks | Why |
|---|---|---|
| 3 | Retention month, Retention basis, Bundle coverage, Priced share | Sets the price. Retention drives the estimator's retained-data ladder; coverage and priced share decide how much of the fleet the quote represents at all |
| 2 | Rated ingest, Region fidelity, Search EIS data, Rate window, Billing window | Changes the price by a bounded amount: ingest the rate did not see, a substitute region's differential, an EIS line, the window a rate or a comparison was measured over |
| 1 | Counter age | A flag beside a figure that was still measured |

```
confidence = Σ (weight × score) / Σ weight     over the evidence checks that apply
```

`band` is **High** at 0.85 and up, **Moderate** at 0.6 and up, **Low** below that. The band is the figure to quote: two decimals of a weighted mean imply a precision these inputs do not have. A check that does not apply is left out of both sums, so a fleet with no Search is not marked down for having no EIS.

`drivers` lists what is holding the band down, heaviest and lowest-scoring first, with the `cost` each one takes off the score. That is the order worth acting on. The chat line names the top two, the consolidation PDF prints the whole table above the check list, and both say that confidence is not the same question as Pass or Fail.

A quote can pass every check and still be Moderate. That is the point: it means the arithmetic is right and the inputs were thin.

## Quote integrity

The score is the mean of the checks that apply. A check with no inputs is n/a and stays out of the mean. Pass is every scored check passing its own bar, 100% of them. The score does not decide it. This is not a serverless bill.

| Check | Score | Pass |
|-------|-------|------|
| Sheet echo | Mean of the components that apply: the Observability echo (Estimator_Reference C2 sums), the Security echo (G5), and the Security E73 component, for every sheet that was read, fleet or regional. echo component = 1 − min(\|echo − written bulk\| ÷ tolerance, 1). A blank echo scores 0. E73 scores 1 when the parsed amount is 0, and 0 when it is blank or \|amount\| > 0.005. | each sheet echo is within the tolerance, and parsed E73 is 0. A gap of exactly the tolerance scores 0 and still passes. A sheet that was never read is left out. |
| Bundle coverage | matched deployments / active ECH deployments (a serverless project on the roster is not counted). When the active count is absent, the denominator is matched bundles plus bundles that fell back. | Every active deployment has a bundle, and the fallback count is 0. |
| Retention basis | Share of bulk on deployments a delete policy or data-stream retention ages. A deployment counts while the store it leaves at the 30-day default is at or under `RETENTION_ASSUMED_MAX_SHARE`, 10%; past that it is blended or assumed and contributes nothing. The share is also on the sensitivity row. | At least 90% of bulk. Below that is a warning, not a failure: the quote is still produced, because the written month is the index average and not this share. Bulk counted inside the threshold is named in the detail. |
| Retention month | 1 when the month written to every Observability and Security sheet is the index average rounded up, or a month `--retention-days` named. 1 when no index was counted but the bundles measured a delete age. 0 when no retention could be ascertained and the sheets took the 30-day default. n/a on the K360 backup, and when no sheet had bulk to write. | Reported as a warning. A default month does not fail the quote, and the notes and `estimation_limits` name it (`retention_month_fallback`). The month sets the estimator's retained-data ladder, so a default month is an assumption, not a measurement. |
| Rated ingest | 1 − (store in unrated indices / local store of matched active deployments). n/a when the bundles were parsed before the count existed. | At most `UNRATED_STORE_MAX_SHARE`, 10%, of local store. Above it is a warning, not a failure, and so is an unrated count with no store to compare it with. An index outside a data stream, with no write alias and never rolled over, has no write phase in the bundle, so its counter cannot become a rate; that is a limit of the measurement, and it says nothing about whether the bulk that was rated is right. The quote is produced and names how much ingest the rate did not see. |
| Estimator monotone | 1 − (steps of the deployment-count plan that cost more than the step before ÷ steps). n/a with fewer than two priced steps. | No step rises more than 2% plus $5 above the one before it. Merging projects removes a project's fixed ingest and minimum search power, so a step that rises means an estimator's total did not fall with the fleet. The read-back stays the dollar; this flags it. |
| Priced share | `priced_mo` ÷ (`priced_mo` + `unpriced_mo`): the share of the ECH deployments' billed cost that belongs to deployments with a serverless price. n/a when the account has no per-deployment months. | The share is at least `--min-priced-ecu-pct`, 90% unless set. |
| Direct read-back | solutions on their own read-back / solutions with a workload | Every solution with a workload is quotable from its own read-back. One region counts. `per_region` counts. A store split does not. A rejection does not. |
| Line agreement | 1 − min(\|sum of quote lines − headline\| / headline, 1). Both $0 scores 1. | The lines and the headline agree to the cent. |
| Year agreement | For each quote line, 1 − min(\|steady annual − 12 × unrounded monthly\| / \|steady annual\|, 1), then the mean. Both sides are rounded to cents first. A line with no steady annual uses its year-1 figure. | The steady annual equals 12 × the unrounded monthly, to the cent. |
| Search EIS data | 1 when filtered Search EIS billing is complete for the quote window. 0 when it is incomplete or missing. n/a when Search was not quoted. | EIS was not added unless the filtered quote-window totals are complete. |
| Region fidelity | Share of written sheets that are an exact match or a same-region alias on the same provider. n/a when no region was written. Every entry records the region it was scoped to, the fleet sheets included, so an alias can be checked against it; an alias with no region recorded is reported as that, not as a region-number mismatch. | Reported as a warning. A substitute region does not fail the quote: the read-back is a real dollar, and withholding the whole quote over it leaves the fleet with nothing. The detail names each one and what it was priced as, and lists a sheet written to another cloud, or with no region recorded, before a same-cloud substitute. Read the price as being for the region named, not for the one the deployments run in. |
| Quote present | 1 when every solution with a workload has a serverless monthly price. 0 when a solution with a workload has no price. n/a when no solution records a monthly price. | A rejected or missing read-back is not a quote. |
| Plan consistency | 1 when the Floor bar is within 1% plus $5 of the headline and the floor is one project per solution and region. 0 when a bar prices a solution the headline did not quote, or the floor gap is larger. n/a when the plan has no comparable floor. | The deployment-count floor matches the headline. |
| Rate window | 1 − min(short-window bulk ÷ bulk, 1). n/a when the short-window field was not recorded. | Short-window bulk above 25% is a warning. The quote is still produced. |
| Counter age | 1 − min(restarted-counter bulk ÷ bulk, 1). n/a when the restarted-counter field was not recorded. | Restarted-counter bulk above 25% is a warning. The quote is still produced. |
| Billing window | 1 when every priced month is at least half the latest priced month. 0 when an earlier month is under that share. n/a with fewer than two priced months. | The comparison is the latest complete month when the window is not one fleet. The earlier months stay on the chart and are not averaged into the difference. |

The detail prints both cents and the gap. A missing monthly or a missing steady annual on a quote line scores 0 for that line. The overall result passes only when every scored check passes. One check below its own bar fails the quote, and its detail names the gap. Rate window, Counter age, Rated ingest, Region fidelity, Retention basis, and Retention month are the exception. A short-window or restarted-counter share above 25%, unrated store above 10%, a sheet written to a substitute region, more than 10% of bulk at the 30-day assumed age, or a written month that is the default rather than the index average, is a warning on the quote, and the quote is still produced. Each of those describes how well the fleet could be measured, or where a sheet had to be written. None of them says a dollar that was read back is wrong, which is what the blocking checks are for. A check can pass its bar and score under 1, so a passing quote can show a score below 1.000. Bundle coverage below 1.0 names each deployment that has no bundle.

## 2–5 projects

1. Sort rows by `store_gb` descending.
2. Place each row into the pack with the smallest `store_gb` so far.
3. Price each pack with the formulas above, not with `store_gb / (3 × 30.416)`.

State the pack's stored GB only as a label for what was grouped. The ingest input is the pack's Σ bulk.

The 2–5 project pack prints inputs only. It does not price Search a second time. Sum of the read-backs is the split-fleet price at the rounded-up index average. Compare that sum with the one-project read-back. The difference is the cost of staying split. A user-set retention month replaces the headline month on every project, including the pool, and does not change Σ bulk. Projects are packed inside one region and one solution.

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

serverless_consolidation_analysis_appendix prints these formulas as Appendix B, on its own page after Appendix A. The numbers are the full-retention bulk quote. Each row is the value, the formula, the inputs used on that run, and the result already in the report. A row is left out when that value was not calculated. D38, E74, and the Search performant total (G110) stay read-backs. The page does not reprice them.
