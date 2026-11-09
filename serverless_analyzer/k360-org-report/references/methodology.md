# Offload & canvas details (read only if needed)

## §10 Serverless savings
From `offload.full_split.lines` + totals:

| Project | Method |
|---------|--------|
| Observability + logging | Observability Complete (ingest+retention) |
| Security | Complete ladders (**proxy** until Security estimator linked) |
| Search | ES3 On-demand (search-only GB; storage-floored VCU + $0.047/GB) |

Totals: `full_ech_mo`, `serverless_total_mo`, `save_vs_ech_{mo,yr,3yr}`. Hybrid = o11y-only. **Never** recommend all-data on ES3 (`all_es3` anti-pattern).

## Kind routing
o11y / logs / security / search via family + name hints. Daily ingest ≈ `gb/(months×30.416)`.

## Workload fields
`search.peak|nonpeak` (`qps`, `complexity`, `pct_hours_mo`), `idle_pct_hours_mo`, `ingest.*`.

## Canvas section order
0 TOC+snapshot → 1 infra → 2 ECU → 3 deps → 4 storage → 5 burndown → 6 mix → 7 ML/AI → 8 workload → **9 recs** → **10 full_split**. Top-N only.
