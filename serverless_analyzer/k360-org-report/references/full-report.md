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

## Artifacts
- PDF/HTML: `<customer>_<date>_Full_report.{pdf,html}`
- Same estimators / Drive folder as exec

## Agent
Embed from `*_canvas.json` + `*_offload.json` only (still no full org JSON). Prefer the existing full-layout canvas pattern. Chat ≤5 bullets from `*_summary.json`.
