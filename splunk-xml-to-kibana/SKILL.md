---
name: splunk-xml-to-kibana
description: >-
  Migrates a Splunk dashboard (Classic Simple XML or Dashboard Studio JSON) into
  Elastic: Splunk-faithful HTML mockup (preserved under mockup/) → Kibana
  Dashboards API panels → data factories that populate every panel → MAPPING.md
  (SPL↔ES|QL + why) → deploy with user-supplied Kibana/ES URL and API key. Use
  when the user provides Splunk dashboard XML, asks to mock up a Splunk
  dashboard in Kibana/Elastic, recreate hunt / investigation dashboards, or
  wire Splunk panels to ES|QL + synthetic data factories.
---

# Splunk XML → Mockup → Kibana → Data Factories

Token-first migration. Prefer scripts/APIs over narrating. Do not paste large XML into chat—parse offline.

**Harness-agnostic.** Works in Cursor Agent and Claude Code. Customer-specific paths live in [`adapters/`](adapters/) — load only when relevant.

## Parameters (ask once per conversion)

| Param | Example | Meaning |
|-------|---------|---------|
| `USE_CASE` | `aws_user_investigation` | Slug for this dashboard package |
| `PACKAGE_ROOT` | `./elastic/<USE_CASE>/` | Where artifacts are written (relative to customer/project repo) |
| `STREAM` | `logs-<tenant>.<dataset>-*` | Primary ES data stream / index pattern for panels |
| `DASH_ID` | `acme-aws-user-investigation` | Kibana dashboard id |
| `DATA_FACTORIES` | path or empty | Shared factory package root if present; else use `templates/factories/` |
| `ADAPTER` | `adapters/blackstone.md` or none | Optional customer adapter (env path, naming, DIMA layout) |

Defaults if the user does not specify: `PACKAGE_ROOT=./elastic/<USE_CASE>/`, factories from this skill’s `templates/factories/`.

**Never** hard-code machine-absolute paths (`/Users/…`) in queries, scripts, or docs produced for a customer.

## Gate 0 — Credentials (required before any deploy)

**Ask once, early:**

1. Elastic/Kibana base URL (e.g. `https://….kb….elastic.cloud:443`)
2. API key (same key for ES + Kibana on serverless is fine)

Rules:

- Never invent, reuse another project's, or read `.env` for **destination** without explicit user OK.
- If user says use a project `.env`, confirm path; still echo which URL you will hit before deploy.
- Store for the session only; do not commit keys. Prefer env vars `KIBANA_URL` / `ELASTICSEARCH_URL` / `ELASTICSEARCH_API_KEY`.

```text
Need destination to deploy:
1) Kibana URL:
2) API key:
(Optional) Elasticsearch URL if different from kb→es rewrite:
```

Skip Gate 0 only for **mockup-only** work (no cluster writes).

## Workflow checklist

```
- [ ] 0 Resolve Parameters (+ optional ADAPTER)
- [ ] 1 Parse Splunk XML → panel inventory (scripts/parse_inventory.py)
- [ ] 2 HTML mockup site → PACKAGE_ROOT/mockup/ (required artifact)
- [ ] 3 Map panels → ES|QL + chart types
- [ ] 4 Data factories for every index/stream
- [ ] 5 Build Kibana dashboards via API (scripts/put_dashboard.py)
- [ ] 6 Write MAPPING.md (SPL ↔ ES|QL + why) — required
- [ ] 7 Prompt URL+key → deploy artifacts + data
- [ ] 8 Spot-check panels; --export if user edits UI
- [ ] 9 Rewire Vega ref_id panels if any (scripts/rewire_vega_refs.py)
```

## 1 — Parse Splunk (minimal tokens)

Run the bundled parser; return **only** a compact inventory:

```bash
python scripts/parse_inventory.py \
  --source path/to/source.xml \
  --out-dir "$PACKAGE_ROOT"
```

Writes `inventory.json` + `splunk_queries.json` under `PACKAGE_ROOT`.

| Field | Source |
|-------|--------|
| `kind` | Classic `<form>`/`<dashboard>` vs Studio `version="2"` + CDATA JSON |
| `title` | `<label>` / `definition.title` |
| `theme` | `theme=` |
| `tabs[]` | Studio `layout.tabs.items` **or** Classic rows as single page |
| `inputs[]` | token, type, default |
| `panels[]` | tab, title, viz type (table/bar/line/single/punchcard/…), search id / query tail |

Do **not** dump full SPL into the reply.

## 2 — Mockup (required artifact)

Build a **Splunk-faithful HTML mockup site** under `PACKAGE_ROOT/mockup/`.
Start from [`templates/mockup/`](templates/mockup/) if helpful.

| Path | Purpose |
|------|---------|
| `mockup/index.html` | Entry point (open in browser; tab nav if multi-tab) |
| `mockup/tabs/<slug>.html` | Optional per-tab pages when Studio tabs exist |
| `mockup/assets/` | Optional shared CSS/JS (prefer inline CSS) |
| `mockup/README.md` | How to open |

Rules:

- Match Splunk chrome (Classic dark vs Studio light).
- Show **real chart affordances** (bars/lines/KPIs), not tables-only.
- Cover **every panel** from the inventory (same titles / layout intent).
- **Control strip checklist:** same labels and control types as Splunk `inputs[]`.
- Self-contained: no build step; works via `file://` or a static file server.
- Do **not** delete `mockup/` when building Kibana JSON or deploying.
- Link `mockup/` from `ANALYSIS.md` and `MAPPING.md` § Links.

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
| Form **inputs** | Dashboard **Controls** (`pinned_panels`) bound to ES\|QL `?params` |
| Custom graph / flow viz | Prefer **Vega** — never silently collapse |

### Inputs → Controls (hard rule)

Every Splunk `input` must appear in an **Inputs → Controls matrix** in `MAPPING.md`:

1. Dashboard **time picker** (`?_tstart` / `?_tend`)
2. Options list / range / **ES|QL variable Control** (`pinned_panels` + `?name`)
3. Explicit **GAP** with user-visible markdown (not a silent hard-coded demo value)

**Ban:** documenting the token in markdown and hard-coding the demo value in every `WHERE`.

```esql
FROM logs-*
| WHERE source.ip == TO_IP(?target_ip)
| WHERE TO_STRING(?aws_account) == "*" OR cloud.account.id == TO_STRING(?aws_account)
| WHERE @timestamp <= ?_tend AND @timestamp > ?_tstart
```

Cast Control values with `TO_STRING` / `TO_IP` when the field is `keyword` / `ip`.

### Custom viz → Vega (hard rule)

| Splunk `custom_viz` / intent | Elastic target (P0) | Allowed fallback (label **GAP**) |
|---|---|---|
| `sankey_diagram*` / flow quantities | **Vega Sankey** (saved viz + `config.ref_id`) | heatmap or path `xy` + GAP |
| `force_directed*` / node-link | **`heatmap`** (src×dest count) + edge `data_table` | edge table only + GAP |
| `*3d_graph*` / GraphLabelPropagation | Vega network by community **or** community table | table-only + GAP |
| GraphCentrality / weighted ARNs | `xy` bar by weight (OK) | — |

**Force-directed → heatmap:** Do **not** attempt Kibana Vega `force` layouts (see [`BUGS.md`](BUGS.md)).

**Do not** silently collapse **Sankey** to table/heatmap — Vega first; label fallbacks as GAP.

Embed Vega via Visualize saved object + dashboard `ref_id`, then **rewire** lens → `visualization` SO refs:

```bash
python scripts/rewire_vega_refs.py \
  --dashboard-id "$DASH_ID" \
  --map path/to/vega_panel_map.json
```

**Vega hard constraints:**

1. **No dotted field paths in Vega transforms/encodes** — alias flat in ES\|QL (`community = cloud.account.name`) or `datum['source.ip']`.
2. **Do not use Vega `force` for `force_directed*`** — default to heatmap.
3. After every Dashboards API PUT with Vega `ref_id` panels, run the rewire script.

**Forbidden:** Lens-only `lnsDatatable` NDJSON with `gridData.tabId` / attributes `tabs`.

Deploy:

```bash
python scripts/put_dashboard.py \
  --kibana-url "$KIBANA_URL" \
  --api-key "$ELASTICSEARCH_API_KEY" \
  --body path/to/dashboard.json
```

Or:

```http
PUT {KIBANA}/api/dashboards/{id}
Elastic-Api-Version: 2023-10-31
kbn-xsrf: true
```

Body: `{ title, description?, time_range, panels[], pinned_panels? }`. Details: [`reference.md`](reference.md).

After user UI edits: `GET` each id → write `kibana/*_dashboards.json` with `"source":"exported_from_kibana"`.

## 4 — Data factories

For **each** panel query, list required data:

- data stream / index / lookup
- minimal ECS (or tenant-prefixed) fields the ES\|QL references
- volume hint

**Graph / custom-viz support fields** (emit before panel build):

| Splunk custom_viz | Required factory fields (examples) |
|---|---|
| `sankey_diagram*` | Edge endpoints + count |
| `force_directed*` | Same edges + optional size/color dims |
| `*3d_graph*` / LabelPropagation | Community / color field |
| GraphCentrality | Weight field on entity docs |

Implementation order:

1. Prefer shared **`DATA_FACTORIES`** package if the project has one (domain modules + thin wrappers under `PACKAGE_ROOT/data_factories/`).
2. Else copy/adapt [`templates/factories/minimal_ndjson.py`](templates/factories/minimal_ndjson.py).
3. Setup/bulk push to destination ES (Gate 0).
4. Lookups: `index.mode=lookup` when `LOOKUP JOIN` needed.
5. Catalog optional: `factories.yaml` listing P0 factories per dashboard. **Every panel needs a P0 producer** or an explicit “empty until real data” note.

Validate: run each panel ES\|QL via `POST {ES}/_query` before declaring done.

## 5 — MAPPING.md (required deliverable)

Write `PACKAGE_ROOT/MAPPING.md` from [`templates/MAPPING.md`](templates/MAPPING.md). Do **not** dump it into chat—link the path.

**Must include:**

1. Header — Splunk app/id, Elastic dashboard id, demo tokens, package + mockup paths  
2. Audit/DIMA JSON used (or “N/A — raw XML only”)  
3. Data factories — scripts, NDJSON → streams → panels  
4. **Inputs → Controls matrix** (required)  
5. Knowledge objects — lookups, macros, datamodels → streams  
6. Panel-by-panel — SPL, ES\|QL, viz, why  
7. Data layout  
8. Gaps / non-goals  
9. Links — mockup, Kibana URL, `splunk_queries.json`

Keep `ANALYSIS.md` short; point at `MAPPING.md`.

## 6 — Package layout

```
PACKAGE_ROOT/                      # e.g. ./elastic/<use_case>/
  ANALYSIS.md
  MAPPING.md
  manifest.json
  splunk_queries.json
  inventory.json
  source.xml                       # or Studio JSON
  mockup/
    index.html
    tabs/                          # optional
    README.md
  kibana/<id>.json
  kibana/*_dashboards.json
  data_factories/                  # wrappers + local out/
  setup_*.py                       # deploy lookups/data/dashboards
```

Scaffold:

```bash
python scripts/scaffold_use_case.py --use-case "$USE_CASE" --out ./elastic
```

## 7 — Deploy sequence

1. Confirm Gate 0 URL+key (echo destination)
2. Templates / data streams / lookups
3. Bulk factory data
4. Create/update Vega saved visualizations when needed, then PUT dashboard (include `pinned_panels`)
5. Rewire Vega `ref_id` panels (`scripts/rewire_vega_refs.py`)
6. Ensure `MAPPING.md` matches deployed ES\|QL + Controls
7. Print open URLs + factory → panel map + paths to `MAPPING.md` and `mockup/index.html`
8. **Spot-check:**
   - [ ] Every Splunk input → working Control or documented GAP
   - [ ] Changing Controls changes panel queries (no leftover hard-coded demos)
   - [ ] Sankey is visual flow (Vega) unless GAP
   - [ ] Force-directed is heatmap + edge table
   - [ ] Vega panels render (no “visualization type not found” / dotted-field errors)

## Anti-patterns

- Deploying without asking for destination credentials
- Chat-only mockup with no files under `PACKAGE_ROOT/mockup/`
- Deleting the Splunk mockup after Kibana deploy
- One mega-dashboard with overlapping `y` from fake tabs
- Committing `.env`, API keys, or real customer PII samples
- Baking customer-absolute paths into the shared skill
- Silent Sankey → heatmap/table collapse without GAP + Vega-first attempt
- Attempting Kibana Vega `force` for Splunk `force_directed*`
- Hard-coding Splunk input defaults into every `WHERE` instead of Controls
- Leaving Dashboards API `ref_id` as lens refs for classic Vega visualizations
- Feeding dotted ECS paths into Vega aggregate/encode field lists

## Adapters

Customer-specific env paths, naming, and DIMA layouts:

| Adapter | When to load |
|---------|----------------|
| [`adapters/blackstone.md`](adapters/blackstone.md) | Blackstone POC / `customers/blackstone/` |

If no adapter applies, use Parameters only.

## References

- Chart/API schemas, factories, MAPPING template: [`reference.md`](reference.md)
- Known platform bugs: [`BUGS.md`](BUGS.md)
- Scripts: [`scripts/`](scripts/)
- Templates: [`templates/`](templates/)
