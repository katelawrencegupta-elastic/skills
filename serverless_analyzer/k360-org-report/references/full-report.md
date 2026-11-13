# Full report (`--report full`)

Expanded deliverable — every TOC section is its own block.

## Canvas
File hint: `<org-slug>-full-report.canvas.tsx`

| # | Section |
|---|---------|
| 0 | TOC + executive snapshot |
| 1 | Cloud & instance footprint |
| 2 | ECU utilization (weekly & monthly) |
| 3 | Deployments — counts, versions, over time |
| 4 | Data storage usage & cost drivers |
| 5 | Burndown & runway |
| 6 | Product mix |
| 7 | ML, vectors & models |
| 8 | Workload (search / ingest) |
| 9 | Projected usage & recommendations |
| 10 | Serverless `full_split` |

## Numbers to use
- §1: provider and region come from the billing SKUs. An AWS region has no provider prefix (`us-west-2`); Azure and GCP regions are shown without theirs (`eastus2`, `us-central1`).
- §2: ECU / mo is `ecu_mo` (lookback ECU ÷ (days ÷ 30.416)). `monthly.by_deployment` is the top deployments' calendar-month series.
- §4: each storage row has its own `as_of`. List `storage.stale` and `storage.not_billing` when they are not empty.
- §5: say whether runout is before, after or on the contract end.
- §6: `families` sum to the org total.
- §7: `ml.vectors.model_counts` and `unnamed_model` are in the canvas JSON.
- §8: hour shares sum to 100. When `workload` shares are `null` the Search price used an assumed 26 / 74 / 0 mix; say it is assumed.
- §10: totals cover `full_split.priced_n` deployments. List `full_split.not_priced` (name, ECU/mo, reason) and state `not_priced_n` and `not_priced_ecu_mo`.
- `truncated` not empty: the org has more than 300 deployments in that aggregation and totals are low. Say so at the top.

## PDF
Same content as the exec PDF with 15 deployments in the table instead of 8.

## Artifacts
- PDF/HTML: `<customer>_<date>_Full_report.{pdf,html}`
- Same Observability estimator copy / Drive folder as exec. The Search (ES3) estimator is not filled.

## Agent
Embed from `*_canvas.json` + `*_offload.json` only (still no full org JSON). Prefer the existing full-layout canvas pattern. Chat ≤5 bullets from `*_summary.json`; the last bullet carries `caveats`.
