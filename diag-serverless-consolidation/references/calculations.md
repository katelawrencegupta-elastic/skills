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

The sheet retention is always a whole month. Round `months` up, then clamp to 1–36. The estimator prices retention only on the integers 1–36, so a fractional month leaves that line at zero. Show both the measured months and the whole month written. Only rows with `retention_days` above 0 enter the weight. A row with no measured age is named and left out. If the rows that have bulk have no measured age, the month stays unset and the sheet is not filled.

Every consolidation run writes two PDFs from the same bulk.

| PDF | Sheet month | File |
|-----|-------------|------|
| Rounded | measured months, rounded up, clamped to 1–36 | `<key>_Serverless_consolidation_savings` |
| 30-day | 1, because 30 / 30.416 is under 1 | `<key>_Serverless_consolidation_30d` |

Each PDF links to its own estimator copy, left at that month. The rounded sheet is not rewritten to 1 after the 30-day read. If the measured ceiling is already 1, the two read-backs match. Still write both PDFs. The deployment-count bars are priced again at the scenario's month: measured months rounded up on the rounded PDF, and 1 month on every project on the 30-day PDF.

When the user sets a retention month, that whole month replaces the headline (`--retention-months`, G11 or G6). Bulk GB/day stays the measured rate. The measured months stay in the arithmetic next to the month written. The 30-day read stays at 1 month.

`retention_days` is the size-weighted ILM delete age, else data-stream retention, else the oldest index age, kept to 3 decimals. The weight uses local primary bytes only. Searchable-snapshot mounts (`partial-*`) and the snapshot portion of `total_data_set_size_in_bytes` stay out of store and out of that weight. `bulk_gb_day` is the sum of `index.primaries.bulk.est_bytes_per_day` on write indices, divided by 1e9. A write index is the last index of a data stream, or an index an alias marks `is_write_index`. For each of those indices the bytes per day are `primaries.bulk.total_size_in_bytes × 86,400 / write_phase_seconds`. The write phase is the time from creation until rollover, or the index age while the current ILM action is `rollover`. A missing write phase contributes 0. Rolled indices are not added. This is the bulk request size over the write window, not stored dataset growth and not node `total_size_in_bytes` divided by JVM uptime.

A row with `uptime_days_min` < 2 has a short node uptime. Keep it in the sum and name it. The bulk rate is still the write-phase estimate.

## Which estimator

| Solution | Inputs | Quote cell |
|----------|--------|------------|
| Observability | G21 logs = 0.90 × bulk_gb_day, G23 metrics = 0.10 × bulk_gb_day, G22 0%, G24 0, G25 100%, G11 months, G4 Complete, G5 Premium, G8 region, G14 No, G18 0 | D32 monthly, E32 year 1, F32 steady annual |
| Security | G4 = bulk_gb_day (all of it), G6 months, G7 region, G8 Complete, G21 Premium | E74 monthly |
| Search | disk = Σ local `store_gb`. Ingest docs/s is the K360 org rate × local search store ÷ org store. On one region, query rate stays the org window. A regional split scales query rate by that region's share of search store. es3 on-demand | `monthly_steady_state` |

The 90/10 logs/metrics split is only the Observability sheet. Security takes the full bulk rate on G4.

G26 on the Observability sheet displays as an integer. Read it unformatted. Accept it when it is within 0.51 GB/day of the bulk written to that sheet, including when local store is 0. A wider miss records both numbers and leaves that solution unquoted. The written bulk stays on the solution block.

Security E74 is the monthly quote only when parsed E73 is 0. A parenthesized credit stays negative. A non-zero E73 leaves Security out. E73 is not subtracted from E74. Add-on inputs are cleared on the copy. The golden template is not edited.

A region with bulk and no measured age has no month of its own. If another region supplied the solution month, that region is written at the solution month. A solution with no measured age anywhere is not written. One regional G26 miss, or a non-zero regional E73, leaves the whole solution out, including regions whose own read-back was accepted. Before the per-region sheets exist, a multi-region total splits the one sheet by local store. A region with bulk and no local bytes contributes $0 on that first pass.

## Per deployment

A deployment at or above 50 GB/day is its own project. Every deployment under 50 GB/day is one pooled project. 50 GB/day is the top of the first Observability ingest tier, so a deployment below that, priced alone, never leaves the top $/GB band. The pool is priced on its Σ bulk and bulk-weighted retention, which lets the combined ingest step into cheaper tiers.

## Deployment-count plan

For an active fleet of N deployments, price five Observability plans on each PDF. Each plan's dollar is the sum of D32. On the rounded PDF, retention on a project is that group's measured months, rounded up. On the 30-day PDF, every project is written as 1 month. Bulk does not change.

| Cut | Projects |
|-----|----------|
| 0% | N, one project per active deployment |
| 25% | round(N × 0.75) |
| 50% | round(N × 0.50) |
| 75% | round(N × 0.25) |
| 100% | 1. Reuses the headline Observability read-back. No new sheet. After a regional pass that headline is one sheet per region. |

Pack the middle three by local `store_gb`: sort descending, place each row in the lightest bin. 100% stays one project even though N × 0 is 0. `round` is half-to-even, so 75% of 2 deployments is 0 projects. On the rounded PDF, a project with bulk and no measured month omits the whole bar set. On the 30-day PDF, a project with bulk is written at month 1.

## Metered ingest rate

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

On-demand, performant, and high availability are priced with the es3 estimator on the sized stored GB, the measured query rate, and Premium support. High availability is the high-demand setting. Show monthly and steady annual (monthly × 12) for each. These dollars are not added to D32 or E74. When the quoted solution is Search, the quote stays the on-demand read-back.

Same formulas inside each project. Each project is its own estimator copy. The month written is the project's measured months rounded up. Each project also gets a 30-day read at 1 month on the same bulk. Sum of the read-backs is the split-fleet price, once at the measured month and once at 30 days. Compare each sum with the matching one-project read-back. The difference is the cost of staying split. A user-set retention month replaces the headline month on every project, including the pool, and does not change Σ bulk.

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
