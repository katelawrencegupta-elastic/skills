# Exec report (`--report exec`)

Shortened deliverable for leadership / quick reads.

## Canvas
File hint: `<org-slug>-exec-report.canvas.tsx`

| # | Section |
|---|---------|
| 0 | TOC + executive snapshot |
| 1–8 | **One** condensed environment summary (infra, ECU, deps, storage, burndown, mix, ML, workload) |
| 9 | Projected usage & recommendations (full) |
| 10 | Serverless `full_split` (full) |

## Numbers to use
- ECU / mo is `ecu_mo`, at the top level and on each item. It is lookback ECU ÷ (days ÷ 30.416), the divisor §10 uses.
- The snapshot says whether runout is before, after or on the contract end. Compare `burndown.runout_date` with `burndown.nearest_contract_end`.
- §10 totals cover `full_split.priced_n` deployments. State `not_priced_n` and `not_priced_ecu_mo` beside them.
- State `storage.stale_n` and `storage.not_billing_n` when either is above 0.
- Recommendations follow the lines: a line that saves is a move, the largest saving is named, and a line where Serverless costs more is kept on ECH.

## PDF
`render_org_pdf.py` prints the same facts: Cloud and Region as names, the fetch range as dates, ECU / mo and its divisor, the stale-storage and not-billing sentence, recommendations built from the data, the §10 table with a deployment count per line, and the "Not compared" sentence with names.

## Artifacts
- PDF/HTML: `<customer>_<date>_Exec_report.{pdf,html}`
- Same Observability estimator copy / Drive folder as full. The Search (ES3) estimator is not filled.

## Agent
Embed from `*_canvas.json` + `*_offload.json` only. Top-N tables. Chat ≤5 bullets from `*_summary.json`; the last bullet carries `caveats`.
