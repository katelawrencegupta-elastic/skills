# Reference — Dashboards API + factories

Read only when building/deploying panels. Keep SKILL.md closed for token budget.

## Auth headers

```http
Authorization: ApiKey <key>
kbn-xsrf: true
x-elastic-internal-origin: kibana
Elastic-Api-Version: 2023-10-31
Content-Type: application/json
```

ES query: `POST {ES}/_query` with `{"query":"<esql>"}` (API key only; no kbn-xsrf).

URL rewrite often: `.kb.` ↔ `.es.` on Elastic Cloud.

## Panel snippets (inline `config`)

### Links nav (tab stand-in)

```json
{
  "type": "links",
  "id": "tab_nav",
  "grid": { "x": 0, "y": 0, "w": 48, "h": 4 },
  "config": {
    "title": "Tabs",
    "links": [
      {
        "type": "dashboardLink",
        "destination": "other-dashboard-id",
        "label": "Other Tab"
      }
    ]
  }
}
```

### Metric

```json
{
  "type": "vis",
  "id": "kpi1",
  "grid": { "x": 0, "y": 4, "w": 12, "h": 6 },
  "config": {
    "title": "Users Present",
    "type": "metric",
    "data_source": {
      "type": "esql",
      "query": "FROM metrics-* | STATS n = COUNT_DISTINCT(user.email)"
    },
    "metrics": [{ "type": "primary", "column": "n" }]
  }
}
```

### Gauge

```json
{
  "type": "vis",
  "id": "g1",
  "grid": { "x": 36, "y": 4, "w": 12, "h": 8 },
  "config": {
    "title": "Accounted Today",
    "type": "gauge",
    "data_source": {
      "type": "esql",
      "query": "FROM logs-* | STATS employees = COUNT_DISTINCT(user.full_name)"
    },
    "metric": { "column": "employees" }
  }
}
```

### XY bar / line

```json
{
  "type": "vis",
  "id": "bar1",
  "grid": { "x": 0, "y": 10, "w": 48, "h": 12 },
  "config": {
    "title": "By Entrance",
    "type": "xy",
    "layers": [
      {
        "type": "bar_horizontal",
        "data_source": {
          "type": "esql",
          "query": "FROM logs-* | STATS employees = COUNT_DISTINCT(user.full_name) BY entrance | SORT employees DESC | LIMIT 20"
        },
        "x": { "column": "entrance" },
        "y": [{ "column": "employees" }]
      }
    ]
  }
}
```

Time series: set `"axis": { "x": { "scale": "temporal", "domain": { "type": "fit", "rounding": false } } }`, put `data_source` **inside** each layer, and use:

```esql
WHERE @timestamp <= ?_tend AND @timestamp > ?_tstart
| STATS n = COUNT(*) BY BUCKET(@timestamp, 75, ?_tstart, ?_tend)
```

`x.column` must be the full `BUCKET(...)` expression (not an alias).

Layer types: `bar`, `bar_horizontal`, `bar_stacked`, `bar_horizontal_stacked`, `line`, `area`, …

Breakdown: `"breakdown_by": { "column": "status" }`.

### Heatmap (punchcard)

```json
{
  "type": "vis",
  "id": "heat1",
  "grid": { "x": 0, "y": 10, "w": 48, "h": 14 },
  "config": {
    "title": "Presence by Day",
    "type": "heatmap",
    "data_source": {
      "type": "esql",
      "query": "FROM logs-* | STATS events = COUNT(*) BY hour = DATE_EXTRACT(\"hour_of_day\", @timestamp), dow = DATE_EXTRACT(\"day_of_week\", @timestamp)"
    },
    "x": { "column": "hour" },
    "y": { "column": "dow" },
    "metric": { "column": "events" }
  }
}
```

### Data table

```json
{
  "type": "vis",
  "id": "tbl1",
  "grid": { "x": 0, "y": 20, "w": 48, "h": 14 },
  "config": {
    "title": "Roster",
    "type": "data_table",
    "data_source": {
      "type": "esql",
      "query": "FROM metrics-* | KEEP user.full_name, present | SORT user.full_name | LIMIT 500"
    },
    "rows": [{ "column": "user.full_name" }],
    "metrics": [{ "column": "present" }]
  }
}
```

### Markdown

```json
{
  "type": "markdown",
  "id": "hdr",
  "grid": { "x": 0, "y": 4, "w": 48, "h": 3 },
  "config": { "content": "### Tab title\nOne-line purpose." }
}
```

### ES|QL Controls (`pinned_panels`)

Put Controls on the dashboard root as `pinned_panels` (not inside `panels`).
`variable_name` becomes `?variable_name` in every panel / Vega ES|QL query.

```json
{
  "title": "Example",
  "time_range": { "from": "now-15m", "to": "now" },
  "pinned_panels": [
    {
      "type": "esql_control",
      "id": "ctl_target_ip",
      "width": "medium",
      "grow": false,
      "config": {
        "title": "AWS Analysis Target IP",
        "variable_name": "target_ip",
        "variable_type": "values",
        "control_type": "STATIC_VALUES",
        "available_options": ["10.40.12.88", "10.40.20.11"],
        "selected_options": ["10.40.12.88"],
        "single_select": true
      }
    },
    {
      "type": "esql_control",
      "id": "ctl_side",
      "config": {
        "title": "Toggle Source/Dest",
        "variable_name": "pivot_side",
        "variable_type": "values",
        "control_type": "STATIC_VALUES",
        "available_options": ["source", "destination"],
        "selected_options": ["source"],
        "single_select": true
      }
    }
  ],
  "panels": []
}
```

`control_type` may also be `VALUES_FROM_QUERY` with `esql_query` returning the option column.
Required: `variable_type` (`values` | `fields` | …) — omitting it fails validation.

### Vega (Sankey / force) via saved visualization + `ref_id`

Dashboards API `vis` discriminators on some clusters **do not** include inline
`"type": "vega"`. Create a Visualize library object, then reference it:

```http
POST {KIBANA}/api/saved_objects/visualization/{id}?overwrite=true
```

```json
{
  "attributes": {
    "title": "My Sankey",
    "visState": "{\"title\":\"My Sankey\",\"type\":\"vega\",\"aggs\":[],\"params\":{\"spec\":\"<stringified Vega JSON>\"}}",
    "uiStateJSON": "{}",
    "description": "",
    "version": 1,
    "kibanaSavedObjectMeta": {
      "searchSourceJSON": "{\"query\":{\"query\":\"\",\"language\":\"kuery\"},\"filter\":[]}"
    }
  }
}
```

Dashboard panel (temporary — see rewire note):

```json
{
  "type": "vis",
  "id": "vega_sankey",
  "grid": { "x": 0, "y": 9, "w": 24, "h": 16 },
  "config": {
    "title": "Visual Representation of Flow (VPC Interaction Quantities)",
    "ref_id": "example-vega-sankey"
  }
}
```

**Critical — rewire lens → visualization:** Dashboards API `config.ref_id` stores
the reference as saved-object type **`lens`**. Classic Vega objects are type
**`visualization`**, so the UI shows **"visualization type not found"** until you
rewire after every PUT:

1. `GET /api/saved_objects/dashboard/{id}`
2. For each Vega panel: set `type: "visualization"`, `panelRefName: "panel_<id>"`,
   `id: "<viz-id>"`
3. Replace lens refs with
   `{ "name": "panel_<id>", "type": "visualization", "id": "<viz-id>" }`
4. `PUT /api/saved_objects/dashboard/{id}` with updated `panelsJSON` + `references`

Bake this into `build_dashboard.py`, or run `scripts/rewire_vega_refs.py` after PUT.

Inside the Vega spec, use ES|QL data + Control defaults. **Alias dotted columns
in ES|QL** (or copy with bracket formulas) before any Vega aggregate/encode:

```json
{
  "data": [{
    "name": "raw",
    "url": {
      "%type%": "esql",
      "%timefield%": "@timestamp",
      "query": "FROM logs-* | WHERE source.ip == TO_IP(?target_ip) | STATS count = COUNT(*) BY src = source.ip, dst = destination.ip, community = cloud.account.name",
      "params": [{ "target_ip": "10.0.0.10" }]
    },
    "transform": [
      {"type": "formula", "expr": "datum.src", "as": "src"},
      {"type": "formula", "expr": "datum.dst", "as": "dst"}
    ]
  }]
}
```

Bad (throws on dotted paths): `"fields": ["cloud.account.name"]` without alias  
Good: alias `community = cloud.account.name` in ES|QL, then `"fields": ["community"]`.

### Force-directed / particle graph → heatmap (P0)

**Hard rule:** Map Splunk `force_directed*` / Verlet particle graphs to a Dashboards
API **`heatmap`** (src×dest flow counts) + companion edge **`data_table`**. Keep the
Splunk panel title. Note in `MAPPING.md` that true force/network Vega is deferred
(see skill `BUGS.md` / GitHub tracking issue).

```json
{
  "type": "vis",
  "id": "heatmap_force_paths",
  "grid": { "x": 24, "y": 9, "w": 24, "h": 16 },
  "config": {
    "title": "Force-Directed Verlet Integration Particle Graph",
    "type": "heatmap",
    "data_source": {
      "type": "esql",
      "query": "FROM logs-* | WHERE … | STATS count = COUNT(*) BY source.ip, destination.ip | SORT count DESC | LIMIT 40"
    },
    "x": { "column": "destination.ip" },
    "y": { "column": "source.ip" },
    "metric": { "column": "count" }
  }
}
```

Do **not** spend conversion time on Kibana Vega `force` / `linkpath` for these
panels until the tracking bug is resolved. Prior attempts failed with
`reading 'aws'|'id'`, Invalid SVG path, and empty panels (Blast Radius).

### Custom viz catalog (P0 → fallback)

| Splunk `custom_viz` / intent | Elastic P0 | Fallback (label **GAP**) | Factory fields |
|---|---|---|---|
| `sankey_diagram*` | Vega Sankey (alluvial / link paths) | heatmap / path xy | edge endpoints + count |
| `force_directed*` | **`heatmap`** src×dest + edge `data_table` | edge `data_table` only | edges + community/account/vpc |
| `*3d_graph*` / LabelPropagation | Vega network by community or community table | table-only | community field |
| GraphCentrality | `xy` bar by weight | — | centrality weight field |

**Ban:** collapsing **Sankey** to table/heatmap without a GAP note and a Vega-first attempt.  
**Ban:** shipping Kibana Vega `force` for `force_directed*` until `BUGS.md` is closed.

## Grid

48 columns. Typical: KPI `w=12 h=6`, chart `w=24–48 h=10–14`, table `w=48 h=12–16`. Next row `y = prev.y + prev.h`.

## Rejected keys (9.6 serverless)

- Root `tabs`
- `grid.tabId`
- Nested `type: "section"` panels (use markdown headers or separate dashboards)

## Factory contract

Prefer a shared `data_factories` package when the project has one (domain modules +
thin wrappers under `PACKAGE_ROOT/data_factories/`). Otherwise use
`templates/factories/minimal_ndjson.py` from this skill.

Minimal catalog / `factories.yaml` entry:

```yaml
factories:
  - id: badge_events
    priority: P0
    purpose: Feed Employees by Entrance + Badge KPIs
    writes:
      - data_stream: logs-demo.badge-default
    feeds_panels:
      - demo-office-metrics/om_entrance
    ecs_schema:
      "@timestamp": date
      user.full_name: keyword
      badge.entrance: keyword
```

Generator must emit fields named exactly as ES\|QL columns. After bulk, `GET {ES}/{stream}/_count` and sample `_query`.

## Export / promote UI edits

```bash
# scripts/put_dashboard.py --export / default PUT
GET /api/dashboards/{id}  →  kibana/{id}.json + bundle source=exported_from_kibana
PUT /api/dashboards/{id}  ←  that JSON (default redeploy path)
```

## Parse helper (Studio XML)

```python
import json, re
from pathlib import Path
text = Path("dashboard.xml").read_text()
raw = re.search(r"<definition><!\[CDATA\[(.*?)\]\]></definition>", text, re.S).group(1)
data = json.loads(raw)
tabs = data["layout"]["tabs"]["items"]
viz = {k: {"type": v["type"], "title": v.get("title")} for k, v in data["visualizations"].items()}
# join layoutDefinitions[*].structure item→viz
```

## MAPPING.md template

Write `elastic/<use_case>/MAPPING.md` once ES|QL panels exist. Source SPL from
`splunk_queries.json`; resolve macros via audit `macros.json` → `macros_resolved.json`
when available. Do not paste the full doc into chat.

```markdown
# <Title> — Splunk → Elastic mapping

**Splunk source:** `<app>` / `<dashboard_id>`
**Elastic target:** `<kibana_dashboard_id>`
**Demo tokens:** …
**Package:** `elastic/<use_case>/`
**Splunk mockup:** `elastic/<use_case>/mockup/index.html`

## 0. DIMA / Splunk audit JSON used
| JSON file | Used? | How |
(e.g. dashboard_to_migrate.json, macros.json, lookup_samples.json —
mark Yes / Consulted / No)

## 0b. Data factories (demo generation)
| Script | Role |
| Factory NDJSON | → ES index/stream | → panels |
(Note shared factories.yaml entries not used, if any.)

## 1. Inputs → Controls matrix (required)
| Splunk input / token | Elastic Control / time / GAP | Drives (`?param`) | Why |

## 2. Knowledge objects
### Lookups
### Macros (definition → Elastic stand-in + why)
### Datamodels → streams

## 3. Panel-by-panel mapping
### <panel title or inferred name> — `<elastic_panel_id>`
**SPL** (fenced `spl`)
**ES|QL** (fenced `esql`)
**Viz:** Splunk X → Kibana `vis` type
**Why:** …

(Repeat per panel. Note omitted panels explicitly.)

## 4. Data layout
| Elastic resource | Feeds panels |

## 5. Gaps / non-goals
| Item | Reason |

## 6. Quick reference
| Splunk pattern | Elastic pattern |

## 7. Links
Dashboard URL · ANALYSIS.md · mockup/index.html · splunk_queries.json ·
source.xml · factory script · DIMA JSON paths
```

Template copy: `templates/MAPPING.md`. Customer adapters may point at worked examples.
