#!/usr/bin/env python3
"""Pull Elasticsearch diagnostic bundles from the Elastic Cloud admin console and read them.

Started deployments only. Stopped deployments are recorded and skipped. A zip that is
already in the output directory is kept. Each bundle is a full capture:

  POST /api/v1/deployments/{id}/elasticsearch/{ref_id}/diagnostics/_capture?mode=full

Auth is FOUND_ADMIN_API_KEY. FOUND_ADMIN_URL must be https://admin.found.no.
Another host is accepted only when it is listed in FOUND_ADMIN_HOST_ALLOWLIST.

  python admin_diags.py --org-id 2513111014 --out serverless_analyzer/diag_artifacts/2513111014_diags_2026-11-04 --env .env
  python admin_diags.py --parse-only --out serverless_analyzer/diag_artifacts/2513111014_diags_2026-11-04

`sizing_from_diags.json` is one row per bundle: bulk GB/day from
`index.primaries.bulk.est_bytes_per_day`, retention, and primary store size.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from privacy import seal_dir, seal_file, seal_text

_ADMIN_HOST = "admin.found.no"
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


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
    """Pinned https admin origin. The API key is sent only to this host."""
    raw = (env.get("FOUND_ADMIN_URL") or "").strip() or f"https://{_ADMIN_HOST}"
    parsed = urllib.parse.urlsplit(raw)
    host = (parsed.hostname or "").lower()
    allowed = {_ADMIN_HOST}
    allowed.update(
        item.strip().lower()
        for item in (env.get("FOUND_ADMIN_HOST_ALLOWLIST") or "").split(",")
        if item.strip()
    )
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
        or host not in allowed
    ):
        raise ValueError(
            "FOUND_ADMIN_URL must be https://admin.found.no with no user, query, or path. "
            "Set FOUND_ADMIN_HOST_ALLOWLIST for another host."
        )
    port = f":{parsed.port}" if parsed.port else ""
    return f"https://{host}{port}"


def safe_id(value, label: str) -> str:
    """Organization, deployment, and ref ids used in an admin URL."""
    text = str(value or "")
    if not _SAFE_ID.fullmatch(text):
        raise ValueError(f"unsafe {label}")
    return text


def capture_url(base: str, deployment_id: str, ref_id: str) -> str:
    dep = safe_id(deployment_id, "deployment id")
    ref = safe_id(ref_id, "ref_id")
    return f"{base}/api/v1/deployments/{dep}/elasticsearch/{ref}/diagnostics/_capture?mode=full"


def capture_target(row: dict) -> dict:
    """Safe deployment id and ref_id, or a skipped row when either id is unsafe."""
    try:
        dep = safe_id(row.get("id"), "deployment id")
        ref = safe_id(row.get("ref_id") or "main-elasticsearch", "ref_id")
    except ValueError as ex:
        return {**row, "result": "skipped", "error": str(ex)}
    return {**row, "id": dep, "ref_id": ref}


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
    url = capture_url(base, row.get("id"), row.get("ref_id") or "main-elasticsearch")
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
    seal_file(dest)
    return {**row, "result": "ok", "bytes": n, "file": str(dest)}


def _capture_with_retry(base: str, key: str, row: dict, dest: Path) -> dict:
    last = "unknown error"
    for attempt in range(4):
        try:
            return _capture_one(base, key, row, dest)
        except ValueError as exc:
            dest.unlink(missing_ok=True)
            return {**row, "result": "skipped", "error": str(exc)}
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
    safe_id(org_id, "organization id")
    key = env.get("FOUND_ADMIN_API_KEY")
    if not key:
        raise RuntimeError("FOUND_ADMIN_API_KEY is not set")
    base = admin_base(env)
    seal_dir(out_dir)
    listing = list_deployments(base, key, org_id)
    targets: list[dict] = []
    skipped: list[dict] = []
    for dep in listing:
        resources = (dep.get("resources") or {}).get("elasticsearch") or []
        if not resources:
            continue
        es = resources[0]
        info = es.get("info") or {}
        row = capture_target({
            "id": dep.get("id"),
            "name": dep.get("name"),
            "status": info.get("status"),
            "hidden": bool((dep.get("metadata") or {}).get("hidden")),
            "ref_id": es.get("ref_id") or "main-elasticsearch",
        })
        if row.get("result") == "skipped":
            skipped.append(row)
            continue
        if row["status"] == "started" and row["id"]:
            targets.append(row)
        else:
            skipped.append({**row, "result": "skipped"})

    reused: list[dict] = []
    pending: list[tuple[dict, Path]] = []
    for row in targets:
        dest = out_dir / f"{_safe(row['name'])}__{row['id']}.zip"
        if _is_zip(dest):
            seal_file(dest)
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
                elif row["result"] == "skipped":
                    print(f"SKIP {row['name']} {row.get('error', '')[:160]}", flush=True)
                else:
                    print(f"ERR {row['name']} {row.get('error', '')[:160]}", flush=True)
    manifest = sorted(reused + fresh, key=lambda r: (r.get("name") or "").lower()) + skipped
    seal_text(out_dir / "manifest.json", json.dumps(manifest, indent=2))
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


def quoted_index_bytes(name: str, store: dict | None) -> dict:
    """Local bytes are the quote. Searchable-snapshot mounts are reported and left out."""
    store = store or {}
    dataset_bytes = int(store.get("total_data_set_size_in_bytes") or store.get("size_in_bytes") or 0)
    local_bytes = int(store.get("size_in_bytes") or 0)
    if str(name).startswith("partial-"):
        return {"quote": 0, "dataset": 0, "snapshot": dataset_bytes or local_bytes}
    if not _user_index(name) or (local_bytes <= 0 and dataset_bytes <= 0):
        return {"quote": 0, "dataset": 0, "snapshot": 0}
    return {
        "quote": local_bytes if local_bytes > 0 else 0,
        "dataset": dataset_bytes or local_bytes,
        "snapshot": 0,
    }


def _user_index(name: str) -> bool:
    """Local user data. Searchable-snapshot mounts and system indexes stay out."""
    if name.startswith("partial-"):
        return False
    if name.startswith(".ds-"):
        rest = name[4:]
        return not (rest.startswith(".") or rest.startswith("ilm"))
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


# The second estimate keeps 60% of write-index bulk.
COMPRESSION_RATIO = 0.60


def compression_share(bulks: list[float], indexes: list[float]) -> float | None:
    """On-disk ÷ bulk. The ratio stays at or below 1.

    Each write index with both rates contributes min(1, index ÷ bulk). The total is
    Σ index ÷ Σ bulk, including bulk whose on-disk rate is 0. When that total is
    above 1, the median of the capped per-index ratios is used instead.
    """
    ratios: list[float] = []
    bulk_sum = 0.0
    index_sum = 0.0
    for bulk, index in zip(bulks, indexes):
        bulk = float(bulk or 0)
        index = float(index or 0)
        if bulk <= 0:
            continue
        bulk_sum += bulk
        index_sum += max(index, 0.0)
        if index > 0:
            ratios.append(min(1.0, index / bulk))
    if bulk_sum <= 0 or not ratios:
        return None
    share = index_sum / bulk_sum
    if share > 1:
        share = float(statistics.median(ratios))
    return min(1.0, share)


def compression_share_from_ratios(bulks: list[float], ratios: list[float | None]) -> float | None:
    """Bulk-weighted total of ratios that are already on-disk ÷ bulk. Never above 1."""
    capped: list[float] = []
    weights: list[float] = []
    for bulk, ratio in zip(bulks, ratios):
        bulk = float(bulk or 0)
        if bulk <= 0 or ratio is None:
            continue
        capped.append(min(1.0, float(ratio)))
        weights.append(bulk)
    if not weights:
        return None
    share = sum(weight * ratio for weight, ratio in zip(weights, capped)) / sum(weights)
    if share > 1:
        share = float(statistics.median(capped))
    return min(1.0, share)


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
        primaries = body.get("primaries") or {}
        bulk_bytes = ((primaries.get("bulk") or {}).get("total_size_in_bytes") or 0)
        info = explain.get(name) or {}
        created_ms = info.get("index_creation_date_millis")
        lifecycle_ms = info.get("lifecycle_date_millis")
        action = info.get("action")
        est_bytes += _est_bytes_per_day(int(bulk_bytes), created_ms, lifecycle_ms, action, collection)
    gb_day = est_bytes / 1e9
    # compressioncalc_accounted_for = bulk GB/day × 0.60.
    # On-disk bytes do not change it.
    if gb_day <= 0:
        compression_ratio = None
        accounted_gb = 0.0
    else:
        compression_ratio = COMPRESSION_RATIO
        accounted_gb = gb_day * COMPRESSION_RATIO

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
    local = 0.0
    dataset = 0.0
    snapshot = 0.0
    observed: list[float] = []
    captured_ms = captured.timestamp() * 1000
    for ix, body in stats.items():
        store = ((body.get("primaries") or {}).get("store") or {})
        measured = quoted_index_bytes(ix, store)
        snapshot += measured["snapshot"]
        if measured["dataset"] <= 0 and measured["quote"] <= 0:
            continue
        dataset += measured["dataset"]
        nbytes = measured["quote"]
        if nbytes <= 0:
            continue
        local += nbytes
        info = explain.get(ix) or {}
        age = delete_age.get(info.get("policy")) if info.get("managed") else None
        if age is None:
            age = ds_age.get(ix)
        if age is None:
            unknown += nbytes
        else:
            by_age[round(age, 3)] += nbytes
        ms = created.get(ix) or info.get("index_creation_date_millis")
        if ms:
            observed.append((captured_ms - int(ms)) / 86_400_000)
        else:
            since = _parse_age(info.get("time_since_index_creation"))
            if since:
                observed.append(since)

    known = local - unknown
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
        "compression_ratio": None if compression_ratio is None else round(compression_ratio, 3),
        "compressioncalc_accounted_for": round(accounted_gb, 3),
        "uptime_days_min": round(min(hot_up), 1) if hot_up else None,
        "retention_days": round(days, 3) if days else None,
        "retention_basis": basis,
        "retention_label": _retention_label(parts, basis),
        "store_gb": round(local / 1e9, 2),
        "dataset_gb": round((dataset + snapshot) / 1e9, 2),
        "snapshot_gb": round(((dataset - local) + snapshot) / 1e9, 2),
    }


def parse_workers(n_zips: int) -> int:
    """Process-pool size for zip parsing. Capped at the CPU count and at 8."""
    cpu = os.cpu_count() or 2
    return max(1, min(8, cpu, n_zips))


def _parse_zip(payload: tuple[str, str, dict]) -> dict:
    """One bundle. A bad zip returns a warn record and is left out of the rows."""
    path_s, captured_iso, id_to_name = payload
    path = Path(path_s)
    captured = datetime.fromisoformat(captured_iso)
    dep = path.stem.split("__")[-1]
    try:
        row = analyze_bundle(path, captured=captured)
    except Exception as exc:
        return {"warn": f"WARN: {path.name} {type(exc).__name__}: {exc}"}
    row["name"] = id_to_name.get(dep, path.name.split("__")[0])
    row["id"] = dep
    row["file"] = str(path)
    return row


def parse_diagnostics(out_dir: Path, *, captured: datetime | None = None, workers: int | None = None) -> list[dict]:
    """Read every zip in the directory. Writes sizing_from_diags.json and returns the rows.

    Parsing starts after the downloads return, so each zip is complete. Two or more
    zips use a process pool.
    """
    out_dir = Path(out_dir)
    manifest_path = out_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else []
    id_to_name = {r["id"]: r["name"] for r in manifest if r.get("id") and r.get("name")}
    when = captured or datetime.now(timezone.utc)
    paths = sorted(out_dir.glob("*.zip"))
    payloads = [(str(path), when.isoformat(), id_to_name) for path in paths]
    pool_size = parse_workers(len(payloads)) if workers is None else max(1, workers)
    if len(payloads) <= 1 or pool_size <= 1:
        parsed = [_parse_zip(payload) for payload in payloads]
    else:
        with ProcessPoolExecutor(max_workers=min(pool_size, len(payloads))) as pool:
            parsed = list(pool.map(_parse_zip, payloads))
    rows: list[dict] = []
    for item in parsed:
        if item.get("warn"):
            print(item["warn"], flush=True)
            continue
        rows.append(item)
    rows.sort(key=lambda r: -(r.get("bulk_gb_day") or 0))
    seal_dir(out_dir)
    seal_text(out_dir / "sizing_from_diags.json", json.dumps(rows, indent=2))
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
        env = _load_env(Path(args.env))
        try:
            base = admin_base(env)
            safe_id(args.org_id, "organization id")
        except ValueError as ex:
            raise SystemExit(str(ex)) from ex
        if not args.skip_vpn_check:
            from vpn_check import require_aviatrix
            require_aviatrix(urllib.parse.urlsplit(base).hostname or _ADMIN_HOST)
        try:
            pull_diagnostics(env, args.org_id, out, workers=args.workers)
        except ValueError as ex:
            raise SystemExit(str(ex)) from ex
    captured = None
    zips = list(out.glob("*.zip"))
    if zips:
        newest = max(p.stat().st_mtime for p in zips)
        captured = datetime.fromtimestamp(newest, timezone.utc)
    parse_diagnostics(out, captured=captured)


if __name__ == "__main__":
    main()
