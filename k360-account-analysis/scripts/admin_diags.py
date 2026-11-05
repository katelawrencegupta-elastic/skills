#!/usr/bin/env python3
"""Pull Elasticsearch diagnostic bundles from the Elastic Cloud admin console and read them.

Started deployments only. Stopped deployments are recorded and skipped. A zip that is
already in the output directory is kept. Each bundle is a full capture:

  POST /api/v1/deployments/{id}/elasticsearch/{ref_id}/diagnostics/_capture?mode=full

Auth is FOUND_ADMIN_API_KEY. FOUND_ADMIN_URL defaults to https://admin.found.no.

  python admin_diags.py --org-id 2513111014 --out artifacts/2513111014_diags_2026-11-04 --env .env
  python admin_diags.py --parse-only --out artifacts/2513111014_diags_2026-11-04

`sizing_from_diags.json` is one row per bundle: bulk GB/day from
`index.primaries.bulk.est_bytes_per_day`, retention, and primary store size.
"""
from __future__ import annotations

import argparse
import json
import re
import time
import urllib.error
import urllib.request
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path


def _load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip("\"'")
    return env


def admin_base(env: dict[str, str]) -> str:
    raw = (env.get("FOUND_ADMIN_URL") or "https://admin.found.no").rstrip("/")
    if raw.startswith("http://"):
        raw = "https://" + raw[len("http://"):]
    return raw


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "deployment"


def _is_zip(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 22:
        return False
    with path.open("rb") as fh:
        return fh.read(2) == b"PK"


def _api(base: str, key: str, method: str, path: str, body: dict | None = None, timeout: int = 60) -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"ApiKey {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def list_deployments(base: str, key: str, org_id: str) -> list[dict]:
    """Every deployment in the organization. Pages until match_count is covered."""
    found: list[dict] = []
    offset = 0
    match_count = None
    while True:
        body = {
            "size": 100,
            "from": offset,
            "query": {"term": {"metadata.organization_id": {"value": str(org_id)}}},
        }
        page = _api(base, key, "POST", "/api/v1/deployments/_search", body, timeout=90)
        batch = page.get("deployments") or []
        if match_count is None:
            match_count = int(page.get("match_count") or len(batch))
        found.extend(batch)
        offset += len(batch)
        if not batch or offset >= match_count:
            break
    return found


def _capture_one(base: str, key: str, row: dict, dest: Path) -> dict:
    url = f"{base}/api/v1/deployments/{row['id']}/elasticsearch/{row['ref_id']}/diagnostics/_capture?mode=full"
    req = urllib.request.Request(
        url,
        data=b"",
        method="POST",
        headers={
            "Authorization": f"ApiKey {key}",
            "Content-Type": "application/json",
            "Accept": "application/zip",
        },
    )
    with urllib.request.urlopen(req, timeout=300) as resp, dest.open("wb") as fh:
        n = 0
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            fh.write(chunk)
            n += len(chunk)
    if not _is_zip(dest):
        dest.unlink(missing_ok=True)
        raise RuntimeError("response was not a zip")
    return {**row, "result": "ok", "bytes": n, "file": str(dest)}


def _capture_with_retry(base: str, key: str, row: dict, dest: Path) -> dict:
    last = "unknown error"
    for attempt in range(4):
        try:
            return _capture_one(base, key, row, dest)
        except urllib.error.HTTPError as exc:
            body = exc.read(300).decode("utf-8", "replace")
            try:
                msg = json.loads(body)["errors"][0]["message"]
            except Exception:
                msg = body[:180]
            last = f"HTTP {exc.code}: {msg}"
            dest.unlink(missing_ok=True)
            if exc.code == 429 and attempt < 3:
                time.sleep(20 * (attempt + 1))
                continue
            break
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
            dest.unlink(missing_ok=True)
            if attempt < 3:
                time.sleep(10)
                continue
            break
    return {**row, "result": "error", "error": last}


def pull_diagnostics(env: dict[str, str], org_id: str, out_dir: Path, *, workers: int = 2) -> list[dict]:
    """Download one full Elasticsearch bundle per started deployment. Existing zips are reused."""
    key = env.get("FOUND_ADMIN_API_KEY")
    if not key:
        raise RuntimeError("FOUND_ADMIN_API_KEY is not set")
    base = admin_base(env)
    out_dir.mkdir(parents=True, exist_ok=True)
    listing = list_deployments(base, key, org_id)
    targets: list[dict] = []
    skipped: list[dict] = []
    for dep in listing:
        resources = (dep.get("resources") or {}).get("elasticsearch") or []
        if not resources:
            continue
        es = resources[0]
        info = es.get("info") or {}
        row = {
            "id": dep.get("id"),
            "name": dep.get("name"),
            "status": info.get("status"),
            "hidden": bool((dep.get("metadata") or {}).get("hidden")),
            "ref_id": es.get("ref_id") or "main-elasticsearch",
        }
        if row["status"] == "started" and row["id"]:
            targets.append(row)
        else:
            skipped.append({**row, "result": "skipped"})

    reused: list[dict] = []
    pending: list[tuple[dict, Path]] = []
    for row in targets:
        dest = out_dir / f"{_safe(row['name'])}__{row['id']}.zip"
        if _is_zip(dest):
            reused.append({**row, "result": "ok", "bytes": dest.stat().st_size, "file": str(dest), "reused": True})
        else:
            pending.append((row, dest))

    fresh: list[dict] = []
    if pending:
        print(f"diagnostics: capturing {len(pending)} started deployments ({len(reused)} already on disk)", flush=True)
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futs = [pool.submit(_capture_with_retry, base, key, row, dest) for row, dest in pending]
            for fut in as_completed(futs):
                row = fut.result()
                fresh.append(row)
                if row["result"] == "ok":
                    print(f"ok {row['bytes']:8d} {row['name']}", flush=True)
                else:
                    print(f"ERR {row['name']} {row.get('error', '')[:160]}", flush=True)
    manifest = sorted(reused + fresh, key=lambda r: (r.get("name") or "").lower()) + skipped
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    ok = [r for r in manifest if r.get("result") == "ok"]
    bad = [r for r in manifest if r.get("result") == "error"]
    print(f"diagnostics: ok={len(ok)} error={len(bad)} skipped={len(skipped)} dir={out_dir}", flush=True)
    return manifest


def _parse_age(value) -> float | None:
    if value is None:
        return None
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)(ms|s|m|h|d)\s*", str(value))
    if not match:
        return None
    n = float(match.group(1))
    return n * {"ms": 1 / 86_400_000, "s": 1 / 86_400, "m": 1 / 1_440, "h": 1 / 24, "d": 1}[match.group(2)]


def _user_index(name: str) -> bool:
    if name.startswith(".ds-"):
        rest = name[4:]
        return not (rest.startswith(".") or rest.startswith("ilm"))
    if name.startswith("partial-"):
        return True
    return not name.startswith(".")


def _zip_json(archive: zipfile.ZipFile, suffix: str):
    name = next((n for n in archive.namelist() if n.endswith(suffix) and not n.endswith("/")), None)
    if not name:
        return None
    return json.loads(archive.read(name))


def _retention_label(parts: list[dict], basis: str | None) -> str | None:
    if not parts:
        return None
    if basis == "observed":
        days = parts[0]["days"]
        if days < 1:
            return "<1 day"
        return f"{round(days):,} days observed"
    if parts[0]["pct"] >= 75:
        n = round(parts[0]["days"])
        return "1 day" if n == 1 else f"{n:,} days"
    return " · ".join(f"{round(p['days'])}d ({p['pct']:.0f}%)" for p in parts[:3])


def _est_bytes_per_day(bulk_bytes: int, creation: int | None, lifecycle: int | None, action: str | None, collection: int) -> int:
    """index.primaries.bulk.est_bytes_per_day from the esdiag indices_stats processor.

    Bytes per day are the primary bulk request size over the index write phase.
    The write phase is creation until rollover, or the index age while ILM's
    current action is rollover. A missing write phase is 0.
    """
    if not bulk_bytes or not creation or creation <= 0 or collection < creation or not lifecycle or lifecycle > collection:
        return 0
    age = collection - int(creation)
    since = collection - int(lifecycle)
    if age > since:
        seconds = (age - since) // 1000
    elif action == "rollover":
        seconds = age // 1000
    else:
        seconds = 0
    if seconds <= 0:
        return 0
    return int(int(bulk_bytes) * (86400.0 / seconds))


def _write_indices(streams: list, alias: dict) -> set[str]:
    """Data-stream write index (last generation) plus any alias marked is_write_index."""
    write: set[str] = set()
    for ds in streams or []:
        idxs = ds.get("indices") or []
        if idxs:
            name = (idxs[-1] or {}).get("index_name") or ""
            if name:
                write.add(name)
    for name, body in (alias or {}).items():
        aliases = (body or {}).get("aliases") or {}
        if any((st or {}).get("is_write_index") for st in aliases.values()):
            write.add(name)
    return write


def analyze_bundle(path: Path, *, captured: datetime) -> dict:
    """Bulk GB/day, retention, and primary store size from one diagnostic zip."""
    with zipfile.ZipFile(path) as archive:
        health = _zip_json(archive, "/cluster_health.json") or {}
        nodes = (_zip_json(archive, "/nodes_stats.json") or {}).get("nodes") or {}
        policies = _zip_json(archive, "/commercial/ilm_policies.json") or {}
        explain = (_zip_json(archive, "/commercial/ilm_explain.json") or {}).get("indices") or {}
        streams = (_zip_json(archive, "/commercial/data_stream.json") or {}).get("data_streams") or []
        stats = (_zip_json(archive, "/indices_stats.json") or {}).get("indices") or {}
        state = _zip_json(archive, "/cluster_state.json") or {}
        alias = _zip_json(archive, "/alias.json") or {}

    hot_up: list[float] = []
    collection = 0
    for node in nodes.values():
        bulk = ((node.get("indices") or {}).get("bulk") or {})
        up = (node.get("jvm") or {}).get("uptime_in_millis") or 0
        size = bulk.get("total_size_in_bytes") or 0
        if up > 0 and size > 0:
            hot_up.append(up / 86_400_000)
        ts = node.get("timestamp") or 0
        if ts > collection:
            collection = ts
    est_bytes = 0
    for name in _write_indices(streams, alias):
        body = stats.get(name) or {}
        bulk_bytes = (((body.get("primaries") or {}).get("bulk") or {}).get("total_size_in_bytes") or 0)
        info = explain.get(name) or {}
        est_bytes += _est_bytes_per_day(
            int(bulk_bytes),
            info.get("index_creation_date_millis"),
            info.get("lifecycle_date_millis"),
            info.get("action"),
            collection,
        )
    gb_day = est_bytes / 1e9

    delete_age: dict[str, float] = {}
    for name, pol in (policies.items() if isinstance(policies, dict) else []):
        phases = ((pol.get("policy") or pol).get("phases") or {})
        age = _parse_age((phases.get("delete") or {}).get("min_age"))
        if age and age > 0:
            delete_age[name] = age
    ds_age: dict[str, float] = {}
    for ds in streams:
        life = ds.get("lifecycle") or {}
        age = _parse_age(life.get("data_retention")) if life.get("enabled", True) else None
        if not age:
            continue
        for ix in ds.get("indices") or []:
            ds_age[ix.get("index_name") or ""] = age

    created: dict[str, int] = {}
    for name, meta in ((state.get("metadata") or {}).get("indices") or {}).items():
        raw = ((meta.get("settings") or {}).get("index") or {}).get("creation_date")
        if raw:
            try:
                created[name] = int(raw)
            except (TypeError, ValueError):
                pass

    by_age: dict[float, float] = defaultdict(float)
    unknown = 0.0
    total = 0.0
    observed: list[float] = []
    captured_ms = captured.timestamp() * 1000
    for ix, body in stats.items():
        if not _user_index(ix):
            continue
        store = ((body.get("primaries") or {}).get("store") or {})
        nbytes = store.get("total_data_set_size_in_bytes") or store.get("size_in_bytes") or 0
        if nbytes <= 0:
            continue
        total += nbytes
        info = explain.get(ix) or {}
        age = delete_age.get(info.get("policy")) if info.get("managed") else None
        if age is None:
            age = ds_age.get(ix)
        if age is None:
            unknown += nbytes
        else:
            by_age[round(age, 2)] += nbytes
        ms = created.get(ix) or info.get("index_creation_date_millis")
        if ms:
            observed.append((captured_ms - int(ms)) / 86_400_000)
        else:
            since = _parse_age(info.get("time_since_index_creation"))
            if since:
                observed.append(since)

    known = total - unknown
    weighted = sum(age * nbytes for age, nbytes in by_age.items()) / known if known > 0 else None
    parts: list[dict] = []
    if known > 0:
        for age, nbytes in sorted(by_age.items(), key=lambda kv: -kv[1]):
            parts.append({"days": age, "pct": round(100 * nbytes / known, 1)})
    observed_days = max(observed) if observed else None
    if weighted:
        basis, days = "policy", weighted
    elif observed_days and observed_days > 0:
        basis, days = "observed", observed_days
        parts = [{"days": round(observed_days, 1), "pct": 100.0, "basis": "observed"}]
    else:
        basis, days = None, None
    return {
        "cluster": health.get("cluster_name"),
        "bulk_gb_day": round(gb_day, 3),
        "uptime_days_min": round(min(hot_up), 1) if hot_up else None,
        "retention_days": round(days, 1) if days else None,
        "retention_basis": basis,
        "retention_label": _retention_label(parts, basis),
        "store_gb": round(total / 1e9, 2),
    }


def parse_diagnostics(out_dir: Path, *, captured: datetime | None = None) -> list[dict]:
    """Read every zip in the directory. Writes sizing_from_diags.json and returns the rows."""
    out_dir = Path(out_dir)
    manifest_path = out_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else []
    id_to_name = {r["id"]: r["name"] for r in manifest if r.get("id") and r.get("name")}
    when = captured or datetime.now(timezone.utc)
    rows: list[dict] = []
    for path in sorted(out_dir.glob("*.zip")):
        dep = path.stem.split("__")[-1]
        try:
            row = analyze_bundle(path, captured=when)
        except Exception as exc:
            print(f"WARN: {path.name} {type(exc).__name__}: {exc}", flush=True)
            continue
        row["name"] = id_to_name.get(dep, path.name.split("__")[0])
        row["id"] = dep
        row["file"] = str(path)
        rows.append(row)
    rows.sort(key=lambda r: -(r.get("bulk_gb_day") or 0))
    (out_dir / "sizing_from_diags.json").write_text(json.dumps(rows, indent=2))
    print(
        f"diagnostics parsed: {len(rows)} bundles, "
        f"bulk {sum(r.get('bulk_gb_day') or 0 for r in rows):,.1f} GB/day, "
        f"store {sum(r.get('store_gb') or 0 for r in rows):,.1f} GB",
        flush=True,
    )
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--org-id", default=None, help="Elastic Cloud organization id (required unless --parse-only)")
    ap.add_argument("--out", required=True, help="Directory of {name}__{id}.zip bundles")
    ap.add_argument("--env", default=".env")
    ap.add_argument("--parse-only", action="store_true", help="Do not call the admin API; read zips already in --out")
    ap.add_argument("--skip-vpn-check", action="store_true", help="Pull without checking that the Aviatrix VPN tunnel is up")
    ap.add_argument("--workers", type=int, default=2)
    args = ap.parse_args()
    out = Path(args.out)
    if not args.parse_only:
        if not args.org_id:
            raise SystemExit("--org-id is required to pull diagnostics")
        if not args.skip_vpn_check:
            from vpn_check import require_aviatrix
            require_aviatrix()
        env = _load_env(Path(args.env))
        pull_diagnostics(env, args.org_id, out, workers=args.workers)
    captured = None
    zips = list(out.glob("*.zip"))
    if zips:
        newest = max(p.stat().st_mtime for p in zips)
        captured = datetime.fromtimestamp(newest, timezone.utc)
    parse_diagnostics(out, captured=captured)


if __name__ == "__main__":
    main()
