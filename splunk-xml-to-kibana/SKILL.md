---
name: splunk-xml-to-kibana
description: >-
  Migrates a Splunk dashboard (Classic Simple XML or Dashboard Studio JSON) into
  Elastic: Splunk-faithful HTML mockup (preserved under mockup/) → Kibana
  Dashboards API panels → data factories that populate every panel → MAPPING.md
  (SPL↔ES|QL + why) → deploy with user-supplied Kibana/ES URL and API key. Use
  when the user provides Splunk dashboard XML, asks to mock up a Splunk
  dashboard in Kibana/Elastic, recreate Employee Safety / hunt dashboards, or
  wire Splunk panels to ES|QL + synthetic data factories.
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
- [ ] 2 HTML mockup site → elastic/<use_case>/mockup/ (required artifact)
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

## 2 — Mockup (required artifact)

Build a **Splunk-faithful HTML mockup site** and **keep it on disk** as part of the
conversion package. Chat-only previews are optional extras — they do **not**
replace the durable files.

**Write to:** `elastic/<use_case>/mockup/`

| Path | Purpose |
|------|---------|
| `mockup/index.html` | Entry point (open in browser; tab nav if multi-tab) |
| `mockup/tabs/<slug>.html` | Optional per-tab pages when Studio tabs exist |
| `mockup/assets/` | Optional shared CSS/JS/images (prefer inline CSS for portability) |
| `mockup/README.md` | One-liner: how to open (`open index.html` / local static server) |

Rules:

- Match Splunk chrome (Classic dark vs Studio light).
- Show **real chart affordances** (bars/lines/KPIs), not tables-only.
- Cover **every panel** from the inventory (same titles / layout intent).
- **Control strip checklist:** the mockup top strip must show the **same labels and
  control types** as Splunk `inputs[]` (text / dropdown / radio / time). Do not
  omit pivots that will become Kibana Controls.
- Illustrative data OK; label as such only if asked.
- Self-contained: no build step; works via `file://` or a static file server.
- Do **not** delete or overwrite `mockup/` when building Kibana JSON or deploying.
- Optional: also emit a `/visualize` content-reference in chat; keep prose ≤3 sentences.
- Link `mockup/` from `ANALYSIS.md` and from `MAPPING.md` § Links.

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
| Form **inputs** (text/dropdown/radio/multiselect) | Dashboard **Controls** (`pinned_panels`: `esql_control` / `options_list_control` / …) bound to ES\|QL `?params` — see **Inputs → Controls** below |
| Custom graph / flow viz | Prefer **Vega** (table below) — never silently collapse |

### Inputs → Controls (hard rule)

After panel inventory, every Splunk `input` must appear in an **Inputs → Controls
matrix** in `MAPPING.md`. Each row is one of:

1. Dashboard **time picker** (`?_tstart` / `?_tend`)
2. Options list / range / **ES|QL variable Control** (`pinned_panels` + `?name` in queries)
3. Explicit **GAP** with user-visible markdown (not a silent hard-coded demo value)

**Ban:** documenting the token in markdown and hard-coding the demo value in every
`WHERE` as the default for text / dropdown / radio pivots.

Wire panels like:

```esql
FROM logs-* 
| WHERE source.ip == TO_IP(?target_ip)
| WHERE TO_STRING(?aws_account) == "*" OR cloud.account.id == TO_STRING(?aws_account)
| WHERE @timestamp <= ?_tend AND @timestamp > ?_tstart
```

Cast Control values with `TO_STRING` / `TO_IP` when the field is `keyword` / `ip` —
STATIC_VALUES that look numeric are often bound as non-keyword and break every panel.

### Custom viz → Vega (hard rule)

| Splunk `custom_viz` / intent | Elastic target (P0) | Allowed fallback (must label **GAP**) |
|---|---|---|
| `sankey_diagram*` / flow quantities | **Vega Sankey** (saved viz + `config.ref_id` if inline `type:vega` unsupported) | heatmap or path `xy` + GAP note |
| `force_directed*` / node-link | **`heatmap`** (src×dest count) + companion edge `data_table` — keep Splunk title | edge `data_table` only + GAP |
| `*3d_graph*` / GraphLabelPropagation | Vega network colored by community **or** community table + scatter | table-only + GAP |
| GraphCentrality / weighted ARNs | `xy` bar by weight (OK) | — |

**Force-directed → heatmap (hard rule):** Do **not** attempt Kibana Vega `force`
layouts for Splunk `force_directed*` / particle graphs. They fail on current
Dashboards/Vega embeds (see tracking bug in skill `BUGS.md`). Ship heatmap +
edge table and note the GAP in `MAPPING.md` (true force viz deferred).

**Do not** silently collapse **Sankey** to table/heatmap — prefer Vega Sankey
first and label any fallback as GAP. Embed Vega via Visualize saved object
(`POST /api/saved_objects/visualization/{id}?overwrite=true`) + dashboard panel
`{"type":"vis","config":{"ref_id":"…","title":"…"}}` when the Dashboards API
`vis` discriminator does not accept `"type":"vega"` inline.
**Always rewire** `ref_id` lens stubs → SO type `visualization` after dashboard PUT
(see [reference.md](reference.md#vega-sankey--force-via-saved-visualization--ref_id)).

**Vega hard constraints (Blast Radius lessons):**

1. **No dotted field paths in Vega transforms/encodes** — ES|QL columns like
   `bx.aws.community` / `source.ip` must be aliased flat in the query
   (`community = bx.aws.community`) or copied with `datum['source.ip']` → `src`.
   Leaving `fields: ["bx.aws.community"]` throws
   `Cannot read properties of undefined (reading 'aws')`.
2. **Do not use Vega `force` for `force_directed*`** — mark-level and data-transform
   attempts still failed on serverless (id / Invalid SVG path / empty panels).
   Default to heatmap; track a proper force recipe in `BUGS.md`.
3. After every Dashboards API PUT that includes Vega `ref_id` panels, run the
   visualization-ref rewire or the UI shows **"visualization type not found"**.

**Forbidden:** Lens-only `lnsDatatable` NDJSON with `gridData.tabId` / attributes `tabs` — API rejects or UI ignores; yields tables-only.

Deploy with:

```http
PUT {KIBANA}/api/dashboards/{id}
Elastic-Api-Version: 2023-10-31
kbn-xsrf: true
```

Body: `{ title, description?, time_range, panels[], pinned_panels? }` — inline
`config` for Lens-style `vis`; Vega via `ref_id`. Details: [reference.md](reference.md).

After user UI edits: `GET` each id → write `kibana/*_dashboards.json` with `"source":"exported_from_kibana"`. Redeploy from that file; do not regenerate from templates unless asked.

## 4 — Data factories

For **each** panel query, list required data:

- data stream / index / lookup
- minimal ECS (or `bx.*`) fields the ES\|QL references
- volume hint (rows for tables; cardinality for charts)

**Graph / custom-viz support fields** (emit in the factory **before** panel build):

| Splunk custom_viz | Required factory fields (examples) |
|---|---|
| `sankey_diagram*` | Edge endpoints + count (`source.ip`, `destination.ip`, …) |
| `force_directed*` | Same edges + optional size/color dims (`bx.aws.vpc_id`, `cloud.account.name`, `bx.aws.community`) |
| `*3d_graph*` / LabelPropagation | Community / color field (e.g. `bx.aws.community`) |
| GraphCentrality | Weight field (e.g. `bx.aws.centrality.weight`) on entity/ARN docs |

Implement generator logic in the shared **`data_factories`** package
(`/Users/klg/Documents/GItHub/data_factories`, modules grouped by data domain, e.g. `data_factories.network.dns_hunt`, `data_factories.identity.hdsi_user`).
Keep a thin wrapper under `elastic/<use_case>/data_factories/` that calls the package
and writes NDJSON → local `out/` (so existing `setup_*.py` subprocess paths keep working).

1. Generator (in `data_factories`) writes NDJSON → `out/` via wrapper
2. Setup/bulk push to destination ES (Gate 0 URL+key)
3. Lookups: `index.mode=lookup` when `LOOKUP JOIN` needed
4. Presence/entity rollups: transform **or** factory-built metrics stream if transform is overkill for POC

Wire a customer catalog YAML (e.g. `customers/<name>/elastic/data_factories/factories.yaml`) **or** a use-case manifest so each dashboard id lists P0 factories. **Every panel must have a P0 producer** or an explicit “empty until real data” note to the user.

Install once: `pip install -e /Users/klg/Documents/GItHub/data_factories`.

Validate: run each panel ES\|QL via `POST {ES}/_query` before declaring done.

## 5 — MAPPING.md (required deliverable)

After panels and factories exist (before or right after deploy), write
`elastic/<use_case>/MAPPING.md`. Do **not** dump this into chat—link the path.

**Must include:**

1. **Header** — Splunk app/id, Elastic dashboard id, demo tokens, package path, **mockup path** (`mockup/index.html`)  
2. **DIMA / audit JSON used** — which files under the Splunk/DIMA export (e.g. `dashboard_to_migrate.json`, `macros.json`, `lookup_samples.json`) were used, consulted-only, or unused — and for what  
3. **Data factories** — generator script(s), setup/deploy script, NDJSON outputs → ES indices/streams → panels; note shared factories **not** used  
4. **Inputs → Controls matrix** — every Splunk token → Control / time picker / **GAP** (required; see §3)  
5. **Knowledge objects** — lookups, macros (expand from `macros.json` when present), datamodels → streams/indexes + why  
6. **Panel-by-panel** — for each panel: original **SPL** (from `splunk_queries.json`), **ES|QL**, viz type, **why** (incl. stand-ins and omissions)  
7. **Data layout** — which stream/index/lookup feeds which panel (may cross-ref factories section)  
8. **Gaps / non-goals** — placeholders skipped, macros missing, map→bar, etc.  
9. **Links** — include `mockup/` (Splunk HTML site) alongside Kibana URLs and `splunk_queries.json`

Keep `ANALYSIS.md` short; point it at `MAPPING.md` for full SPL rationale.  
Template & example: [reference.md](reference.md#mappingmd-template) · worked example `elastic/hdsi_user_investigation/MAPPING.md`.

## 6 — Package layout

```
# Shared implementations:
#   data_factories/src/data_factories/<data_domain>/<factory>.py
#     (network, threat_intel, identity, cloud_audit, cloud_billing, llm, apm, ...)
# Customer catalog: elastic/data_factories/factories.yaml

elastic/<use_case>/
  ANALYSIS.md              # short: Splunk source → Elastic map (+ link MAPPING.md + mockup/)
  MAPPING.md               # required: SPL ↔ ES|QL + why per panel
  manifest.json
  splunk_queries.json      # full SPL offline
  macros_resolved.json     # optional when macros used
  mockup/                  # required: Splunk-faithful HTML site (before Elastic)
    index.html
    tabs/                  # optional per Studio tab
    assets/                # optional
    README.md              # how to open
  kibana/<id>.json         # per dashboard (exported or built)
  kibana/*_dashboards.json # bundle + source stamp
  data_factories/          # thin wrappers → data_factories package + local out/
  esql/                    # optional panel query catalog
  setup_*.py               # deploy lookups/data/dashboards
```

## 7 — Deploy sequence

1. Confirm Gate 0 URL+key
2. Templates / data streams / lookups
3. Bulk factory data
4. Create/update Vega saved visualizations when needed, then `PUT /api/dashboards/{id}` (include `pinned_panels`)
5. **Rewire** Vega `ref_id` panels from lens → `visualization` SO refs
6. Ensure `MAPPING.md` is written/updated to match deployed ES|QL + Controls matrix
7. Print open URLs + which factory feeds which panel + paths to `MAPPING.md` and `mockup/index.html`
8. **Spot-check (required):**
   - [ ] Every Splunk input has a working Control or documented GAP
   - [ ] Changing Control values changes panel queries (no leftover hard-coded demo IP / token)
   - [ ] Sankey panels are visual flow (Vega), not only tables — unless GAP is explicit
   - [ ] Force-directed panels are `heatmap` (src×dest) + edge table (not broken Vega force)
   - [ ] Vega Sankey panels render (no “visualization type not found” / dotted-field errors)

## Anti-patterns

- Deploying without asking for destination credentials
- Chat-only mockup with no files under `elastic/<use_case>/mockup/`
- Deleting the Splunk mockup after Kibana deploy (“cleanup”)
- Classic Simple XML paste-as-dashboard when content is hunts → prefer saved ES\|QL / Timeline
- One mega-dashboard with overlapping `y` from fake tabs
- Committing `.env`, API keys, or real customer PII samples
- Silent Sankey → heatmap/table collapse without GAP + Vega-first attempt
- Attempting Kibana Vega `force` for Splunk `force_directed*` (use heatmap + edge table; see `BUGS.md`)
- Hard-coding Splunk input defaults into every `WHERE` instead of Controls
- Leaving Dashboards API `ref_id` as lens refs for classic Vega visualizations
- Feeding dotted ECS paths (`bx.aws.*`, `source.ip`) into Vega aggregate/encode field lists

## References

- Chart/API schemas, factories, **MAPPING.md template**: [reference.md](reference.md)
- Mapping example: repo `elastic/hdsi_user_investigation/MAPPING.md`
- Blackstone Employee Safety: repo `elastic/employee_safety/` + `build_tabbed_dashboard.py`
