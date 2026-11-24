"""Search quote from a copy of the Search Serverless Ballpark Pricing Estimator (v2.3).

The default Search path. It copies the Search template the way Observability and
Security are copied, writes the inputs by cell address, and reads the performant
total back from the copy. `--search-notebook` keeps the es3-estimator library
(`es3_estimate.py`, the code behind estimator.ipynb) as the other path. Both
return the same result shape, so everything downstream reads either.

Inputs are on tab `Estimator`, column C. Prices are per profile in columns F
(on-demand), G (performant) and H (high availability), rows 98 to 110.
"""
from __future__ import annotations

from pathlib import Path

from es3_estimate import (
    PROVIDER_BY_NAME, SUPPORT_BY_TIER, ingest_workloads, scale_search_workload, search_bands, workload_share,
)
from sheets_io import _execute, assert_not_template, copy_verified, export_xlsx, push_cells, read_cells, trash_file, write_search_sheet
from sizing_fill import _region_rejected, match_region, region_match_text, run_es3
from sizing_shared import SEARCH_READBACK, SEARCH_TAB, sheet_money
from constants import HOURS_PER_MONTH, MODERATE_MAX_MS, SIMPLE_MAX_MS

QUOTED_PROFILE = "performant"
PROFILE_COLUMN = {"on-demand": "F", "performant": "G", "high availability": "H"}
# The sheet's Query Complexity list (Reference!P26:Q28). Milliseconds are what its QTT lookup applies.
SHEET_COMPLEXITY = {"simple": 100.0, "moderate": 500.0, "complex": 5000.0}
LIBRARY_COMPLEXITY = {"low": "simple", "medium": "moderate", "high": "complex"}
SHEET_PROVIDER = {"AWS": "AWS", "AZURE": "Azure", "GCP": "GCP"}
REGION_TABLE = "'Reference'!A4:B60"


def search_region_options(sheets, sid: str) -> list[str]:
    """'AWS: us-east-1 (N. Virginia)' for every priced region, from the copy's own price table."""
    assert_not_template(sid)
    res = _execute(lambda: sheets.spreadsheets().values().get(spreadsheetId=sid, range=REGION_TABLE))
    options: list[str] = []
    for row in res.get("values", []):
        if len(row) >= 2 and str(row[0]).strip() and str(row[1]).strip():
            option = f"{str(row[0]).strip()}: {str(row[1]).strip()}"
            if option not in options:
                options.append(option)
    return options


def sheet_complexity(band: dict, label: str, assumptions: list[str]) -> str:
    """The Documentation tab's rule: QTT up to 250 ms is simple, 250 to 500 moderate, over 500 complex."""
    ms = band.get("complexity_ms")
    if ms is not None and float(ms) > 0:
        ms = float(ms)
        pick = "simple" if ms <= SIMPLE_MAX_MS else "moderate" if ms <= MODERATE_MAX_MS else "complex"
        assumptions.append(f"{label} query time {ms:g} ms is '{pick}' by the sheet's rule (simple up to 250 ms, moderate 250 to 500, complex over 500).")
        return pick
    named = str(band.get("complexity") or "").strip().lower()
    if named in SHEET_COMPLEXITY:
        return named
    if named in LIBRARY_COMPLEXITY:
        return LIBRARY_COMPLEXITY[named]
    assumptions.append(f"{label} query complexity was not measured. The sheet was left at 'simple'.")
    return "simple"


def search_input_cells(payload: dict, region_text: str, provider: str, support: str, assumptions: list[str]) -> dict:
    """Every input the sheet reads. Zeros are written, so no template default survives into a quote."""
    stored = max(float(payload.get("stored_gb") or 0), 0.0)
    org_gb = max(float(payload.get("org_stored_gb") or 0), 0.0)
    share = workload_share(stored, org_gb)
    workload = scale_search_workload(payload.get("workload") or {}, share)
    if org_gb and stored and share < 1:
        assumptions.append(f"Docs/s and QPS scaled by this project's share of diagnostic org store ({share:.3f}).")
    search = (workload or {}).get("search") or {}
    # Always asked, so a workload with no measured search band says so in `assumptions` ("Search $ is the idle
    # storage floor only") instead of writing zero QPS with nothing recorded. That row has the name None.
    sized = search_bands(workload, assumptions)
    bands: dict[str, dict] = {}
    # Each row carries its band name: a band with no hour share is appended last, so position is not identity.
    for name, row in sized:
        if name is None:
            continue
        bands[name] = {
            "qps": float(row["QPS"]),
            "share": round(min(max(float(row["active_hours_per_month"]) / HOURS_PER_MONTH, 0.0), 1.0), 6),
            "complexity": sheet_complexity(search[name], "Peak" if name == "peak" else "Non-peak", assumptions),
        }
    ingest = ingest_workloads(workload, assumptions)[0]
    peak = bands.get("peak") or {"qps": 0.0, "share": 0.0, "complexity": "simple"}
    nonpeak = bands.get("nonpeak") or {"qps": 0.0, "share": 0.0, "complexity": "simple"}
    return {
        "C5": provider,
        "C6": region_text,
        "C7": support,
        "C8": "text",
        "C12": round(stored, 4),
        "C19": peak["qps"], "C20": peak["complexity"], "C21": peak["share"], "C22": 0,
        "C24": nonpeak["qps"], "C25": nonpeak["complexity"], "C26": nonpeak["share"], "C27": 0,
        "C30": 0,
        "C33": float(ingest["docs_per_sec"]),
        "C34": float(ingest["avg_doc_size_bytes"]),
        "C35": float(ingest["active_hours_per_month"]),
        "C37": 0,
        "C40": 0, "C41": 0, "C42": 0, "C43": 0, "C44": 0, "C45": 0,
        "C48": "No",
    }


def _num(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _round(value, places: int = 2):
    return None if value is None else round(float(value), places)


def profile_rows(values: dict) -> list[dict]:
    """One row per search power profile, with the keys the es3 library's rows carry."""
    rows = []
    for name, col in PROFILE_COLUMN.items():
        total = _num(values.get(f"{col}110"))
        rows.append({
            "Search Power Setting": name,
            "Total $/month": _round(total),
            "Search $/month": _round(_num(values.get(f"{col}103"))),
            "Ingest $/month": _round(_num(values.get(f"{col}104"))),
            "Storage $/month": _round(_num(values.get(f"{col}105"))),
            "Data out $/month": _round(_num(values.get(f"{col}106"))),
            "CPS $/month": _round(_num(values.get(f"{col}107"))),
            "Support $/month": _round(_num(values.get(f"{col}109"))),
            "Provisioned Search VCU/h": _num(values.get(f"{col}89")),
            "Idle Search VCU/h": _num(values.get(f"{col}96")),
            "Total Search VCU-hrs/mo": _num(values.get(f"{col}98")),
            "Ingest VCU": _num(values.get("F62")),
            "Ingest VCU-hrs/mo": _num(values.get("F65")),
            "Storage (GB)": _num(values.get("F55")),
        })
    return rows


def readback_from(profiles: list[dict]) -> dict | None:
    """The performant total or nothing. No other profile stands in for it."""
    chosen = next((row for row in profiles if row.get("Search Power Setting") == QUOTED_PROFILE), None)
    monthly = None if not chosen else sheet_money(chosen.get("Total $/month"))
    if monthly is None:
        return None
    # Round once and derive the year from the rounded month, the same as the notebook path. Year agreement
    # holds the steady annual to 12 x the monthly to the cent.
    monthly = round(monthly, 2)
    return {
        "profile": QUOTED_PROFILE,
        "monthly_steady_state": monthly,
        "annual_year1": round(monthly * 12, 2),
        "annual_steady_state": round(monthly * 12, 2),
        "search_monthly": chosen.get("Search $/month"),
        "ingest_monthly": chosen.get("Ingest $/month"),
        "storage_monthly": chosen.get("Storage $/month"),
        "support_monthly": chosen.get("Support $/month"),
        "provisioned_search_vcu": chosen.get("Provisioned Search VCU/h"),
        "idle_search_vcu": chosen.get("Idle Search VCU/h"),
        "minimum_search_vcu": 0.0,
        "search_vcu_hours": chosen.get("Total Search VCU-hrs/mo"),
        "ingest_vcu": chosen.get("Ingest VCU"),
        "ingest_vcu_hours": chosen.get("Ingest VCU-hrs/mo"),
        "storage_gb": chosen.get("Storage (GB)"),
    }


def fill_search(sheets, sid: str, payload: dict, region_options_: list[str]) -> dict:
    """Write one Search copy and read it back. Returns the es3-shaped result."""
    assumptions = [
        "Workload type is text. Vector and time-series profiles are not inferred from K360.",
        "QPS and query time are day averages from index telemetry, not peak-second load.",
        "Performant is the quoted profile. On-demand and high availability are shown beside it.",
        "Priced on the Search Serverless Ballpark Pricing Estimator sheet. Minimum VCU, data out, cross-project search and fast refresh are all off.",
    ]
    raw_provider = str(payload.get("provider") or "").strip()
    provider = SHEET_PROVIDER.get(str(PROVIDER_BY_NAME.get(raw_provider.lower(), raw_provider)).upper())
    if provider is None:
        return {"status": "error", "error": f"region {payload.get('region')} is not on the estimator list"}
    options = [opt for opt in region_options_ if opt.upper().startswith(provider.upper() + ":")]
    option, how = match_region(provider, payload.get("region"), options)
    if how == "none" or not option:
        return {"status": "error", "error": _region_rejected(payload.get("region"))}
    region_text = option.split(":", 1)[1].strip()
    if how == "provider_default":
        assumptions.append(f"Region {payload.get('region')!r} is not on the estimator list. It is priced as {region_text}.")
    if how == "nearest":
        assumptions.append(f"Region {payload.get('region')!r} is not on the estimator list. Nearest same-provider option {region_text} was written.")
    tier = str(payload.get("support_tier") or "Premium").strip().lower()
    support = SUPPORT_BY_TIER.get(tier)
    if support is None:
        support = "Enterprise"
        assumptions.append(f"Support tier {payload.get('support_tier')!r} mapped to Enterprise (15%).")
    try:
        cells = search_input_cells(payload, region_text, provider, support, assumptions)
    except ValueError as exc:
        return {"status": "error", "error": str(exc)}
    written = push_cells(sheets, sid, SEARCH_TAB, cells, allowed_text={region_text, provider, support, *SHEET_COMPLEXITY, "text"})
    values = read_cells(sheets, sid, SEARCH_TAB, SEARCH_READBACK)
    if str(values.get("region_valid") or "").strip().lower() != "yes":
        return {"status": "error", "error": _region_rejected(payload.get("region")), "cells": cells}
    profiles = profile_rows(values)
    rb = readback_from(profiles)
    return {
        "status": "estimated" if rb else f"estimated — {QUOTED_PROFILE} total was not numeric",
        "source": "Search Serverless Ballpark Pricing Estimator (Google Sheet copy)",
        "region_match": region_match_text(option, how, payload.get("region")),
        "support_level": support,
        "assumptions": assumptions,
        "inputs": {
            "provider": provider, "region": region_text, "support_level": support,
            "stored_gb": cells["C12"], "cells": cells,
        },
        "cells": cells,
        "cells_written": written,
        "profiles": profiles,
        "readback": rb,
    }


def price_search(drive, sheets, folder_id: str, estimate_dir: Path, title: str, payload: dict, *,
                 notebook: bool, template_id: str, reuse: bool, skip_xlsx: bool) -> tuple[dict, dict]:
    """Price one Search project. Returns (result, published).

    `notebook` is the es3-estimator library path: its CSV report becomes a Google
    Sheet. Otherwise a copy of the Search template is filled. Both results carry
    status, readback, profiles and region_match; `published` carries local, url
    and spreadsheet_id, or an error.
    """
    if notebook:
        result = run_es3(payload)
        published: dict = {}
        if result.get("report_csv"):
            try:
                published = write_search_sheet(drive, estimate_dir, folder_id, title, result)
            except Exception as ex:  # noqa: BLE001
                published = {"error": str(ex)}
        return result, published
    copied, reused, layout = copy_verified(drive, sheets, template_id, title, folder_id, reuse=reuse, kind="search", tab=SEARCH_TAB)
    try:
        result = fill_search(sheets, copied["id"], payload, search_region_options(sheets, copied["id"]))
    except Exception:
        if not reused:
            try:
                trash_file(drive, copied["id"])
            except Exception:  # noqa: BLE001
                pass
        raise
    if result.get("status") == "error":
        if not reused:
            try:
                trash_file(drive, copied["id"])
            except Exception:  # noqa: BLE001
                pass
        return result, {}
    published = {"spreadsheet_id": copied["id"], "url": copied["url"], "reused_existing_copy": reused, "template_version": layout["version"]}
    if not skip_xlsx:
        try:
            published["local"] = str(export_xlsx(drive, copied["id"], estimate_dir / f"{title}.xlsx"))
        except Exception as ex:  # noqa: BLE001
            published["local_xlsx_error"] = str(ex)
    return result, published
