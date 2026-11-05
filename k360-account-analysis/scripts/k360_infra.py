#!/usr/bin/env python3
"""Cloud / instance / region detail from K360 billing capacity SKUs (1 ES call).

Supplements fetch_org.py, whose SKU regex misses plain regions like `us-east-1`.

  python k360_infra.py --org-json /tmp/org.json --env .env --out /tmp/infra.json
"""
from __future__ import annotations

import argparse
import json
import re
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

# Cloud-Enterprise_aws.es.datahot.c6gd_us-east-1_3502080_3
# Cloud-Standard_gcp.es.datafrozen.n2.68x10x95_gcp-europe-west1_61440_2
# Cloud-Enterprise_azure.kibana.fsv2_azure-eastus2_1024_1
_SKU = re.compile(
    r"^Cloud-(?P<plan>[^_]+)_(?P<provider>aws|gcp|azure)\."
    r"(?P<product>[a-z]+)\."
    r"(?:(?P<tier>datahot|datawarm|datacold|datafrozen|master|coordinating|ml|ingest)\.)?"
    r"(?P<instance>[^_]+)_(?P<region>[^_]+)_(?P<ram_mb>\d+)_(?P<zones>\d+)$",
    re.IGNORECASE,
)

NON_CAPACITY_TYPES = (
    "data-transfer-in",
    "data-transfer-out",
    "data-transfer-inter-node",
    "snapshot-storage",
    "snapshot-api-1k",
)


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
    kibana = env.get("KIBANA_URL") or env.get("ELASTIC_KIBANA_URL")
    key = env.get("ELASTIC_API_KEY") or env.get("ES_API_KEY")
    if not kibana or not key:
        raise SystemExit("Need KIBANA_URL and ELASTIC_API_KEY in .env")
    return kibana.rstrip("/").replace(".kb.", ".es."), key


def resolve_org_name(base: str, key: str, name: str) -> str:
    """Exact organization name before the long fetch.

    A quoted phrase only. The unquoted ``Health*`` fallback in fetch_org.py
    has matched a different customer and spent a full run on it.
    """
    name = (name or "").strip()
    if not name:
        raise SystemExit("empty --org")
    # organization.name is a keyword: match_phrase is case-sensitive and the
    # unquoted prefix fallback in fetch_org.py matches unrelated customers.
    escaped = "".join("\\" + c if c in "*?\\" else c for c in name)
    res = es_post(base, key, "cloud-tracker-organization_pivot/_search", {
        "size": 20,
        "_source": ["organization.name", "organization.id"],
        "query": {"wildcard": {"organization.name": {"value": f"*{escaped}*", "case_insensitive": True}}},
    })
    total = (res.get("hits", {}).get("total") or {}).get("value") or 0
    by_id: dict[str, str] = {}
    for h in res.get("hits", {}).get("hits", []):
        org = (h.get("_source") or {}).get("organization") or {}
        if org.get("name"):
            by_id[str(org.get("id") or org["name"])] = org["name"]
    names = list(dict.fromkeys(by_id.values()))
    folded = name.casefold()
    exact = [n for n in names if n.casefold() == folded]
    if len(exact) == 1:
        return exact[0]
    starts = [n for n in names if n.casefold().startswith(folded)]
    if len(starts) == 1 and total <= len(res.get("hits", {}).get("hits", [])):
        return starts[0]
    shown = ", ".join(f"{n} ({i})" for i, n in list(by_id.items())[:8]) or "none"
    extra = f" ({total} matches)" if total > len(names) else ""
    raise SystemExit(f"No single organization match for {name!r}. Matches: {shown}{extra}")


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


def parse_sku(sku: str) -> dict | None:
    m = _SKU.match(sku or "")
    if not m:
        return None
    d = m.groupdict()
    region = d["region"]
    region = re.sub(r"^(aws|gcp|azure)-", "", region, flags=re.IGNORECASE)
    return {
        "plan": d["plan"],
        "provider": d["provider"].upper(),
        "product": d["product"].lower(),
        "tier": (d["tier"] or ("kibana" if d["product"].lower() == "kibana" else "other")).lower(),
        "instance": f"{d['provider'].lower()}.{d['product'].lower()}."
        + (f"{d['tier'].lower()}." if d["tier"] else "")
        + d["instance"],
        "instance_short": d["instance"],
        "region": region,
        "ram_mb": int(d["ram_mb"]),
        "zones": int(d["zones"]),
    }


def hex_id(n: str) -> bool:
    return len(n) == 32 and all(c in "0123456789abcdef" for c in n.lower())


def fetch_infra(base: str, key: str, org: dict) -> dict:
    org_id = (org.get("organization") or {}).get("id")
    org_name = (org.get("organization") or {}).get("name")
    rng = org.get("range") or {}
    if org_id:
        org_q = {"term": {"organization.id": org_id}}
    else:
        org_q = {"match_phrase": {"organization.name": org_name}}
    filt = [org_q]
    if rng.get("start") and rng.get("end"):
        filt.append(
            {
                "range": {
                    "@timestamp": {
                        "gte": f"{rng['start']}T00:00:00Z",
                        "lte": f"{rng['end']}T23:59:59Z",
                    }
                }
            }
        )
    res = es_post(
        base,
        key,
        "cloud-tracker-billing-*/_search",
        {
            "size": 0,
            "query": {"bool": {"filter": filt}},
            "aggs": {
                "by_dep": {
                    "terms": {"field": "deployment.name", "size": 200, "order": {"ecu": "desc"}},
                    "aggs": {
                        "ecu": {"sum": {"field": "cost"}},
                        "cloud_skus": {
                            "terms": {"field": "sku", "size": 60, "include": "Cloud-.*"},
                            "aggs": {"ecu": {"sum": {"field": "cost"}}},
                        },
                        # latest-month SKUs show the *current* shape (zones / RAM)
                        "latest": {
                            "top_hits": {
                                "size": 1,
                                "sort": [{"@timestamp": "desc"}],
                                "_source": ["@timestamp"],
                            }
                        },
                    },
                },
                "types_total": {
                    "terms": {"field": "type", "size": 80},
                    "aggs": {"ecu": {"sum": {"field": "cost"}}},
                },
                "cloud_skus_total": {
                    "terms": {"field": "sku", "size": 300, "include": "Cloud-.*", "order": {"ecu": "desc"}},
                    "aggs": {"ecu": {"sum": {"field": "cost"}}},
                },
                # Serverless projects bill under hex-id deployment names + serverless.* SKUs;
                # fetch_org.py drops hex ids, so they are recovered here.
                "serverless": {
                    "filter": {"prefix": {"sku": "serverless."}},
                    "aggs": {
                        "ecu": {"sum": {"field": "cost"}},
                        "by_project": {
                            "terms": {"field": "deployment.name", "size": 150, "order": {"ecu": "desc"}},
                            "aggs": {
                                "ecu": {"sum": {"field": "cost"}},
                                "template": {"terms": {"field": "deployment.template", "size": 3}},
                                "provider": {"terms": {"field": "deployment.provider", "size": 2}},
                                "created": {"min": {"field": "deployment.creation_date"}},
                                "first_bill": {"min": {"field": "@timestamp"}},
                                "last_bill": {"max": {"field": "@timestamp"}},
                                "types": {"terms": {"field": "type", "size": 10}, "aggs": {"ecu": {"sum": {"field": "cost"}}}},
                            },
                        },
                        "by_month": {
                            "date_histogram": {"field": "@timestamp", "calendar_interval": "month", "min_doc_count": 0},
                            "aggs": {"ecu": {"sum": {"field": "cost"}}, "projects": {"cardinality": {"field": "deployment.name"}}},
                        },
                        "types": {"terms": {"field": "type", "size": 15}, "aggs": {"ecu": {"sum": {"field": "cost"}}}},
                        "skus": {"terms": {"field": "sku", "size": 25, "order": {"ecu": "desc"}}, "aggs": {"ecu": {"sum": {"field": "cost"}}}},
                    },
                },
            },
        },
    )
    aggs = res.get("aggregations", {})

    svl = aggs.get("serverless") or {}
    svl_projects = []
    for b in svl.get("by_project", {}).get("buckets", []):
        e = float(b["ecu"]["value"])
        if e <= 0:
            continue
        tmpl = (b["template"]["buckets"][0]["key"] if b["template"]["buckets"] else None)
        svl_projects.append(
            {
                "project_id": b["key"],
                "ecu": round(e, 2),
                "template": tmpl,
                "solution": {"security": "security", "observability": "observability", "elasticsearch": "search", "search": "search"}.get((tmpl or "").lower(), "search"),
                "provider": (b["provider"]["buckets"][0]["key"].upper() if b["provider"]["buckets"] else None),
                "created": (b["created"].get("value_as_string") or "")[:10] or None,
                "first_bill": (b["first_bill"].get("value_as_string") or "")[:10] or None,
                "last_bill": (b["last_bill"].get("value_as_string") or "")[:10] or None,
                "types": {t["key"]: round(t["ecu"]["value"], 2) for t in b["types"]["buckets"]},
            }
        )
    serverless = {
        "ecu": round(float((svl.get("ecu") or {}).get("value") or 0), 2),
        "project_count": len(svl_projects),
        "projects": svl_projects,
        "by_solution": {},
        "by_month": [
            {"month": m["key_as_string"][:7], "ecu": round(m["ecu"]["value"], 2), "projects": m["projects"]["value"]}
            for m in svl.get("by_month", {}).get("buckets", [])
        ],
        "types": [{"type": t["key"], "ecu": round(t["ecu"]["value"], 2)} for t in svl.get("types", {}).get("buckets", [])],
        "skus": [{"sku": t["key"], "ecu": round(t["ecu"]["value"], 2)} for t in svl.get("skus", {}).get("buckets", [])],
    }
    for p in svl_projects:
        s = serverless["by_solution"].setdefault(p["solution"], {"projects": 0, "ecu": 0.0})
        s["projects"] += 1
        s["ecu"] = round(s["ecu"] + p["ecu"], 2)

    prov_ecu: Counter[str] = Counter()
    region_ecu: Counter[str] = Counter()
    region_prov: dict[str, str] = {}
    tier_ecu: Counter[str] = Counter()
    inst_ecu: Counter[str] = Counter()
    inst_tier: dict[str, str] = {}
    plan_ecu: Counter[str] = Counter()
    zones_ecu: Counter[int] = Counter()
    total_cap = 0.0
    for b in aggs.get("cloud_skus_total", {}).get("buckets", []):
        p = parse_sku(b["key"])
        e = float(b["ecu"]["value"])
        if not p or e <= 0:
            continue
        total_cap += e
        prov_ecu[p["provider"]] += e
        region_ecu[p["region"]] += e
        region_prov[p["region"]] = p["provider"]
        tier_ecu[p["tier"]] += e
        inst_ecu[p["instance"]] += e
        inst_tier[p["instance"]] = p["tier"]
        plan_ecu[p["plan"]] += e
        zones_ecu[p["zones"]] += e

    by_dep = []
    region_deps: Counter[str] = Counter()
    prov_deps: Counter[str] = Counter()
    inst_deps: Counter[str] = Counter()
    for b in aggs.get("by_dep", {}).get("buckets", []):
        name = b["key"]
        if hex_id(name):
            continue
        dep_ecu = float(b["ecu"]["value"])
        tiers: Counter[str] = Counter()
        regions: Counter[str] = Counter()
        provs: Counter[str] = Counter()
        hot_inst: Counter[str] = Counter()
        zones_seen: Counter[int] = Counter()
        ram_by_tier: dict[str, int] = defaultdict(int)
        # Dominant Cloud SKU per component. The last SKU field is the zone count;
        # ECH places one node in each zone, so that count is the node count.
        shape: dict[str, tuple[float, dict]] = {}
        for sb in b.get("cloud_skus", {}).get("buckets", []):
            p = parse_sku(sb["key"])
            e = float(sb["ecu"]["value"])
            if not p:
                continue
            comp = p["tier"] if p["tier"] not in ("", "other") else p["product"]
            prev = shape.get(comp)
            if prev is None or e > prev[0]:
                shape[comp] = (e, {"tier": comp, "ram_gb": round(p["ram_mb"] / 1024, 2), "nodes": p["zones"]})
            if e <= 0:
                continue
            tiers[p["tier"]] += e
            regions[p["region"]] += e
            provs[p["provider"]] += e
            zones_seen[p["zones"]] += e
            if p["tier"] == "datahot":
                hot_inst[p["instance"]] += e
            ram_by_tier[p["tier"]] = max(ram_by_tier[p["tier"]], p["ram_mb"] * p["zones"])
        region = regions.most_common(1)[0][0] if regions else None
        provider = provs.most_common(1)[0][0] if provs else None
        hot = hot_inst.most_common(1)[0][0] if hot_inst else None
        if region:
            region_deps[region] += 1
        if provider:
            prov_deps[provider] += 1
        if hot:
            inst_deps[hot] += 1
        by_dep.append(
            {
                "name": name,
                "ecu": round(dep_ecu, 2),
                "provider": provider,
                "region": region,
                "hot_instance": hot,
                "zones": zones_seen.most_common(1)[0][0] if zones_seen else None,
                "tiers": {t: round(e, 2) for t, e in tiers.most_common()},
                "tier_ram_gb_max": {t: round(mb / 1024, 1) for t, mb in ram_by_tier.items()},
                "components": [c for _, c in sorted(shape.values(), key=lambda item: -item[0])],
            }
        )

    types = []
    total_types = 0.0
    for b in aggs.get("types_total", {}).get("buckets", []):
        e = float(b["ecu"]["value"])
        total_types += e
        types.append({"type": b["key"], "ecu": round(e, 2)})
    types.sort(key=lambda x: -x["ecu"])
    non_cap = [t for t in types if t["type"] in NON_CAPACITY_TYPES]
    for t in types:
        t["pct"] = round(100.0 * t["ecu"] / total_types, 1) if total_types else 0.0

    def pct(e: float, tot: float) -> float:
        return round(100.0 * e / tot, 1) if tot else 0.0

    return {
        "capacity_ecu": round(total_cap, 2),
        "summary": {
            "provider": prov_ecu.most_common(1)[0][0] if prov_ecu else None,
            "region": region_ecu.most_common(1)[0][0] if region_ecu else None,
            "hot_instance": next(
                (i for i, _ in inst_ecu.most_common() if inst_tier.get(i) == "datahot"), None
            ),
            "plan": plan_ecu.most_common(1)[0][0] if plan_ecu else None,
            "zones": zones_ecu.most_common(1)[0][0] if zones_ecu else None,
            "multi_region": len(region_ecu) > 1,
            "multi_provider": len(prov_ecu) > 1,
        },
        "providers": [
            {"provider": p, "ecu": round(e, 2), "pct": pct(e, total_cap), "deployments": prov_deps.get(p, 0)}
            for p, e in prov_ecu.most_common()
        ],
        "regions": [
            {
                "region": r,
                "provider": region_prov.get(r),
                "ecu": round(e, 2),
                "pct": pct(e, total_cap),
                "deployments": region_deps.get(r, 0),
            }
            for r, e in region_ecu.most_common()
        ],
        "tiers": [
            {"tier": t, "ecu": round(e, 2), "pct": pct(e, total_cap)} for t, e in tier_ecu.most_common()
        ],
        "instances": [
            {
                "instance": i,
                "tier": inst_tier.get(i),
                "ecu": round(e, 2),
                "pct": pct(e, total_cap),
                "deployments_hot": inst_deps.get(i, 0),
            }
            for i, e in inst_ecu.most_common(15)
        ],
        "plans": [{"plan": p, "ecu": round(e, 2)} for p, e in plan_ecu.most_common()],
        "zones": [{"zones": z, "ecu": round(e, 2), "pct": pct(e, total_cap)} for z, e in zones_ecu.most_common()],
        "billing_types": types,
        "non_capacity": non_cap,
        "non_capacity_ecu": round(sum(t["ecu"] for t in non_cap), 2),
        "by_deployment": by_dep,
        "serverless": serverless,
        "notes": [
            "Parsed from Cloud-<plan>_<provider>.<product>.<tier>.<instance>_<region>_<ramMB>_<zones> billing SKUs",
            "tier_ram_gb_max = largest RAM×zones seen for the tier over the lookback (not a point-in-time size)",
            "components.nodes is the Cloud SKU zone count for that tier. ECH places one node in each zone; extra autoscaled nodes are not a separate field",
        ],
    }


def fill_ingest_doc_bytes(base: str, key: str, doc: dict) -> int | None:
    """Fill a missing ingest average from index telemetry size and document count.

    ``cloud-tracker-telemetry`` is empty for some orgs, so fetch leaves
    ``avg_doc_bytes`` null and the Search estimator prices ingest at $0.
    Index telemetry still has ``index.dataset_size_bytes`` and ``index.docs_count``.
    """
    wl = doc.get("workload") if isinstance(doc.get("workload"), dict) else None
    if wl is None:
        return None
    ingest = wl.get("ingest") if isinstance(wl.get("ingest"), dict) else {}
    if ingest.get("avg_doc_bytes"):
        return int(round(float(ingest["avg_doc_bytes"])))
    org_id = str(
        ((doc.get("meta") or {}).get("org") or {}).get("id")
        or (doc.get("organization") or {}).get("id")
        or ""
    )
    if not org_id:
        return None
    exclude = {"bool": {"must_not": [
        {"prefix": {"index.name": "."}},
        {"prefix": {"index.name": "partial-"}},
    ]}}
    try:
        res = es_post(base, key, "cloud-tracker-index_telemetry-*/_search", {
            "size": 0,
            "query": {"bool": {"filter": [
                {"term": {"organization.id": org_id}},
                exclude,
                {"range": {"@timestamp": {"gte": "now-21d"}}},
            ]}},
            "aggs": {"by_idx": {"terms": {"field": "index.name", "size": 200}, "aggs": {
                "docs": {"max": {"field": "index.docs_count"}},
                "nbytes": {"max": {"field": "index.dataset_size_bytes"}},
            }}},
        })
    except SystemExit:
        return None
    sum_bytes = sum_docs = 0.0
    for bucket in res.get("aggregations", {}).get("by_idx", {}).get("buckets", []):
        docs = float((bucket.get("docs") or {}).get("value") or 0)
        nbytes = float((bucket.get("nbytes") or {}).get("value") or 0)
        if docs > 0 and nbytes > 0:
            sum_bytes += nbytes
            sum_docs += docs
    if sum_docs <= 0:
        return None
    avg = int(round(sum_bytes / sum_docs))
    ingest = dict(ingest)
    ingest["avg_doc_bytes"] = avg
    ingest["avg_doc_bytes_source"] = "cloud-tracker-index_telemetry dataset_size_bytes / docs_count"
    notes = list(wl.get("notes") or [])
    note = "avg_doc_bytes = index.dataset_size_bytes / index.docs_count over the last 21 days"
    if note not in notes:
        notes.append(note)
    wl = dict(wl)
    wl["ingest"] = ingest
    wl["notes"] = notes
    doc["workload"] = wl
    search = ((doc.get("sizing") or {}).get("consolidated") or {}).get("search")
    if isinstance(search, dict) and isinstance(search.get("workload"), dict):
        sw = dict(search["workload"])
        si = dict(sw.get("ingest") or {})
        if not si.get("avg_doc_bytes"):
            si["avg_doc_bytes"] = avg
            si["avg_doc_bytes_source"] = ingest["avg_doc_bytes_source"]
            sw["ingest"] = si
            search["workload"] = sw
    return avg


def fill_storage_from_data_pivot(base: str, key: str, org: dict) -> None:
    """Fill storage from Mission Control's data pivot when the telemetry snapshot is empty.

    The Mission Control dashboard reads ``dataset_size_bytes`` from
    ``cloud-tracker-data_pivot``. ``fetch_org.py`` only reads
    ``cloud-tracker-telemetry``, which has no documents for some organizations.
    """
    st = org.setdefault("storage", {})
    if float(st.get("data_stored_gb") or 0) > 0:
        return
    org_id = str((org.get("organization") or {}).get("id") or "")
    if not org_id:
        return
    idx = "cloud-tracker-data_pivot/_search"
    try:
        mx = es_post(base, key, idx, {
            "size": 0,
            "query": {"term": {"organization.id": org_id}},
            "aggs": {"max_ts": {"max": {"field": "@timestamp"}}},
        })["aggregations"]["max_ts"]
        ts = mx.get("value_as_string")
        if not ts:
            return
        buckets = es_post(base, key, idx, {
            "size": 0,
            "query": {"bool": {"filter": [
                {"term": {"organization.id": org_id}},
                {"range": {"@timestamp": {"gte": ts, "lte": ts}}},
            ]}},
            "aggs": {"by_dep": {"terms": {"field": "deployment.name", "size": 500}, "aggs": {
                "bytes": {"sum": {"field": "dataset_size_bytes"}},
                "docs": {"sum": {"field": "docs_count"}},
            }}},
        })["aggregations"]["by_dep"]["buckets"]
    except SystemExit as ex:
        notes = list(st.get("notes") or [])
        notes.append(f"cloud-tracker-data_pivot lookup failed: {ex}")
        st["notes"] = notes
        return
    rows = []
    sum_bytes = 0.0
    sum_docs = 0.0
    for b in buckets:
        nbytes = float(b["bytes"]["value"] or 0)
        ndocs = float(b["docs"]["value"] or 0)
        sum_bytes += nbytes
        sum_docs += ndocs
        avg_kb = (nbytes / ndocs / 1024.0) if ndocs else 0.0
        rows.append({
            "name": b["key"],
            "as_of": ts[:10],
            "data_stored_gb": round(nbytes / (1024 ** 3), 3),
            "docs_count": int(ndocs) if ndocs == int(ndocs) else ndocs,
            "avg_doc_kb": round(avg_kb, 4),
            "calculated_data_stored_gb": round(ndocs * avg_kb / (1024 ** 2), 3) if ndocs else 0.0,
        })
    rows.sort(key=lambda r: -(r["data_stored_gb"] or 0))
    org_avg = (sum_bytes / sum_docs / 1024.0) if sum_docs else 0.0
    notes = [n for n in (st.get("notes") or []) if "cloud-tracker-telemetry latest snapshot" not in n]
    notes.insert(0, (
        f"dataset_size_bytes summed from cloud-tracker-data_pivot on {ts[:10]} "
        "(Mission Control). cloud-tracker-telemetry had no snapshot. All tiers for a deployment are added."
    ))
    st.update({
        "as_of": ts[:10],
        "deployments_with_telemetry": len(rows),
        "data_stored_gb": round(sum_bytes / (1024 ** 3), 3),
        "docs_count": int(sum_docs) if sum_docs == int(sum_docs) else sum_docs,
        "avg_doc_kb": round(org_avg, 4),
        "calculated_data_stored_gb": round(sum_docs * org_avg / (1024 ** 2), 3) if sum_docs else 0.0,
        "by_deployment": rows,
        "notes": notes,
        "source": "cloud-tracker-data_pivot",
    })


def fill_storage_monthly(base: str, key: str, org: dict) -> None:
    """Monthly peak stored size from data_pivot, even when the telemetry snapshot is already filled.

    Each month is the sum of each deployment's largest ``dataset_size_bytes`` in that month.
    A single telemetry snapshot cannot draw this chart.
    """
    st = org.setdefault("storage", {})
    org_id = str((org.get("organization") or {}).get("id") or "")
    rng = org.get("range") or {}
    start = str(rng.get("start") or "now-365d")[:10]
    end = str(rng.get("end") or "now")[:10]
    if not org_id:
        return
    try:
        res = es_post(base, key, "cloud-tracker-data_pivot/_search", {
            "size": 0,
            "query": {"bool": {"filter": [
                {"term": {"organization.id": org_id}},
                {"range": {"@timestamp": {"gte": start, "lte": end}}},
            ]}},
            "aggs": {"by_month": {
                "date_histogram": {"field": "@timestamp", "calendar_interval": "month", "min_doc_count": 1},
                "aggs": {"by_dep": {"terms": {"field": "deployment.name", "size": 80}, "aggs": {
                    "bytes": {"max": {"field": "dataset_size_bytes"}},
                    "docs": {"max": {"field": "docs_count"}},
                }}},
            }},
        })
    except SystemExit as ex:
        notes = list(st.get("notes") or [])
        notes.append(f"monthly storage lookup failed: {ex}")
        st["notes"] = notes
        return
    months, gb, docs = [], [], []
    for bucket in res.get("aggregations", {}).get("by_month", {}).get("buckets", []):
        deps = bucket.get("by_dep", {}).get("buckets") or []
        nbytes = sum(float((d.get("bytes") or {}).get("value") or 0) for d in deps)
        ndocs = sum(float((d.get("docs") or {}).get("value") or 0) for d in deps)
        months.append(str(bucket.get("key_as_string") or "")[:7])
        gb.append(round(nbytes / (1024 ** 3), 2))
        docs.append(int(ndocs))
    if not months:
        return
    st["monthly"] = {
        "months": months,
        "gb": gb,
        "docs": docs,
        "source": "cloud-tracker-data_pivot",
        "note": "Each month is the sum of each deployment's largest dataset_size_bytes snapshot in that month.",
    }


def _name_key(name: str) -> str:
    """Name with the -elastic / -search suffix and the obeservability spelling removed."""
    n = str(name or "").lower().replace("obeservability", "observability")
    for suffix in ("-elastic", "-search"):
        if n.endswith(suffix):
            n = n[: -len(suffix)]
    return n


def size_gaps(base: str, key: str, acc: dict) -> list[dict]:
    """Explain each ECH deployment with no row on the size snapshot day.

    Looks up the last day that name had a size in ``cloud-tracker-data_pivot``,
    and a name on the snapshot that shares its base name (``apc-ecom-elastic``
    and ``apc-ecom-search``) and started reporting after it stopped.
    """
    st = acc.get("storage") or {}
    org_id = str(((acc.get("meta") or {}).get("org") or {}).get("id") or "")
    on_snapshot = {r["name"] for r in st.get("by_deployment") or []}
    missing = [d["name"] for d in (acc.get("solutions") or {}).get("by_deployment") or []
               if d.get("platform") != "serverless" and d["name"] not in on_snapshot]
    if not org_id or not missing or not on_snapshot:
        return []
    idx = "cloud-tracker-data_pivot/_search"
    try:
        last = es_post(base, key, idx, {
            "size": 0,
            "query": {"bool": {"filter": [{"term": {"organization.id": org_id}}, {"terms": {"deployment.name": missing}}]}},
            "aggs": {"by_dep": {"terms": {"field": "deployment.name", "size": 500}, "aggs": {"days": {
                "date_histogram": {"field": "@timestamp", "calendar_interval": "1d", "min_doc_count": 1},
                "aggs": {
                    "bytes": {"sum": {"field": "dataset_size_bytes"}},
                    "last": {"bucket_sort": {"sort": [{"_key": {"order": "desc"}}], "size": 1}},
                },
            }}}},
        })["aggregations"]["by_dep"]["buckets"]
        first = es_post(base, key, idx, {
            "size": 0,
            "query": {"bool": {"filter": [{"term": {"organization.id": org_id}}, {"terms": {"deployment.name": sorted(on_snapshot)}}]}},
            "aggs": {"by_dep": {"terms": {"field": "deployment.name", "size": 500}, "aggs": {"first": {"min": {"field": "@timestamp"}}}}},
        })["aggregations"]["by_dep"]["buckets"]
    except SystemExit:
        return []
    last_by = {}
    for b in last:
        days = b["days"]["buckets"]
        if days:
            last_by[b["key"]] = {"day": days[0]["key_as_string"][:10], "gb": round((days[0]["bytes"]["value"] or 0) / (1024 ** 3), 1)}
    first_by = {b["key"]: str(b["first"].get("value_as_string") or "")[:10] for b in first}
    snap_by_key: dict[str, list[str]] = defaultdict(list)
    for n in on_snapshot:
        snap_by_key[_name_key(n)].append(n)
    rows = []
    for name in missing:
        seen = last_by.get(name)
        row = {"name": name, "last_size_day": seen["day"] if seen else None, "last_size_gb": seen["gb"] if seen else None, "size_now_on": None, "size_now_since": None}
        if seen:
            for sib in sorted(snap_by_key.get(_name_key(name)) or []):
                since = first_by.get(sib)
                if since and since >= seen["day"]:
                    row["size_now_on"], row["size_now_since"] = sib, since
                    break
        rows.append(row)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--org-json", required=True)
    ap.add_argument("--env", default=".env")
    ap.add_argument("--out", default="-")
    args = ap.parse_args()
    org = json.loads(Path(args.org_json).read_text())
    base, key = es_base_and_key(load_env(Path(args.env)))
    out = fetch_infra(base, key, org)
    text = json.dumps(out, separators=(",", ":"))
    if args.out == "-":
        print(text)
    else:
        Path(args.out).write_text(text)
        print(f"wrote {args.out} ({len(out['by_deployment'])} deps, {out['capacity_ecu']} capacity ECU)")


if __name__ == "__main__":
    main()
