# Reference — MAPPING.md

Load at R6. Prefer `scripts/render_mapping.py` then edit Why/GAP only. Do not paste full MAPPING into chat.

Write `PACKAGE_ROOT/MAPPING.md`. Source SPL from `splunk_queries.json` (offline). Template: `templates/MAPPING.md`.

## Required sections

1. Header — Splunk app/id, Elastic dashboard id, demo tokens, package + mockup paths
2. Audit/DIMA JSON used (or N/A — raw XML only)
3. Data factories — scripts, NDJSON → streams → panels
4. **Inputs → Controls matrix** (required)
5. Knowledge objects — lookups, macros, datamodels → streams
6. Panel-by-panel — SPL, ES|QL, viz, why (script can stub; agent fills non-obvious Why)
7. Data layout
8. Gaps / non-goals
9. Links — mockup, Kibana URL, `splunk_queries.json`

Keep `ANALYSIS.md` short; point at `MAPPING.md`.

## Export / promote UI edits

```bash
python scripts/put_dashboard.py --export --id DASH_ID --out kibana/DASH_ID.exported.json
```
