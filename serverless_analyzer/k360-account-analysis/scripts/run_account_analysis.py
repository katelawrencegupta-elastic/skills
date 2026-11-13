#!/usr/bin/env python3
"""K360 account summary: fetch → infra → analyze → report → account PDF.

  python run_account_analysis.py --org "Acme Corp" --skip-diags --skip-sizing
  python run_account_analysis.py --org "Acme Corp" --reuse /tmp/org_report.json --skip-diags --skip-sizing

Run from the project root that holds `.env` (KIBANA_URL, ELASTIC_API_KEY).
Depends on the k360-org-report skill for fetch_org.py + sheets_auth.py
(override location with K360_ORG_SKILL_DIR).

A run that does not pass both --skip-diags and --skip-sizing is a Serverless
quote and hands off to diag-serverless-consolidation/scripts/run_consolidation.py.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from skill_paths import artifacts_root, find_skill, scripts_dir  # noqa: E402


def diag_script_dir() -> Path:
    """Sibling diag-serverless-consolidation scripts. DIAG_SKILL_DIR overrides the skill root."""
    return scripts_dir(find_skill("diag-serverless-consolidation", HERE))


sys.path.insert(0, str(diag_script_dir()))
from analyze import build_account, chat_bullets, component_summary  # noqa: E402
from privacy import seal_dir, seal_text, warn_unignored_secrets  # noqa: E402
from quote_checks import e73_blocks_quote, money as _sheet_money  # noqa: E402
from k360_infra import es_base_and_key, fetch_infra, fill_ingest_doc_bytes, fill_storage_from_data_pivot, fill_storage_monthly, load_env, resolve_org_name, size_gaps  # noqa: E402
from report_md import render  # noqa: E402

ORG_SKILL = find_skill("k360-org-report", HERE)


def _run(cmd: list[str]) -> None:
    print("+", " ".join(str(c) for c in cmd), flush=True)
    subprocess.check_call(cmd)


def _sheets_py() -> Path:
    return Path(os.environ.get("SERVERLESS_SHEETS_PY") or Path.home() / ".config/serverless-analyzer/venv/bin/python")


def _rnd(v, nd: int = 0):
    try:
        if v is None or isinstance(v, bool):
            return v
        return round(float(v), nd)
    except (TypeError, ValueError):
        return v


def _est(v):
    if not isinstance(v, dict):
        return None
    keep = ("title", "url", "status", "readback", "region_match")
    return {k: v[k] for k in keep if k in v}


def _token_meter_brief(meter: dict | None) -> dict | None:
    """Compact token meter for the brief. Full rows stay in account.json."""
    if not isinstance(meter, dict) or not (meter.get("rows") or meter.get("million_tokens")):
        return None
    rows = []
    for r in (meter.get("rows") or [])[:6]:
        rows.append({
            "model": r.get("model_label"),
            "kind": r.get("kind_label"),
            "million_tokens": r.get("million_tokens"),
            "ecu": r.get("ecu"),
            "ecu_per_million": r.get("ecu_per_million"),
        })
    out = {
        "unit": "million tokens",
        "million_tokens": meter.get("million_tokens"),
        "ecu": meter.get("ecu"),
        "rows": rows,
    }
    extra = len(meter.get("rows") or []) - len(rows)
    if extra > 0:
        out["rows_omitted"] = extra
    split = {k: v for k, v in (meter.get("by_solution") or {}).items() if k != "unattributed" or (v or {}).get("ecu")}
    if len(split) > 1:
        out["by_solution"] = split
    return out


def agent_brief(acc: dict) -> dict:
    """One small JSON the agent reads. account.json stays on disk."""
    m = acc.get("meta") or {}
    org = m.get("org") or {}
    p = acc.get("platform") or {}
    cloud = acc.get("cloud") or {}
    ecu = acc.get("ecu") or {}
    monthly = ecu.get("monthly") or {}
    deps = acc.get("deployments") or {}
    st = acc.get("storage") or {}
    sol = (acc.get("solutions") or {}).get("totals") or {}
    sol_by = {d.get("name"): d.get("solution") for d in ((acc.get("solutions") or {}).get("by_deployment") or [])}
    an = acc.get("analysis") or {}
    burn = an.get("burndown") or {}
    series = burn.get("series") or {}
    months = series.get("months") or []
    start = months.index(series["forecast_from"]) if series.get("forecast_from") in months else 0
    window = slice(start, start + 18)
    sz = acc.get("sizing") or {}
    cons = acc.get("consolidation") or {}
    est_in = sz.get("estimators") if isinstance(sz.get("estimators"), dict) else {}
    estimators = {k: view for k, v in est_in.items() if (view := _est(v))}
    sec_rb = (estimators.get("security") or {}).get("readback") or {}
    cps_val = _sheet_money(sec_rb.get("cps_addon_monthly")) if sec_rb else None
    notes = list((cons.get("notes") or [])[:3])
    if sec_rb and e73_blocks_quote(sec_rb.get("cps_addon_monthly")):
        if cps_val is None:
            notes.append("Security was left out of the quote. Sheet E73 was blank or could not be read.")
        else:
            notes.append(f"Security was left out of the quote. Sheet E73, the template cross-project search add-on, is ${cps_val:,.0f}.")
    exports = acc.get("exports") or {}
    slug = (m.get("customer_slug") or "account").replace("_", "-").lower()
    return {
        "customer": m.get("customer"),
        "run_key": m.get("run_key"),
        "date": m.get("date"),
        "lookback": m.get("lookback"),
        "org": {
            "id": org.get("id"), "ae": org.get("AE"), "arr": _rnd(org.get("ARR"), 2),
            "segment": org.get("segment"), "geo": org.get("geo"), "customer_type": org.get("customer_type"),
            "contract": org.get("contract"),
        },
        "platform": {
            "label": p.get("label"), "ech_count": p.get("ech_count"), "serverless_count": p.get("serverless_count"),
            "ech_ecu": _rnd(p.get("ech_ecu")), "serverless_ecu": _rnd(p.get("serverless_ecu"), 2),
            "ech_share_pct": p.get("ech_share_pct"), "summary": cloud.get("summary"),
            "zones": cloud.get("zones"), "regions": cloud.get("regions"), "tiers": (cloud.get("tiers") or [])[:6],
            "one_az": [d.get("name") for d in (cloud.get("by_deployment") or []) if d.get("zones") == 1][:8],
            "top_deployments": [
                {
                    "name": d.get("name"), "version": d.get("version"), "region": d.get("region"),
                    "provider": d.get("provider"), "zones": d.get("zones"),
                    "ecu": _rnd(d.get("ecu")),
                    "solution": sol_by.get(d.get("name")) or d.get("use_case"),
                    "nodes": component_summary(d) or None,
                }
                for d in (cloud.get("by_deployment") or [])[:8]
            ],
        },
        "ecu": {
            "total": _rnd(ecu.get("total_lookback")), "monthly_avg": _rnd(ecu.get("monthly_avg_complete_months")),
            "peak": ecu.get("monthly_peak"), "low": ecu.get("monthly_low"),
            "daily_avg": _rnd(ecu.get("daily_avg")), "last_30d_daily_avg": _rnd(ecu.get("last_30d_daily_avg")),
            "trend": ecu.get("trend"), "months": monthly.get("months"),
            "totals": [_rnd(x) for x in (monthly.get("totals") or [])],
            "partial_months": monthly.get("partial_months"), "contract": ecu.get("contract"),
        },
        "versions": (deps.get("by_version") or [])[:6],
        "version_summary": deps.get("version_summary"),
        "deployment_trend": deps.get("trend"),
        "deployment_counts": ((deps.get("over_time") or {}).get("month")),
        "storage": {
            "as_of": st.get("as_of"), "telemetry_age_days": st.get("telemetry_age_days"),
            "tb": st.get("data_stored_tb"), "gb": _rnd(st.get("data_stored_gb"), 1),
            "docs_count": st.get("docs_count"), "avg_doc_kb": st.get("avg_doc_kb"),
            "notes": (st.get("notes") or [])[:2],
            "top": [{"name": r.get("name"), "gb": _rnd(r.get("data_stored_gb"), 1), "as_of": r.get("as_of")} for r in (st.get("by_deployment") or [])[:5]],
            "monthly": {
                "months": (st.get("monthly") or {}).get("months"),
                "gb": (st.get("monthly") or {}).get("gb"),
                "source": (st.get("monthly") or {}).get("source"),
            } if (st.get("monthly") or {}).get("months") else None,
        },
        "solutions": {k: {"ecu_pct": v.get("ecu_pct"), "deployments": v.get("deployments"), "gb": _rnd(v.get("gb"), 1)} for k, v in sol.items() if isinstance(v, dict)},
        "ai_ml": {
            "ml_node_ecu": _rnd((acc.get("ai_ml") or {}).get("ml_node_ecu")),
            "eis_inference_ecu": _rnd((acc.get("ai_ml") or {}).get("eis_inference_ecu"), 2),
            "feature_counts": (acc.get("ai_ml") or {}).get("feature_counts"),
            "token_meter": _token_meter_brief((acc.get("ai_ml") or {}).get("token_meter")),
        },
        "burndown": {
            "level": burn.get("level"), "flags": burn.get("flags"), "runway_days": burn.get("runway_days"),
            "runout_date": burn.get("runout_date"), "contract_end": burn.get("nearest_contract_end"),
            "balance": burn.get("ecu_balance"), "daily_30d": burn.get("daily_30d"),
            "commit_pace": burn.get("commit_pace"), "required_pace": burn.get("required_pace"),
            "consumption_rate": burn.get("consumption_rate"),
            "months": months[window],
            "balance_at_rate": (series.get("implied_balance") or [])[window],
            "balance_at_commit": (series.get("commit_pace_balance") or [])[window],
        },
        "projections": an.get("projections") or {},
        "recommendations": [
            {"kind": kind, "severity": r.get("severity"), "title": r.get("title"), "evidence": r.get("evidence")}
            for kind in ("performance", "cost") for r in (an.get(kind) or [])[:5]
        ],
        "contract_rate": cons.get("contract_rate"),
        "savings": {k: (cons.get("savings") or {}).get(k) for k in ("ech_basis", "ech_list_mo", "serverless_mo", "list_mo", "list_yr", "list_pct") if (cons.get("savings") or {}).get(k) is not None},
        "quotes": ((cons.get("reconciliation") or {}).get("quotes")) or [],
        "integrity": cons.get("integrity"),
        "fleet": ((cons.get("sensitivity") or {}).get("fleet")) or [],
        "notes": notes,
        "estimators": estimators,
        "links": {
            "folder": sz.get("folder"),
            "drive": (sz.get("drive_folder") or {}).get("url"),
            "estimators": {k: v.get("url") for k, v in estimators.items() if v.get("url")},
            "pdfs": {
                k: {field: v.get(field) for field in ("pdf", "html", "drive_url") if v.get(field)}
                for k, v in exports.items() if isinstance(v, dict)
            },
        },
        "chat_bullets": chat_bullets(acc),
        "canvas_file": f"{slug}-account-analysis.canvas.tsx",
        "ingest_source": cons.get("source") or sz.get("ingest_source") or "k360",
        "consolidation_canvas_file": f"{slug}-serverless-consolidation-savings.canvas.tsx" if cons else None,
        "method": m.get("method"),
        "ech_scope": cons.get("ech_scope") if cons else None,
        "k360_ingest": _k360_brief(acc),
        "k360_canvas_file": f"{slug}-serverless-consolidation-from-k360.canvas.tsx" if acc.get("k360_ingest") else None,
        "diagnostics_coverage": sz.get("diagnostics") if (cons.get("source") == "diagnostics" or sz.get("ingest_source") == "diagnostics") else None,
        "compressioncalc_accounted_for": acc.get("compressioncalc_accounted_for"),
    }


def _k360_brief(acc: dict) -> dict | None:
    block = acc.get("k360_ingest") or {}
    if not block:
        return None
    keep = ("source", "savings", "quotes", "notes", "contract_rate")
    return {k: block[k] for k in keep if k in block}

def wants_quote(args) -> bool:
    """The account summary passes both skip flags. Any other run is a Serverless quote."""
    return not (args.skip_diags and args.skip_sizing)


def parse_args(argv=None, description: str | None = None):
    ap = argparse.ArgumentParser(description=description or __doc__)
    ap.add_argument("--org", required=True, help="Organization name as in K360 (match_phrase)")
    ap.add_argument("--org-id", action="append", default=None, help="Cloud organization id. Repeat when one customer name covers several orgs")
    ap.add_argument("--env", default=".env")
    ap.add_argument("--days", type=int, default=365, help="Trailing fetch window in days. The account summary uses the past year (365)")
    ap.add_argument("--lookback-days", type=int, default=90, help="Quote window in days: the ECH month and the active-deployment roster. Default 90 (3 months). Maximum 365. The account summary fetch still uses --days")
    ap.add_argument("--ech-scope", choices=("priced", "all"), default="priced", help="Which billed cost is the ECH side of the comparison. priced (default): only deployments that got a serverless price. all: the whole billing window")
    ap.add_argument("--unmeasured", choices=("exclude", "k360", "fail"), default="exclude", help="An active deployment with no diagnostic bundle. exclude (default): not sized, on neither side. k360: priced from K360 stored GB and labelled. fail: stop the run")
    ap.add_argument("--min-priced-ecu-pct", type=float, default=90.0, help="Integrity fails when deployments with a serverless price carry less than this share of the ECH deployments' billed cost. Default 90")
    ap.add_argument("--keep-serverless-spend", action="store_true", help="With --ech-scope all, leave existing serverless projects' cost in the ECH figure. Off by default")
    ap.add_argument("--reuse", default=None, help="Existing fetch_org.py JSON (skips the K360 fetch)")
    ap.add_argument("--artifacts-root", default=None, help="Parent of <customer>_<date>/ (default serverless_analyzer/diag_artifacts)")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD for the run key (default today UTC)")
    ap.add_argument("--retention-months", type=float, default=3.0, help="Assumed retention for the K360 backup (stored GB ÷ retention). K360 has no raw ingest total")
    ap.add_argument("--security-retention-months", type=float, default=None, help="Override for the Security estimator (default = --retention-months)")
    ap.add_argument("--skip-sizing", action="store_true", help="Never run the ECH>50%% estimator step")
    ap.add_argument("--force-sizing", action="store_true", help="Run sizing even if ECH <= 50%%")
    ap.add_argument("--skip-workload", action="store_true", help="Pass through to fetch_org.py (saves 2 ES calls)")
    ap.add_argument("--xlsx", action="store_true", help="Also write local xlsx copies of the filled estimators")
    ap.add_argument("--skip-xlsx", action="store_true", help="Skip xlsx export (this is the default)")
    ap.add_argument("--always-copy", action="store_true", help="Copy estimators even for empty slices")
    ap.add_argument("--fresh", action="store_true", help="Always copy new estimator sheets instead of reusing same-named copies in the Drive folder (use after changing the template)")
    ap.add_argument("--skip-pdf", action="store_true", help="No PDF export of the canvases")
    ap.add_argument("--skip-pdf-upload", action="store_true", help="Keep PDFs local (no Drive upload)")
    ap.add_argument("--skip-diags", action="store_true", help="Do not pull diagnostics. The quote uses the K360 backup (stored GB ÷ assumed retention)")
    ap.add_argument("--diags-dir", default=None, help="Existing directory of admin-console diagnostic zips. Skips the admin API and parses this folder")
    ap.add_argument("--k360-ingest", action="store_true", help="Also price serverless from K360 stored GB ÷ assumed retention. K360 has no raw ingest total, so this backup can sit far below diagnostic bulk. Used automatically when bundles are unavailable")
    ap.add_argument("--allow-missing-diags", action="store_true", help="Consolidation only: continue when a diagnostic download still fails after its retries. Page 1 names the deployments that were not measured")
    ap.add_argument("--skip-vpn-check", action="store_true", help="Pull diagnostics without checking that the Aviatrix VPN tunnel is up")
    ap.add_argument("--exclude-deployment", action="append", default=None, metavar="ID", help="Leave this deployment out of the whole run (billing, bundles, plan, estimates). Repeat, or pass a comma list. 32-character deployment id")
    ap.add_argument("--keep-monitoring", action="store_true", help="Consolidation only: keep stack-monitoring deployments (names containing 'monitoring'). They are left out by default")
    ap.add_argument("--skip-deployment-plan", action="store_true", help="Skip the Observability 0/25/50/75/100%% deployment-count sheets. The headline quote still runs")
    args = ap.parse_args(argv)
    if not 1 <= int(args.lookback_days) <= 365:
        ap.error("--lookback-days must be from 1 to 365")
    if not 0 <= float(args.min_priced_ecu_pct) <= 100:
        ap.error("--min-priced-ecu-pct must be from 0 to 100")
    if args.exclude_deployment:
        from exclusions import parse_ids
        args.exclude_ids = parse_ids(args.exclude_deployment)
    return args


def load_id_to_name(args, env: dict, org: dict, need: list[str] | None = None, names: list[str] | None = None) -> dict[str, str]:
    """Deployment id → name: diagnostic manifests on disk first, then the admin listing.

    The admin listing is skipped when the manifest already covers every id in `need`, or every
    name in `names`. With neither, the listing is always read.
    """
    from exclusions import manifest_names

    id_to_name: dict[str, str] = {}
    dirs = [Path(args.diags_dir)] if getattr(args, "diags_dir", None) else []
    for d in dirs:
        mf = d / "manifest.json"
        if mf.is_file():
            id_to_name.update(manifest_names(json.loads(mf.read_text())))
    covered = (need is not None and all(i in id_to_name for i in need)) or (
        names is not None and set(names) <= set(id_to_name.values()))
    if not covered:
        meta_org = (org.get("organization") or {})
        org_ids = [str(i) for i in (getattr(args, "org_id", None) or ([meta_org.get("id")] if meta_org.get("id") else [])) if i]
        try:
            sys.path.insert(0, str(HERE.parent.parent / "diag-serverless-consolidation" / "scripts"))
            from admin_diags import admin_base, list_deployments
            key = env.get("FOUND_ADMIN_API_KEY")
            if key:
                base = admin_base(env)
                for oid in org_ids:
                    for dep in list_deployments(base, key, oid):
                        id_to_name[str(dep.get("id")).lower()] = dep.get("name")
        except Exception as ex:  # noqa: BLE001
            print(f"WARN: admin deployment list unavailable ({ex!r})", file=sys.stderr)
    return id_to_name


def resolve_excluded(args, env: dict, org: dict, ids: list[str]) -> list[dict]:
    """Map each excluded deployment id to its name. An id that matches nothing stops the run."""
    from exclusions import resolve_names

    return resolve_names(ids, load_id_to_name(args, env, org, ids))


def auto_exclude_monitoring(args, env: dict, org: dict, acc: dict) -> list[str]:
    """Add stack-monitoring deployments to the exclusion ids. Returns the names found.

    Consolidation only (`args.auto_exclude_monitoring`). Names come from the account's own
    deployment rows, so billing, stored GB and the plan drop them even when no id can be
    resolved; the ids are what keep their diagnostic bundles from being pulled or parsed.
    """
    from exclusions import ids_for_names, monitoring_names

    if not getattr(args, "auto_exclude_monitoring", False):
        return []
    rows = ((acc.get("solutions") or {}).get("by_deployment") or []) + (((acc.get("cloud") or {}).get("by_deployment")) or [])
    names = monitoring_names(d.get("name") for d in rows if d.get("platform") != "serverless")
    if not names:
        return []
    ids, unresolved = ids_for_names(names, load_id_to_name(args, env, org, None, names))
    if unresolved:
        print(f"WARN: no deployment id for monitoring cluster(s) {', '.join(unresolved)}: they are left out of billing and the plan, but a bundle on disk for them is not skipped. Pass --exclude-deployment <id> to be sure.", file=sys.stderr)
    merged = list(getattr(args, "exclude_ids", None) or [])
    merged += [i for i in ids if i not in merged]
    args.exclude_ids = merged
    args.auto_excluded_names = names
    print(f"leaving out monitoring cluster(s): {', '.join(names)} (--keep-monitoring to include)", flush=True)
    return names


def check_reused_org(org: dict, args) -> None:
    """Stop when a reused org report is another customer's. Warn when it is old.

    The name has to be the one asked for, or start with it. With --org-id, at
    least one id has to be in the file. Nothing is fetched under --reuse, so
    this is the only place a wrong file can be caught.
    """
    info = org.get("organization") or {}
    have = str(info.get("name") or "").strip()
    want = str(args.org or "").strip()
    if not have or not (have.casefold() == want.casefold() or have.casefold().startswith(want.casefold())):
        raise SystemExit(
            f"ERROR: --reuse is the org report for {have or 'an unnamed organization'!r}, and --org is {want!r}. "
            "Pass the org report for that customer, or drop --reuse to fetch it."
        )
    asked = {str(i) for i in (getattr(args, "org_id", None) or []) if i}
    ids = {str(i) for i in ([info.get("id")] + list(info.get("ids") or [])) if i}
    if asked and ids and not (asked & ids):
        raise SystemExit(f"ERROR: --reuse covers organization ids {', '.join(sorted(ids))}, and --org-id is {', '.join(sorted(asked))}.")
    if have != want:
        print(f"org → {have}", flush=True)
        args.org = have
    try:
        from analyze import report_day
        age = (datetime.now(timezone.utc).date() - report_day(org)).days
    except Exception:  # noqa: BLE001
        age = 0
    if age > 7:
        print(
            f"WARN: the reused org report was fetched {age} days ago ({(org.get('range') or {}).get('end')}). "
            "Its billing window ends on that day, not today. Drop --reuse for current billing.",
            file=sys.stderr, flush=True,
        )


def prepare_account(args):
    """Fetch the org, fill infra, and write the account folder. Returns acc, infra, folder, env, acc_path."""
    warn_unignored_secrets()

    fetch_py = ORG_SKILL / "scripts" / "fetch_org.py"
    if not fetch_py.is_file():
        raise SystemExit(f"fetch_org.py not found at {fetch_py} — install k360-org-report or set K360_ORG_SKILL_DIR")

    env_path = Path(args.env)
    env = load_env(env_path)

    # 1. fetch (daily chart grain so daily avg/peak are exact)
    fetched_here = None
    if args.reuse:
        org_json = Path(args.reuse)
        if not org_json.is_file():
            raise SystemExit(f"--reuse not found: {org_json}")
    else:
        base, key = es_base_and_key(env)
        canonical = resolve_org_name(base, key, args.org)
        if canonical != args.org:
            print(f"org → {canonical}", flush=True)
        args.org = canonical
        # A file of its own, readable only by this user. A fixed path in /tmp was shared by every
        # run, so two runs could read each other's customer.
        import tempfile
        handle, tmp_name = tempfile.mkstemp(prefix="k360_org_", suffix=".json")
        os.close(handle)
        org_json = fetched_here = Path(tmp_name)
        cmd = [sys.executable, str(fetch_py), "--org", args.org, "--env", str(env_path), "--days", str(args.days), "--chart", "day", "--out", str(org_json)]
        for oid in args.org_id or []:
            cmd.extend(["--org-id", str(oid)])
        if args.skip_workload:
            cmd.append("--skip-workload")
        _run(cmd)
    org = json.loads(org_json.read_text())
    if fetched_here is not None:
        fetched_here.unlink(missing_ok=True)
    if args.reuse:
        check_reused_org(org, args)

    # 2. infra (1 ES call)
    base, key = es_base_and_key(env)
    fill_storage_from_data_pivot(base, key, org)
    fill_storage_monthly(base, key, org)
    fill_ingest_doc_bytes(base, key, org)
    infra = fetch_infra(base, key, org)

    # 3. analyze
    when = date.fromisoformat(args.date) if args.date else datetime.now(timezone.utc).date()
    acc = build_account(
        org, infra, when=when, retention_months=args.retention_months,
        security_retention_months=args.security_retention_months,
        quote_window_days=int(getattr(args, "lookback_days", 90) or 90),
    )
    excluded = []
    auto_names = auto_exclude_monitoring(args, env, org, acc)
    exclude_ids = getattr(args, "exclude_ids", None)
    if exclude_ids or auto_names:
        from exclusions import apply_exclusions, run_key_suffix
        excluded = args.excluded = resolve_excluded(args, env, org, exclude_ids or [])
        # A monitoring cluster with no resolvable id is still removed, by name.
        have = {e["name"] for e in excluded}
        excluded += [{"id": None, "name": n, "reason": "monitoring cluster"} for n in auto_names if n not in have]
        for e in excluded:
            if e["name"] in auto_names:
                e["reason"] = "monitoring cluster"
        apply_exclusions(acc, org, excluded)
        acc["meta"]["run_key"] = acc["meta"]["run_key"] + run_key_suffix(excluded)
    acc["storage"]["size_gaps"] = size_gaps(base, key, acc)
    run_key = acc["meta"]["run_key"]
    root = Path(args.artifacts_root) if args.artifacts_root else artifacts_root()
    folder = root / run_key
    seal_dir(root)
    seal_dir(folder)
    acc["sizing"]["folder"] = str(folder)
    if args.force_sizing:
        acc["sizing"]["triggered"] = True
        acc["platform"]["sizing_triggered"] = True
        acc["sizing"]["forced"] = True
        acc["sizing"]["notes"].append("sizing forced via --force-sizing")
    if args.skip_sizing:
        acc["sizing"]["skipped_reason"] = "--skip-sizing" + (" (would have triggered: ECH > 50%)" if acc["sizing"]["triggered"] else "")
        acc["sizing"]["triggered"] = False
        acc["sizing"]["notes"].append("sizing skipped via --skip-sizing")

    acc_path = folder / f"{run_key}_account.json"
    seal_text(acc_path, json.dumps(acc, indent=2))
    seal_text(folder / f"{run_key}_org_report.json", json.dumps(org, separators=(",", ":")))
    seal_text(folder / f"{run_key}_infra.json", json.dumps(infra, separators=(",", ":")))

    return acc, infra, folder, env, acc_path


def finish_account(acc: dict, folder: Path, acc_path: Path, args) -> None:
    """Account summary report, PDF, and brief."""
    run_key = acc["meta"]["run_key"]
    report_path = folder / f"{run_key}_account_report.md"
    seal_text(report_path, render(acc))
    if not args.skip_pdf:
        try:
            from render_pdf import export

            acc["exports"] = export(acc, folder, only="account")
            for kind, entry in acc["exports"].items():
                if isinstance(entry, dict) and entry.get("pdf"):
                    print(f"{kind} PDF → {entry['pdf']}", flush=True)
                elif isinstance(entry, dict) and entry.get("html"):
                    print(f"{kind} HTML → {entry['html']}", flush=True)
                else:
                    print(f"{kind} → {entry.get('error') if isinstance(entry, dict) else entry}", flush=True)
        except Exception as ex:
            print(f"WARN: PDF export failed ({ex!r})", file=sys.stderr)
            acc["exports"] = {"error": repr(ex)}
        seal_text(acc_path, json.dumps(acc, indent=2))
        py = _sheets_py()
        exports = acc.get("exports") or {}
        if (
            not args.skip_pdf_upload
            and (acc["sizing"].get("drive_folder") or {}).get("id")
            and py.is_file()
            and any((entry or {}).get("pdf") for entry in exports.values() if isinstance(entry, dict))
        ):
            try:
                _run([str(py), str(HERE / "upload_pdfs.py"), "--account-json", str(acc_path)])
                acc = json.loads(acc_path.read_text())
            except subprocess.CalledProcessError as exc:
                print(f"WARN: PDF upload failed ({exc}); PDFs kept local", file=sys.stderr)
    brief = agent_brief(acc)
    brief_path = folder / f"{run_key}_brief.json"
    seal_text(brief_path, json.dumps(brief, separators=(",", ":")))
    est_in = acc["sizing"].get("estimators") if isinstance(acc["sizing"].get("estimators"), dict) else {}
    seal_text(folder / f"{run_key}_summary.json", json.dumps({
        "run_key": run_key,
        "chat_bullets": brief["chat_bullets"],
        "brief": str(brief_path),
        "links": brief["links"],
        "estimators": {k: view for k, value in est_in.items() if (view := _est(value))},
        "savings": brief["savings"],
        "ingest_source": brief.get("ingest_source"),
        "k360_ingest": brief.get("k360_ingest"),
    }, separators=(",", ":")))
    print(f"brief {brief_path}", flush=True)


def main() -> None:
    args = parse_args()
    if wants_quote(args):
        script = diag_script_dir() / "run_consolidation.py"
        if not script.is_file():
            raise SystemExit(f"diagnostic consolidation script not found at {script}")
        os.execv(sys.executable, [sys.executable, str(script), *sys.argv[1:]])
    acc, infra, folder, env, acc_path = prepare_account(args)
    del infra, env
    finish_account(acc, folder, acc_path, args)


if __name__ == "__main__":
    main()
