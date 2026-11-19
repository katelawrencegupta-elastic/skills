"""Search EIS and ML-node ECU reads for the consolidation quote.

One completeness gate (`_ai_ml_window`) serves both. The EIS line is a Search
price; the ML-node figure the ECH comparison takes out is not, and must not
depend on whether Search happened to return a read-back.
"""
from __future__ import annotations

from constants import ECHO_TOLERANCE_GB_DAY, ECHO_TOLERANCE_SHARE
from consolidation_scope import DAYS_PER_MONTH, billed_series


def _ai_ml_window(acc: dict, names: set[str]) -> dict:
    """Quote-window AI/ML billing for `names`, only from complete, reconciled daily data.

    The composite total query is the authoritative source for fetched-period
    totals. Missing data is never read as a zero-cost deployment, and a daily
    series is used only when it covers the exact window and reconciles to those
    totals. `by_name` carries each deployment's window sums; `scale` turns a
    window total into a month.
    """
    ai_ml = acc.get("ai_ml") or {}
    source_totals = ai_ml.get("billing_by_deployment") or {}
    daily_usage = ai_ml.get("daily_usage_by_deployment") or {}
    e = acc.get("ecu") or {}
    quote_days = int(e.get("quote_lookback_days") or 0)
    daily_labels, _daily_totals = billed_series(e)
    window_labels = daily_labels[-quote_days:] if quote_days and daily_labels else []

    def short(reason: str) -> dict:
        return {"status": "incomplete", "reason": reason, "window_labels": [], "by_name": {}, "scale": None}

    if not names:
        return {"status": "not_applicable", "reason": "no deployments to read", "window_labels": [], "by_name": {}, "scale": None}
    if not bool(ai_ml.get("billing_by_deployment_complete")):
        return short(
            (ai_ml.get("billing_by_deployment_status") or {}).get("reason")
            or "complete filtered deployment totals are unavailable"
        )

    def total_for(name: str) -> dict | None:
        raw = source_totals.get(name)
        if raw is None:
            # A completed composite has no bucket for a deployment with no
            # billing documents in the fetched interval, which means zero.
            return {"eis": 0.0, "tokens": 0.0, "ml": 0.0}
        try:
            return {
                "eis": float(raw["eis_ecu"]),
                "tokens": float(raw["million_tokens"]),
                "ml": float(raw["ml_node_ecu"]),
            }
        except (KeyError, TypeError, ValueError):
            return None

    totals = {name: total_for(name) for name in names}
    if any(value is None for value in totals.values()):
        return short("a deployment total is missing a required filtered billing metric")
    if not (bool(ai_ml.get("daily_usage_complete")) and window_labels):
        return short(
            "complete daily EIS and ML-node telemetry for the exact quote window is unavailable "
            "or does not reconcile to fetched-period totals"
        )
    # A completed composite with a zero total and no daily row is a deployment with no AI/ML
    # billing at all, not missing telemetry. Reading it as zero is the same convention
    # `total_for` already uses for an absent bucket, and it is what lets this gate run over
    # every deployment in the comparison rather than only the ones with inference activity.
    silent = {
        name for name in names
        if not (daily_usage.get(name) or {}).get("days")
        and not any(abs(totals[name][key]) > 0 for key in ("eis", "tokens", "ml"))
    }
    for name in names - silent:
        data = daily_usage.get(name) or {}
        days = list(data.get("days") or [])
        fields = ("eis_ecu", "million_tokens", "ml_node_ecu", "models")
        if any(len(data.get(field) or []) != len(days) for field in fields) or any(day not in days for day in window_labels):
            return short(
                "complete daily EIS and ML-node telemetry for the exact quote window is unavailable "
                "or does not reconcile to fetched-period totals"
            )
        for field, total_key in (("eis_ecu", "eis"), ("million_tokens", "tokens"), ("ml_node_ecu", "ml")):
            daily_sum = sum(float(value or 0) for value in (data.get(field) or []))
            tolerance = max(ECHO_TOLERANCE_GB_DAY, abs(totals[name][total_key]) * ECHO_TOLERANCE_SHARE)
            if abs(daily_sum - totals[name][total_key]) > tolerance:
                return short(
                    "complete daily EIS and ML-node telemetry for the exact quote window is unavailable "
                    "or does not reconcile to fetched-period totals"
                )

    by_name: dict[str, dict] = {}
    for name in silent:
        by_name[name] = {"eis": 0.0, "tokens": 0.0, "ml": 0.0, "models": []}
    for name in names - silent:
        data = daily_usage.get(name) or {}
        indices = {day: index for index, day in enumerate(data.get("days") or [])}

        def sum_window(field: str, _data=data, _indices=indices) -> float:
            values = _data.get(field) or []
            return sum(float(values[_indices[day]] or 0) for day in window_labels if day in _indices)

        models_by_day = data.get("models") or []
        by_name[name] = {
            "eis": sum_window("eis_ecu"),
            "tokens": sum_window("million_tokens"),
            "ml": sum_window("ml_node_ecu"),
            "models": sorted({
                str(model) for day in window_labels if day in indices
                for model in (models_by_day[indices[day]] or []) if model
            }),
        }
    return {
        "status": "complete",
        "reason": None,
        "window_labels": window_labels,
        "by_name": by_name,
        "scale": DAYS_PER_MONTH / len(window_labels),
    }


def ml_node_ecu(acc: dict, names) -> dict:
    """Quote-window ML-node ECU as a month, over the deployments in the ECH comparison.

    No serverless project bills an ML node, so that capacity comes out of the ECH
    side of the comparison. That is true of an Observability or Security fleet as
    much as a Search one, so this read does not depend on a Search read-back.
    Incomplete or unreconciled telemetry excludes nothing: an unverified deduction
    would overstate the saving.
    """
    names = {str(name) for name in (names or set()) if name}
    if not names:
        return {"status": "not_applicable", "reason": "no deployments are in the ECH comparison", "monthly": 0.0, "deployments": 0}
    window = _ai_ml_window(acc, names)
    if window["status"] != "complete":
        return {"status": window["status"], "reason": window.get("reason"), "monthly": 0.0, "deployments": 0}
    scale = window["scale"]
    found = {name: row["ml"] for name, row in window["by_name"].items() if row["ml"] > 0}
    return {
        "status": "complete",
        "reason": None,
        "monthly": round(sum(found.values()) * scale, 2),
        "window_ecu": round(sum(found.values()), 2),
        "deployments": len(found),
        "usage_days": len(window["window_labels"]),
        "by_deployment_mo": {
            name: round(value * scale, 2)
            for name, value in sorted(found.items(), key=lambda item: -item[1])
        },
    }


def _search_eis(
    acc: dict,
    months: float,
    *,
    usage_factor: float = 1.0,
    eligible_names: set[str] | None = None,
) -> dict:
    """Price EIS only from complete source totals and reconciled daily data."""
    del months, usage_factor
    region = {
        d.get("name"): d.get("region")
        for d in ((acc.get("cloud") or {}).get("by_deployment") or [])
        if d.get("region")
    }
    eligible_names = eligible_names or set()
    if not eligible_names:
        return {"status": "not_applicable", "reason": "no priced Search deployments are eligible for EIS"}
    window = _ai_ml_window(acc, eligible_names)
    if window["status"] != "complete":
        return {"status": "incomplete", "reason": window.get("reason")}

    ai_ml = acc.get("ai_ml") or {}
    deployment_rows = {d.get("name"): d for d in ai_ml.get("by_deployment") or [] if d.get("name")}
    window_labels = window["window_labels"]
    scale = window["scale"]
    rows = []
    for name in eligible_names:
        d = deployment_rows.get(name) or {}
        measured = window["by_name"][name]
        models = measured["models"]
        labels = dict(zip(d.get("models") or [], d.get("model_labels") or []))
        row = {
            "name": name, "region": region.get(name) or "unknown region",
            "ml": measured["ml"],
            "eis": measured["eis"], "million_tokens": measured["tokens"],
            "models": models, "model_labels": [labels.get(model, model) for model in models],
            "elser": any("elser" in model.lower() for model in models),
        }
        if row["ml"] <= 0 and row["eis"] <= 0 and not row["elser"] and not row["models"]:
            continue
        rows.append(row)
    exact = True
    if not rows:
        return {
            "status": "complete",
            "usage_basis": "quote_window_daily",
            "usage_days": len(window_labels) if exact else None,
            "monthly": 0.0,
            "by_region": {},
            "ml_node_ecu": 0.0,
            "ml_node_ecu_mo": 0.0,
            "models": [],
            "deployments": 0,
            "workloads": [],
        }
    by_region: dict[str, float] = {}
    for r in rows:
        by_region[r["region"]] = by_region.get(r["region"], 0.0) + r["eis"] * scale
    models = sorted({m for r in rows for m in r["models"] if m})
    if any(r["elser"] for r in rows) and "ELSER" not in models:
        models.append("ELSER")
    workloads = []
    for r in rows:
        if r["eis"] <= 0 and r["million_tokens"] <= 0:
            continue
        labels = r["model_labels"] or r["models"]
        workloads.append({
            "model": ", ".join(str(label) for label in labels) or r["name"],
            "kind": f"Inference tokens · {r['region']}",
            "deployment": r["name"],
            "million_tokens": round(r["million_tokens"], 4),
            "ecu": round(r["eis"], 2),
            "monthly": round(r["eis"] * scale, 2),
        })
    return {
        "status": "complete",
        "monthly": round(sum(by_region.values()), 2),
        "by_region": {k: round(v, 2) for k, v in by_region.items()},
        "ml_node_ecu_mo": round(sum(r["ml"] for r in rows) * scale, 2),
        "ml_node_ecu": round(sum(r["ml"] for r in rows), 2),
        "models": models,
        "deployments": len(rows),
        "workloads": workloads,
        "usage_basis": "quote_window_daily",
        "usage_days": len(window_labels) if exact else None,
    }


def _apply_search_eis(
    acc: dict,
    by_solution: dict,
    months: float,
    notes: list[str],
    *,
    usage_factor: float = 1.0,
    eligible_names: set[str] | None = None,
) -> None:
    eis = _search_eis(acc, months, usage_factor=usage_factor, eligible_names=eligible_names)
    blk = by_solution.get("search") or {}
    if "quotable" not in str(blk.get("pricing") or ""):
        blk["eis_data_status"] = {"status": "not_applicable", "reason": "Search base estimate is not quotable"}
        return
    blk["eis_data_status"] = {key: value for key, value in eis.items() if key in ("status", "reason", "usage_basis", "usage_days")}
    if eis.get("status") == "incomplete":
        notes.append(
            "Search EIS was not added because complete filtered deployment billing totals could not be verified. "
            "The Search estimate stays the read-back from the notebook or spreadsheet."
        )
        return
    if eis.get("status") == "not_applicable":
        return
    if eis.get("monthly") is None:
        return
    base = sum(float(blk.get(k) or 0) for k in ("search_mo", "ingest_mo", "retention_mo"))
    support = float(blk.get("support_mo") or 0)
    rate = (support / base) if base else 0.0
    usage = float(eis["monthly"] or 0)
    blk["eis_mo"] = usage
    blk["eis_models"] = eis["models"]
    blk["ml_node_ecu"] = eis.get("ml_node_ecu")
    blk["ml_node_ecu_mo"] = eis.get("ml_node_ecu_mo")
    blk["eis_workloads"] = eis["workloads"]
    blk["eis_deployments"] = eis["deployments"]
    blk["eis_usage_basis"] = eis["usage_basis"]
    blk["eis_usage_days"] = eis.get("usage_days")
    extra_support = round(usage * rate, 2)
    blk["eis_with_support_mo"] = round(usage * (1 + rate), 2)
    if blk.get("by_region"):
        for r in blk["by_region"]:
            part = float(eis["by_region"].get(r["region"]) or 0)
            r["eis_mo"] = round(part, 2)
            if r.get("monthly") is not None:
                r["monthly"] = round(float(r["monthly"]) + part * (1 + rate), 2)
        if blk.get("regional_mode") == "store_split":
            blk["total_mo"] = round(float(blk.get("total_mo") or 0) + usage * (1 + rate), 2)
        else:
            blk["total_mo"] = round(sum(float(r.get("monthly") or 0) for r in blk["by_region"]), 2)
    else:
        blk["total_mo"] = round(float(blk.get("total_mo") or 0) + usage * (1 + rate), 2)
    blk["total_yr"] = round(float(blk["total_mo"]) * 12, 2)
    blk["support_mo"] = round(support + extra_support, 2)
    for row in blk.get("regional_profiles") or []:
        if isinstance(row.get("Total $/month"), (int, float)):
            row["Total $/month"] = round(float(row["Total $/month"]) + usage * (1 + rate), 2)
        if isinstance(row.get("Support $/month"), (int, float)):
            row["Support $/month"] = round(float(row["Support $/month"]) + extra_support, 2)
    if eis["usage_basis"] == "quote_window_daily":
        pace_text = f"EIS inference billing was summed over the {eis['usage_days']}-day quote window"
    else:
        pace_text = "Fetched-period EIS inference billing was scaled by the account quote-usage factor"
    notes.append(
        f"The Search EIS line includes activity found on {eis['deployments']} sized ECH Search deployment(s). "
        f"{pace_text}. EIS is ${usage:,.2f}/mo before support, based on billed inference ECU and tokens. "
        f"ML-node ECU on those deployments (${eis['ml_node_ecu_mo']:,.2f}/mo) is not in this EIS line. "
        "ML capacity is carried to the serverless total separately, across every deployment in the comparison."
    )



