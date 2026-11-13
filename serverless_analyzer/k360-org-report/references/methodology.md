# Offload & canvas details (read only if needed)

## §10 Serverless savings
From `offload.full_split.lines` + totals:

| Project | Method |
|---------|--------|
| Observability + logging | Observability Complete (ingest+retention) |
| Security | Complete ladders (**proxy** until Security estimator linked) |
| Search | ES3 On-demand (search-only GB; storage-floored VCU + $0.047/GB) |

Totals: `full_ech_mo`, `serverless_total_mo`, `save_vs_ech_{mo,yr,3yr}`. Hybrid = o11y-only. **Never** recommend all-data on ES3 (`all_es3` anti-pattern).

### What is compared
A deployment is compared when it runs on ECH and has stored GB. Every other deployment is left off both sides and named in `full_split.not_priced` with `ecu_mo` and a reason:

| Reason | Meaning |
|--------|---------|
| `no stored GB in telemetry` | No storage snapshot, or a snapshot of 0 GB. There is nothing to price. |
| `already serverless` | The deployment bills as a Serverless project. It is not an ECH cost to move. |
| `no bill in the last 90 days of the range` | It has a snapshot and stopped billing. Its GB is not in org storage. |

`full_ech_mo` is the ECH bill of the `priced_n` compared deployments. `org_ech_mo` is the whole org. `not_priced_n`, `not_priced_ecu_mo` and `not_priced_by_reason` total what was left out. The saving, $/yr and 3-yr are computed on the compared deployments only. An org with no stored GB shows a saving of 0 and every deployment in `not_priced`. No deployment is priced at $0.

The summary, the canvas and the PDF say how many deployments and how much ECU/mo were not compared. The PDF lists the first 12 by ECU.

### ECH dollars
ECH $/mo is the K360 billing `cost` field as billed: lookback ECU ÷ (`--days` ÷ 30.416). 1 ECU is $1. No ARR ÷ commit rate is applied. The report's own "ECU / mo" (`totals.ecu_mo`) uses the same divisor, so `full_ech_mo + not_priced_ecu_mo = totals.ecu_mo`.

## Kind routing
security / o11y / logs / search via family + name hints, tested in that order. Security is first: a deployment whose latest use case is security carries its ECU in `families.security`. Daily ingest ≈ `gb/(months×30.416)`.

## Solution families
`families` splits each deployment's ECU by billing `type` into search, observability, genai and other. ECU that no listed type explains is other. A deployment whose use case is security moves its search, observability and other ECU to security; inference stays in genai. The five families sum to the deployment's ECU and to the org total.

## Storage
Storage is the newest `cloud-tracker-telemetry` snapshot of each deployment. Each row keeps its own snapshot day. A snapshot more than 45 days older than the range end is listed in `storage.stale` and still counts. A deployment with no bill in the last 90 days of the range is listed in `storage.not_billing` and is left out of stored GB, docs and `by_deployment`. The summary and the PDF name both conditions.

The Search share of stored GB is `offload.search_share` = search GB ÷ `storage.data_stored_gb`. That one share scales the Search document count and the ingest docs/sec, in the Python price and in the estimator cells.

## Workload fields
`search.peak|nonpeak` (`qps`, `complexity`, `pct_hours_mo`), `idle_pct_hours_mo`, `ingest.*`.

Hour shares are day shares. A day at or below 1 QPS and at or below the 10th percentile is idle. Of the other days, those at or above the 75th percentile are peak. The rest are non-peak. Each share is 0–100 and the three sum to 100.

With `--skip-workload`, or with no index telemetry, the shares are not measured and stay `null` in `workload`. The Search price then assumes 26% peak / 74% non-peak / 0% idle and 730 ingest hours. The estimator cells carry the same values, and `apps_script_payload.es3.hour_mix.assumed` is `true`.

## Estimator fills
- Every cell in a payload has a value. A value that was not measured is 0 and its cell is in `unmeasured_cells`. An assumed value is in `assumed_cells`. `push_estimator.py` writes a blank for a cell with no value, so a template sample never stays in a cell the fill covers.
- The region is not a fixed cell value. `cloud` carries the org's provider and region from the billing SKUs. The fill reads the copy's `Estimator_Reference` options and writes the one for that provider and region (`region_match`: `exact` or `alias`, e.g. `eastus2` ⇄ `us-east-2`). With no such option the cell is not written, the copy keeps the template's value, and `region_match` is `none`. Another cloud's region is never written.
- Observability: the copy is checked against `estimator_layout.py` (labels and version) before any write. A mismatch trashes the copy and stops.
- Search (ES3): `estimator_layout.py` records no ES3 layout, so the fill is refused. `--kind both` fills Observability and reports that Search was not filled. `--kind es3` stops. Recording `TEMPLATE_ANCHORS["es3"]` turns the fill on, and the copy is then checked the same way.
- `--no-copy` fills a sheet named with `--o11y-id` / `--es3-id`. A golden template id, or an id set in `ES3_SHEET_ID` / `O11Y_SHEET_ID` / `SECURITY_SHEET_ID`, is refused before any Google call.
- The Apps Script importer checks the block's `layout` tag against the workbook's `Version Control` tab and stops on a mismatch or a missing tag. It refuses a golden template.

## Canvas section order
0 TOC+snapshot → 1 infra → 2 ECU → 3 deps → 4 storage → 5 burndown → 6 mix → 7 ML/AI → 8 workload → **9 recs** → **10 full_split**. Top-N only.
