"""Shared Vega Sankey / alluvial spec for SX2K (src → dst flows).

ES|QL must return columns aliased as: src, dst, count.
"""

from __future__ import annotations


def edge_esql(stream: str, src_field: str, dst_field: str, limit: int = 40) -> str:
    return (
        f"FROM {stream}\n"
        f"| WHERE @timestamp <= ?_tend AND @timestamp > ?_tstart\n"
        f"| STATS count = COUNT(*) BY src = {src_field}, dst = {dst_field}\n"
        f"| SORT count DESC\n"
        f"| LIMIT {limit}"
    )


def sankey_spec(esql: str, description: str = "Sankey flow (SX2K)") -> dict:
    """Left→right alluvial; data URL uses Kibana ES|QL (%type%=esql)."""
    return {
        "$schema": "https://vega.github.io/schema/vega/v5.json",
        "description": description,
        "padding": {"left": 8, "right": 8, "top": 8, "bottom": 8},
        "autosize": {"type": "fit", "contains": "padding"},
        "signals": [
            {"name": "width", "update": "containerSize()[0]"},
            {"name": "height", "update": "containerSize()[1]"},
            {"name": "nodeWidth", "value": 14},
        ],
        "data": [
            {
                "name": "raw",
                "url": {
                    "%type%": "esql",
                    "%timefield%": "@timestamp",
                    "query": esql,
                },
                "transform": [
                    {"type": "formula", "expr": "datum.src", "as": "src"},
                    {"type": "formula", "expr": "datum.dst", "as": "dst"},
                    {"type": "formula", "expr": "datum.count", "as": "value"},
                ],
            },
            {
                "name": "src_nodes",
                "source": "raw",
                "transform": [
                    {
                        "type": "aggregate",
                        "groupby": ["src"],
                        "fields": ["value"],
                        "ops": ["sum"],
                        "as": ["v"],
                    },
                    {"type": "collect", "sort": {"field": "v", "order": "descending"}},
                    {
                        "type": "window",
                        "ops": ["sum"],
                        "fields": ["v"],
                        "as": ["ycum"],
                    },
                    {
                        "type": "joinaggregate",
                        "fields": ["v"],
                        "ops": ["sum"],
                        "as": ["stack_max"],
                    },
                    {"type": "formula", "expr": "datum.ycum - datum.v", "as": "y0"},
                    {"type": "formula", "expr": "datum.ycum", "as": "y1"},
                    {
                        "type": "formula",
                        "expr": "datum.y0 / max(1, datum.stack_max) * height",
                        "as": "y0s",
                    },
                    {
                        "type": "formula",
                        "expr": "datum.y1 / max(1, datum.stack_max) * height",
                        "as": "y1s",
                    },
                    {"type": "formula", "expr": "0", "as": "x"},
                    {"type": "formula", "expr": "datum.src", "as": "label"},
                ],
            },
            {
                "name": "dst_nodes",
                "source": "raw",
                "transform": [
                    {
                        "type": "aggregate",
                        "groupby": ["dst"],
                        "fields": ["value"],
                        "ops": ["sum"],
                        "as": ["v"],
                    },
                    {"type": "collect", "sort": {"field": "v", "order": "descending"}},
                    {
                        "type": "window",
                        "ops": ["sum"],
                        "fields": ["v"],
                        "as": ["ycum"],
                    },
                    {
                        "type": "joinaggregate",
                        "fields": ["v"],
                        "ops": ["sum"],
                        "as": ["stack_max"],
                    },
                    {"type": "formula", "expr": "datum.ycum - datum.v", "as": "y0"},
                    {"type": "formula", "expr": "datum.ycum", "as": "y1"},
                    {
                        "type": "formula",
                        "expr": "datum.y0 / max(1, datum.stack_max) * height",
                        "as": "y0s",
                    },
                    {
                        "type": "formula",
                        "expr": "datum.y1 / max(1, datum.stack_max) * height",
                        "as": "y1s",
                    },
                    {
                        "type": "formula",
                        "expr": "width - nodeWidth",
                        "as": "x",
                    },
                    {"type": "formula", "expr": "datum.dst", "as": "label"},
                ],
            },
            {
                "name": "links",
                "source": "raw",
                "transform": [
                    {
                        "type": "lookup",
                        "from": "src_nodes",
                        "key": "src",
                        "fields": ["src"],
                        "values": ["y0s", "y1s", "v"],
                        "as": ["sy0", "sy1", "sv"],
                    },
                    {
                        "type": "lookup",
                        "from": "dst_nodes",
                        "key": "dst",
                        "fields": ["dst"],
                        "values": ["y0s", "y1s", "v"],
                        "as": ["ty0", "ty1", "tv"],
                    },
                    {
                        "type": "formula",
                        "expr": "(datum.sy0 + datum.sy1) / 2",
                        "as": "sy",
                    },
                    {
                        "type": "formula",
                        "expr": "(datum.ty0 + datum.ty1) / 2",
                        "as": "ty",
                    },
                    {
                        "type": "formula",
                        "expr": (
                            "max(1.5, datum.value / max(1, datum.sv) "
                            "* (datum.sy1 - datum.sy0))"
                        ),
                        "as": "strokeW",
                    },
                ],
            },
        ],
        "marks": [
            {
                "type": "path",
                "from": {"data": "links"},
                "encode": {
                    "enter": {
                        "stroke": {"value": "#5ccfe6"},
                        "strokeOpacity": {"value": 0.35},
                        "tooltip": {
                            "signal": "datum.src + ' → ' + datum.dst + ': ' + datum.value"
                        },
                    },
                    "update": {
                        "strokeWidth": {"field": "strokeW"},
                        "path": {
                            "signal": (
                                "'M' + nodeWidth + ',' + datum.sy "
                                "+ 'C' + (width/2) + ',' + datum.sy + ' ' "
                                "+ (width/2) + ',' + datum.ty + ' ' "
                                "+ (width - nodeWidth) + ',' + datum.ty"
                            )
                        },
                    },
                },
            },
            {
                "type": "rect",
                "from": {"data": "src_nodes"},
                "encode": {
                    "update": {
                        "x": {"field": "x"},
                        "y": {"field": "y0s"},
                        "width": {"signal": "nodeWidth"},
                        "y2": {"field": "y1s"},
                        "fill": {"value": "#65a637"},
                        "tooltip": {"signal": "datum.label + ': ' + datum.v"},
                    }
                },
            },
            {
                "type": "rect",
                "from": {"data": "dst_nodes"},
                "encode": {
                    "update": {
                        "x": {"field": "x"},
                        "y": {"field": "y0s"},
                        "width": {"signal": "nodeWidth"},
                        "y2": {"field": "y1s"},
                        "fill": {"value": "#e5a100"},
                        "tooltip": {"signal": "datum.label + ': ' + datum.v"},
                    }
                },
            },
            {
                "type": "text",
                "from": {"data": "src_nodes"},
                "encode": {
                    "update": {
                        "x": {"signal": "nodeWidth + 4"},
                        "y": {"signal": "(datum.y0s + datum.y1s) / 2"},
                        "align": {"value": "left"},
                        "baseline": {"value": "middle"},
                        "fill": {"value": "#ddd"},
                        "fontSize": {"value": 10},
                        "text": {"field": "label"},
                    }
                },
            },
            {
                "type": "text",
                "from": {"data": "dst_nodes"},
                "encode": {
                    "update": {
                        "x": {"signal": "width - nodeWidth - 4"},
                        "y": {"signal": "(datum.y0s + datum.y1s) / 2"},
                        "align": {"value": "right"},
                        "baseline": {"value": "middle"},
                        "fill": {"value": "#ddd"},
                        "fontSize": {"value": 10},
                        "text": {"field": "label"},
                    }
                },
            },
        ],
    }


def visualization_attributes(title: str, esql: str, description: str = "") -> dict:
    import json

    spec = sankey_spec(esql, description or title)
    vis_state = {
        "title": title,
        "type": "vega",
        "aggs": [],
        "params": {"spec": json.dumps(spec)},
    }
    return {
        "title": title,
        "visState": json.dumps(vis_state),
        "uiStateJSON": "{}",
        "description": description or "",
        "version": 1,
        "kibanaSavedObjectMeta": {"searchSourceJSON": "{}"},
    }
