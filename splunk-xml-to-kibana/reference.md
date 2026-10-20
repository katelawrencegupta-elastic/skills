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

## Grid

48 columns. Typical: KPI `w=12 h=6`, chart `w=24–48 h=10–14`, table `w=48 h=12–16`. Next row `y = prev.y + prev.h`.

## Rejected keys (9.6 serverless)

- Root `tabs`
- `grid.tabId`
- Nested `type: "section"` panels (use markdown headers or separate dashboards)

## Factory contract

Implementations live in `/Users/klg/Documents/GItHub/data_factories`
(grouped by data domain: `data_factories.network.*`, `threat_intel.*`, `identity.*`, `cloud_audit.*`, …). Customer use-case trees keep
thin wrappers under `elastic/<use_case>/data_factories/` that write local `out/`.

Minimal catalog / `factories.yaml` entry:

```yaml
factories:
  - id: badge_events
    priority: P0
    purpose: Feed Employees by Entrance + Badge KPIs
    writes:
      - data_stream: logs-bx.badge-default
    feeds_panels:
      - bx-es-office-metrics/om_entrance
      - bx-employee-safety-dashboard/ea_kpi
    ecs_schema:
      "@timestamp": date
      user.full_name: keyword
      bx.badge.entrance: keyword
```

Generator must emit fields named exactly as ES\|QL columns. After bulk, `GET {ES}/{stream}/_count` and sample `_query`.

## Export / promote UI edits

```bash
# pattern used by employee_safety/build_tabbed_dashboard.py
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

## 0. DIMA / Splunk audit JSON used
| JSON file | Used? | How |
(e.g. dashboard_to_migrate.json, macros.json, lookup_samples.json —
mark Yes / Consulted / No)

## 0b. Data factories (demo generation)
| Script | Role |
| Factory NDJSON | → ES index/stream | → panels |
(Note shared factories.yaml entries not used, if any.)

## 1. Inputs
| Splunk | Elastic | Why |

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
Dashboard URL · ANALYSIS.md · splunk_queries.json · source.xml ·
factory script · DIMA JSON paths
```

Worked example: Blackstone `elastic/hdsi_user_investigation/MAPPING.md`.
