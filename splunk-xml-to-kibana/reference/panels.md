# Reference — panel snippets

Load at R3 when writing `esql/panels.json`. Keep SKILL.md closed.

## Auth

```http
Authorization: ApiKey <key>
kbn-xsrf: true
x-elastic-internal-origin: kibana
Elastic-Api-Version: 2023-10-31
Content-Type: application/json
```

ES: `POST {ES}/_query` with `{"query":"<esql>"}` (API key only). Cloud URL: `.kb.` ↔ `.es.`.

## Grid

48 columns. KPI `w=12 h=6`; chart `w=24–48 h=10–14`; table `w=48 h=12–16`. Next row `y = prev.y + prev.h`.

Rejected (9.6 serverless): root `tabs`, `grid.tabId`, nested `type: "section"`.

## Metric

```json
{
  "type": "vis",
  "id": "kpi1",
  "grid": { "x": 0, "y": 4, "w": 12, "h": 6 },
  "config": {
    "title": "Users Present",
    "type": "metric",
    "data_source": { "type": "esql", "query": "FROM metrics-* | STATS n = COUNT_DISTINCT(user.email)" },
    "metrics": [{ "type": "primary", "column": "n" }]
  }
}
```

## Gauge

```json
{
  "type": "vis",
  "id": "g1",
  "grid": { "x": 36, "y": 4, "w": 12, "h": 8 },
  "config": {
    "title": "Accounted Today",
    "type": "gauge",
    "data_source": { "type": "esql", "query": "FROM logs-* | STATS employees = COUNT_DISTINCT(user.full_name)" },
    "metric": { "column": "employees" }
  }
}
```

## XY bar / line

```json
{
  "type": "vis",
  "id": "bar1",
  "grid": { "x": 0, "y": 10, "w": 48, "h": 12 },
  "config": {
    "title": "By Entrance",
    "type": "xy",
    "layers": [{
      "type": "bar_horizontal",
      "data_source": {
        "type": "esql",
        "query": "FROM logs-* | STATS employees = COUNT_DISTINCT(user.full_name) BY entrance | SORT employees DESC | LIMIT 20"
      },
      "x": { "column": "entrance" },
      "y": [{ "column": "employees" }]
    }]
  }
}
```

Time series: `"axis": { "x": { "scale": "temporal", "domain": { "type": "fit", "rounding": false } } }`, `data_source` inside each layer:

```esql
WHERE @timestamp <= ?_tend AND @timestamp > ?_tstart
| STATS n = COUNT(*) BY BUCKET(@timestamp, 75, ?_tstart, ?_tend)
```

`x.column` = full `BUCKET(...)` expression. Layers: `bar`, `bar_horizontal`, `bar_stacked`, `bar_horizontal_stacked`, `line`, `area`. Breakdown: `"breakdown_by": { "column": "status" }`.

## Heatmap

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

## Data table

```json
{
  "type": "vis",
  "id": "tbl1",
  "grid": { "x": 0, "y": 20, "w": 48, "h": 14 },
  "config": {
    "title": "Roster",
    "type": "data_table",
    "data_source": { "type": "esql", "query": "FROM metrics-* | KEEP user.full_name, present | SORT user.full_name | LIMIT 500" },
    "rows": [{ "column": "user.full_name" }],
    "metrics": [{ "column": "present" }]
  }
}
```

## Markdown / links nav

```json
{ "type": "markdown", "id": "hdr", "grid": { "x": 0, "y": 4, "w": 48, "h": 3 }, "config": { "content": "### Tab title\nOne-line purpose." } }
```

```json
{
  "type": "links",
  "id": "tab_nav",
  "grid": { "x": 0, "y": 0, "w": 48, "h": 4 },
  "config": {
    "title": "Tabs",
    "links": [{ "type": "dashboardLink", "destination": "other-dashboard-id", "label": "Other Tab" }]
  }
}
```

## Catalog shape (`esql/panels.json`)

See `templates/esql/panels.example.json`. Fields: `id`, `title`, `viz` (`metric|gauge|xy|heatmap|data_table|markdown|links|vega_ref`), `esql`, optional `rows`, `metrics`, `x`, `y`, `layer` (`bar_horizontal`…), `grid`, `ref_id` (vega).

## Factory contract

Shared `data_factories` package when present; else `templates/factories/minimal_ndjson.py`. Emit fields exactly as ES|QL columns. After bulk: count + sample `_query`.
