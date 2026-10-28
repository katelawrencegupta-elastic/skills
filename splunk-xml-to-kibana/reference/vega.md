# Reference — Vega / custom viz

Load at R5 **only** when inventory has sankey / force / custom graph viz. See also `BUGS.md`.

## Custom viz catalog

| Splunk `custom_viz` / intent | Elastic P0 | Fallback (label **GAP**) |
|---|---|---|
| `sankey_diagram*` | Vega Sankey (saved viz + `ref_id`) | heatmap / path xy |
| `force_directed*` | **heatmap** src×dest + edge `data_table` | edge table only |
| `*3d_graph*` / LabelPropagation | Vega network by community or table | table-only |
| GraphCentrality | `xy` bar by weight | — |

**Ban:** silent Sankey → table/heatmap. **Ban:** Vega `force` for `force_directed*` until BUGS.md closed.

## Saved visualization + `ref_id`

Inline `"type":"vega"` often unsupported. Create Visualize SO then reference:

```http
POST {KIBANA}/api/saved_objects/visualization/{id}?overwrite=true
```

Dashboard panel:

```json
{
  "type": "vis",
  "id": "vega_sankey",
  "grid": { "x": 0, "y": 9, "w": 24, "h": 16 },
  "config": { "title": "Flow", "ref_id": "example-vega-sankey" }
}
```

**Rewire lens → visualization** after every Dashboards API PUT (`scripts/rewire_vega_refs.py`):

1. GET `/api/saved_objects/dashboard/{id}`
2. Set panel `type: "visualization"`, `panelRefName: "panel_<id>"`, `id: "<viz-id>"`
3. References: `{ "name": "panel_<id>", "type": "visualization", "id": "<viz-id>" }`
4. PUT updated `panelsJSON` + `references`

## Dotted fields

Alias in ES|QL before Vega aggregate/encode:

```esql
STATS count = COUNT(*) BY src = source.ip, dst = destination.ip, community = cloud.account.name
```

Bad: `"fields": ["cloud.account.name"]`. Good: `"fields": ["community"]`.

## Force-directed → heatmap (P0)

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
      "query": "FROM logs-* | STATS count = COUNT(*) BY source.ip, destination.ip | SORT count DESC | LIMIT 40"
    },
    "x": { "column": "destination.ip" },
    "y": { "column": "source.ip" },
    "metric": { "column": "count" }
  }
}
```
