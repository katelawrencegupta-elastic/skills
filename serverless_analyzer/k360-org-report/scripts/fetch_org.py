#!/usr/bin/env python3
"""Fetch compact K360 org-report JSON for canvas embedding.

  python scripts/fetch_org.py --org "Name" --out <artifacts folder>/org_report.json
  # defaults: 365d lookback, weekly chart, compact (no indent, no per-type dump)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[1]
TAX = json.loads((ROOT / "references" / "taxonomy.json").read_text())
SEARCH = set(TAX["search"])
O11Y = set(TAX["observability"])
AI = set(TAX["genai"])
SVLS_TYPES = set(TAX["serverless_types"])
SVLS_TMPL = set(TAX["serverless_templates"])
ORG_KEEP = (
    "name", "id", "AE", "ARR", "HV", "segment", "geo", "region", "contract", "customer_type"
)

# One month is 30.416 days everywhere a lookback total becomes a monthly figure.
DAYS_PER_MONTH = 30.416
# Size of every terms aggregation on deployment.name. An aggregation that still
# leaves deployments out is named in the output's `truncated` list.
DEP_TERMS_SIZE = 300
# A storage snapshot older than this many days before the range end is stale.
STORAGE_STALE_DAYS = 45
# A deployment with no bill in this many days before the range end is not in org stored GB.
STORAGE_BILLING_DAYS = 90
# Search hour mix used when no hour shares were measured: peak, non-peak, idle.
ASSUMED_HOUR_MIX = (26.0, 74.0, 0.0)
# Ingest hours a month used when no active hours were measured.
ASSUMED_ACTIVE_HOURS = 730


def lookback_months(days) -> float:
    """Months in a lookback of `days` days. ECU over the lookback ÷ this is ECU a month."""
    try:
        n = float(days or 0)
    except (TypeError, ValueError):
        n = 0.0
    return max(n, 1.0) / DAYS_PER_MONTH


def load_env(path: Path) -> dict[str, str]:
    if not path.exists():
        raise SystemExit(f".env not found: {path}")
    env: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip("\"'")
    return env


def es_base_and_key(env: dict[str, str]) -> tuple[str, str]:
    """Elasticsearch base URL and API key from .env. The URL must be https with no credentials in it."""
    kibana = (env.get("KIBANA_URL") or env.get("ELASTIC_KIBANA_URL") or "").strip()
    key = env.get("ELASTIC_API_KEY") or env.get("ES_API_KEY")
    if not kibana or not key:
        raise SystemExit("Need KIBANA_URL and ELASTIC_API_KEY in .env")
    parsed = urllib.parse.urlsplit(kibana)
    if parsed.scheme != "https" or not parsed.hostname:
        raise SystemExit("KIBANA_URL must be https")
    if parsed.username or parsed.password:
        raise SystemExit("KIBANA_URL must not include a username or password")
    return kibana.rstrip("/").replace(".kb.", ".es."), key


def es_post(base: str, key: str, path: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{base}/{path.lstrip('/')}",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"ApiKey {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"ES HTTP {e.code} {path}: {e.read()[:600]!r}") from e


def family(t: str) -> str:
    tl = (t or "").strip().lower()
    if tl in AI or tl.startswith("inference"):
        return "genai"
    if tl in O11Y or tl.startswith("synthetics") or tl.startswith("integration"):
        return "observability"
    if tl in SEARCH:
        return "search"
    return "other"


def hex_id(n: str) -> bool:
    return len(n) == 32 and all(c in "0123456789abcdef" for c in n.lower())


def _sku_model_suffix(base: str, marker: str) -> str | None:
    """Model id after `marker` (`chat-input_anthropic-claude-4-6-sonnet` → that suffix)."""
    idx = base.lower().find(marker)
    if idx < 0:
        return None
    rest = base[idx + len(marker) :].lstrip("_.")
    return rest or None


def inference_model_label(model: str | None) -> str | None:
    """Display name for a model id taken from the SKU. No id → no name."""
    if not model:
        return None
    low = model.lower()
    if "rainbow-sprinkles" in low:
        return "Elastic Managed LLM"
    if "claude" in low or any(x in low for x in ("haiku", "sonnet", "opus")):
        family = "Haiku" if "haiku" in low else "Sonnet" if "sonnet" in low else "Opus" if "opus" in low else "Claude"
        name = "Claude" if family == "Claude" else f"Claude {family}"
        m = re.search(r"claude-(\d+)(?:-(\d+))?", low)
        if m:
            ver = m.group(1) + (f".{m.group(2)}" if m.group(2) else "")
            return f"{name} {ver}"
        return name
    return model


def decode_inference_sku(sku: str) -> dict:
    """Human label for EIS / inference billing SKUs.

    Chat model ids are the suffix after `chat-input_` / `chat-output_`
    (`anthropic-claude-4-6-sonnet`, `rainbow-sprinkles`). A SKU with no
    suffix has no model id — do not invent one. `quantity` is millions of tokens.
    """
    s = sku or ""
    base = s.split(".", 1)[-1] if "." in s else s  # strip global./serverless.
    low = base.lower()
    kind, model = "inference", None
    if "embed-text-dense_" in low:
        kind, model = "embed_dense", _sku_model_suffix(base, "embed-text-dense_")
    elif "embed-text-sparse_" in low or ("elser" in low and "chat-" not in low):
        kind = "embed_sparse"
        model = _sku_model_suffix(base, "embed-text-sparse_") if "embed-text-sparse_" in low else (
            base.split("_", 1)[-1] if "_" in base else base
        )
    elif "chat-input" in low:
        kind, model = "chat_input", _sku_model_suffix(base, "chat-input")
    elif "chat-output" in low:
        kind, model = "chat_output", _sku_model_suffix(base, "chat-output")
    pretty = inference_model_label(model)
    kind_name = {
        "chat_input": "chat input",
        "chat_output": "chat output",
        "embed_dense": "dense embed",
        "embed_sparse": "sparse embed",
    }.get(kind)
    if kind_name and pretty:
        label = f"EIS {kind_name} ({pretty})"
    elif kind_name:
        label = f"EIS {kind_name}"
    else:
        label = base
    return {
        "sku": s,
        "kind": kind,
        "model": model,
        "model_label": pretty,
        "label": label,
    }


_ML_SKU_RE = re.compile(
    r"\.es\.ml\.([^_]+)_(.+?)_(\d+)_(\d+)$",
    re.IGNORECASE,
)
# Cloud-Enterprise_aws.es.ml.c5d_us-west-2_2048_3            (AWS: no provider prefix on the region)
# Cloud-Enterprise_azure.es.datahot.edsv4_azure-eastus2_8192_2
# Cloud-Standard_gcp.es.datafrozen.n2.68x10x95_gcp-europe-west1_61440_2
_CLOUD_SKU_RE = re.compile(
    r"Cloud-[^_]+_((?:aws|gcp|azure))\.(es\.(?:datahot|datawarm|datafrozen|datacold|master|ml|coordinating)\.[^_]+)"
    r"_(?:(?:aws|gcp|azure)-)?([a-z0-9-]+)_\d+_\d+",
    re.IGNORECASE,
)
# Provider and region of any Cloud-* SKU (Kibana, APM, legacy instance names).
_CLOUD_REGION_RE = re.compile(
    r"Cloud-[^_]+_((?:aws|gcp|azure))\.[^_]+_(?:(?:aws|gcp|azure)-)?([a-z0-9-]+)_\d+_\d+$",
    re.IGNORECASE,
)


def is_ml_sku(sku: str) -> bool:
    s = (sku or "").lower()
    return ".es.ml." in s or ".ml." in s


def parse_cloud_sku(sku: str) -> dict | None:
    """Extract provider / instance family / region from Cloud-* capacity SKUs.

    The region is returned without a provider prefix: `us-west-2`, `eastus2`,
    `europe-west1`. AWS SKUs carry no prefix; Azure and GCP SKUs do.
    """
    m = _CLOUD_SKU_RE.search(sku or "")
    if not m:
        return None
    provider, instance, region = m.groups()
    tier = instance.split(".")[1] if instance.count(".") >= 1 else None
    return {
        "provider": provider.upper(),
        "instance": instance,
        "region": region.lower(),
        "tier": tier,
    }


def cloud_sku_region(sku: str) -> dict | None:
    """Provider and region of any Cloud-* SKU, capacity or not. None when the SKU is not one."""
    m = _CLOUD_REGION_RE.search(sku or "")
    if not m:
        return None
    return {"provider": m.group(1).upper(), "region": m.group(2).lower()}


def decode_ml_sku(sku: str) -> dict:
    """Human label for in-cluster ML node capacity SKUs (*.es.ml.*)."""
    s = sku or ""
    m = _ML_SKU_RE.search(s)
    if not m:
        return {
            "sku": s,
            "kind": "ml",
            "instance": None,
            "region": None,
            "ram_mb": None,
            "nodes": None,
            "label": s,
        }
    instance, region, ram_s, nodes_s = m.groups()
    ram_mb = int(ram_s)
    nodes = int(nodes_s)
    region_clean = re.sub(r"^(gcp|aws|azure)-", "", region, flags=re.IGNORECASE)
    ram_label = f"{ram_mb // 1024} GB" if ram_mb >= 1024 else f"{ram_mb} MB"
    label = f"ML {instance} · {region_clean} · {ram_label} × {nodes}"
    return {
        "sku": s,
        "kind": "ml_node",
        "instance": instance,
        "region": region_clean,
        "ram_mb": ram_mb,
        "nodes": nodes,
        "label": label,
    }


def platform(types: dict[str, float], skus: dict[str, float], template: str | None) -> str:
    if sum(c for s, c in skus.items() if s.lower().startswith("serverless.")) > 0:
        return "serverless"
    if sum(c for t, c in types.items() if t.strip().lower() in SVLS_TYPES) > 0:
        return "serverless"
    tmpl = (template or "").lower()
    classic = sum(c for s, c in skus.items() if s.lower().startswith("cloud-"))
    if tmpl in SVLS_TMPL and classic == 0:
        return "serverless"
    return "ech"


def burndown(pivot: dict) -> dict:
    pace, runout, org = pivot.get("pace") or {}, pivot.get("runout") or {}, pivot.get("organization") or {}
    rate = float(pace.get("consumption_rate") or 0)
    runway = runout.get("runway_days")
    runout_date = (runout.get("runout_date") or "")[:10] or None
    ends = org.get("contract", {}).get("end") or []
    if isinstance(ends, str):
        ends = [ends]
    nearest = min((str(e)[:10] for e in ends), default=None)
    reasons, priority, watch = [], False, False
    if rate > 1.0:
        reasons.append(f"consumption_rate {rate:.3f} > 1.0")
        priority = True
    if runout_date and nearest and runout_date < nearest:
        reasons.append(f"runout {runout_date} before contract end {nearest}")
        priority = True
    if runway is not None and runway < 90:
        reasons.append(f"runway_days {runway} < 90")
        if rate >= 1.0:
            priority = True
        else:
            watch = True
    return {
        "level": "PRIORITY: OVERAGE" if priority else ("WATCH" if watch else "OK"),
        "priority": priority,
        "watch": watch,
        "reasons": reasons,
        "consumption_rate": rate,
        "commit": pace.get("commit"),
        "required": pace.get("required"),
        "runway_days": runway,
        "runout_date": runout_date,
        "ecu_balance": runout.get("balance"),
        "daily_30d": (pivot.get("consumption") or {}).get("30d", {}).get("daily"),
        "annual_projection": (pivot.get("consumption") or {}).get("annual_projection"),
        "nearest_contract_end": nearest,
    }


def _pctile(vals: list[float], p: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    i = min(len(s) - 1, max(0, int(round((p / 100.0) * (len(s) - 1)))))
    return s[i]


def _complexity(ms: float | None) -> str | None:
    if ms is None:
        return None
    if ms < 50:
        return "Low"
    if ms < 200:
        return "Medium"
    return "High"


def hour_shares(qps: list[float]) -> tuple[float, float, float]:
    """Peak, non-peak and idle share of days, in percent. Each is 0–100 and the three sum to 100.

    Every day is in exactly one class. A day at or below 1 QPS and at or below
    the 10th percentile is idle. Of the other days, those at or above the 75th
    percentile are peak. The rest are non-peak.
    """
    vals = [float(q) for q in qps or []]
    n = len(vals)
    if not n:
        return 0.0, 0.0, 0.0
    p75 = _pctile(vals, 75)
    p10 = _pctile(vals, 10)
    idle_n = sum(1 for q in vals if q <= 1.0 and q <= p10)
    peak_n = sum(1 for q in vals if not (q <= 1.0 and q <= p10) and q >= p75)
    peak = min(100.0, max(0.0, round(100.0 * peak_n / n, 1)))
    idle = min(100.0 - peak, max(0.0, round(100.0 * idle_n / n, 1)))
    nonpeak = max(0.0, round(100.0 - peak - idle, 1))
    return peak, nonpeak, round(idle, 1)


def org_clause(org_ids: list[str]) -> dict:
    """One id is a term filter. Several ids are one terms filter."""
    ids = [str(i) for i in org_ids if str(i) and str(i) != "__none__"]
    if len(ids) == 1:
        return {"term": {"organization.id": ids[0]}}
    return {"terms": {"organization.id": ids}}


def fetch_workload(base: str, key: str, org_id: str | list[str], avg_doc_bytes: float | None) -> dict:
    """Day-average search/ingest sizing inputs from index_telemetry (2 ES calls).

    Cadence is ~daily → QPS/docs-sec are day averages; % hours use day-share proxies.
    """
    org = org_clause([org_id] if isinstance(org_id, str) else list(org_id))
    exclude = {"bool": {"must_not": [
        {"prefix": {"index.name": "."}},
        {"prefix": {"index.name": "partial-"}},
    ]}}
    top = es_post(
        base,
        key,
        "cloud-tracker-index_telemetry-*/_search",
        {
            "size": 0,
            "query": {"bool": {"filter": [
                org, exclude, {"range": {"@timestamp": {"gte": "now-5d"}}},
            ]}},
            "aggs": {
                "by_search": {
                    "terms": {"field": "index.name", "size": 100, "order": {"mx": "desc"}},
                    "aggs": {"mx": {"max": {"field": "index.search_total"}}},
                },
                "by_docs": {
                    "terms": {"field": "index.name", "size": 100, "order": {"mx": "desc"}},
                    "aggs": {"mx": {"max": {"field": "index.docs_count"}}},
                },
            },
        },
    )
    names = list({
        *(b["key"] for b in top["aggregations"]["by_search"]["buckets"]),
        *(b["key"] for b in top["aggregations"]["by_docs"]["buckets"]),
    })
    empty = {
        "search": {
            "peak": {"qps": None, "complexity": None, "complexity_ms": None, "pct_hours_mo": None},
            "nonpeak": {"qps": None, "complexity": None, "complexity_ms": None, "pct_hours_mo": None},
            "idle_pct_hours_mo": None,
        },
        "ingest": {
            "docs_per_sec": None,
            "avg_doc_bytes": round(avg_doc_bytes) if avg_doc_bytes else None,
            "active_hours_mo": None,
        },
        "notes": ["No index_telemetry for org"],
        "window_days": 0,
    }
    if not names:
        return empty

    hist = es_post(
        base,
        key,
        "cloud-tracker-index_telemetry-*/_search",
        {
            "size": 0,
            "query": {"bool": {"filter": [
                org,
                {"range": {"@timestamp": {"gte": "now-21d"}}},
                {"terms": {"index.name": names}},
            ]}},
            "aggs": {
                "by_day": {
                    "date_histogram": {"field": "@timestamp", "calendar_interval": "day"},
                    "aggs": {
                        "by_idx": {
                            "terms": {"field": "index.name", "size": len(names)},
                            "aggs": {
                                "st": {"max": {"field": "index.search_total"}},
                                "tm": {"max": {"field": "index.search_time_ms"}},
                                "docs": {"max": {"field": "index.docs_count"}},
                            },
                        }
                    },
                }
            },
        },
    )
    series: dict[str, dict[str, tuple[float, float, float]]] = defaultdict(dict)
    days: list[str] = []
    for b in hist["aggregations"]["by_day"]["buckets"]:
        day = b["key_as_string"][:10]
        days.append(day)
        for ib in b["by_idx"]["buckets"]:
            series[ib["key"]][day] = (
                float(ib["st"]["value"] or 0),
                float(ib["tm"]["value"] or 0),
                float(ib["docs"]["value"] or 0),
            )

    search_rows: list[dict] = []
    ingest_dps: list[float] = []
    active_ingest_days = 0
    for i in range(1, len(days)):
        d0, d1 = days[i - 1], days[i]
        ds = dt = dd = 0.0
        for hist_i in series.values():
            if d0 not in hist_i or d1 not in hist_i:
                continue
            s0, t0, doc0 = hist_i[d0]
            s1, t1, doc1 = hist_i[d1]
            dsv = (s1 - s0) if s1 >= s0 else s1
            if dsv > 0:
                ds += dsv
                dt += (t1 - t0) if t1 >= t0 else max(0.0, t1)
            if doc1 > doc0:
                dd += doc1 - doc0
        if ds > 0:
            search_rows.append({"qps": ds / 86400.0, "ms": dt / ds})
        if dd > 1000:
            active_ingest_days += 1
            ingest_dps.append(dd / 86400.0)

    notes = [
        "Day averages from index_telemetry (≈1 sample/day); not peak-second QPS",
        "Complexity: Δsearch_time_ms/Δsearch_total → Low<50 / Med<200 / High",
        "% hours = day-share proxy (idle: day ≤1 QPS and ≤p10; peak: other days ≥p75; rest non-peak; the three sum to 100)",
        "Ingest docs/sec = median daily Δdocs_count; active_hours/mo = active_day_frac×730",
    ]

    if not search_rows:
        return {
            **empty,
            "ingest": {
                "docs_per_sec": round(median(ingest_dps), 2) if ingest_dps else None,
                "avg_doc_bytes": round(avg_doc_bytes) if avg_doc_bytes else None,
                "active_hours_mo": round(active_ingest_days / max(len(days) - 1, 1) * 730)
                if days else None,
            },
            "notes": notes,
            "window_days": max(len(days) - 1, 0),
        }

    med_q = median(r["qps"] for r in search_rows)
    clean = [r for r in search_rows if r["qps"] <= max(med_q * 4, med_q + 50)]
    if len(clean) < len(search_rows) * 0.5:
        clean = search_rows
    qps = [r["qps"] for r in clean]
    peak = max(clean, key=lambda r: r["qps"])
    non_q = median(qps)
    nonpeak = min(clean, key=lambda r: abs(r["qps"] - non_q))
    peak_pct, nonpeak_pct, idle_pct = hour_shares(qps)
    day_n = max(len(days) - 1, 1)

    return {
        "search": {
            "peak": {
                "qps": round(peak["qps"], 2),
                "complexity": _complexity(peak["ms"]),
                "complexity_ms": round(peak["ms"], 2),
                "pct_hours_mo": peak_pct,
            },
            "nonpeak": {
                "qps": round(nonpeak["qps"], 2),
                "complexity": _complexity(nonpeak["ms"]),
                "complexity_ms": round(nonpeak["ms"], 2),
                "pct_hours_mo": nonpeak_pct,
            },
            "idle_pct_hours_mo": idle_pct,
        },
        "ingest": {
            "docs_per_sec": round(median(ingest_dps), 2) if ingest_dps else None,
            "avg_doc_bytes": round(avg_doc_bytes) if avg_doc_bytes else None,
            "active_hours_mo": round(active_ingest_days / day_n * 730),
        },
        "notes": notes,
        "window_days": day_n,
        "indices": len(names),
    }


# Observability Serverless Complete-tier ladders (Estimator_Reference v1.19)
_O11Y_INGEST_EDGES = (50, 100, 200, 500, 1000, 2000, 5000, 1e18)
_O11Y_INGEST_PRICES = (0.50, 0.325, 0.225, 0.15, 0.105, 0.10, 0.095, 0.0925)
_O11Y_RET_EDGES = (10_000, 20_000, 50_000, 100_000, 250_000, 1_000_000, 2_500_000, 1e18)
_O11Y_RET_PRICES = (0.040, 0.032, 0.030, 0.028, 0.026, 0.022, 0.020, 0.019)
_LOG_NAME_HINTS = (
    "log", "syslog", "kong", "qed", "sccm", "github", "rpa", "learning",
    "gigi", "sso", "network", "dhcp", "filebeat", "fleet",
)
_O11Y_NAME_HINTS = ("observability", "monitor", "apm", "synthetics", "uptime")
_SEC_NAME_HINTS = ("security", "cyberark", "infoblox", "siem", "endpoint")


def classify_dep_kind(name: str, families: dict | None) -> str:
    """Bucket a deployment for serverless offload analysis.

    Security is tested first. A deployment whose use case is security carries
    its ECU in `families.security`, and most deployments bill some
    Observability type (the integrations server), so testing Observability
    first sent every security deployment to Observability.
    """
    n = (name or "").lower()
    fam = families or {}
    if float(fam.get("security") or 0) > 0 or any(h in n for h in _SEC_NAME_HINTS):
        return "security"
    if float(fam.get("observability") or 0) > 0 or any(h in n for h in _O11Y_NAME_HINTS):
        return "o11y"
    if any(h in n for h in _LOG_NAME_HINTS):
        return "logs"
    return "search"


FAMILY_KEYS = ("search", "observability", "security", "genai", "other")


def split_families(types: dict[str, float], ecu: float, use_case: str | None) -> dict[str, float]:
    """One deployment's ECU split across the solution families. The parts sum to `ecu`.

    Billing `type` decides search, observability, genai and other. Billing has
    no security type, so a deployment whose use case is security moves its
    search, observability and other ECU to security. Inference stays in genai.
    ECU that no listed type explains goes to other.
    """
    fam = {k: 0.0 for k in FAMILY_KEYS}
    for t, c in (types or {}).items():
        fam[family(t)] += float(c or 0)
    fam["other"] += float(ecu or 0) - sum(fam.values())
    if abs(fam["other"]) < 0.005:
        fam["other"] = 0.0
    if str(use_case or "").strip().lower() == "security":
        fam["security"] = fam["search"] + fam["observability"] + fam["other"]
        fam["search"] = fam["observability"] = fam["other"] = 0.0
    return fam


# Billing windows whose exact deployment roster is saved on the chart.
BILLED_WINDOW_DAYS = (30, 90)


def _tiered_cost(gb: float, edges: tuple, prices: tuple) -> float:
    left = float(gb)
    cost = 0.0
    lo = 0.0
    for hi, price in zip(edges, prices):
        take = min(left, hi - lo)
        if take > 0:
            cost += take * price
        left -= take
        lo = hi
        if left <= 0:
            break
    return cost


def price_o11y_serverless(
    daily_raw_gb: float,
    retention_months: float,
    *,
    meter_factor: float = 1.595,
    support_uplift: float = 0.15,
) -> dict:
    """Complete + Premium ballpark from Observability estimator ladders.

    meter_factor default ≈ sheet expansion seen at ~1k GB/d raw (1069→1704).
    """
    days = 30.416
    metered = daily_raw_gb * meter_factor
    ingest_mo = _tiered_cost(metered, _O11Y_INGEST_EDGES, _O11Y_INGEST_PRICES) * days
    retained = metered * days * retention_months
    retain_mo = _tiered_cost(retained, _O11Y_RET_EDGES, _O11Y_RET_PRICES)
    sub = ingest_mo + retain_mo
    support = sub * support_uplift
    total = sub + support
    return {
        "daily_raw_gb": round(daily_raw_gb, 1),
        "daily_metered_gb": round(metered, 1),
        "retention_months": retention_months,
        "retained_gb": round(retained, 1),
        "ingest_mo": round(ingest_mo, 2),
        "retention_mo": round(retain_mo, 2),
        "support_mo": round(support, 2),
        "total_mo": round(total, 2),
        "total_yr": round(total * 12, 2),
    }


def search_hour_mix(workload: dict | None) -> dict:
    """Peak / non-peak / idle percent of a 730 h month, for the Python price and the sheet alike.

    Measured shares are normalised to 100. With no measured share (workload
    skipped, or no index telemetry) the mix is ASSUMED_HOUR_MIX and `assumed`
    is true.
    """
    search = (workload or {}).get("search") or {}

    def _pct(v) -> float:
        try:
            return max(float(v or 0), 0.0)
        except (TypeError, ValueError):
            return 0.0

    peak = _pct((search.get("peak") or {}).get("pct_hours_mo"))
    non = _pct((search.get("nonpeak") or {}).get("pct_hours_mo"))
    idle = _pct(search.get("idle_pct_hours_mo"))
    mix = peak + non + idle
    assumed = mix <= 0
    if assumed:
        peak, non, idle = ASSUMED_HOUR_MIX
    else:
        peak, idle = round(100.0 * peak / mix, 1), round(100.0 * idle / mix, 1)
        idle = min(idle, round(100.0 - peak, 1))
        non = max(0.0, round(100.0 - peak - idle, 1))
    return {"peak_pct": peak, "nonpeak_pct": non, "idle_pct": idle, "assumed": assumed}


def price_es3_search(
    storage_gb: float,
    workload: dict | None,
    *,
    support_uplift: float = 0.15,
    retain_per_gb: float = 0.047,
    search_vcu_hr: float = 0.09,
    ingest_vcu_hr: float = 0.14,
) -> dict:
    """On-demand ES3 ballpark calibrated to Estimator v2.2 at ~140 TB.

    Search VCU is storage-floored (cache RAM), not QPS-floored at logging scale.
    """
    hours_mo = 730.0
    # Observed: 139760 GB → ~1352 peak VCU, ~1204 idle VCU (on-demand)
    ref_gb, ref_peak_vcu, ref_idle_vcu = 139760.0, 1352.0, 1204.0
    gb = max(float(storage_gb), 0.0)
    scale = gb / ref_gb if ref_gb else 0.0
    peak_vcu = max(28.0, ref_peak_vcu * scale)
    idle_vcu = max(28.0, ref_idle_vcu * scale)

    wl = workload or {}
    hour_mix = search_hour_mix(wl)
    peak_pct = hour_mix["peak_pct"] / 100.0
    non_pct = hour_mix["nonpeak_pct"] / 100.0
    idle_pct = hour_mix["idle_pct"] / 100.0

    peak_h = hours_mo * peak_pct
    non_h = hours_mo * non_pct
    idle_h = hours_mo * idle_pct
    # non-peak still storage-floored at same VCU as peak for this ballpark
    search_vcu_hr_total = peak_h * peak_vcu + non_h * peak_vcu + idle_h * idle_vcu
    search_mo = search_vcu_hr_total * search_vcu_hr
    retain_mo = gb * retain_per_gb

    ingest = wl.get("ingest") or {}
    dps = float(ingest.get("docs_per_sec") or 0)
    avg_b = float(ingest.get("avg_doc_bytes") or 0)
    active_h = float(ingest.get("active_hours_mo") or 0)
    # scale ingest by storage share vs org if caller already passed slice-only wl; else proportional
    ingest_mb_s = (dps * avg_b) / (1024 * 1024) if dps and avg_b else 0.0
    ingest_vcu = max(4.0, ingest_mb_s * 4.0) if ingest_mb_s else 4.0
    if active_h <= 0:
        active_h = float(ASSUMED_ACTIVE_HOURS)
    ingest_mo = ingest_vcu * active_h * ingest_vcu_hr

    sub = search_mo + retain_mo + ingest_mo
    support = sub * support_uplift
    total = sub + support
    return {
        "profile": "On-demand",
        "storage_gb": round(gb, 3),
        "peak_vcu": round(peak_vcu, 1),
        "idle_vcu": round(idle_vcu, 1),
        "hour_mix": hour_mix,
        "search_mo": round(search_mo, 2),
        "retention_mo": round(retain_mo, 2),
        "ingest_mo": round(ingest_mo, 2),
        "support_mo": round(support, 2),
        "total_mo": round(total, 2),
        "total_yr": round(total * 12, 2),
    }


NOT_PRICED_SERVERLESS = "already serverless"
NOT_PRICED_NO_GB = "no stored GB in telemetry"


def build_serverless_offload(
    deployments: list[dict],
    storage_by_dep: list[dict],
    org_total_ecu: float,
    workload: dict | None = None,
    *,
    lookback_days: float = 365,
    org_stored_gb: float | None = None,
    not_billing: list[dict] | None = None,
) -> dict:
    """ECH → multi-project serverless savings (Search + Observability + Security).

    Only a deployment that can be priced is compared: it runs on ECH and has
    stored GB. Any other deployment is left off both sides and listed in
    `full_split.not_priced`. ECU a month is lookback ECU ÷ (lookback_days ÷
    30.416). `org_stored_gb` is the one denominator for the Search share of
    stored GB; it defaults to the stored GB of the priced deployments.
    `not_billing` is `storage.not_billing`: a deployment listed there has a
    snapshot that was left out of storage, and its reason is carried over.
    """
    months_n = lookback_months(lookback_days)
    stor = {s["name"]: s for s in storage_by_dep}
    left_out = {x["name"]: x.get("reason") for x in (not_billing or [])}
    by_kind: dict[str, dict] = {
        k: {"n": 0, "ecu": 0.0, "gb": 0.0, "deps": []}
        for k in ("o11y", "logs", "security", "search")
    }
    not_priced: list[dict] = []
    for d in deployments:
        gb = float((stor.get(d["name"]) or {}).get("data_stored_gb") or 0)
        ecu = float(d.get("ecu") or 0)
        reason = None
        if str(d.get("platform") or "ech").lower() == "serverless":
            reason = NOT_PRICED_SERVERLESS
        elif gb <= 0:
            reason = left_out.get(d["name"]) or NOT_PRICED_NO_GB
        if reason:
            not_priced.append({
                "name": d["name"],
                "ecu": round(ecu, 2),
                "ecu_mo": round(ecu / months_n, 2),
                "reason": reason,
            })
            continue
        kind = classify_dep_kind(d["name"], d.get("families"))
        bucket = by_kind[kind]
        bucket["n"] += 1
        bucket["ecu"] += ecu
        bucket["gb"] += gb
        bucket["deps"].append({"name": d["name"], "ecu": round(ecu, 2), "gb": round(gb, 3)})
    not_priced.sort(key=lambda x: -x["ecu"])
    priced_n = sum(b["n"] for b in by_kind.values())
    priced_ecu = sum(b["ecu"] for b in by_kind.values())
    not_priced_ecu = sum(x["ecu"] for x in not_priced)

    for bucket in by_kind.values():
        bucket["ecu"] = round(bucket["ecu"], 2)
        bucket["gb"] = round(bucket["gb"], 3)
        bucket["ecu_mo"] = round(bucket["ecu"] / months_n, 2)
        bucket["deps"].sort(key=lambda x: -x["gb"])

    def slice_kinds(*kinds: str) -> dict:
        n = sum(by_kind[k]["n"] for k in kinds)
        ecu = sum(by_kind[k]["ecu"] for k in kinds)
        gb = sum(by_kind[k]["gb"] for k in kinds)
        return {
            "kinds": list(kinds),
            "n": n,
            "ecu": round(ecu, 2),
            "ecu_mo": round(ecu / months_n, 2),
            "gb": round(gb, 3),
        }

    slices = {
        "o11y_only": slice_kinds("o11y"),
        "o11y_logs": slice_kinds("o11y", "logs"),
        "security": slice_kinds("security"),
        "search": slice_kinds("search"),
        "o11y_logs_security": slice_kinds("o11y", "logs", "security"),
        "remain_search_security": slice_kinds("search", "security"),
        "remain_search": slice_kinds("search"),
    }

    months_default = 3.0
    # The ECH side of the comparison is the priced deployments only.
    org_mo = round(priced_ecu / months_n, 2)
    org_all_mo = round(float(org_total_ecu or 0) / months_n, 2)
    org_gb = sum(by_kind[k]["gb"] for k in by_kind)
    share_gb = float(org_stored_gb) if org_stored_gb else org_gb

    # --- product-line serverless prices ---
    o11y_slice = slices["o11y_logs"]
    sec_slice = slices["security"]
    search_slice = slices["search"]

    o11y_daily = (o11y_slice["gb"] / (months_default * 30.416)) if o11y_slice["gb"] else 0.0
    sec_daily = (sec_slice["gb"] / (months_default * 30.416)) if sec_slice["gb"] else 0.0
    o11y_svls = price_o11y_serverless(o11y_daily, months_default) if o11y_daily else None
    # Security serverless: same Complete ingest/retain ladders as proxy until Security estimator wired
    security_svls = (
        {**price_o11y_serverless(sec_daily, months_default), "pricing_proxy": "observability_complete"}
        if sec_daily
        else None
    )

    # Scale ingest workload to search-slice storage share for ES3. The estimator
    # payload uses this same share for docs and docs/sec.
    wl = dict(workload or {})
    share = min(search_slice["gb"] / share_gb, 1.0) if share_gb > 0 else 0.0
    search_share = {
        "search_gb": search_slice["gb"],
        "org_stored_gb": round(share_gb, 3),
        "share": round(share, 6),
    }
    if share > 0 and wl.get("ingest"):
        ing = dict(wl["ingest"])
        if ing.get("docs_per_sec") is not None:
            ing["docs_per_sec"] = round(float(ing["docs_per_sec"]) * share, 2)
        wl = {**wl, "ingest": ing}
    search_svls = (
        price_es3_search(search_slice["gb"], wl) if search_slice["gb"] > 0 else None
    )
    # All-in ES3 (anti-pattern): entire org on Search serverless
    all_es3 = price_es3_search(org_gb, workload) if org_gb > 0 else None

    def line(name: str, project: str, sl: dict, svls: dict | None, method: str) -> dict:
        ech_mo = float(sl["ecu_mo"])
        tot = float((svls or {}).get("total_mo") or 0)
        return {
            "name": name,
            "project": project,
            "method": method,
            "n": sl["n"],
            "gb": sl["gb"],
            "ech_mo": round(ech_mo, 2),
            "serverless_mo": round(tot, 2),
            "serverless_yr": round(tot * 12, 2),
            "save_mo": round(ech_mo - tot, 2),
            "save_yr": round((ech_mo - tot) * 12, 2),
            "detail": svls,
        }

    lines = [
        line(
            "Observability + logging",
            "Observability (Complete)",
            o11y_slice,
            o11y_svls,
            "ingest+retention @ 3mo implied",
        ),
        line(
            "Security",
            "Security (Complete proxy)",
            sec_slice,
            security_svls,
            "same ladders as o11y until Security estimator linked",
        ),
        line(
            "Search",
            "Elasticsearch ES3 On-demand",
            search_slice,
            search_svls,
            "storage-floored VCU × hour mix + $0.047/GB retain",
        ),
    ]

    split_mo = round(sum(x["serverless_mo"] for x in lines), 2)
    reasons: dict[str, dict] = {}
    for x in not_priced:
        r = reasons.setdefault(x["reason"], {"n": 0, "ecu_mo": 0.0})
        r["n"] += 1
        r["ecu_mo"] = round(r["ecu_mo"] + x["ecu_mo"], 2)
    full_split = {
        "retention_months_assumed": months_default,
        "lookback_days": lookback_days,
        "lookback_months": round(months_n, 4),
        "lines": lines,
        "priced_n": priced_n,
        "not_priced": not_priced,
        "not_priced_n": len(not_priced),
        "not_priced_ecu_mo": round(not_priced_ecu / months_n, 2),
        "not_priced_by_reason": reasons,
        "org_ech_mo": org_all_mo,
        "serverless_total_mo": split_mo,
        "serverless_total_yr": round(split_mo * 12, 2),
        # ECH bill of the priced deployments. Deployments in not_priced are on neither side.
        "full_ech_mo": org_mo,
        "full_ech_yr": round(org_mo * 12, 2),
        "save_vs_ech_mo": round(org_mo - split_mo, 2),
        "save_vs_ech_yr": round((org_mo - split_mo) * 12, 2),
        "save_vs_ech_3yr": round((org_mo - split_mo) * 36, 2),
        "vs_all_es3": {
            "all_es3_mo": (all_es3 or {}).get("total_mo"),
            "all_es3_yr": (all_es3 or {}).get("total_yr"),
            "split_saves_vs_all_es3_mo": round(
                float((all_es3 or {}).get("total_mo") or 0) - split_mo, 2
            )
            if all_es3
            else None,
        },
    }

    # Legacy o11y-only scenarios (still useful for sheet push)
    scenarios = []
    for key, months in (
        ("o11y_only_3mo", 3),
        ("o11y_logs_3mo", 3),
        ("o11y_logs_1mo", 1),
        ("o11y_logs_6mo", 6),
        ("o11y_only_1mo", 1),
    ):
        base = slices["o11y_only" if key.startswith("o11y_only") else "o11y_logs"]
        if base["gb"] <= 0:
            continue
        daily = base["gb"] / (months * 30.416)
        priced = price_o11y_serverless(daily, months)
        ech_mo = base["ecu_mo"]
        save_mo = round(ech_mo - priced["total_mo"], 2)
        scenarios.append(
            {
                "id": key,
                "primary": key == "o11y_logs_3mo",
                "slice": base,
                "assumed_retention_months": months,
                "implied_daily_raw_gb": round(daily, 1),
                "serverless": priced,
                "ech_slice_mo": ech_mo,
                "save_vs_ech_slice_mo": save_mo,
                "save_vs_ech_slice_yr": round(save_mo * 12, 2),
                "sheet_inputs": {
                    "feature_tier": "Complete",
                    "support": "Premium",
                    "retention_months": months,
                    "logs_gb_day": round(daily * 0.90, 0),
                    "metrics_gb_day": round(daily * 0.10, 0),
                    "k8s_logs_pct": "0%",
                    "traces_per_min": 0,
                    "synthetics": "No",
                },
            }
        )

    primary = next((s for s in scenarios if s["primary"]), scenarios[0] if scenarios else None)
    remain = slices["remain_search_security"]
    hybrid = None
    if primary:
        hybrid_mo = round(primary["serverless"]["total_mo"] + remain["ecu_mo"], 2)
        hybrid = {
            "note": "O11y/logs → Observability SVLS; search+security stay on ECH",
            "o11y_serverless_mo": primary["serverless"]["total_mo"],
            "remain_ech_mo": remain["ecu_mo"],
            "total_mo": hybrid_mo,
            "total_yr": round(hybrid_mo * 12, 2),
            "full_ech_mo": org_mo,
            "save_vs_full_ech_mo": round(org_mo - hybrid_mo, 2),
            "save_vs_full_ech_yr": round((org_mo - hybrid_mo) * 12, 2),
            "save_vs_full_ech_3yr": round((org_mo - hybrid_mo) * 36, 2),
        }

    return {
        "by_kind": {k: {kk: vv for kk, vv in v.items() if kk != "deps"} for k, v in by_kind.items()},
        "by_kind_deps": {k: v["deps"][:25] for k, v in by_kind.items()},
        "slices": slices,
        "scenarios": scenarios,
        "primary": primary,
        "hybrid": hybrid,
        "full_split": full_split,
        "search_share": search_share,
        "all_es3": all_es3,
        "estimator": {
            "sheets_api": True,
            "o11y_tab": "Observability Estimator",
            "es3_tab": "Estimator",
            "tier": "Complete",
            "support": "Premium",
            "meter_factor": 1.595,
            "security_pricing": "proxy_observability_complete",
        },
        "notes": [
            "full_split = Observability SVLS (o11y+logs) + Security SVLS (proxy) + ES3 Search (search-only GB)",
            "Only ECH deployments with stored GB are compared; full_split.not_priced names the rest and they are on neither side",
            f"ECH $/mo = lookback ECU ÷ {round(months_n, 4)} months ({lookback_days} days ÷ {DAYS_PER_MONTH}); ECU is the billed cost field, 1 ECU = $1",
            "Do NOT put all  fleets on ES3 Search — storage-floored VCU makes that ~all-ES3 anti-pattern",
            "Daily ingest = stored_gb / (retention_months × 30.416); confirm retention with customer",
            "Push sheets via push_estimator.py (OAuth); prefer sheet read-back totals when quoting",
            "Security uses Observability Complete ladders as proxy until a Security estimator is linked",
        ],
    }


def bare_region(region: str | None) -> str | None:
    """`azure-eastus2` → `eastus2`. A region with no provider prefix is returned as is."""
    r = str(region or "").strip().lower()
    return re.sub(r"^(aws|gcp|azure)-", "", r) or None


def build_storage(
    telem_buckets: list[dict],
    last_bill: dict[str, str],
    range_end,
    *,
    stale_days: int = STORAGE_STALE_DAYS,
    billing_days: int = STORAGE_BILLING_DAYS,
) -> tuple[dict, float, float]:
    """Org storage from the newest telemetry snapshot of each deployment.

    `last_bill` is each deployment's last day with a bill in the range. A
    deployment with no bill in the last `billing_days` days of the range is
    listed in `not_billing` and is not in org stored GB, docs, or
    `by_deployment`. A kept snapshot more than `stale_days` days older than
    the range end is listed in `stale`; it still counts.
    Returns (storage, bytes, docs) for the kept deployments.
    """
    cutoff = (range_end - timedelta(days=billing_days - 1)).isoformat()
    rows: list[dict] = []
    not_billing: list[dict] = []
    stale: list[dict] = []
    sum_bytes = sum_docs = 0.0
    as_of_dates: list[str] = []
    for b in telem_buckets or []:
        hits = b["latest"]["hits"]["hits"]
        if not hits:
            continue
        src = hits[0]["_source"]
        tot = (src.get("telemetry") or {}).get("totals") or {}
        size_b = float(tot.get("dataset_size_bytes") or 0)
        docs = float(tot.get("docs_count") or 0)
        ts = str(src.get("@timestamp") or "")[:10]
        try:
            age = (range_end - date.fromisoformat(ts)).days if ts else None
        except ValueError:
            age = None
        gb = round(size_b / (1024**3), 3)
        last = last_bill.get(b["key"])
        if last is None or last < cutoff:
            not_billing.append({
                "name": b["key"],
                "as_of": ts or None,
                "data_stored_gb": gb,
                "last_billed": last,
                "reason": "no bill in the range" if last is None else f"no bill in the last {billing_days} days of the range",
            })
            continue
        if ts:
            as_of_dates.append(ts)
        sum_bytes += size_b
        sum_docs += docs
        avg_kb = (size_b / docs / 1024.0) if docs > 0 else 0.0
        rows.append(
            {
                "name": b["key"],
                "as_of": ts or None,
                "age_days": age,
                "data_stored_gb": gb,
                "docs_count": int(docs) if docs == int(docs) else docs,
                "avg_doc_kb": round(avg_kb, 4),
                "calculated_data_stored_gb": round((docs * avg_kb) / (1024**2), 3) if docs else 0.0,
            }
        )
        if age is None or age > stale_days:
            stale.append({"name": b["key"], "as_of": ts or None, "age_days": age, "data_stored_gb": gb})
    rows.sort(key=lambda x: -x["data_stored_gb"])
    stale.sort(key=lambda x: -x["data_stored_gb"])
    not_billing.sort(key=lambda x: -x["data_stored_gb"])
    org_avg_kb = (sum_bytes / sum_docs / 1024.0) if sum_docs > 0 else 0.0
    notes = [
        "From cloud-tracker-telemetry latest snapshot per deployment (telemetry.totals)",
        "avg_doc_kb = dataset_size_bytes / docs_count / 1024",
        "calculated_data_stored_gb = docs_count × avg_doc_kb / 1024² (should match data_stored_gb)",
        "Each row's as_of is that deployment's own snapshot day; storage.as_of is the newest of them",
    ]
    if stale:
        ages = [x["age_days"] for x in stale if x["age_days"] is not None]
        notes.insert(0, (
            f"{len(stale)} of {len(rows)} storage snapshots are more than {stale_days} days older than the range end"
            + (f" (oldest {max(ages)} days)" if ages else "")
        ))
    if not_billing:
        notes.append(
            f"{len(not_billing)} deployments with a snapshot have no bill in the last {billing_days} days of the range "
            f"and are left out of org stored GB ({round(sum(x['data_stored_gb'] for x in not_billing), 3)} GB)"
        )
    storage = {
        "as_of": max(as_of_dates) if as_of_dates else None,
        "as_of_oldest": min(as_of_dates) if as_of_dates else None,
        "deployments_with_telemetry": len(rows),
        "data_stored_gb": round(sum_bytes / (1024**3), 3),
        "docs_count": int(sum_docs) if sum_docs == int(sum_docs) else sum_docs,
        "avg_doc_kb": round(org_avg_kb, 4),
        "calculated_data_stored_gb": round((sum_docs * org_avg_kb) / (1024**2), 3) if sum_docs else 0.0,
        "by_deployment": rows,
        "stale_after_days": stale_days,
        "stale": stale,
        "stale_gb": round(sum(x["data_stored_gb"] for x in stale), 3),
        "billing_window_days": billing_days,
        "not_billing": not_billing,
        "not_billing_gb": round(sum(x["data_stored_gb"] for x in not_billing), 3),
        "notes": notes,
    }
    return storage, sum_bytes, sum_docs


def _cplx(label: str | None) -> str:
    if label == "Low":
        return "simple"
    if label == "High":
        return "complex"
    return "medium"


def estimator_payload(
    org_name: str,
    generated_at: str,
    infra_summary: dict,
    storage: dict,
    workload: dict,
    offload: dict,
) -> dict:
    """Estimator inputs for push_estimator.py and the Apps Script importer. No network.

    Every cell in `cells` has a value, so no template sample is left behind. A
    value that was not measured is 0 and its cell is in `unmeasured_cells`. A
    value that is this skill's own assumption is in `assumed_cells`. The cloud
    region is not a cell here: `cloud` carries the org's provider and region,
    and the fill matches them to the sheet's own options.
    """
    cloud = {
        "provider": str(infra_summary.get("provider") or "").upper() or None,
        "region": bare_region(infra_summary.get("region")),
    }
    primary = offload.get("primary") or {}
    si = primary.get("sheet_inputs") or {}
    share = offload.get("search_share") or {}
    search_gb = float(share.get("search_gb") or 0)
    share_gb = float(share.get("org_stored_gb") or 0)
    search_share = min(search_gb / share_gb, 1.0) if share_gb > 0 else 0.0
    search_docs = int(float(storage.get("docs_count") or 0) * search_share)
    search = workload.get("search") or {}
    peak = search.get("peak") or {}
    nonpeak = search.get("nonpeak") or {}
    ingest = workload.get("ingest") or {}
    mix = search_hour_mix(workload)
    assumed: list[str] = []
    unmeasured: list[str] = []

    def measured(cell: str, value):
        if value is None:
            unmeasured.append(cell)
            return 0
        return value

    if mix["assumed"]:
        assumed += ["C21", "C25", "C27"]
    for cell, band in (("C20", peak), ("C24", nonpeak)):
        if band.get("complexity") is None:
            assumed.append(cell)
    active_h = ingest.get("active_hours_mo")
    if not active_h or float(active_h) <= 0:
        active_h = ASSUMED_ACTIVE_HOURS
        assumed.append("C32")
    dps = ingest.get("docs_per_sec")
    es3_cells = {
        "C12": round(search_gb, 3),
        "C13": search_docs,
        "C14": storage.get("avg_doc_kb") or 0,
        "C19": measured("C19", peak.get("qps")),
        "C20": _cplx(peak.get("complexity")),
        "C21": f"{mix['peak_pct']:g}%",
        "C23": measured("C23", nonpeak.get("qps")),
        "C24": _cplx(nonpeak.get("complexity")),
        "C25": f"{mix['nonpeak_pct']:g}%",
        "C27": f"{mix['idle_pct']:g}%",
        "C30": round(float(measured("C30", dps)) * search_share, 2),
        "C31": measured("C31", ingest.get("avg_doc_bytes")),
        "C32": active_h,
    }
    o11y_unmeasured = [c for c, k in (("G23", "logs_gb_day"), ("G25", "metrics_gb_day")) if si.get(k) is None]
    return {
        "org": org_name,
        "generated_at": generated_at,
        "methodology": "full_split: ES3=search-only storage; O11y=o11y+logs @3mo",
        "cloud": cloud,
        "es3": {
            "sheet_tab": "Estimator",
            # No ES3 layout is recorded in estimator_layout.py, so push_estimator.py does not fill this block.
            "layout": None,
            # The provider and region cells are not written: the sheet's option strings are not known here.
            "cloud": {**cloud, "provider_cell": "C5", "region_cell": "C6"},
            "search_share": share,
            "hour_mix": mix,
            "assumed": bool(assumed),
            "assumed_cells": assumed,
            "unmeasured_cells": unmeasured,
            "cells": es3_cells,
        },
        "o11y": {
            "sheet_tab": "Observability Estimator",
            # Cell addresses for Observability estimator v1.22 (estimator_layout.py).
            "layout": "1.22",
            # G8 is matched at fill time to an option on the sheet (estimator_layout.match_region).
            "cloud": {**cloud, "cell": "G8"},
            "unmeasured_cells": o11y_unmeasured,
            "cells": {
                "G4": si.get("feature_tier", "Complete"),
                "G5": si.get("support", "Premium"),
                "G11": si.get("retention_months", 3),
                "G12": si.get("retention_months", 3),   # TSDS metrics keep the same month
                "G15": si.get("synthetics", "No"),
                "G19": 0,                                # Option 1 is not used
                "G20": 0,
                "G23": si.get("logs_gb_day") or 0,
                "G24": si.get("k8s_logs_pct", "0%"),
                "G25": si.get("metrics_gb_day") or 0,
                "G26": si.get("traces_per_min", 0),
                "G27": "100%",
                "G33": "No",                             # cross-project search add-on
            },
        },
    }


def org_name_query(name: str) -> dict:
    """Organizations whose name starts with `name`, any case. `*`, `?` and `\\` in the name are literal."""
    escaped = "".join("\\" + c if c in "*?\\" else c for c in name)
    return {"wildcard": {"organization.name": {"value": f"{escaped}*", "case_insensitive": True}}}


def choose_pivots(pivots: list[dict], wanted: str) -> list[dict]:
    """The pivots that are the organization asked for.

    An exact name, any case, wins. With none, the one organization whose name
    starts with `wanted` is taken. A hit that is neither stops the run, and so
    do several different organizations that start with it.
    """
    want = " ".join(str(wanted or "").split()).casefold()

    def _name(p: dict) -> str:
        return " ".join(str((p.get("organization") or {}).get("name") or "").split())

    exact = [p for p in pivots if _name(p).casefold() == want]
    if exact:
        return exact
    prefix = [p for p in pivots if want and _name(p).casefold().startswith(want)]
    names = sorted({_name(p) for p in prefix})
    if len(names) == 1:
        print(f"org → {names[0]}", file=sys.stderr)
        return prefix
    shown = ", ".join(
        f"{(p.get('organization') or {}).get('id')} {_name(p)}" for p in (prefix or pivots)[:10]
    )
    if names:
        raise SystemExit(
            f"Several organizations start with {wanted!r}. Pass the exact name or --org-id. Matches: {shown}"
        )
    raise SystemExit(
        f"No organization is named {wanted!r} or starts with it. Pass the exact name or --org-id. Closest hits: {shown}"
    )


def write_private(path: Path, text: str) -> None:
    """Write a customer file readable by this user only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def slim_org(org: dict | None) -> dict:
    if not org:
        return {}
    out = {k: org[k] for k in ORG_KEEP if k in org}
    c = org.get("contract") or {}
    if c:
        out["contract"] = {
            k: c[k]
            for k in ("start", "end", "status", "opportunity", "number", "term")
            if k in c
        }
    return out


def rollup_weekly(days: list[str], series: list[list[float]], total: list[float], step: int = 7):
    w_days, w_series, w_total = [], [[] for _ in series], []
    for i in range(0, len(days), step):
        w_days.append(days[i])
        for si, row in enumerate(series):
            w_series[si].append(round(sum(row[i : i + step]), 2))
        w_total.append(round(sum(total[i : i + step]), 2))
    # drop trailing all-zero bucket
    while w_total and w_total[-1] == 0 and all(s[-1] == 0 for s in w_series):
        w_days.pop()
        w_total.pop()
        for s in w_series:
            s.pop()
    return w_days, w_series, w_total


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--org", required=True)
    ap.add_argument(
        "--org-id",
        action="append",
        default=None,
        help="Cloud organization id. Repeat when one customer name covers several orgs. Billing, storage, and workload cover every id. The contract pivot is the id with the highest commit.",
    )
    ap.add_argument("--env", default=".env")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--out", default="-")
    ap.add_argument("--chart", choices=("week", "day"), default="week", help="Chart granularity (default week)")
    ap.add_argument("--pretty", action="store_true", help="Indent JSON (larger)")
    ap.add_argument("--verbose", action="store_true", help="Include per-type cost dumps")
    ap.add_argument(
        "--skip-workload",
        action="store_true",
        help="Skip search/ingest workload (saves 2 ES calls)",
    )
    args = ap.parse_args()

    if args.days < 1:
        raise SystemExit("--days must be 1 or more")
    env = load_env(Path(args.env))
    base, key = es_base_and_key(env)
    lb_months = lookback_months(args.days)
    truncated: list[str] = []

    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=args.days - 1)
    rng = {"gte": f"{start.isoformat()}T00:00:00Z", "lte": f"{end.isoformat()}T23:59:59Z"}
    org_ids_arg = [str(i).strip() for i in (args.org_id or []) if str(i).strip()]
    if org_ids_arg:
        pivots = [
            h["_source"]
            for h in es_post(
                base,
                key,
                "cloud-tracker-organization_pivot/_search",
                {"size": len(org_ids_arg), "query": {"terms": {"organization.id": org_ids_arg}}},
            )["hits"]["hits"]
        ]
        found = {str((p.get("organization") or {}).get("id")): p for p in pivots}
        missing = [i for i in org_ids_arg if i not in found]
        if missing:
            raise SystemExit(f"No organization_pivot for {missing}")

        def _commit(p: dict) -> float:
            try:
                return float((p.get("pace") or {}).get("commit") or 0)
            except (TypeError, ValueError):
                return 0.0

        pivot = max((found[i] for i in org_ids_arg), key=_commit)
        org_ids = org_ids_arg
    else:
        org_q = {"match_phrase": {"organization.name": args.org}}
        pivots = [
            h["_source"]
            for h in es_post(base, key, "cloud-tracker-organization_pivot/_search", {"size": 20, "query": org_q})[
                "hits"
            ]["hits"]
        ]
        if not pivots:
            # The name is not sent through query_string: "Foo Corp" parsed there as name:Foo OR Corp*.
            pivots = [
                h["_source"]
                for h in es_post(
                    base,
                    key,
                    "cloud-tracker-organization_pivot/_search",
                    {"size": 20, "query": org_name_query(args.org.strip())},
                )["hits"]["hits"]
            ]
        if not pivots:
            raise SystemExit(f"No organization_pivot for {args.org!r}")
        chosen = choose_pivots(pivots, args.org)
        if len({str((p.get("organization") or {}).get("id")) for p in chosen}) > 1:
            shown = ", ".join(
                f"{(p.get('organization') or {}).get('id')} {(p.get('organization') or {}).get('name')}"
                for p in chosen
            )
            raise SystemExit(
                f"Several cloud orgs share {args.org!r}. Pass each --org-id. Matches: {shown}"
            )
        pivot = chosen[0]
        org_ids = []
    org_id = (pivot.get("organization") or {}).get("id", "__none__")
    # Prefer resolved pivot name/id — match_phrase on a short CLI token
    # (e.g. "GlaxoSmithKline") misses billing docs named "… LLC".
    org_name = (pivot.get("organization") or {}).get("name") or args.org
    if not org_ids:
        org_ids = [str(org_id)] if org_id and org_id != "__none__" else []
    if org_ids:
        org_q = org_clause(org_ids)
    else:
        org_q = {"match_phrase": {"organization.name": org_name}}
    if len(org_ids) > 1:
        print(
            f"organizations: {', '.join(org_ids)}; contract pivot {org_id} (highest commit)",
            file=sys.stderr,
        )
    filt = [org_q, {"range": {"@timestamp": rng}}]

    bill = es_post(
        base,
        key,
        "cloud-tracker-billing-*/_search",
        {
            "size": 0,
            "query": {"bool": {"filter": filt}},
            "aggs": {
                "by_dep": {
                    "terms": {"field": "deployment.name", "size": DEP_TERMS_SIZE, "order": {"ecu": "desc"}},
                    "aggs": {
                        "ecu": {"sum": {"field": "cost"}},
                        "types": {"terms": {"field": "type", "size": 50}, "aggs": {"ecu": {"sum": {"field": "cost"}}}},
                        "skus": {"terms": {"field": "sku", "size": 40}, "aggs": {"ecu": {"sum": {"field": "cost"}}}},
                        "ai_skus": {
                            "terms": {"field": "sku", "size": 40, "include": ".*inference.*"},
                            "aggs": {"ecu": {"sum": {"field": "cost"}}, "qty": {"sum": {"field": "quantity"}}},
                        },
                        "ml_skus": {
                            "terms": {"field": "sku", "size": 30, "include": ".*(\\.es\\.ml\\.|\\.ml\\.).*"},
                            "aggs": {"ecu": {"sum": {"field": "cost"}}},
                        },
                        "templates": {"terms": {"field": "deployment.template", "size": 5}},
                        "versions": {"terms": {"field": "deployment.stack_version", "size": 5}},
                    },
                },
                "by_day": {
                    "date_histogram": {
                        "field": "@timestamp",
                        "calendar_interval": "day",
                        "min_doc_count": 0,
                        "extended_bounds": {"min": rng["gte"], "max": rng["lte"]},
                    },
                    "aggs": {
                        "by_dep": {
                            # Same size and order as the top-level roster, so a big spender with
                            # few billing lines is not dropped from a day.
                            "terms": {"field": "deployment.name", "size": DEP_TERMS_SIZE, "order": {"ecu": "desc"}},
                            "aggs": {"ecu": {"sum": {"field": "cost"}}},
                        },
                        "ecu": {"sum": {"field": "cost"}},
                    },
                },
                "types_total": {"terms": {"field": "type", "size": 80}, "aggs": {"ecu": {"sum": {"field": "cost"}}}},
                "cloud_skus": {
                    "terms": {"field": "sku", "size": 200, "include": "Cloud-.*", "order": {"ecu": "desc"}},
                    "aggs": {"ecu": {"sum": {"field": "cost"}}},
                },
                "ai_skus": {
                    "terms": {"field": "sku", "size": 80, "include": ".*inference.*"},
                    "aggs": {"ecu": {"sum": {"field": "cost"}}, "qty": {"sum": {"field": "quantity"}}},
                },
                "inference_total": {
                    "filter": {"wildcard": {"sku": "*inference*"}},
                    "aggs": {
                        "ecu": {"sum": {"field": "cost"}},
                        "qty": {"sum": {"field": "quantity"}},
                    },
                },
                "ml_skus": {
                    "terms": {"field": "sku", "size": 40, "include": ".*(\\.es\\.ml\\.|\\.ml\\.).*"},
                    "aggs": {"ecu": {"sum": {"field": "cost"}}},
                },
            },
        },
    )

    feats = es_post(
        base,
        key,
        "cloud-tracker-features-*/_search",
        {
            "size": 0,
            "query": {"bool": {"filter": [org_q, {"range": {"@timestamp": {"gte": "now-400d"}}}]}},
            "aggs": {
                "use_case": {"terms": {"field": "use_case", "size": 10}},
                "by_dep": {
                    "terms": {"field": "deployment.name", "size": DEP_TERMS_SIZE},
                    "aggs": {
                        "latest": {
                            "top_hits": {
                                "size": 1,
                                "sort": [{"@timestamp": "desc"}],
                                "_source": ["use_case", "feature", "deployment.stack_version"],
                            }
                        }
                    },
                },
            },
        },
    )

    # Dataset size / docs from telemetry (latest snapshot per deployment)
    telem = es_post(
        base,
        key,
        "cloud-tracker-telemetry/_search",
        {
            "size": 0,
            "query": {"bool": {"filter": [org_clause(org_ids or [org_id])]}},
            "aggs": {
                "by_dep": {
                    "terms": {"field": "deployment.name", "size": DEP_TERMS_SIZE},
                    "aggs": {
                        "latest": {
                            "top_hits": {
                                "size": 1,
                                "sort": [{"@timestamp": "desc"}],
                                "_source": [
                                    "@timestamp",
                                    "deployment.name",
                                    "telemetry.totals",
                                    "telemetry.tiers",
                                ],
                            }
                        }
                    },
                }
            },
        },
    )

    inv_res = es_post(
        base,
        key,
        "cloud-tracker-deployments/_search",
        {
            "size": DEP_TERMS_SIZE,
            "query": org_clause(org_ids or [org_id]),
            "_source": ["name", "stack_version", "template", "provider", "hot_instance_type"],
        },
    )["hits"]
    inv = {h["_source"]["name"]: h["_source"] for h in inv_res["hits"] if h["_source"].get("name")}

    # A terms aggregation that left deployments out says so in sum_other_doc_count.
    def _other(agg: dict | None) -> int:
        return int((agg or {}).get("sum_other_doc_count") or 0)

    if _other(bill["aggregations"]["by_dep"]) > 0:
        truncated.append("billing.by_dep")
    if any(_other(b.get("by_dep")) > 0 for b in bill["aggregations"]["by_day"]["buckets"]):
        truncated.append("billing.by_day.by_dep")
    if _other(feats.get("aggregations", {}).get("by_dep")) > 0:
        truncated.append("features.by_dep")
    if _other(telem.get("aggregations", {}).get("by_dep")) > 0:
        truncated.append("telemetry.by_dep")
    inv_total = inv_res.get("total")
    inv_total = inv_total.get("value") if isinstance(inv_total, dict) else inv_total
    if inv_total and int(inv_total) > len(inv_res["hits"]):
        truncated.append("deployments.inventory")
    for name in truncated:
        print(
            f"WARN: {name} holds more than {DEP_TERMS_SIZE} deployments. Some are left out of this report; "
            "the output's `truncated` list names the aggregation.",
            file=sys.stderr,
        )

    feat_by = {}
    for b in feats.get("aggregations", {}).get("by_dep", {}).get("buckets", []):
        hits = b["latest"]["hits"]["hits"]
        if hits:
            feat_by[b["key"]] = hits[0]["_source"]

    deployments, fam_tot = [], {k: 0.0 for k in FAMILY_KEYS}
    pc, pe = {"ech": 0, "serverless": 0}, {"ech": 0.0, "serverless": 0.0}
    # name -> {sku: {ecu, qty}} collected after we have per-dep sku terms with inference include
    dep_meta: dict[str, dict] = {}

    for b in bill["aggregations"]["by_dep"]["buckets"]:
        name = b["key"]
        if hex_id(name):
            continue
        types = {t["key"]: t["ecu"]["value"] for t in b["types"]["buckets"]}
        skus = {t["key"]: t["ecu"]["value"] for t in b["skus"]["buckets"]}
        template = b["templates"]["buckets"][0]["key"] if b["templates"]["buckets"] else None
        version = str(b["versions"]["buckets"][0]["key"]) if b["versions"]["buckets"] else None
        feat = feat_by.get(name) or {}
        inv_row = inv.get(name) or {}
        version = version or inv_row.get("stack_version") or (feat.get("deployment") or {}).get("stack_version")
        template = template or inv_row.get("template")
        provider = inv_row.get("provider")
        hot_instance = inv_row.get("hot_instance_type")
        use_case = feat.get("use_case") or "unknown"
        plat = platform(types, skus, template)
        ecu = float(b["ecu"]["value"])
        pc[plat] += 1
        pe[plat] += ecu
        # The families partition the deployment's ECU, so the org families sum to the org total.
        families = split_families(types, ecu, use_case)
        for k, v in families.items():
            fam_tot[k] += v
        ff = feat.get("feature") or {}
        ml_skus = []
        for sb in b.get("ml_skus", {}).get("buckets", []):
            if sb["ecu"]["value"] <= 0:
                continue
            dec = decode_ml_sku(sb["key"])
            ml_skus.append({**dec, "ecu": round(sb["ecu"]["value"], 2)})
        # Fallback if dedicated agg empty (older indices / include miss)
        if not ml_skus:
            for s, c in skus.items():
                if c > 0 and is_ml_sku(s):
                    ml_skus.append({**decode_ml_sku(s), "ecu": round(c, 2)})
        ml_skus.sort(key=lambda x: -x["ecu"])
        ml_ecu = round(sum(x["ecu"] for x in ml_skus), 2)
        # Infer provider/region from billing SKUs when inventory missing
        region = None
        for s, c in skus.items():
            parsed = parse_cloud_sku(s)
            where = parsed or cloud_sku_region(s)
            if not where:
                continue
            if not provider:
                provider = where["provider"]
            if not region:
                region = where["region"]
            if parsed and not hot_instance and parsed["tier"] == "datahot":
                hot_instance = f"{parsed['provider'].lower()}.{parsed['instance']}"
        ai_sku_agg = b.get("ai_skus") or {}
        infer_skus = []
        for sb in ai_sku_agg.get("buckets") or []:
            if sb["ecu"]["value"] <= 0 and sb["qty"]["value"] <= 0:
                continue
            dec = decode_inference_sku(sb["key"])
            ecu_v, qty_v = float(sb["ecu"]["value"]), float(sb["qty"]["value"])
            infer_skus.append({
                **dec,
                "ecu": round(ecu_v, 4),
                "qty": round(qty_v, 6),
                "million_tokens": round(qty_v, 6),
                "ecu_per_million": round(ecu_v / qty_v, 4) if qty_v else None,
            })
        infer_skus.sort(key=lambda x: -x["ecu"])
        models = sorted({x["model"] for x in infer_skus if x.get("model")})
        item = {
            "name": name,
            "version": version,
            "template": template,
            "provider": provider,
            "region": region,
            "hot_instance_type": hot_instance,
            "platform": plat,
            "use_case": use_case,
            "ecu": round(ecu, 2),
            # Lookback ECU ÷ (days ÷ 30.416): the ECU / mo figure the report prints.
            "ecu_mo": round(ecu / lb_months, 2),
            "ecu_monthly_avg": 0.0,  # filled after month rollup (mean of calendar-month totals)
            "months_active": 0,
            "families": {k: round(v, 2) for k, v in families.items()},
            "ml_ecu": ml_ecu,
            "ml_skus": ml_skus[:8],
            "ai": {
                "elser": bool((ff.get("search") or {}).get("elser")),
                "genai_connector": bool((ff.get("search") or {}).get("genai_connector")),
                "playground": bool((ff.get("search") or {}).get("playground")),
                "vector_mappings": bool((ff.get("search") or {}).get("vector_mappings")),
                "ai_assistant": bool((ff.get("observability") or {}).get("ai_assistant")),
                "attack_discovery": bool((ff.get("security") or {}).get("attack_discovery")),
                "inference_ecu": round(families["genai"], 2),
                "inference_skus": infer_skus,
                "inference_skus_truncated": int(ai_sku_agg.get("sum_other_doc_count") or 0) > 0,
                "models": models,
            },
        }
        if args.verbose:
            item["types"] = {k: round(v, 2) for k, v in sorted(types.items(), key=lambda x: -x[1])[:15]}
            item["skus_top"] = {k: round(v, 2) for k, v in sorted(skus.items(), key=lambda x: -x[1])[:10]}
        deployments.append(item)
        dep_meta[name] = item
    deployments.sort(key=lambda d: -d["ecu"])

    top = [d["name"] for d in deployments[:4]]
    days, total_daily, series = [], [], {n: [] for n in top}
    other_daily = []
    # month -> dep -> ecu (for monthly averages)
    month_dep: dict[str, dict[str, float]] = {}
    # day -> set of named deps with spend (for deployments-over-time)
    day_active_deps: list[set[str]] = []
    # deployment -> last day in the range with a bill (hex ids included)
    last_bill: dict[str, str] = {}
    for bucket in bill["aggregations"]["by_day"]["buckets"]:
        day = bucket["key_as_string"][:10]
        days.append(day)
        total_daily.append(round(bucket["ecu"]["value"], 2))
        dep_map = {x["key"]: x["ecu"]["value"] for x in bucket["by_dep"]["buckets"]}
        active = {k for k, v in dep_map.items() if not hex_id(k) and v > 0}
        day_active_deps.append(active)
        for k, v in dep_map.items():
            if v > 0:
                last_bill[k] = day
        ym = day[:7]
        md = month_dep.setdefault(ym, {})
        for k, v in dep_map.items():
            if hex_id(k):
                continue
            md[k] = md.get(k, 0.0) + v
        for n in top:
            series[n].append(round(dep_map.get(n, 0.0), 2))
        other_daily.append(
            round(sum(v for k, v in dep_map.items() if k not in top and not hex_id(k)), 2)
        )
    series_list = [series[n] for n in top]
    names = top + (["Other"] if any(other_daily) else [])
    if any(other_daily):
        series_list.append(other_daily)

    # Monthly avg = mean of calendar-month totals over months in range (incl. partials)
    months_sorted = sorted(month_dep.keys())
    n_months = max(len(months_sorted), 1)
    for d in deployments:
        monthly_vals = [month_dep[m].get(d["name"], 0.0) for m in months_sorted]
        active = [v for v in monthly_vals if v > 0]
        d["months_active"] = len(active)
        # average across all months in lookback (zeros pull down short-lived deps)
        d["ecu_monthly_avg"] = round(sum(monthly_vals) / n_months, 2)
        d["ecu_monthly_avg_active"] = round(sum(active) / len(active), 2) if active else 0.0

    month_series = {
        "months": months_sorted,
        "series_names": names,
        "ecu_series": [
            [round(month_dep[m].get(n, 0.0), 2) for m in months_sorted]
            for n in top
        ]
        + (
            [
                [
                    round(
                        sum(v for k, v in month_dep[m].items() if k not in top and not hex_id(k)),
                        2,
                    )
                    for m in months_sorted
                ]
            ]
            if any(other_daily)
            else []
        ),
        "by_deployment": {
            d["name"]: {
                "months": months_sorted,
                "ecu": [round(month_dep[m].get(d["name"], 0.0), 2) for m in months_sorted],
                "avg": d["ecu_monthly_avg"],
                "avg_active": d["ecu_monthly_avg_active"],
            }
            for d in deployments
        },
    }

    billed_all = list(zip(days, total_daily, day_active_deps))
    while billed_all and billed_all[-1][1] <= 0:
        billed_all.pop()
    # Days before the org's first bill are not billed days.
    first_billed = next((i for i, row in enumerate(billed_all) if row[1] > 0), 0)
    billed_all = billed_all[first_billed:]

    def _billed(n: int) -> dict:
        """Who billed on the last `n` days that end on the last day with a positive total."""
        rows = billed_all[-n:]
        names_n: set[str] = set()
        for _day, _ecu, active in rows:
            names_n |= active
        return {
            "start": rows[0][0] if rows else None,
            "end": rows[-1][0] if rows else None,
            "days": len(rows),
            "names": sorted(names_n),
        }

    # The quote window is 90 days by default. 30 stays for a run that asks for it and for older readers.
    billed_windows = {str(n): _billed(n) for n in BILLED_WINDOW_DAYS}
    billed_window = billed_windows["30"]
    if args.chart == "week":
        c_days, c_series, c_total = rollup_weekly(days, series_list, total_daily)
        chart = {"grain": "week", "days": c_days, "series_names": names, "ecu_series": c_series, "total": c_total, "billed_window": billed_window, "billed_windows": billed_windows}
    else:
        chart = {
            "grain": "day",
            "days": days,
            "series_names": names,
            "ecu_series": series_list,
            "total": total_daily,
            "billed_window": billed_window,
            "billed_windows": billed_windows,
        }

    # Distinct named deployments with billing spend > 0 per week / month
    week_periods, week_counts = [], []
    step = 7
    for i in range(0, len(days), step):
        union: set[str] = set()
        for s in day_active_deps[i : i + step]:
            union |= s
        week_periods.append(days[i])
        week_counts.append(len(union))
    while week_counts and week_counts[-1] == 0:
        week_periods.pop()
        week_counts.pop()
    month_counts = [sum(1 for v in month_dep[m].values() if v > 0) for m in months_sorted]
    dep_over_time = {
        "note": "Distinct named deployments with billing ECU > 0 in period (32-char hex ids excluded)",
        "week": {"periods": week_periods, "count": week_counts},
        "month": {"periods": months_sorted, "count": month_counts},
    }

    ai_billing = {
        t["key"]: round(t["ecu"]["value"], 2)
        for t in bill["aggregations"]["types_total"]["buckets"]
        if family(t["key"]) == "genai" or t["key"].strip().lower().startswith("inference")
    }
    ai_sku_agg = bill["aggregations"].get("ai_skus") or {}
    ai_skus = []
    for b in ai_sku_agg.get("buckets") or []:
        if b["ecu"]["value"] <= 0 and b["qty"]["value"] <= 0:
            continue
        dec = decode_inference_sku(b["key"])
        ecu_v, qty_v = float(b["ecu"]["value"]), float(b["qty"]["value"])
        ai_skus.append({
            **dec,
            "ecu": round(ecu_v, 4),
            "qty": round(qty_v, 6),
            "million_tokens": round(qty_v, 6),
            "ecu_per_million": round(ecu_v / qty_v, 4) if qty_v else None,
        })
    inf_tot = (bill["aggregations"].get("inference_total") or {})
    inf_billed_ecu = float((inf_tot.get("ecu") or {}).get("value") or 0)
    inf_billed_qty = float((inf_tot.get("qty") or {}).get("value") or 0)

    # Per-deployment AI rollup for the report table
    ai_by_dep = [
        {
            "name": d["name"],
            "elser": d["ai"]["elser"],
            "genai_connector": d["ai"]["genai_connector"],
            "playground": d["ai"]["playground"],
            "vector_mappings": d["ai"]["vector_mappings"],
            "ml_ecu": d["ml_ecu"],
            "inference_ecu": d["ai"]["inference_ecu"],
            "models": d["ai"]["models"],
            "inference_skus": d["ai"]["inference_skus"],
        }
        for d in deployments
        if d["ai"]["elser"]
        or d["ai"]["genai_connector"]
        or d["ai"]["playground"]
        or d["ai"]["vector_mappings"]
        or d["ai"]["inference_ecu"] > 0
        or d["ml_ecu"] > 0
        or d["ai"]["models"]
    ]

    org_ml_skus = []
    for b in bill["aggregations"]["ml_skus"]["buckets"]:
        if b["ecu"]["value"] <= 0:
            continue
        org_ml_skus.append({**decode_ml_sku(b["key"]), "ecu": round(b["ecu"]["value"], 2)})
    org_ml_skus.sort(key=lambda x: -x["ecu"])
    ml_total = round(sum(d["ml_ecu"] for d in deployments), 2)
    org_total_ecu = round(sum(d["ecu"] for d in deployments), 2)
    ml_by_dep = [
        {
            "name": d["name"],
            "ml_ecu": d["ml_ecu"],
            "pct_of_dep": round(100.0 * d["ml_ecu"] / d["ecu"], 1) if d["ecu"] else 0.0,
            "elser": d["ai"]["elser"],
            "skus": d["ml_skus"],
        }
        for d in deployments
        if d["ml_ecu"] > 0
    ]
    ml_instances = sorted({s["instance"] for s in org_ml_skus if s.get("instance")})
    ml_regions = sorted({s["region"] for s in org_ml_skus if s.get("region")})

    ver_counts = Counter(str(d.get("version") or "unknown") for d in deployments)
    by_version = [{"version": v, "count": c} for v, c in ver_counts.most_common()]

    # Infrastructure rollup — providers / regions / hot instances / capacity SKUs
    prov_counts: Counter[str] = Counter()
    hot_counts: Counter[str] = Counter()
    tmpl_counts: Counter[str] = Counter()
    region_counts: Counter[str] = Counter()
    for d in deployments:
        if d.get("provider"):
            prov_counts[str(d["provider"]).upper()] += 1
        if d.get("hot_instance_type"):
            hot_counts[str(d["hot_instance_type"])] += 1
        if d.get("template"):
            tmpl_counts[str(d["template"])] += 1
        if d.get("region"):
            region_counts[str(d["region"])] += 1
    # Fill gaps from inventory-only deps (no billing in lookback)
    for name, src in inv.items():
        if any(d["name"] == name for d in deployments):
            continue
        if src.get("provider"):
            prov_counts[str(src["provider"]).upper()] += 1
        if src.get("hot_instance_type"):
            hot_counts[str(src["hot_instance_type"])] += 1
        if src.get("template"):
            tmpl_counts[str(src["template"])] += 1

    sku_provider_ecu: Counter[str] = Counter()
    sku_region_ecu: Counter[str] = Counter()
    sku_instance_ecu: Counter[str] = Counter()
    sku_tier_ecu: Counter[str] = Counter()
    for sb in bill["aggregations"].get("cloud_skus", {}).get("buckets", []):
        parsed = parse_cloud_sku(sb["key"])
        if not parsed:
            continue
        ecu_v = float(sb["ecu"]["value"])
        if ecu_v <= 0:
            continue
        sku_provider_ecu[parsed["provider"]] += ecu_v
        sku_region_ecu[parsed["region"]] += ecu_v
        sku_instance_ecu[parsed["instance"]] += ecu_v
        if parsed.get("tier"):
            sku_tier_ecu[parsed["tier"]] += ecu_v

    infrastructure = {
        "providers": [{"provider": p, "deployments": c} for p, c in prov_counts.most_common()],
        "regions": [{"region": r, "deployments": c} for r, c in region_counts.most_common()]
        or [{"region": r, "ecu": round(e, 2)} for r, e in sku_region_ecu.most_common(8)],
        "hot_instance_types": [
            {"instance": i, "deployments": c} for i, c in hot_counts.most_common()
        ],
        "templates": [{"template": t, "deployments": c} for t, c in tmpl_counts.most_common()],
        "capacity_by_instance": [
            {"instance": i, "ecu": round(e, 2)} for i, e in sku_instance_ecu.most_common(12)
        ],
        "capacity_by_tier": [
            {"tier": t, "ecu": round(e, 2)} for t, e in sku_tier_ecu.most_common()
        ],
        "capacity_by_provider": [
            {"provider": p, "ecu": round(e, 2)} for p, e in sku_provider_ecu.most_common()
        ],
        "summary": {
            "provider": prov_counts.most_common(1)[0][0] if prov_counts else (
                sku_provider_ecu.most_common(1)[0][0] if sku_provider_ecu else None
            ),
            "region": (
                region_counts.most_common(1)[0][0]
                if region_counts
                else (sku_region_ecu.most_common(1)[0][0] if sku_region_ecu else None)
            ),
            "hot_instance": hot_counts.most_common(1)[0][0] if hot_counts else None,
            "template": tmpl_counts.most_common(1)[0][0] if tmpl_counts else None,
        },
    }

    storage, sum_bytes, sum_docs = build_storage(
        telem.get("aggregations", {}).get("by_dep", {}).get("buckets", []), last_bill, end
    )
    storage_by_dep = storage["by_deployment"]
    if storage["stale"]:
        print(f"WARN: {storage['notes'][0]}", file=sys.stderr)
    if storage["not_billing"]:
        print(
            f"WARN: {len(storage['not_billing'])} deployments have a storage snapshot and no bill in the last "
            f"{STORAGE_BILLING_DAYS} days of the range; they are left out of org stored GB.",
            file=sys.stderr,
        )

    avg_doc_bytes = (sum_bytes / sum_docs) if sum_docs > 0 else None
    if args.skip_workload:
        workload = {
            "search": {
                "peak": {"qps": None, "complexity": None, "complexity_ms": None, "pct_hours_mo": None},
                "nonpeak": {"qps": None, "complexity": None, "complexity_ms": None, "pct_hours_mo": None},
                "idle_pct_hours_mo": None,
            },
            "ingest": {
                "docs_per_sec": None,
                "avg_doc_bytes": round(avg_doc_bytes) if avg_doc_bytes else None,
                "active_hours_mo": None,
            },
            "notes": ["skipped (--skip-workload)"],
            "window_days": 0,
        }
    else:
        workload = fetch_workload(base, key, org_ids or [org_id], avg_doc_bytes)
        if len(org_ids) > 1:
            workload.setdefault("notes", []).append(
                "Search and ingest rates cover organization ids " + ", ".join(org_ids)
            )

    offload = build_serverless_offload(
        deployments,
        storage_by_dep,
        org_total_ecu,
        workload=workload,
        lookback_days=args.days,
        org_stored_gb=float(storage.get("data_stored_gb") or 0),
        not_billing=storage.get("not_billing"),
    )
    fs_np = offload["full_split"]
    if fs_np["not_priced_n"]:
        print(
            f"WARN: {fs_np['not_priced_n']} of {len(deployments)} deployments ({fs_np['not_priced_ecu_mo']} ECU/mo) "
            "are not in the ECH-vs-Serverless comparison; full_split.not_priced names them.",
            file=sys.stderr,
        )
    # Sheets API / Apps Script payload — ES3 = search-only GB (full_split); O11y = primary
    offload["apps_script_payload"] = estimator_payload(
        (pivot.get("organization") or {}).get("name") or args.org,
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        infrastructure.get("summary") or {},
        storage,
        workload,
        offload,
    )

    # Vector + model inventory for ML canvas section
    vector_inventory = []
    model_count: Counter[str] = Counter()
    for d in deployments:
        ai = d["ai"]
        has_vec = bool(ai["vector_mappings"])
        has_elser = bool(ai["elser"])
        models = list(ai["models"] or [])
        if not (has_vec or has_elser or models or d["ml_ecu"] > 0):
            continue
        vector_inventory.append(
            {
                "name": d["name"],
                "version": d.get("version"),
                "vector_mappings": has_vec,
                "elser": has_elser,
                "models": models,
                "inference_ecu": ai["inference_ecu"],
                "ml_ecu": d["ml_ecu"],
                "inference_skus": ai.get("inference_skus") or [],
            }
        )
        if has_vec:
            if has_elser:
                model_count["ELSER (in-cluster)"] += 1
            if models:
                for m in models:
                    model_count[m] += 1
            elif not has_elser:
                model_count["(unnamed — vectors only)"] += 1
    vector_inventory.sort(key=lambda x: (-(x["ml_ecu"] or 0), -(x["inference_ecu"] or 0), x["name"]))
    vec_deps = [x for x in vector_inventory if x["vector_mappings"]]
    named_on_vec = sum(1 for x in vec_deps if x["models"])
    unnamed_vec = sum(1 for x in vec_deps if not x["models"] and not x["elser"])

    out = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "range": {"start": start.isoformat(), "end": end.isoformat(), "days": args.days},
        "organization": slim_org(pivot.get("organization")) | ({"ids": list(org_ids)} if len(org_ids) > 1 else {}),
        "license": pivot.get("license"),
        "burndown": burndown(pivot),
        "infrastructure": infrastructure,
        "storage": storage,
        "workload": workload,
        "offload": offload,
        "deployments": {
            "count": len(deployments),
            "ech_count": pc["ech"],
            "serverless_count": pc["serverless"],
            "ech_ecu": round(pe["ech"], 2),
            "serverless_ecu": round(pe["serverless"], 2),
            "by_version": by_version,
            "over_time": dep_over_time,
            "months": months_sorted,
            "items": deployments,
        },
        "families": {k: round(v, 2) for k, v in fam_tot.items()},
        "use_case_feature_docs": {
            b["key"]: b["doc_count"]
            for b in feats.get("aggregations", {}).get("use_case", {}).get("buckets", [])
        },
        "ai": {
            "elser": sum(1 for d in deployments if d["ai"]["elser"]),
            "genai_connector": sum(1 for d in deployments if d["ai"]["genai_connector"]),
            "playground": sum(1 for d in deployments if d["ai"]["playground"]),
            "vector_mappings": sum(1 for d in deployments if d["ai"]["vector_mappings"]),
            "ai_assistant": sum(1 for d in deployments if d["ai"]["ai_assistant"]),
            "attack_discovery": sum(1 for d in deployments if d["ai"]["attack_discovery"]),
            "billing_ecu_by_type": ai_billing,
            "billing_ecu_total": round(sum(ai_billing.values()), 2),
            "inference_billed": {
                "ecu": round(inf_billed_ecu, 4),
                "million_tokens": round(inf_billed_qty, 6),
            },
            "inference_skus_truncated": int(ai_sku_agg.get("sum_other_doc_count") or 0) > 0,
            "inference_skus": ai_skus,
            "models": sorted({m for d in deployments for m in d["ai"]["models"]}),
            "by_deployment": ai_by_dep,
            "notes": [
                "genai_connector is a boolean; Kibana connector provider is not in K360",
                "rainbow-sprinkles = Elastic Managed LLM via EIS",
                "chat model id is the SKU suffix after chat-input_ / chat-output_; a SKU with no suffix has no model id",
                "inference quantity is millions of tokens; ecu_per_million = cost / quantity",
                "embed-text-dense_* model id is the EIS dense embedding model",
                "ELSER / in-cluster dense models on ML nodes are not named in billing SKUs",
            ],
        },
        "ml": {
            "total_ecu": ml_total,
            "pct_of_org": round(100.0 * ml_total / org_total_ecu, 1) if org_total_ecu else 0.0,
            "deployments_with_ml": len(ml_by_dep),
            "elser_deps": sum(1 for d in deployments if d["ai"]["elser"]),
            "instances": ml_instances,
            "regions": ml_regions,
            "skus": org_ml_skus[:15],
            "by_deployment": ml_by_dep,
            "vectors": {
                "count": len(vec_deps),
                "with_elser": sum(1 for x in vec_deps if x["elser"]),
                "with_named_models": named_on_vec,
                "unnamed_model": unnamed_vec,
                "model_counts": [
                    {"model": m, "deployments": c} for m, c in model_count.most_common()
                ],
                "by_deployment": vector_inventory,
            },
            "notes": [
                "ml_ecu is in-cluster ML node capacity (*.es.ml.* SKUs), separate from EIS inference",
                "Model id for in-cluster ELSER/dense is not in billing SKUs — use feature.search.elser",
                "ml.vectors inventories all deps with vectors, ELSER, named EIS models, or ML nodes",
            ],
        },
        "chart": chart,
        "monthly": month_series,
        "totals": {
            "ecu": org_total_ecu,
            # The report's ECU / mo: lookback ECU ÷ (days ÷ 30.416), the divisor §10 uses.
            "ecu_mo": round(org_total_ecu / lb_months, 2),
            "lookback_days": args.days,
            "lookback_months": round(lb_months, 4),
            # Mean of calendar-month totals. `months` counts the calendar months the range touches (13 for 365 days).
            "ecu_monthly_avg": round(org_total_ecu / n_months, 2),
            "months": n_months,
            "ml_ecu": ml_total,
        },
        # Aggregations that left deployments out (more than DEP_TERMS_SIZE names). Empty when none did.
        "truncated": truncated,
    }

    text = json.dumps(out, indent=2 if args.pretty else None, separators=(",", ":"))
    if args.out == "-":
        sys.stdout.write(text)
    else:
        write_private(Path(args.out), text)
        n = len(text)
        print(
            f"wrote {args.out} ({len(deployments)} deps, {out['totals']['ecu']} ECU, "
            f"chart={args.chart}/{len(chart['days'])} pts, {n} bytes)",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
