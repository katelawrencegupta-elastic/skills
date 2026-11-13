#!/usr/bin/env python3
"""Render <run_key>_account_report.md from the account JSON (tables + Mermaid charts).

  python report_md.py --account-json serverless_analyzer/diag_artifacts/Acme_2026-11-02/Acme_2026-11-02_account.json
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from analyze import component_summary, display_solution


def n0(x) -> str:
    try:
        return f"{float(x):,.0f}"
    except (TypeError, ValueError):
        return "—"


def n1(x) -> str:
    try:
        return f"{float(x):,.1f}"
    except (TypeError, ValueError):
        return "—"


def s(x) -> str:
    return "—" if x in (None, "", []) else str(x)


def table(headers: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


def mermaid_pie(title: str, items: list[tuple[str, float]]) -> str:
    body = "\n".join(f'    "{k}" : {v}' for k, v in items if v)
    return f"```mermaid\npie showData title {title}\n{body}\n```"


def mermaid_xy(title: str, labels: list[str], series: list[float], kind: str = "bar", ylabel: str = "ECU") -> str:
    if not labels:
        return ""
    lab = ", ".join(f'"{l}"' for l in labels)
    vals = ", ".join(str(round(v, 2)) for v in series)
    return (
        "```mermaid\nxychart-beta\n"
        f'    title "{title}"\n'
        f"    x-axis [{lab}]\n"
        f'    y-axis "{ylabel}"\n'
        f"    {kind} [{vals}]\n```"
    )


def sev(x: str) -> str:
    return {"high": "🔴 high", "medium": "🟠 medium", "low": "🟡 low"}.get(x, x)


def usd(x) -> str:
    return "—" if x is None else f"${float(x):,.0f}"


def ech_topology_mermaid(acc: dict) -> list[str]:
    """Current ECH layout: provider, then one subgraph per region."""
    cl = acc.get("cloud") or {}
    regions = [r for r in (cl.get("regions") or []) if r.get("region")]
    deps = cl.get("by_deployment") or []
    if not regions or not deps:
        return []
    sol_by = {d.get("name"): (d.get("solution") or "search") for d in ((acc.get("solutions") or {}).get("by_deployment") or [])}
    summary = cl.get("summary") or {}
    provider = " ".join(p for p in (summary.get("provider"), summary.get("plan")) if p) or "ECH"
    L = ["**Current ECH architecture**", "", "```mermaid", "flowchart TB", f'  cloud["{provider} · {len(deps)} ECH deployments"]']
    for i, region in enumerate(regions):
        rows = [d for d in deps if d.get("region") == region.get("region")]
        rows.sort(key=lambda d: -float(d.get("ecu") or 0))
        share = f" · {region.get('pct')}%" if region.get("pct") is not None else ""
        L.append(f'  subgraph r{i}["{region.get("region")} · {len(rows)} deployments · {n0(region.get("ecu"))} ECU{share}"]')
        by_az: dict = {}
        for d in rows:
            by_az.setdefault(d.get("zones"), []).append(d)
        for zi, (zones, group) in enumerate(sorted(by_az.items(), key=lambda kv: -(kv[0] if isinstance(kv[0], (int, float)) else -1))):
            az = f"{int(zones)} AZ" if isinstance(zones, (int, float)) else "AZ"
            L.append(f'    subgraph r{i}z{zi}["{az}"]')
            for d in group:
                node = "d_" + re.sub(r"[^A-Za-z0-9]", "_", str(d.get("name") or "dep"))
                sol = (sol_by.get(d.get("name")) or "search").title()
                nodes = component_summary(d)
                label = f'{d.get("name")} · {sol}'
                if nodes:
                    label += f"<br/>{nodes}"
                L.append(f'      {node}["{label}"]')
            L.append("    end")
        L.append("  end")
        L.append(f"  cloud --> r{i}")
    L.extend(["```", "", architecture_table_md(acc), ""])
    return L


def architecture_table_md(acc: dict) -> str:
    cl = acc.get("cloud") or {}
    deps = [d for d in (cl.get("by_deployment") or []) if (d.get("platform") or "ech") != "serverless" and d.get("name")]
    if not deps:
        return ""
    lookback = float((acc.get("ecu") or {}).get("total_lookback") or 0)
    total = lookback if lookback > 0 else sum(float(d.get("ecu") or 0) for d in deps)
    stor = {r.get("name"): r for r in ((acc.get("storage") or {}).get("by_deployment") or [])}
    classified = {d.get("name"): d for d in ((acc.get("solutions") or {}).get("by_deployment") or [])}
    providers = {d.get("provider") for d in deps if d.get("provider")}
    deps.sort(key=lambda d: -float(d.get("ecu") or 0))
    as_of = (acc.get("storage") or {}).get("as_of")
    headers = ["Deployment", "Version", "Region", "Solution", "AZ", "ECU", "Utilization", "Stored", "Documents", "Avg doc"]
    rows = []
    for d in deps:
        name = d.get("name")
        info = classified.get(name) or {}
        solution = display_solution(info, d).title()
        size = stor.get(name) or {}
        zones = d.get("zones")
        ecu = float(d.get("ecu") or 0)
        share = f"{round(100 * ecu / total, 1)}%" if total else "—"
        gb = size.get("data_stored_gb")
        gb_s = "—" if name not in stor or gb is None else (f"{float(gb):,.2f} GB" if float(gb) < 10 else f"{float(gb):,.1f} GB")
        kb = size.get("avg_doc_kb")
        region = s(d.get("region"))
        if len(providers) > 1 and d.get("provider"):
            region = f"{d.get('provider')} {region}"
        row = [name, s(d.get("version")), region]
        row += [
            solution,
            f"{int(zones)} AZ" if isinstance(zones, (int, float)) else "—",
            n0(ecu),
            share,
            gb_s,
            n0(size.get("docs_count")) if name in stor else "—",
            f"{float(kb):,.2f} KB" if name in stor and kb is not None else "—",
        ]
        rows.append(row)
    note = "Utilization is the deployment's share of lookback ECU. Node counts on the topology are one node per billed zone."
    if as_of:
        note += f" Size and documents are the telemetry snapshot as of {as_of}."
    return table(headers, rows) + "\n\n" + note


def architecture_mermaid(arch: dict) -> list[str]:
    if not (arch.get("groups") and arch.get("projects")):
        return []
    L = ["**How ECH deployments map to serverless projects**", "", "```mermaid", "flowchart LR"]
    for i, g in enumerate(arch["groups"]):
        L.append(f'  g{i}["{g["solution"].title()} · {g["region"]}<br/>{g["n"]} deployment{"s" if g["n"] != 1 else ""} · {n0(g.get("ech_ecu_mo"))} ECU/mo · {n0(g.get("stored_gb"))} GB"]')
    for p in arch["projects"]:
        price = f"{usd(p['serverless_mo'])}/mo estimate" if p.get("serverless_mo") is not None else "not priced"
        node = f"p_{p['solution']}_{p.get('region') or 'all'}".replace("-", "_")
        L.append(f'  {node}(["{p["solution"].title()} · {p.get("region") or "one region"}<br/>{n0(p.get("stored_gb"))} GB · {price}"])')
    for i, g in enumerate(arch["groups"]):
        node = f"p_{g['solution']}_{g.get('region') or 'all'}".replace("-", "_")
        L.append(f"  g{i} --> {node}")
    L.extend(["```", ""])
    return L


def consolidation_report_exists(acc: dict) -> bool:
    c = acc.get("consolidation") or {}
    return bool((acc.get("sizing") or {}).get("triggered") and c and not c.get("error"))


def cost_sizing_md(acc: dict) -> list[str]:
    """§9 cost recs and §10 sizing. Lives with the consolidation report when that report ran."""
    an, sz = acc["analysis"], acc.get("sizing") or {}
    L: list[str] = ["## 9. Cost-savings analysis — recommendations"]
    for i, r in enumerate(an["cost"], 1):
        L.append(f"{i}. **{r['title']}** ({sev(r['severity'])}) — {r['evidence']}  \n   → {r['action']}" + (f" _(impact: {r['impact']})_" if r.get("impact") else ""))
    if not an["cost"]:
        L.append("No cost findings above threshold.")
    L.append("")
    sb = acc.get("serverless_ballpark") or {}
    if sb.get("lines"):
        L.append("<details><summary>Serverless ballpark (pre-estimator, from org-report methodology)</summary>\n")
        L.append(table(["Line", "Project", "ECH ECU/mo", "Serverless $/mo", "Save $/yr"], [[l["name"], l["project"], n0(l["ech_mo"]), n0(l["serverless_mo"]), n0(l["save_yr"])] for l in sb["lines"]]))
        L.append(f"\nTotal: ECH {n0(sb.get('full_ech_mo'))} ECU/mo vs serverless ${n0(sb.get('serverless_total_mo'))}/mo → save ${n0(sb.get('save_vs_ech_yr'))}/yr (3-yr ${n0(sb.get('save_vs_ech_3yr'))}). Never recommend all-data-on-ES3.")
        L.append("\n</details>\n")
    L.append("## 10. Serverless sizing (ECH > 50% rule)")
    if not sz.get("triggered"):
        if sz.get("skipped_reason"):
            L.append(f"Skipped ({sz['skipped_reason']}) — ECH share {sz.get('ech_share_pct')}%.")
        else:
            L.append(f"Not triggered — ECH share {sz.get('ech_share_pct')}% ≤ {sz.get('threshold_pct')}%.")
    else:
        con = sz.get("consolidated") or {}
        L.append(f"Triggered — ECH share {sz.get('ech_share_pct')}%. Folder `{s(sz.get('folder'))}`" + (f" · Drive {sz['drive_folder'].get('url')}" if sz.get("drive_folder") else ""))
        L.append("")
        L.append(table(["Solution", "Deps", "ECU/mo", "Stored GB", "Retention (mo)", "Daily raw GB", "GB estimated?"],
                       [[k.title(), v["deployments"], n0(v["ecu_monthly_avg"]), n0(v["stored_gb"]), v.get("retention_months"), n1(v["daily_raw_gb"]), "yes (ECU share)" if v.get("gb_estimated_from_ecu_share") else "no"] for k, v in con.items()]))
        L.append("")
        est = sz.get("estimators") or {}
        rows = []
        for k in ("observability", "security", "search"):
            x = est.get(k) or {}
            if not x:
                rows.append([k.title(), "pending", "", "", ""])
                continue
            rb = x.get("readback") or {}
            if k == "search" and not rb:
                rows.append([k.title(), f"[estimate]({x.get('url')})" if x.get("url") else s(x.get("local")), "—", "—", s(x.get("note"))])
            else:
                def _money_cell(v):
                    return usd(v) if isinstance(v, (int, float)) else s(v)
                annual_bits = [f"{lbl} {_money_cell(rb[kk])}" for kk, lbl in (("annual_year1", "Y1"), ("annual_year2", "Y2"), ("annual_year3", "Y3"), ("annual_steady_state", "steady")) if rb.get(kk)]
                rows.append([k.title(), f"[{x.get('title')}]({x.get('url')})" if x.get("url") else s(x.get("status")), _money_cell(rb.get("monthly_steady_state")), " · ".join(annual_bits) or "—", s(x.get("region_match"))])
        L.append(table(["Estimator", "Copy", "Monthly", "Annual", "Note"], rows))
        L.append("")
    return L


def consolidation_section(acc: dict) -> list[str]:
    c = acc.get("consolidation") or {}
    L = ["## 11. Serverless consolidation savings (ECH > 50% rule)"]
    sz = acc.get("sizing") or {}
    if not sz.get("triggered"):
        L.append("Not run — sizing not triggered.")
        L.append("")
        return L
    if c.get("error"):
        L.append(f"Consolidation analysis failed: `{c['error']}`")
        L.append("")
        return L
    sv, comp, svl, fr, cr = c.get("savings") or {}, c.get("ech_composition") or {}, c.get("serverless") or {}, c.get("fragmentation") or {}, c.get("contract_rate") or {}
    b = c.get("basis") or {}
    per_region = (c.get("architecture") or {}).get("per_region")
    shape = "one serverless project per solution in each region" if per_region else "one serverless project per solution"
    if sv.get("ech_contract_mo") is not None:
        L.append(f"ECH today **{n0(sv.get('ech_list_mo'))} ECU/mo** at the contract rate, **{usd(sv.get('ech_contract_mo'))}/mo** → {shape}, **{usd(sv.get('serverless_mo'))}/mo estimate** → **{usd(sv.get('cost_mo'))}/mo ({s(sv.get('cost_pct'))}%)** below the contract rate, {usd(sv.get('cost_yr'))}/yr.")
        L.append("")
        L.append("_Contract rate_ = ARR ÷ (commit ECU/day × 365), applied to ECH only. _Estimate_ = from the serverless estimators, not a quote or a bill. Serverless is not discounted.")
    else:
        L.append(f"ECH today **{n0(sv.get('ech_list_mo'))} ECU/mo** at list ($1 per ECU) → {shape}, **{usd(sv.get('serverless_mo'))}/mo estimate** → **{usd(sv.get('list_mo'))}/mo ({s(sv.get('list_pct'))}%)** below list, {usd(sv.get('list_yr'))}/yr.")
        L.append("")
        L.append("_List_ = $1 per ECU. _Estimate_ = from the serverless estimators, not a quote or a bill.")
    L.append("")
    L.append("Three mechanisms: (1) metered usage replaces 24×7 provisioned RAM; (2) per-deployment fixed overhead disappears; (3) one estimator read-back covers a solution instead of a price per deployment. Deployments are shown as ECU, GB, and overhead share.")
    L.append("")
    L.append("### 11.0 Data used and the arithmetic")
    st = acc.get("storage") or {}
    e = acc.get("ecu") or {}
    wl = acc.get("workload") or {}
    rng = e.get("range") or {}
    partial = (e.get("monthly") or {}).get("partial_months") or []
    ingest = wl.get("ingest") or {}
    missing = (fr.get("n_deployments") or 0) - (fr.get("n_with_gb") if fr.get("n_with_gb") is not None else (fr.get("n_priced") or 0))
    src = (st.get("notes") or ["account storage block"])[0]
    from_diags = c.get("source") == "diagnostics" or (acc.get("sizing") or {}).get("ingest_source") == "diagnostics"
    L.append(f"- **Lookback ECU** {n0(e.get('total_lookback'))} from {s(rng.get('start'))} → {s(rng.get('end'))} ({s(e.get('lookback_days'))} d), cloud-tracker-billing. Headline ECH/mo is the complete-month average, {n0(sv.get('ech_list_mo'))} ECU. Partial months excluded: {', '.join(partial) if partial else 'none'}.")
    if from_diags:
        L.append(f"- **Stored size** {n1(b.get('stored_gb'))} GB on the quote. Matched deployments use the diagnostic primary store. A deployment with no bundle keeps its K360 size. GB/day is measured bulk, not stored GB ÷ retention.")
        L.append(f"- **Retention** {s(b.get('retention_months'))} is the ingest-weighted diagnostic retention written to the estimator (1–36 months). K360 does not provide a raw ingest total. Stored GB ÷ assumed retention is the --k360-ingest backup and can sit far below this bulk rate.")
    else:
        L.append(f"- **Stored size** {n1(st.get('data_stored_gb'))} GB as of {s(st.get('as_of'))}, {s(st.get('deployments_with_telemetry'))} deployments with a row. {src} K360 does not provide a raw ingest total. This backup uses stored GB ÷ (retention months × 30.416), which can sit far below measured bulk.")
        L.append(f"- **Retention** {s(b.get('retention_months'))} is the run assumption, not a measured ILM policy. It changes implied GB/day. The estimator was not re-run, so the quoted dollars stay put.")
    L.append(f"- **Workload** {s(wl.get('window_days'))} d, {s(wl.get('indices'))} indices, index_telemetry day averages. Ingest {n1(ingest.get('docs_per_sec'))} docs/s, active {n0(ingest.get('active_hours_mo'))} h/mo, avg doc bytes {s(ingest.get('avg_doc_bytes')) or 'missing'}. Search QPS is not scaled to the search slice. Ingest docs/s is, by stored-GB share. Missing ingest inputs leave Search ingest at $0 in the es3-estimator.")
    L.append(f"- **Contract** {s(cr.get('note'))} Shown rate ${s(cr.get('implied_usd_per_ecu'))}/ECU.")
    if fr.get("size_gaps"):
        L.append(f"- **Size coverage** {missing} of {s(fr.get('n_deployments'))} deployments have no size on {s(st.get('as_of'))}. {s(fr.get('n_size_moved'))} now report under a sibling name already in the stored-GB total. {missing - (fr.get('n_size_moved') or 0)} have no current size.")
    else:
        L.append(f"- **Size coverage** {missing} of {s(fr.get('n_deployments'))} deployments have no stored-GB row. They stay in the ECU totals and out of the stored-GB total. They are not given a serverless price.")
    L.append("")
    if sv.get("ech_contract_mo") is not None:
        L.append(f"ECH at the contract rate: {n0(sv.get('ech_list_mo'))} ECU × ARR ÷ (commit × 365) = **{usd(sv.get('ech_contract_mo'))}/mo**. The serverless estimate is {usd(sv.get('serverless_mo'))}/mo, **{usd(sv.get('cost_mo'))}/mo ({s(sv.get('cost_pct'))}%)** below that, {usd(sv.get('cost_yr'))}/yr. Serverless is not discounted.")
    else:
        L.append(f"At list, $1 per ECU: {n0(sv.get('ech_list_mo'))} − {usd(sv.get('serverless_mo'))} = **{usd(sv.get('list_mo'))}/mo ({s(sv.get('list_pct'))}%)**, {usd(sv.get('list_yr'))}/yr.")
    L.append("")
    bs_early = svl.get("by_solution") or {}
    search = bs_early.get("search") or {}
    if str(search.get("pricing") or "").startswith("es3-estimator"):
        L.append(
            f"Search Serverless, performant: "
            f"search VCU {usd(search.get('search_mo'))} + ingest {usd(search.get('ingest_mo'))} + storage {usd(search.get('retention_mo'))} + support {usd(search.get('support_mo'))} "
            f"= **{usd(search.get('total_mo'))}/mo**. Provisioned search VCU {s(next((p.get('Provisioned Search VCU/h') for p in (search.get('regional_profiles') or []) if str(p.get('Search Power Setting')) == 'performant'), search.get('peak_vcu')))}, idle VCU {s(next((p.get('Idle Search VCU/h') for p in (search.get('regional_profiles') or []) if str(p.get('Search Power Setting')) == 'performant'), search.get('idle_vcu')))}."
        )
        L.append("")
    elif search.get("peak_vcu") is not None:
        mix = search.get("hour_mix") or {}
        L.append(f"Search model: peak VCU = max(28, 1,352 × GB / 139,760) = {search.get('peak_vcu')}; idle VCU = max(28, 1,204 × GB / 139,760) = {search.get('idle_vcu')}. Hour mix peak {s(mix.get('peak_pct'))}% / non-peak {s(mix.get('nonpeak_pct'))}% / idle {s(mix.get('idle_pct'))}% of 730 h, non-peak charged at peak VCU. Search $ = hours × VCU × $0.09 = {usd(search.get('search_mo'))}. Retention $ = GB × $0.047 = {usd(search.get('retention_mo'))}. Ingest $ = VCU (floor 4 when doc size is missing) × active hours × $0.14 = {usd(search.get('ingest_mo'))}. Support 15% = {usd(search.get('support_mo'))}. **Total {usd(search.get('total_mo'))}/mo. Model, not the es3-estimator.**")
        L.append("")
    o11y = bs_early.get("observability") or {}
    if "quotable" in str(o11y.get("pricing") or ""):
        L.append(f"Observability Serverless is **{usd(o11y.get('total_mo'))}/mo**. That estimator gives one total. It is not split into ingest, retention, and support.")
        L.append("")
    L.append("### 11.1 Provisioned → metered")
    L.append(table(["", "ECH (monthly)", "Serverless (monthly)"], [
        ["Billed on", "RAM-hours × tier × zones + snapshots + transfer", "metered ingest GB + retained GB (+ support)"],
        ["Data tiers", n0(comp.get("data_tiers_mo")), f"ingest {usd(((svl.get('by_solution') or {}).get(svl.get('dominant_solution')) or {}).get('ingest_mo'))}"],
        ["Fixed overhead (master / Kibana / coord / integrations)", f"{n0(comp.get('fixed_overhead_mo'))} ({s(comp.get('fixed_overhead_pct'))}%)", f"retention {usd(((svl.get('by_solution') or {}).get(svl.get('dominant_solution')) or {}).get('retention_mo'))}"],
        ["ML nodes", n0(comp.get("ml_nodes_mo")), f"support {usd(((svl.get('by_solution') or {}).get(svl.get('dominant_solution')) or {}).get('support_mo'))}"],
        ["Snapshots + transfer", n0(comp.get("non_capacity_mo")), "data out is not in this total"],
        ["Cost per stored GB-month", f"{s(svl.get('ech_ecu_per_stored_gb_mo'))} ECU", f"${s(svl.get('eff_usd_per_stored_gb_mo'))}"],
        ["Effective unit rates", "—", f"${s(svl.get('eff_ingest_usd_per_gb'))}/GB ingest · ${s(svl.get('eff_retain_usd_per_gb_mo'))}/GB-mo retain"],
    ]))
    L.append("")
    bs = svl.get("by_solution") or {}
    L.append(table(["Project", "Deps", "Stored GB", "Raw GB/d", "Metered GB/d", "Ingest $/mo", "Retain $/mo", "Support", "Total $/mo", "ECH ECU/mo", "Pricing"],
                   [[k.title(), s(v.get("deployments")), n0(v.get("stored_gb")), n1(v.get("daily_raw_gb")), n0(v.get("daily_metered_gb")), usd(v.get("ingest_mo")), usd(v.get("retention_mo")), usd(v.get("support_mo")), usd(v.get("total_mo")), n0(v.get("ech_ecu_mo")), "Search Serverless, performant" if str(v.get("pricing") or "").startswith("es3") else ("Observability Serverless" if "observability" in str(v.get("pricing") or "").lower() and "quotable" in str(v.get("pricing") or "") else s(v.get("pricing")))] for k, v in bs.items() if (v.get("stored_gb") or 0) > 0 or (v.get("ech_ecu_mo") or 0) > 0]))
    L.append("")
    search_blk_early = bs.get("search") or {}
    eis_rows = search_blk_early.get("eis_workloads") or []
    if eis_rows:
        L.append("### Search machine learning, priced as EIS")
        L.append(f"Deployments that billed jina embeddings also ran {n0(search_blk_early.get('jina_ml_ecu'))} ECU of ML nodes over the year ({n0(search_blk_early.get('jina_ml_ecu_mo'))} ECU/mo). That capacity is in the EIS line at list, with the billed tokens: **{usd(search_blk_early.get('eis_mo'))}/mo** before support. It is in the Search total, the regional projects, every Search profile, and every fleet row. Quieter and busier months scale the rest of the estimate and leave this line unchanged. Another {n0(search_blk_early.get('other_ml_ecu'))} ECU of ML nodes have no jina SKU and are not in the line.")
        L.append("")
        L.append(table(["EIS workload", "What is billed", "Million tokens", "ECU, full year", "$ / mo"],
                       [[w["model"], w["kind"], "—" if w.get("million_tokens") is None else f"{float(w.get('million_tokens') or 0):,.4f}", f"{float(w.get('ecu') or 0):,.2f}", f"${float(w.get('monthly') or 0):,.2f}"] for w in eis_rows]
                       + [["Total", "", "", f"{sum(float(w.get('ecu') or 0) for w in eis_rows):,.2f}", f"${float(search_blk_early.get('eis_mo') or 0):,.2f}"]]))
        L.append("")
    oh = fr.get("fixed_overhead_pct")
    if oh is None and fr.get("fixed_overhead_ecu_mo") and fr.get("ech_ecu_mo"):
        oh = round(100 * float(fr["fixed_overhead_ecu_mo"]) / float(fr["ech_ecu_mo"]), 1)
    if oh is None:
        oh = comp.get("fixed_overhead_pct")
    shown_fixed = fr.get("fixed_overhead_ecu_mo") or comp.get("fixed_overhead_mo")
    L.append(f"### 11.2 {s(oh)}% of the active deployments' ECU is cluster overhead" if oh is not None else "### 11.2 Cluster overhead")
    search_blk = ((svl.get("by_solution") or {}).get("search") or {})
    if search_blk.get("eis_mo") is not None and search_blk.get("ml_node_ecu_mo"):
        ml_sentence = f"Machine learning on Search is in the EIS line: {usd(search_blk.get('eis_mo'))}/mo, the billed tokens plus {n0(search_blk.get('jina_ml_ecu'))} ECU of ML nodes on deployments that billed jina."
    else:
        ml_sentence = f"Machine learning adds {n0(comp.get('ml_nodes_mo'))} ECU/mo."
    L.append(f"These are nodes that do not hold data: master, Kibana, coordinating, integrations, enterprise search, and APM. Together they are {n0(shown_fixed)} ECU/mo. Serverless does not charge for them. {ml_sentence} Snapshot storage, snapshot API calls, and data transfer add {n0(comp.get('non_capacity_mo'))} ECU/mo. Data in stays free. Data out carries over outside the serverless total: Observability and Security list it at 50 GB free, then $0.05 per GB, including Private Link. Inter-node does not carry over, and on Azure it is already waived. The bill is ECU, not GB, so this does not convert the hosted charge into a serverless dollar.")
    L.append("")
    if c.get("overhead_buckets"):
        L.append(table(["Size", "Clusters", "ECU", "Overhead %"], [[x["bucket"], x["deployments"], n0(x["ecu_lookback"]), s(x.get("fixed_overhead_pct"))] for x in c["overhead_buckets"]]))
        L.append("")
    hi = c.get("highest_overhead_deployments") or []
    if hi:
        L.append(f"{hi[0]['name']} is the highest single cluster, at {s(hi[0].get('fixed_overhead_pct'))}%.")
        L.append("")
    t = c.get("tail") or {}
    if t.get("n"):
        L.append(f"Smallest clusters — {t['n']} under {1:g}% of spend each: their bill is {n0(t.get('ech_ecu_mo'))} ECU/mo ({s(t.get('ech_share_pct'))}% of ECU), {n0(t.get('stored_gb'))} GB stored. Fixed overhead on them is {n0(t.get('fixed_overhead_ecu_mo'))} ECU/mo. Merging them onto fewer clusters saves about {n0(t.get('ech_consolidation_saving_mo'))} ECU/mo, if a quarter of their bill goes away.")
        L.append("")
    L.append("### 11.3 One project per solution in each region" if (c.get("architecture") or {}).get("per_region") else "### 11.3 One project per solution")
    if c.get("ladder"):
        L.append(table(["Metered GB/d band", "$ / GB", "GB in band", "$ / mo"], [[x["band_gb_day"], f"${x['usd_per_gb']:.4f}", n1(x["gb_in_band"]), usd(x["usd_mo"])] for x in c["ladder"]]))
        L.append("")
    L.append(f"{s(fr.get('n_with_gb'))} of {s(fr.get('n_deployments'))} deployments have a stored-GB row ({n0(fr.get('stored_gb'))} GB). Fixed overhead is {n0(fr.get('fixed_overhead_ecu_mo'))} ECU/mo. The quote is one read-back per solution, {usd(sv.get('serverless_mo'))}/mo. Deployments are not priced on their own.")
    L.append("")
    arch = c.get("architecture") or {}
    if arch.get("groups") and arch.get("projects"):
        L.extend(architecture_mermaid(arch))
        if arch.get("per_region"):
            L.append(f"The deployments run in {len(arch.get('regions') or [])} regions ({', '.join(arch.get('regions') or [])}). The estimate is one serverless project per solution per region, each with its own sizing. Search is re-estimated in each region. Observability and Security are a sheet filled for that region.")
            L.append("")
    L.append(mermaid_xy("Largest deployments, ECH ECU/mo", [r["name"] for r in (c.get("by_deployment") or [])[:8]], [r.get("ech_ecu_mo") or 0 for r in (c.get("by_deployment") or [])[:8]], ylabel="ECU/mo"))
    L.append("")
    L.append("### 11.4 Where ECH spend sits")
    L.append(table(["Deployment", "Solution", "Stored GB", "List $/mo", "Calculated $/mo", "Fixed ECU/mo", "Overhead %"],
                   [[r["name"], r["solution"], n0(r["stored_gb"]) if r.get("gb_known") else (f"on {r['size_now_on']}" if r.get("size_now_on") else "no GB"), usd(r["ech_ecu_mo"]), usd(r.get("ech_cost_mo")), n0(r.get("fixed_overhead_ecu_mo")), s(r.get("overhead_pct"))] for r in c.get("by_deployment") or []]))
    L.append("\nList $/mo is ECU at $1. Calculated $/mo is that ECU × ARR ÷ (commit × 365).\n")
    L.append("")
    for x in c.get("callouts") or []:
        L.append(f"- **{x['title']}** — {x['detail']}")
    L.append("")
    L.append("### 11.5 What if data is kept longer, or the fleet is busier")
    sens = c.get("sensitivity") or {}
    if sens.get("retention"):
        used = next((r for r in sens["retention"] if r.get("used")), None)
        L.append(table(["Kept for", "GB stored", "GB per day"],
                       [[f"{r['retention_months']:g} mo" + (" (quote)" if r.get("used") else ""), n0(r.get("stored_gb")), n1(r.get("raw_gb_day"))] for r in sens["retention"]]))
        if used:
            L.append(f"\nSearch holds {n0(used.get('stored_gb'))} GB. The quote keeps it for {used['retention_months']:g} months, about {n1(used.get('raw_gb_day'))} GB per day. A shorter keep means the same data arrived faster. The price was not recalculated.\n")
    if sens.get("fleet"):
        if sv.get("ech_contract_mo") is not None:
            L.append(table(["Usage", "ECH at contract rate", "Serverless, estimate", "Savings vs. contract rate"],
                           [[r["scenario"], usd(r.get("ech_cost_mo")), usd(r.get("serverless_mo")), usd(r.get("save_vs_contract_mo"))] for r in sens["fleet"]]))
        else:
            L.append(table(["Usage", "ECH at list", "Serverless, estimate", "Savings vs. list"],
                           [[r["scenario"], usd(r["ech_ecu_mo"]), usd(r.get("serverless_mo")), usd(r.get("save_vs_list_mo"))] for r in sens["fleet"]]))
        L.append("")
        eis_bit = next((r.get("eis_mo") for r in sens["fleet"] if r.get("eis_mo")), None)
        held = f" EIS stays at {usd(eis_bit)}/mo in every row." if eis_bit else ""
        L.append(f"The average row uses the serverless estimate in this report. The other rows scale the rest of the estimate by the same usage change.{held} The estimators were not re-run for them.\n")
    L.append("### 11.6 Reconciliation & caveats")
    rc = c.get("reconciliation") or {}
    if rc.get("quotes"):
        L.append(table(["Solution", "Estimator", "$ / mo", "Year 1", "Steady annual"], [[q.get("solution", "").title(), s(q.get("source")), usd(q.get("monthly")), usd(q.get("annual_year1")), usd(q.get("annual_steady_state"))] for q in rc["quotes"]] + [["Total", "both estimators", usd(rc.get("total_mo")), "—", "—"]]))
        L.append("")
        L.append("These rows are the only serverless dollars in the report.")
        L.append("")
    elif rc:
        L.append(f"Estimator read-back ({rc['estimator'].title()}): sheet {usd(rc['sheet_monthly'])}/mo vs ladder model {usd(rc['model_monthly'])}/mo (Δ {rc.get('delta_pct')}%) · sheet Y1 {usd(rc.get('sheet_annual_year1'))} · steady {usd(rc.get('sheet_annual_steady'))} · metered {n0(rc.get('sheet_metered_gb_day'))} vs {n0(rc.get('model_metered_gb_day'))} GB/d. **Quote the sheet.**" + (f" [{rc['url']}]({rc['url']})" if rc.get("url") else ""))
    else:
        L.append("No estimator read-back available for reconciliation (sizing step skipped or failed).")
    L.append("")
    for x in (c.get("caveats") or []) + (c.get("notes") or []):
        L.append(f"- {x}")
    L.append(f"\nPricing source: {s(svl.get('pricing_source'))}\n")
    L.extend(cost_sizing_md(acc))
    integrity = c.get("integrity") or {}
    if integrity.get("checks"):
        score = integrity.get("score")
        passed = integrity.get("passed")
        if score is None:
            lead = "Quote integrity was not scored."
        else:
            lead = f"Quote integrity **{float(score):.3f}** — {'Pass' if passed else 'Fail'}."
        L.append("\n### Appendix\n")
        L.append(lead)
        L.append("")
        L.append("The score is the mean of the checks that apply. A check that does not apply is n/a and stays out of the mean. A score of 0.90 or higher passes. A check that misses its own bar stays flagged. An echo gap of 0.51 scores 0 on that component and still passes that component. This is not a serverless bill.")
        L.append("")
        L.append(table(
            ["Check", "Score", "Pass", "Measured"],
            [[
                row.get("check") or "",
                "—" if row.get("score") is None else f"{float(row['score']):.3f}",
                "n/a" if row.get("score") is None else ("Pass" if row.get("passed") else "Fail"),
                row.get("detail") or "",
            ] for row in integrity["checks"]],
        ))
        L.append("")
    return L


def render(acc: dict) -> str:
    m, p, c, e, d, st, sol, ai, wl, an, sz = (
        acc["meta"], acc["platform"], acc["cloud"], acc["ecu"], acc["deployments"], acc["storage"],
        acc["solutions"], acc["ai_ml"], acc["workload"], acc["analysis"], acc.get("sizing") or {},
    )
    cs = c["summary"]
    org = m.get("org") or {}
    L: list[str] = []
    L.append(f"# {m['customer']} — K360 account analysis ({m['date']})")
    L.append("")
    L.append(
        f"Lookback {e.get('lookback_days')} days ({(e.get('range') or {}).get('start')} → {(e.get('range') or {}).get('end')}) · "
        f"generated {m['generated_at']} · AE {s(org.get('AE'))} · ARR {s(org.get('ARR'))} · segment {s(org.get('segment'))}"
    )
    L.append("")
    L.append("## Snapshot")
    L.append(
        table(
            ["Metric", "Value"],
            [
                ["Platform", f"**{p['label']}** — {p['ech_count']} ECH / {p['serverless_count']} serverless · ECH share {p['ech_share_pct']}% of ECU"],
                ["Cloud", f"{s(cs.get('provider'))} · {s(cs.get('region'))} · hot {s(cs.get('hot_instance'))} · {s(cs.get('zones'))} AZ · plan {s(cs.get('plan'))}"],
                ["ECU (lookback)", f"{n0(e['total_lookback'])} · monthly avg {n0(e.get('monthly_avg_complete_months') or e.get('monthly_avg_all'))} · peak {s((e.get('monthly_peak') or {}).get('month'))} = {n0((e.get('monthly_peak') or {}).get('ecu'))} · daily avg {n0(e.get('daily_avg'))}"],
                ["Deployments", f"{d['count']} · trend **{d['trend'].get('flag')}** · reference version {s(d['version_summary'].get('reference_version'))}"],
                ["Storage", f"{st['data_stored_tb']} TB · {n0(st.get('docs_count'))} docs · avg doc {st.get('avg_doc_kb')} KB (as of {st.get('as_of')})"],
                ["ECU by solution", " / ".join(f"{k.title()} {v['ecu_pct']}%" for k, v in sol['totals'].items())],
                ["Burndown", f"**{an['burndown']['level']}** · rate {s(an['burndown'].get('consumption_rate'))}× · runway {s(an['burndown'].get('runway_days'))} d"],
                ["Serverless sizing", ("triggered (ECH > 50%)" + (" — forced" if sz.get("forced") else "")) if sz.get("triggered") else (f"skipped: {sz['skipped_reason']}" if sz.get("skipped_reason") else "not triggered (ECH ≤ 50%)")],
            ],
        )
    )
    L.append("")

    # 1 platform + cloud
    L.append("## 1. Platform, cloud & instance details")
    L.append(f"**{p['label']}** — ECH {p['ech_count']} deployments / {n0(p['ech_ecu'])} ECU ({p['ech_share_pct']}%); Serverless {p['serverless_count']} projects / {n0(p['serverless_ecu'])} ECU ({p['serverless_share_pct']}%).")
    L.append("")
    L.extend(ech_topology_mermaid(acc))
    svl = p.get("serverless") or {}
    if svl.get("projects"):
        bysol = svl.get("by_solution") or {}
        L.append("Serverless by solution: " + ", ".join(f"{k.title()} {v['projects']} projects / {n0(v['ecu'])} ECU" for k, v in bysol.items()))
        L.append("")
        L.append("<details><summary>Serverless projects (top by ECU)</summary>\n")
        L.append(table(["Project id", "Solution", "Provider", "ECU", "Created", "Last billed"], [[x["project_id"], s(x.get("solution")), s(x.get("provider")), n0(x["ecu"]), s(x.get("created")), s(x.get("last_bill"))] for x in svl["projects"][:15]]))
        L.append("\n</details>\n")
    if c.get("regions"):
        L.append(table(["Region", "Provider", "ECU", "%", "Deployments"], [[r["region"], s(r.get("provider")), n0(r["ecu"]), r.get("pct", "—"), r.get("deployments", "—")] for r in c["regions"][:8]]))
        L.append("")
    if c.get("tiers"):
        L.append(table(["Tier", "ECU", "% of capacity"], [[t["tier"], n0(t["ecu"]), t["pct"]] for t in c["tiers"]]))
        L.append("")
    if c.get("instances"):
        L.append(table(["Instance", "Tier", "ECU", "%", "Deps (hot)"], [[i["instance"], s(i.get("tier")), n0(i["ecu"]), i["pct"], i.get("deployments_hot", "—")] for i in c["instances"][:8]]))
        L.append("")
    if c.get("zones"):
        L.append("Availability zones: " + ", ".join(f"{z['zones']} AZ = {z['pct']}%" for z in c["zones"]))
        L.append("")
    z1 = c.get("single_zone") or []
    if z1:
        shown = ", ".join(r["name"] for r in z1[:8])
        more = f" and {len(z1) - 8} more" if len(z1) > 8 else ""
        L.append(f"**Warning — {len(z1)} deployment(s) in a single availability zone:** {shown}{more}. A zone outage takes these offline; move production to 2–3 zones.")
        L.append("")
    if c.get("non_capacity"):
        L.append("Non-capacity billing: " + ", ".join(f"{t['type']} {n0(t['ecu'])} ECU ({t['pct']}%)" for t in c["non_capacity"] if t["ecu"]))
        L.append("")
    if c.get("by_deployment"):
        L.append("<details><summary>Per-deployment cloud detail (top 15 by ECU)</summary>\n")
        L.append(table(["Deployment", "Platform", "Version", "Region", "Hot instance", "AZ", "Hot RAM GB (max)", "Frozen RAM GB", "ECU"],
                       [[r["name"], s(r.get("platform")), s(r.get("version")), s(r.get("region")), s(r.get("hot_instance")), s(r.get("zones")),
                         s((r.get("tier_ram_gb_max") or {}).get("datahot")), s((r.get("tier_ram_gb_max") or {}).get("datafrozen")), n0(r["ecu"])] for r in c["by_deployment"][:15]]))
        L.append("\n</details>\n")

    # 2 deployments & versions & ECU
    L.append("## 2. Deployments, versions & ECU")
    L.append(table(["Version", "Deployments", "ECU", "% ECU"], [[v["version"], v["count"], n0(v["ecu"]), v["ecu_pct"]] for v in d["by_version"]]))
    L.append("")
    L.append(mermaid_pie("Deployments by stack version", [(v["version"], v["count"]) for v in d["by_version"]]))
    L.append("")
    vs = d["version_summary"]
    if vs.get("lagging"):
        L.append(f"Lagging versions ({vs['lagging_count']} deployments, {vs['lagging_ecu_pct']}% of ECU): " + "; ".join(f"{x['version']}×{x['count']} — {x['reason']}" for x in vs["lagging"]))
        L.append("")
    L.append(
        table(
            ["ECU metric", "Value"],
            [
                ["Total (lookback)", n0(e["total_lookback"])],
                ["Monthly average (complete months)", n0(e.get("monthly_avg_complete_months"))],
                ["Monthly average (all months incl. partial)", n0(e.get("monthly_avg_all"))],
                ["Monthly peak", f"{s((e.get('monthly_peak') or {}).get('month'))} = {n0((e.get('monthly_peak') or {}).get('ecu'))}"],
                ["Monthly low", f"{s((e.get('monthly_low') or {}).get('month'))} = {n0((e.get('monthly_low') or {}).get('ecu'))}"],
                ["Daily average", n0(e.get("daily_avg"))],
                ["Daily peak", f"{s((e.get('daily_peak') or {}).get('day'))} = {n0((e.get('daily_peak') or {}).get('ecu'))}"],
                ["Last 30 days", f"{n0(e.get('last_30d_total'))} ({n0(e.get('last_30d_daily_avg'))}/day; {s((e.get('trend') or {}).get('direction'))})"],
                ["Commit / required pace (ECU/day)", f"{s(e['contract'].get('commit_pace_ecu_per_day'))} / {s(e['contract'].get('required_pace_ecu_per_day'))}"],
                ["ECU balance", n0(e["contract"].get("ecu_balance"))],
            ],
        )
    )
    L.append("")
    mo = e.get("monthly") or {}
    L.append(mermaid_xy("Monthly ECU", mo.get("months") or [], mo.get("totals") or []))
    L.append("")
    L.append("Top deployments by ECU:")
    L.append("")
    L.append(table(["Deployment", "Version", "Use case", "ECU", "%", "ECU/mo avg", "Months active"], [[t["name"], s(t.get("version")), s(t.get("use_case")), n0(t["ecu"]), t["ecu_pct"], n0(t.get("ecu_monthly_avg")), s(t.get("months_active"))] for t in d["top"][:10]]))
    L.append("")

    # 3 burndown (was 9)
    bd = an["burndown"]
    L.append(f"## 3. Burndown analysis — **{bd['level']}**")
    ser = bd.get("series") or {}
    if ser.get("months"):
        L.append(mermaid_xy("Implied ECU balance (history + forecast at recent rate)", ser["months"], ser.get("implied_balance") or [], kind="line", ylabel="ECU remaining"))
        L.append("")
        L.append(ser.get("note") or "")
        L.append("")
    L.append(
        table(
            ["Metric", "Value"],
            [
                ["Consumption rate (actual ÷ commit pace)", f"{bd.get('consumption_rate')}×"],
                ["Actual daily (30d)", n0(bd.get("daily_30d"))],
                ["Commit pace / required pace (ECU/day)", f"{s(bd.get('commit_pace'))} / {s(bd.get('required_pace'))}"],
                ["ECU balance", n0(bd.get("ecu_balance"))],
                ["Runway", f"{s(bd.get('runway_days'))} days → runout {s(bd.get('runout_date'))}"],
                ["Nearest contract end", s(bd.get("nearest_contract_end"))],
                ["Annual projection", n0(bd.get("annual_projection"))],
            ],
        )
    )
    L.append("")
    pr = an.get("projections") or {}
    if pr.get("horizons"):
        L.append(pr.get("note") or "")
        L.append("")
        L.append(
            table(
                ["Horizon", "As of", "ECU", "Balance after", "Contract $"],
                [
                    [
                        f"{h.get('months')} months",
                        s(h.get("as_of")),
                        n0(h.get("ecu")),
                        n0(h.get("balance")),
                        f"${float(h['usd']):,.0f}" if h.get("usd") is not None else "—",
                    ]
                    for h in pr["horizons"]
                ],
            )
        )
        L.append("")
        L.append(f"Pace {n0(pr.get('ecu_per_month'))} ECU/mo ({n0(pr.get('ecu_per_day'))} ECU/day). Year-pace runway {s(pr.get('runway_days'))} days → {s(pr.get('runout_date'))}.")
        L.append("")
    for f in bd["flags"]:
        L.append(f"- {sev(f['severity'])} **{f['flag']}** — {f['detail']}")
    if not bd["flags"]:
        L.append("- No burndown flags.")
    L.append("")

    # 4 deployments over time
    L.append("## 4. Deployments over time")
    ot = d["over_time"]["month"]
    L.append(mermaid_xy("Active deployments per month", ot.get("periods") or [], ot.get("count") or [], kind="line", ylabel="deployments"))
    L.append("")
    tr = d["trend"]
    if tr.get("flag") and "first_avg" in tr:
        L.append(
            f"**{tr['flag'].title()}**: avg {tr['first_avg']} deployments ({tr['first_period'][0]}→{tr['first_period'][1]}) → {tr['last_avg']} ({tr['last_period'][0]}→{tr['last_period'][1]}), "
            f"Δ {tr['delta']:+} ({tr['delta_pct']:+}%). Peak {tr['peak']['count']} in {tr['peak']['month']}; largest month-over-month change {s((tr.get('largest_mom_change') or {}).get('delta'))} in {s((tr.get('largest_mom_change') or {}).get('month'))}."
        )
    else:
        L.append(f"Trend: {tr.get('flag')} — {tr.get('note', '')}")
    L.append("")

    # 5 storage
    L.append("## 5. Data storage")
    L.append(
        table(
            ["Metric", "Value"],
            [
                ["Data stored", f"{st['data_stored_tb']} TB ({n0(st['data_stored_gb'])} GB) as of {st.get('as_of')}" + (f" ⚠ {st['telemetry_age_days']} d old" if (st.get('telemetry_age_days') or 0) > 45 else "")],
                ["Docs", n0(st.get("docs_count"))],
                ["Avg doc size", f"{st.get('avg_doc_kb')} KB"],
                ["ECU per stored GB per month (org)", s(st.get("ecu_per_gb_mo_org"))],
                ["Tier mix (capacity ECU)", ", ".join(f"{k} {v['pct']}%" for k, v in (st.get("tier_mix") or {}).items()) or "—"],
                ["Frozen tier", "yes" if st.get("has_frozen") else "no"],
                ["Snapshot storage ECU", n0(st.get("snapshot_storage_ecu"))],
                ["Largest deployment share", f"{st.get('top_deployment_share_pct')}%"],
            ],
        )
    )
    L.append("")
    L.append(table(["Deployment", "GB", "Docs", "Avg doc KB", "ECU/mo", "ECU per GB/mo"], [[r["name"], n0(r["data_stored_gb"]), n0(r.get("docs_count")), s(r.get("avg_doc_kb")), n0(r.get("ecu_monthly_avg")), s(r.get("ecu_per_gb_mo"))] for r in st["by_deployment"][:10]]))
    L.append("")

    # 6 ECU by solution
    L.append("## 6. ECU by solution (Observability / Search / Security)")
    L.append(table(["Solution", "Deployments", "ECU", "%", "ECU/mo", "Stored GB"], [[k.title(), v["deployments"], n0(v["ecu"]), v["ecu_pct"], n0(v["ecu_monthly_avg"]), n0(v["gb"])] for k, v in sol["totals"].items()]))
    L.append("")
    L.append(mermaid_pie("ECU by solution", [(k.title(), v["ecu"]) for k, v in sol["totals"].items()]))
    L.append("")
    L.append(f"Classification: {sol['method']} (counts: {sol['method_counts']}). Of which EIS inference {n0(sol['cross_cutting']['genai_eis_inference_ecu'])} ECU, ML nodes {n0(sol['cross_cutting']['ml_node_ecu'])} ECU.")
    L.append("")

    # 7 AI / ML
    L.append("## 7. ML, vectors & AI by deployment")
    tm = ai.get("token_meter") or {}
    token_bit = ""
    if tm.get("rows"):
        token_bit = f" · {tm.get('million_tokens')} million tokens"
    L.append(f"ML node ECU {n0(ai['ml_node_ecu'])} ({ai['ml_node_pct_of_org']}% of org) · EIS inference {n0(ai['eis_inference_ecu'])} ECU{token_bit} · vector deployments {s(ai.get('vector_deployments'))}")
    L.append("")
    fc = ai.get("feature_counts") or {}
    L.append("Feature flags (deployments): " + ", ".join(f"{k} {v}" for k, v in fc.items()))
    L.append("")
    if tm.get("rows"):
        L.append("### EIS token meter")
        L.append("Each row is one billed model. Quantity is already in millions of tokens, and ECU per million is the rate on the bill. A blank model means the SKU has no model suffix.")
        split = tm.get("by_solution") or {}
        if split:
            L.append("By solution: " + ", ".join(f"{k} {v.get('million_tokens')}M tokens / {v.get('ecu')} ECU" for k, v in split.items()) + ".")
        L.append("")
        def _mt(v):
            try:
                v = float(v)
            except (TypeError, ValueError):
                return "—"
            return f"{v:,.2f}" if abs(v) >= 0.1 else f"{v:,.4f}"
        L.append(table(
            ["Model", "What is billed", "Where", "Million tokens", "ECU", "ECU / million"],
            [[r.get("model_label") or ("Chat" if str(r.get("kind") or "").startswith("chat") else "—"), r.get("kind_label") or "—", r.get("scope") or "—", _mt(r.get("million_tokens")), f"{float(r.get('ecu') or 0):,.2f}", "—" if r.get("ecu_per_million") is None else f"{float(r['ecu_per_million']):.2f}"] for r in tm["rows"]]
            + [["Total", "", "", _mt(tm.get("million_tokens")), f"{float(tm.get('ecu') or 0):,.2f}", ""]],
        ))
        L.append("")
        for note in (tm.get("notes") or [])[:2]:
            L.append(note)
        L.append("")
    if ai.get("by_deployment"):
        L.append(table(["Deployment", "Version", "ML node ECU", "EIS ECU", "M tokens", "AI % of dep", "ELSER", "Vectors", "Models", "ML SKUs"],
                       [[r["name"], s(r.get("version")), n0(r["ml_node_ecu"]), f"{r['eis_inference_ecu']:.2f}", s(r.get("million_tokens")) if r.get("million_tokens") else "—", r["ai_pct_of_dep"], "✓" if r["elser"] else "", "✓" if r["vector_mappings"] else "", ", ".join(r.get("model_labels") or r.get("models") or []) or "—", "; ".join(x for x in r["ml_skus"] if x) or "—"] for r in ai["by_deployment"][:12]]))
        L.append("")
    else:
        L.append("No ML node, EIS inference or vector/ELSER usage detected.")
        L.append("")

    # workload
    sw = (wl.get("search") or {})
    if (sw.get("peak") or {}).get("qps") is not None:
        L.append("### Workload (day-average from index telemetry)")
        L.append(table(["", "QPS", "Complexity", "% hours/mo"], [["Peak", n0(sw["peak"].get("qps")), f"{s(sw['peak'].get('complexity'))} ({s(sw['peak'].get('complexity_ms'))} ms)", s(sw["peak"].get("pct_hours_mo"))], ["Non-peak", n0((sw.get("nonpeak") or {}).get("qps")), f"{s((sw.get('nonpeak') or {}).get('complexity'))} ({s((sw.get('nonpeak') or {}).get('complexity_ms'))} ms)", s((sw.get("nonpeak") or {}).get("pct_hours_mo"))]]))
        ing = wl.get("ingest") or {}
        L.append(f"\nIngest ≈ {n0(ing.get('docs_per_sec'))} docs/s · avg doc {s(ing.get('avg_doc_bytes'))} B · active {s(ing.get('active_hours_mo'))} h/mo")
        L.append("")

    # analysis
    L.append("## 8. Current State Analysis & Recommendations")
    for i, r in enumerate(an["performance"], 1):
        L.append(f"{i}. **{r['title']}** ({sev(r['severity'])}) — {r['evidence']}  \n   → {r['action']}" + (f" _(impact: {r['impact']})_" if r.get("impact") else ""))
    if not an["performance"]:
        L.append("No performance findings above threshold.")
    L.append("")
    if not consolidation_report_exists(acc):
        L.extend(cost_sizing_md(acc))

    # consolidation savings (same trigger); §9 and §10 are appended there when it ran
    L.extend(consolidation_section(acc))
    L.append("---")
    L.append("Notes: " + " · ".join((e.get("notes") or []) + (sz.get("notes") or [])[:1]))
    return "\n".join(L) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--account-json", required=True)
    ap.add_argument("--out", default=None, help="default: <run_key>_account_report.md beside the JSON")
    args = ap.parse_args()
    src = Path(args.account_json)
    acc = json.loads(src.read_text())
    out = Path(args.out) if args.out else src.with_name(f"{acc['meta']['run_key']}_account_report.md")
    out.write_text(render(acc))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
