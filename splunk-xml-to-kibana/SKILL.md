---
name: splunk-xml-to-kibana
description: >-
  Migrates a Splunk dashboard (Classic Simple XML or Dashboard Studio JSON) into
  Elastic: Splunk-faithful HTML mockup → Kibana Dashboards API panels → data
  factories that populate every panel → MAPPING.md (SPL↔ES|QL + why) → deploy
  with user-supplied Kibana/ES URL and API key. Use when the user provides Splunk
  dashboard XML, asks to mock up a Splunk dashboard in Kibana/Elastic, recreate
  Employee Safety / hunt dashboards, or wire Splunk panels to ES|QL + synthetic
  data factories.
---

# Splunk XML → Mockup → Kibana → Data Factories

Token-first migration. Prefer scripts/APIs over narrating. Do not paste large XML into chat—parse offline.

## Gate 0 — Credentials (required before any deploy)

**Ask once, early:**

1. Elastic/Kibana base URL (e.g. `https://….kb….elastic.cloud:443`)
2. API key (same key for ES + Kibana on serverless is fine)

Rules:
- Never invent, reuse another project's, or read `.env` for **destination** without explicit user OK.
- If user says use project `.env`, confirm path; still echo which URL you will hit before deploy.
- Store for the session only; do not commit keys. Prefer env vars `KIBANA_URL` / `ELASTICSEARCH_URL` / `ELASTICSEARCH_API_KEY` set by user for the run.

```text
Need destination to deploy:
1) Kibana URL:
2) API key:
(Optional) Elasticsearch URL if different from kb→es rewrite:
```

Skip Gate 0 only for **mockup-only** work (no cluster writes).

## Workflow checklist

```
- [ ] 1 Parse Splunk XML → panel inventory
- [ ] 2 HTML mockup (visualize skill)
- [ ] 3 Map panels → ES|QL + chart types
- [ ] 4 Data factories for every index/stream
- [ ] 5 Build Kibana dashboards via API
- [ ] 6 Write MAPPING.md (SPL ↔ ES|QL + why) — required
- [ ] 7 Prompt URL+key → deploy artifacts + data
- [ ] 8 Spot-check panels; --export if user edits UI
```

## 1 — Parse Splunk (minimal tokens)

Run a local script; return **only** a compact inventory:

| Field | Source |
|-------|--------|
| `kind` | Classic `<form>`/`<dashboard>` vs Studio `version="2"` + CDATA JSON |
| `title` | `<label>` / `definition.title` |
| `theme` | `theme=` |
| `tabs[]` | Studio `layout.tabs.items` **or** Classic rows as single page |
| `inputs[]` | token, type, default |
| `panels[]` | tab, title, viz type (table/bar/line/single/punchcard/…), search id / query tail |

Do **not** dump full SPL into the reply. Keep queries in `elastic/<use_case>/splunk_queries.json` or similar.

## 2 — Mockup

Read `/visualize` skill → `references/mockups.md` + foundations.

- Match Splunk chrome (Classic dark vs Studio light).
- Show **real chart affordances** (bars/lines/KPIs), not tables-only.
- Illustrative data OK; label as such only if asked.
- Emit visualization content-reference tag; keep prose ≤3 sentences.

## 3 — Elastic panel map

Hard constraints (Kibana **9.4+** Dashboards API, serverless included):

| Splunk | Elastic |
|--------|---------|
| Dashboard Studio **tabs** | **No** native tabs. One dashboard per tab + `links` panel (`dashboardLink`) nav |
| `theme` | Ignore for API; optional markdown note |
| table | `vis` `data_table` ES\|QL |
| column/bar | `vis` `xy` layer `bar` / `bar_horizontal` / `*_stacked` |
| line/area | `vis` `xy` layer `line` / `area` |
| single value | `vis` `metric` |
| radial / gauge | `vis` `gauge` |
| punchcard | `vis` `heatmap` |
| markdown/html | `markdown` |

**Forbidden:** Lens-only `lnsDatatable` NDJSON with `gridData.tabId` / attributes `tabs` — API rejects or UI ignores; yields tables-only.

Deploy with:

```http
PUT {KIBANA}/api/dashboards/{id}
Elastic-Api-Version: 2023-10-31
kbn-xsrf: true
```

Body: `{ title, description?, time_range, panels[] }` — inline `config` (prefer over `ref_id`). Details: [reference.md](reference.md).

After user UI edits: `GET` each id → write `kibana/*_dashboards.json` with `"source":"exported_from_kibana"`. Redeploy from that file; do not regenerate from templates unless asked.

## 4 — Data factories

For **each** panel query, list required data:

- data stream / index / lookup
- minimal ECS (or `bx.*`) fields the ES\|QL references
- volume hint (rows for tables; cardinality for charts)

Then implement or extend factories under `elastic/<use_case>/data_factories/`:

1. Generator writes NDJSON → `out/`
2. Setup/bulk push to destination ES (Gate 0 URL+key)
3. Lookups: `index.mode=lookup` when `LOOKUP JOIN` needed
4. Presence/entity rollups: transform **or** factory-built metrics stream if transform is overkill for POC

Wire `factories.yaml` (or use-case manifest) so each dashboard id lists P0 factories. **Every panel must have a P0 producer** or an explicit “empty until real data” note to the user.

Validate: run each panel ES\|QL via `POST {ES}/_query` before declaring done.

## 5 — MAPPING.md (required deliverable)

After panels and factories exist (before or right after deploy), write
`elastic/<use_case>/MAPPING.md`. Do **not** dump this into chat—link the path.

**Must include:**

1. **Header** — Splunk app/id, Elastic dashboard id, demo tokens, package path  
2. **DIMA / audit JSON used** — which files under the Splunk/DIMA export (e.g. `dashboard_to_migrate.json`, `macros.json`, `lookup_samples.json`) were used, consulted-only, or unused — and for what  
3. **Data factories** — generator script(s), setup/deploy script, NDJSON outputs → ES indices/streams → panels; note shared factories **not** used  
4. **Inputs** — Splunk tokens → Elastic time/filters (and why)  
5. **Knowledge objects** — lookups, macros (expand from `macros.json` when present), datamodels → streams/indexes + why  
6. **Panel-by-panel** — for each panel: original **SPL** (from `splunk_queries.json`), **ES|QL**, viz type, **why** (incl. stand-ins and omissions)  
7. **Data layout** — which stream/index/lookup feeds which panel (may cross-ref factories section)  
8. **Gaps / non-goals** — placeholders skipped, macros missing, map→bar, etc.

Keep `ANALYSIS.md` short; point it at `MAPPING.md` for full SPL rationale.  
Template & example: [reference.md](reference.md#mappingmd-template) · worked example `elastic/hdsi_user_investigation/MAPPING.md`.

## 6 — Package layout

```
elastic/<use_case>/
  ANALYSIS.md              # short: Splunk source → Elastic map (+ link MAPPING.md)
  MAPPING.md               # required: SPL ↔ ES|QL + why per panel
  manifest.json
  splunk_queries.json      # full SPL offline
  macros_resolved.json     # optional when macros used
  kibana/<id>.json         # per dashboard (exported or built)
  kibana/*_dashboards.json # bundle + source stamp
  data_factories/
  esql/                    # optional panel query catalog
  setup_*.py               # deploy lookups/data/dashboards
```

## 7 — Deploy sequence

1. Confirm Gate 0 URL+key
2. Templates / data streams / lookups
3. Bulk factory data
4. `PUT /api/dashboards/{id}` for each tab dashboard
5. Ensure `MAPPING.md` is written/updated to match deployed ES|QL
6. Print open URLs + which factory feeds which panel + path to `MAPPING.md`

## Anti-patterns

- Deploying without asking for destination credentials
- Classic Simple XML paste-as-dashboard when content is hunts → prefer saved ES\|QL / Timeline
- One mega-dashboard with overlapping `y` from fake tabs
- Committing `.env`, API keys, or real customer PII samples

## References

- Chart/API schemas, factories, **MAPPING.md template**: [reference.md](reference.md)
- Mapping example: repo `elastic/hdsi_user_investigation/MAPPING.md`
- Blackstone Employee Safety: repo `elastic/employee_safety/` + `build_tabbed_dashboard.py`
