---
name: cloud-tracker-org-report
description: >-
  K360 org report parent: run exec (short) or full (expanded §1–10) via
  --report flag. Infra, storage, search/ingest, multi-project serverless
  savings, ECU/burndown, product mix, AI/ML. Triggers: org report, exec
  report, full report, cloud tracker, burndown, serverless savings, offload.
---

# Cloud Tracker Org Report (parent)

One skill, two deliverables — pick with **`--report`**.

| Flag | Deliverable | Canvas | PDF |
|------|-------------|--------|-----|
| `--report exec` (default) | Exec report | condensed §1–8 + §9–10 | `*_Exec_report.pdf` |
| `--report full` | Full report | expanded §1–10 | `*_Full_report.pdf` |

Details: [exec-report.md](references/exec-report.md) · [full-report.md](references/full-report.md)

## Hard rules (token / model / runs)
1. **Pass `--report exec|full` on every run** (default `exec` if omitted).
2. **One K360 fetch** — never re-query unless `fetch_org.py` fails. Prefer `--reuse`.
3. **Never `Read` the full `*_org_report.json`**. Use `*_canvas.json`, `*_offload.json`, `*_summary.json` only.
4. **One pipeline shell** — no mid-run skill exploration, no subagents, no TodoWrite.
5. **Never** fill Sheets via Chrome/AppleScript.
6. Chat **≤5 bullets** from `*_summary.json`. Methodology/setup refs only if blocked.

## Run
```bash
SCR=~/.cursor/skills/k360-org-report/scripts

# Exec (short)
python $SCR/run_org_report.py --org "ORG_NAME" --report exec

# Full (expanded)
python $SCR/run_org_report.py --org "ORG_NAME" --report full

# Same fetch, other flavor:
python $SCR/run_org_report.py --org "ORG_NAME" --report full --reuse /tmp/org_report.json
```

`.env`: `KIBANA_URL`, `ELASTIC_API_KEY`. Other flags: `--skip-pdf` · `--skip-push` · `--skip-workload` · `--days N`.

**Pipeline:** fetch → `artifacts/<customer>_<date>/` → slim JSON → estimators (Drive+xlsx) → HTML+PDF (named by `--report`) → Drive PDF upload.

## After the script (agent only)
1. Open the matching child doc (`exec-report.md` or `full-report.md`).
2. **Canvas** — use `canvas_file_hint` from the pipeline JSON / `*_summary.json`; embed slim JSON only.
3. **Chat** — ≤5 bullets from `*_summary.json` (+ folder / Drive URL).

## Artifacts (same folder for both)
`artifacts/<customer>_<date>/` holds Search/Observability estimators, chosen `*_Exec_report` or `*_Full_report` PDF, HTML, org JSON, canvas/offload/summary, `artifacts.json`. Both PDFs may coexist in the folder.

## §10 (required)
`offload.full_split` lines + totals; $/yr + 3yr; hybrid secondary; never recommend all-ES3. → [methodology.md](references/methodology.md)

## ML + vectors
One section from slim `ml` / `ai`. No invented model ids when only `vector_mappings`. Chat model ids are the SKU suffix; a chat SKU with no suffix has no model name. `quantity` is millions of tokens.
