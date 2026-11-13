# Analysis heuristics (`analyze.py`)

All rules are evidence-gated; each list is sorted high → low severity and capped at 5. Tune constants at the top of `analyze.py`.

## Platform / solution classification
- Platform per deployment from parent `fetch_org.py` (`serverless.*` SKU → serverless, else ECH). Serverless **projects** bill under 32-char hex ids which the parent drops → recovered by `k360_infra.py` (`sku: serverless.*`, `deployment.template` = security | observability | elasticsearch).
- Solution per deployment: serverless template → `use_case` (features index) → billing family (> 50 % observability types; any security) → name hints → default `search`. `solutions.method_counts` shows how much came from each rule.

## Versions
- `reference_version` = newest version adopted by ≥ max(2, 10 %) of deployments (a single canary doesn't flag the fleet).
- Lagging: major < reference, or ≥ 3 minors behind within the same major, or ≤ 7.x (EOL).

## Deployments over time
- Monthly distinct deployments with ECU > 0 (ECH, named). Partial months excluded.
- Compare avg of first k vs last k complete months (k = min(3, n/2)). `|Δ| ≥ 20 %` and `|Δ| ≥ 3` → **significant**; `≥ 10 %` → moderate; else stable. Also records peak month and largest month-over-month change.

## ECU
- Monthly totals = Σ named deployments + serverless projects per month. Peak/low exclude partial months.
- Daily avg = Σ daily / days (trailing zero days trimmed). Trend = last-30-day avg vs lookback avg: > +15 % accelerating, < −15 % decelerating.
- The run looks back 365 days. `analysis.projections` holds the next 3 and 6 months at the complete-month average of that year (partial months excluded). Remaining balance subtracts that ECU from the current balance. `usd` is projected ECU × ARR ÷ (commit ECU/day × 365), and is omitted when ARR or the commit pace is missing. This pace is separate from the last-30-day burndown rate.

## Performance recs (candidates)
| Rule | Severity |
|------|----------|
| Lagging versions (EOL major → high) | medium/high |
| Peak search complexity High (≥ 200 ms) / Medium at > 1 000 QPS | high / medium |
| Avg doc < 0.3 KB (TSDS / LogsDB) · > 4 KB (mapping review) | medium / low |
| ≥ 5 TB and no warm/cold/frozen SKUs | high |
| One deployment ≥ 60 % of storage (and > 3 deps) | medium |
| ≥ 3 hot instance families | low |
| Any deployment billed in 1 AZ (medium if ≥ 3 deployments or ≥ 10 % of capacity ECU) | medium/low |
| ML nodes billed with no ELSER/vectors/EIS models | low |
| Ingest active < 400 h/month | low |

## Cost recs (candidates)
| Rule | Severity |
|------|----------|
| Parent `full_split` saves > 0 → move **dominant solution** to its serverless project (ballpark only; quote from estimators) | high |
| `data-transfer-inter-node` ≥ 5 % of spend (≥ 10 % → high) | medium/high |
| `data-transfer-out` ≥ 3 % | medium |
| ≥ 2 TB stored and frozen < 5 % of capacity ECU (≥ 20 TB → high) | medium/high |
| ≥ 8 deployments each < 1 % of spend → consolidate | medium |
| Near-idle deployments (active ≥ 2 months, < 0.2 % of org monthly) | low |
| ML node spend ≥ 5 % of a deployment with no detected ML usage | medium |
| Lagging versions + ≥ 1 TB → LogsDB/TSDS savings | medium |
| snapshot-storage ≥ 2 % of spend | low |
| Chat input ≥ 10 ECU: keep half the prompt tokens, then reprice remaining above-Haiku input at the Haiku rate (1.50 ECU/million, or the Haiku rate already on the bill). Kept even when the other recs fill the list. | medium if the saving is ≥ 20 ECU, else low |

## Burndown flags
- K360 `level` passed through (`PRIORITY: OVERAGE` / `WATCH` / `OK`); `inf`/missing commit pace → `WATCH (no commit pace)`.
- Flags: rate > 1 (high) · rate < 0.6 under-consumption (medium) · runout before contract end (high) · runway < 90 d (high if < 45) · consumption accelerating/decelerating · peak month > 1.5× avg · significant deployment-count change.

## Consolidation savings (`consolidation.py`, ECH > 50 % only)

Owned by `diag-serverless-consolidation`. The quote scripts, including `consolidation.py`, `size_serverless.py`, and `admin_diags.py`, live in that skill. The account-summary skill does not run this step.
- Dollars: ECH dollars are the K360 billing `cost` field as billed. That field is the ECU total, and 1 ECU is $1. No ARR ÷ (commit ECU/day × 365) rate is applied. Serverless dollars are estimator read-backs only — es3-estimator performant for Search, the sheet for Observability and Security. Serverless is not discounted.
- The quote window is `--lookback-days`, default 90, maximum 365. Quote ECH monthly ECU is that window's daily average × 30.416, narrowed to the deployments in the comparison (`ech_scope`). Estimator GB/day is diagnostic bulk as measured; the window does not scale it. `ecu.monthly_avg_complete_months` scales the other fleet rows and is not the quote. The account fetch stays the trailing year.
- `ech_composition` from `infra.by_deployment.tiers`: fixed = master / kibana / coordinating / integrations server / enterprise search / apm; `ml`; `data*` tiers; non-capacity from `cloud.non_capacity_ecu`.
- `contract_rate` carries ARR and commit for reference, with `implied_usd_per_ecu` null and `basis` `k360_cost`. `savings.list_*` is the ECH billed cost minus serverless at estimator list. Serverless is not discounted.
- `fragmentation`: deployment count, how many have a size row, stored GB, ECU/mo, and fixed-overhead ECU/mo for the active deployments on the quote window. `fixed_overhead_pct` is that overhead ÷ their ECU. `ech_composition.fixed_overhead_mo` is the full lookback and includes deployments with no bill in the quote window. No per-deployment serverless price.
- `tail`: deployments < 1 % of lookback ECU. `ech_ecu_mo` is their whole bill. `fixed_overhead_ecu_mo` is their fixed overhead. The merge saving is 25 % of their bill. No serverless price for the tail.
- `sensitivity.retention`: stored GB ÷ 1 / 3 / 6 / 12 months. On a diagnostic quote this is not the bulk rate and does not reprice the estimate. `sensitivity.fleet`: the first row is the quote lookback and matches the estimator. The complete-month average and the quietest month scale that read-back. Those rows are not re-estimated.
- `reconciliation.quotes`: the read-backs that sum to `savings.serverless_mo`. When a solution spans regions, one quote per region.
- When a solution's deployments span more than one region, the estimate is one serverless project per region, and each project has its own sizing named by region. Search is re-estimated with es3 in that region. Observability and Security are a sheet filled for that region. The report links each sizing. The headline is the sum of those read-backs. One region keeps the single read-back.
- When Search deployments bill jina embeddings, the ML-node ECU on those deployments is included in the EIS line at list, along with inference tokens billed on those Search deployments. Tokens billed on Observability or Security stay in the §7 token meter and are not added to Search. ML nodes on deployments with no jina SKU are not included. The amount stays in the Search total, the regional projects, every profile, and every fleet row. Fleet scenarios scale the rest of the estimate and leave the EIS line unchanged.
- `callouts`: largest deployment (ECU, GB, overhead %), fixed overhead on the active deployments with the full-lookback figure named when it differs, and the small deployments' bill rather than a second overhead rate. `search_power` is present only when Search was estimated.
- The first cost recommendation is rewritten to the billed-cost comparison once the read-backs exist. When serverless is estimated to cost more, it is retitled, set to low severity, and says not to position the move as a saving.
- The quoted document uses ingest and retention from admin-console Elasticsearch bundles (`admin_diags.py`). Bulk GB/day is the write-index sum of index.primaries.bulk.est_bytes_per_day. Retention is the size-weighted ILM delete age, else data-stream retention, else the oldest index age, then ingest-weighted up to the solution and the region. Store size replaces K360 stored GB on matched deployments. Search query shape and ECH ECU stay on the K360 bill. Retention written to a sheet is rounded up to a whole month and clamped to 1–36. A deployment with no bundle keeps the K360 size and the assumed retention. K360 does not provide a raw ingest total, so that fallback is stored GB ÷ assumed retention and can sit far below measured bulk. `--k360-ingest` also writes `<key>_full retention_k360_serverless_consolidation_plan` for the whole fleet on that backup. Estimator titles for the backup are suffixed `_from_k360`. When no bundles are available, the quoted document is that backup and the notes say so.

## Units
ECH dollars are the K360 billed cost: ECU at $1. Only estimator read-backs are quoted as serverless $.
