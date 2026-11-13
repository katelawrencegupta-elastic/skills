# `<key>_account.json` — top-level keys (embed in canvas; ~80–120 KB)

| Key | Contents | Canvas use |
|-----|----------|------------|
| `meta` | customer, slug, run_key, date, lookback, org (AE/ARR/segment/contract). On a consolidation run, `method`: the commit, whether the checkout had uncommitted changes, and the rules that produced the run | header, PDF footer |
| `platform` | label (`ECH` / `Serverless` / `Serverless + ECH (hybrid)`), counts, ECU, `ech_share_pct`, `sizing_triggered`, `serverless.{by_solution,by_month,projects,types}` | §1 |
| `cloud` | summary (provider, region, hot_instance, plan, zones), providers, regions, tiers, instances, zones, plans, templates, non_capacity, billing_types, by_deployment[40] (version, region, zones, components[{tier, ram_gb, nodes}]) | architecture table, then the topology page |
| `ecu` | total_lookback, monthly.{months,totals,partial_months}, `monthly_by_deployment` (each deployment's cost by month), `monthly_serverless` (serverless projects' cost by month), monthly_peak/low, monthly_avg_*, daily_avg, daily_peak, daily_days, daily_totals, last_30d_*, trend, weekly_series, contract. On a consolidation run, `quote_lookback_requested`, `quote_lookback_days` (the billed days priced; null with no daily series), `quote_monthly_ecu`, `quote_usage_factor`, `quote_ingest_factor` | §2 |
| `deployments` | `active` (the quote-window roster: window_start, window_end, window_days, names, ecu_complete_month), count, ech/serverless counts, by_version[{version,count,ecu,ecu_pct}], version_summary.{reference_version,lagging}, over_time.month, trend, top[15] | §2, §4 |
| `storage` | GB/TB, docs, avg_doc_kb, tier_mix, has_frozen, telemetry_age_days, by_deployment[20] | §5 |
| `solutions` | totals.{observability,search,security}.{deployments,ecu,ecu_pct,ech_ecu,serverless_ecu,gb}, method_counts, cross_cutting, by_deployment | §6 |
| `ai_ml` | ml_node_ecu, eis_inference_ecu, feature_counts, models, model_counts, token_meter.{million_tokens,ecu,by_kind,by_solution,rows}, by_deployment (every deployment with an AI signal; the report shows the top 12) | §7 |
| `workload` | search.peak/nonpeak (qps, complexity), ingest | §7 |
| `analysis.performance[≤5]` / `analysis.cost[≤5]` | `{title, severity, evidence, action, impact}` | §8–9 |
| `cloud.single_zone` | deployments billed in 1 AZ: `{name, ecu, version, region}` | §1 warning |
| `analysis.burndown` | level, flags[{severity,flag,detail}], rate, runway, runout, balance, `series.{months, implied_balance, commit_pace_balance, forecast_from}` | §3 |
| `analysis.projections` | trailing-year pace (`ecu_per_month`, `ecu_per_day`, `complete_months`, `usd_per_ecu`), `horizons` at 3 and 6 months (`as_of`, `ecu`, `balance`, `usd`), `forward` (next 6 months), year-pace `runway_days` / `runout_date` | §3 |
| `serverless_ballpark` | parent `full_split` totals + lines (directional) | §9 (collapsed) |
| `sizing` | triggered, consolidated.{observability,security,search}, folder, drive_folder, estimators.{observability,security,search}.{url,readback,region_match,status,local}. Search `readback.monthly_steady_state` is the es3-estimator performant total. | §10 |
| `consolidation` (ECH > 50 % only) | basis, ech_composition, serverless.by_solution (read-back totals; `by_region` when a solution spans regions; `ballpark` is unused), contract_rate (ARR and commit for reference; `implied_usd_per_ecu` null, `basis` `k360_cost`), savings.{ech_basis,ech_list_mo,serverless_mo,list_mo,list_yr,list_pct} (`ech_list_mo` is the ECH billed cost in the comparison), ech_scope.{scope,window_mo,priced_mo,unpriced_mo,serverless_mo,other_mo,in_comparison_mo,priced_share_pct,unpriced[],by_deployment_mo}, integrity.{score,passed,checks[]}, fragmentation.{n_deployments,n_with_gb,stored_gb,ech_ecu_mo,fixed_overhead_ecu_mo}, architecture.{groups,projects,per_region} (one project per solution per region), by_deployment[15].{name,solution,stored_gb,ech_ecu_mo,fixed_overhead_ecu_mo,overhead_pct,gb_known}, tail (ECU and GB only), sensitivity.{retention: raw_gb_day, fleet: stored_gb + ech_ecu_mo}, reconciliation.quotes[], callouts, caveats, notes | §11 (own canvas) |

| `k360_ingest` | Backup consolidation, present on the brief only when `--k360-ingest` ran beside a diagnostic quote: `savings`, `quotes`, `notes`, `contract_rate`. Not a second copy of `account.json`. | K360 backup canvas |
| `exports` | `account` / `consolidation` / `consolidation_k360` → `{title, html, pdf, drive_url, error}` | — |

`<key>_summary.json` (consolidation runs): `run_key`, `chat_bullets[≤6]`, `brief` (path), `links`, `estimators` (urls, read-backs), `savings`, `ingest_source`, `k360_ingest`. `<key>_brief.json` and `<key>_consolidation_brief.json` are what the skills read for chat and canvases. Never load `*_org_report.json` or `*_infra.json`.

Canvas file hints: `<customer-slug>-account-analysis.canvas.tsx` (§0–10) and `<customer-slug>-serverless-consolidation-savings.canvas.tsx` (§11, only when `consolidation` is present). Charts: version pie (`deployments.by_version`), monthly ECU bars (`ecu.monthly`), deployments-over-time line (`deployments.over_time.month`), solution pie (`solutions.totals`); §11: list comparison of ECH ECU vs read-back dollars, stacked ECH-vs-serverless build-up, deployment ECU bars (`by_deployment`), fleet ECU bars (`sensitivity.fleet`).
