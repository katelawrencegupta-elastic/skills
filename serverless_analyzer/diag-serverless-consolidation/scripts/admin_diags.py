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


def _zip_ok(path: Path) -> bool:
    """A complete bundle: a readable zip whose every member passes its CRC check."""
    if not _is_zip(path):
        return False
    try:
        with zipfile.ZipFile(path) as zf:
            return bool(zf.namelist()) and zf.testzip() is None
    except (zipfile.BadZipFile, OSError, EOFError, RuntimeError):
        return False


# One try plus three retries before a deployment is reported as failed.
CAPTURE_RETRIES = 3


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
        expected = resp.headers.get("Content-Length")
        n = 0
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            fh.write(chunk)
            n += len(chunk)
    if expected and expected.isdigit() and int(expected) != n:
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"download was cut short ({n} of {expected} bytes)")
    if not _zip_ok(dest):
        dest.unlink(missing_ok=True)
        raise RuntimeError("response was not a complete zip")
    seal_file(dest)
    return {**row, "result": "ok", "bytes": n, "file": str(dest)}


def _capture_with_retry(base: str, key: str, row: dict, dest: Path, *, retries: int = CAPTURE_RETRIES, pause: float = 10) -> dict:
    last = "unknown error"
    attempts = retries + 1
    for attempt in range(attempts):
        try:
            done = _capture_one(base, key, row, dest)
            return {**done, "attempts": attempt + 1}
        except ValueError as exc:
            dest.unlink(missing_ok=True)
            return {**row, "result": "skipped", "error": str(exc), "attempts": attempt + 1}
        except urllib.error.HTTPError as exc:
            body = exc.read(300).decode("utf-8", "replace")
            try:
                msg = json.loads(body)["errors"][0]["message"]
            except Exception:
                msg = body[:180]
            last = f"HTTP {exc.code}: {msg}"
            dest.unlink(missing_ok=True)
            if attempt + 1 < attempts:
                time.sleep(2 * pause * (attempt + 1) if exc.code == 429 else pause)
                continue
            break
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
            dest.unlink(missing_ok=True)
            if attempt + 1 < attempts:
                time.sleep(pause)
                continue
            break
    return {**row, "result": "error", "error": last, "attempts": attempts}


def pull_diagnostics(env: dict[str, str], org_id: str, out_dir: Path, *, workers: int = 2, exclude_ids: set[str] | None = None) -> list[dict]:
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
        if exclude_ids and str(row["id"]).lower() in exclude_ids:
            skipped.append({**row, "result": "skipped", "error": "excluded by --exclude-deployment"})
            continue
        if row["status"] == "started" and row["id"]:
            targets.append(row)
        else:
            skipped.append({**row, "result": "skipped"})

    reused: list[dict] = []
    pending: list[tuple[dict, Path]] = []
    for row in targets:
        dest = out_dir / f"{_safe(row['name'])}__{row['id']}.zip"
        if dest.exists() and not _zip_ok(dest):
            print(f"diagnostics: {dest.name} on disk is not a complete zip; capturing it again", flush=True)
            dest.unlink(missing_ok=True)
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
    # A last pass over what still failed, one at a time so the console is not rate limited.
    failed = [r for r in fresh if r.get("result") == "error"]
    if failed:
        print(f"diagnostics: retrying {len(failed)} failed deployments one at a time", flush=True)
        by_id = {r["id"]: dest for r, dest in pending}
        for bad_row in failed:
            again = _capture_with_retry(base, key, {k: v for k, v in bad_row.items() if k not in ("result", "error", "attempts")}, by_id[bad_row["id"]])
            again["attempts"] = int(bad_row.get("attempts") or 0) + int(again.get("attempts") or 0)
            fresh[fresh.index(bad_row)] = again
            print(("ok " if again["result"] == "ok" else "ERR ") + f"{again['name']} after {again['attempts']} attempts", flush=True)
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
    """One member of a bundle, by its path inside the archive.

    An admin-console bundle nests every file under `api-diagnostics-<ts>/`, so the
    leading slash matches. A bundle written by the standalone `esdiag` runner has
    those same files at the archive root, with no folder to match the slash, so a
    root-relative name is accepted too.
    """
    root = suffix.lstrip("/")
    names = [n for n in archive.namelist() if not n.endswith("/")]
    name = next((n for n in names if n.endswith(suffix)), None) or next(
        (n for n in names if n == root), None
    )
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
        label = "1 day" if n == 1 else f"{n:,} days"
    else:
        label = " · ".join(f"{round(p['days'])}d ({p['pct']:.0f}%)" for p in parts[:3])
    return label + (" (part observed)" if basis == "blended" else "")


def _est_bytes_per_day(bulk_bytes: int, creation: int | None, lifecycle: int | None, action: str | None, collection: int) -> int:
    """index.primaries.bulk.est_bytes_per_day from the esdiag indices_stats processor.

    Bytes per day are the primary bulk request size over the index write phase.
    The write phase is creation until rollover, or the index age while ILM's
    current action is rollover. A missing write phase is 0.
    """
    seconds = _write_phase_sec(creation, lifecycle, action, collection)
    if not bulk_bytes or seconds <= 0:
        return 0
    return int(int(bulk_bytes) * (86400.0 / seconds))


def _write_phase_sec(creation: int | None, lifecycle: int | None, action: str | None, collection: int) -> int:
    """index.write_phase_sec from esdiag: creation until rollover, or the age while ILM waits to roll over."""
    if not creation or creation <= 0 or collection < creation or not lifecycle or lifecycle > collection:
        return 0
    age = collection - int(creation)
    since = collection - int(lifecycle)
    if age > since:
        return (age - since) // 1000
    if action == "rollover":
        return age // 1000
    return 0


# A series is rated over at least this long when earlier generations allow it.
MIN_RATE_WINDOW_SEC = 86_400
# A dated index family is rated only while its newest index is this young.
DATED_FAMILY_MAX_AGE_SEC = 32 * 86_400
_DATED_NAME = re.compile(r"^(?P<base>.+?)[-_.](?P<date>20\d{2}[.\-_]?\d{2}(?:[.\-_]?\d{2})?)$")


def _window_rate(generations: list[tuple[int, int, bool]], minimum: int = MIN_RATE_WINDOW_SEC) -> tuple[int, int, int]:
    """Bytes per day over the newest generations of one series.

    `generations` is newest first: (bulk bytes, write-phase seconds, usable).
    The first entry is the current write index and is rated by esdiag's own
    formula. While the window is under `minimum`, the next older generation is
    added, bytes and seconds both, as long as it is usable: it still holds its
    bulk counter and has a write phase. Returns the rate, the window in seconds,
    and how many generations were used. A write index with no write phase is 0.
    """
    if not generations:
        return 0, 0, 0
    bulk, seconds, _usable = generations[0]
    if bulk <= 0 or seconds <= 0:
        return 0, 0, 0
    used = 1
    for more_bulk, more_seconds, usable in generations[1:]:
        if seconds >= minimum or not usable or more_bulk <= 0 or more_seconds <= 0:
            break
        bulk += more_bulk
        seconds += more_seconds
        used += 1
    return int(int(bulk) * (86400.0 / seconds)), int(seconds), used


def ingest_kind(series: str, index_mode: str | None = None) -> str:
    """logs, k8s_logs, metrics, or traces, from the data stream or index name.

    The Observability estimator prices TSDS metrics on their own ladder, meters
    Kubernetes logs at a higher expansion, and takes traces separately. Anything
    that is not named as metrics or traces is logs.
    """
    name = (series or "").lower()
    if name.startswith(".ds-"):
        name = name[4:]
    if (index_mode or "").lower() == "time_series" or name.startswith(("metrics-", "metricbeat-")):
        return "metrics"
    if name.startswith("traces-"):
        return "traces"
    if name.startswith("logs-kubernetes."):
        return "k8s_logs"
    return "logs"


# The second estimate keeps 60% of write-index bulk.
COMPRESSION_RATIO = 0.60


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


def _write_alias_names(alias: dict) -> set[str]:
    """Aliases that have a write index. An index under one of them is a rolled generation."""
    names: set[str] = set()
    for body in (alias or {}).values():
        for alias_name, st in (((body or {}).get("aliases")) or {}).items():
            if (st or {}).get("is_write_index"):
                names.add(alias_name)
    return names


def _rolled_over(info: dict) -> bool:
    """ILM moved this index past rollover, so a later write index carries its series."""
    created = (info or {}).get("index_creation_date_millis")
    lifecycle = (info or {}).get("lifecycle_date_millis")
    return bool(created and lifecycle and int(lifecycle) > int(created))


def unrated_ingest(stats: dict, streams: list, alias: dict, explain: dict, rated: dict[str, int], covered: set[str] | None = None) -> dict:
    """User indices that took bulk writes and contribute nothing to bulk GB/day.

    The rate is summed over write indices that have a write phase. Two kinds of
    index fall outside it: a write index with no write phase (not managed by ILM,
    or managed by a data stream lifecycle), and an index that is not in a data
    stream, not under a write alias, and was never rolled over, such as a daily
    index written by name. A rolled generation is not listed, because its series
    is rated on the current write index.

    `bulk_gb` is the bulk counter on those indices since their shards started. It
    is a volume, not a rate, and it is not added to bulk GB/day.
    """
    members = {
        (ix or {}).get("index_name")
        for ds in streams or [] for ix in (ds.get("indices") or [])
    }
    write = _write_indices(streams, alias)
    write_aliases = _write_alias_names(alias)
    found: list[dict] = []
    for name, body in (stats or {}).items():
        if not _user_index(name):
            continue
        primaries = (body or {}).get("primaries") or {}
        bulk_bytes = int(((primaries.get("bulk") or {}).get("total_size_in_bytes")) or 0)
        if bulk_bytes <= 0 or rated.get(name, 0) > 0 or name in (covered or ()):
            continue
        if name in write:
            reason = "write index with no write phase"
        else:
            own_aliases = set((((alias or {}).get(name) or {}).get("aliases")) or {})
            if name in members or own_aliases & write_aliases or _rolled_over((explain or {}).get(name) or {}):
                continue
            reason = "not a data stream, write alias, or rollover"
        store_bytes = int(((primaries.get("store") or {}).get("size_in_bytes")) or 0)
        found.append({"index": name, "reason": reason, "store_bytes": store_bytes, "bulk_bytes": bulk_bytes})
    found.sort(key=lambda row: -row["bulk_bytes"])
    return {
        "unrated_indices": len(found),
        "unrated_store_gb": round(sum(row["store_bytes"] for row in found) / 1e9, 2),
        "unrated_bulk_gb": round(sum(row["bulk_bytes"] for row in found) / 1e9, 2),
        "unrated_top": [
            {
                "index": row["index"],
                "reason": row["reason"],
                "store_gb": round(row["store_bytes"] / 1e9, 2),
                "bulk_gb": round(row["bulk_bytes"] / 1e9, 2),
            }
            for row in found[:5]
        ],
    }


def _bulk_bytes(stats: dict, name: str) -> int:
    return int((((stats.get(name) or {}).get("primaries") or {}).get("bulk") or {}).get("total_size_in_bytes") or 0)


def _store_bytes(stats: dict, name: str) -> int:
    return int((((stats.get(name) or {}).get("primaries") or {}).get("store") or {}).get("size_in_bytes") or 0)


# An older generation whose bulk-to-store ratio is under this share of the write
# index's ratio has probably lost part of its bulk counter to a shard move.
COUNTER_INTACT_MIN = 0.5


def _counter_intact(stats: dict, older: str, write_name: str) -> bool:
    """Does the older generation still hold bulk bytes in proportion to what it stores?"""
    write_bulk, write_store = _bulk_bytes(stats, write_name), _store_bytes(stats, write_name)
    old_bulk, old_store = _bulk_bytes(stats, older), _store_bytes(stats, older)
    if min(write_bulk, write_store, old_store) <= 0:
        return old_bulk > 0
    return (old_bulk / old_store) >= COUNTER_INTACT_MIN * (write_bulk / write_store)


def series_rates(stats: dict, streams: list, alias: dict, explain: dict, created: dict, index_mode: dict, collection: int, counter_start: dict | None = None) -> dict:
    """Bulk bytes per day for every series in a bundle, with the logs, metrics, and traces split.

    A series is a data stream, the indices under one write alias, or a family of
    dated indices written by name. Each is rated on its current write index by
    esdiag's formula, bulk bytes × 86,400 ÷ write-phase seconds. Three things go
    beyond the single write index:

    - A write index in its first day is extended with the generations before
      it until the window reaches a day (`_window_rate`). An older generation is
      used only while ILM still has it in the hot phase and its bulk-to-store
      ratio is at least half the write index's, because a shard that moved has
      lost its bulk counter.
    - A data stream with no ILM dates (a data stream lifecycle) takes the write
      index age as its write phase, the same quantity ILM reports while it waits
      to roll over.
    - A dated family with no data stream or write alias is rated on its newest
      index over that index's age, when that index is at most 32 days old.

    `fallback_bytes` is the rate from the last two. `short_window_bytes` is the
    rate still measured over less than a day.

    The bulk counter starts at zero whenever a primary shard starts on a node, so a
    shard that restarted or moved has counted only since then, while the write phase
    runs from the index's creation. `counter_start` is that start for each index
    (the latest primary recovery in the bundle). Where the counter is younger than
    the write phase, `restart_extra_bytes` is what the rate would add if the same
    bytes were spread over the counter's own age. The quoted rate stays esdiag's
    figure; this is the flag beside it.
    """
    members_of: dict[str, list[str]] = {}
    for ds in streams or []:
        names = [(ix or {}).get("index_name") or "" for ix in (ds.get("indices") or [])]
        names = [n for n in names if n]
        if names:
            members_of[names[-1]] = names
    stream_write = set(members_of)
    alias_members: dict[str, list[str]] = defaultdict(list)
    alias_write: dict[str, str] = {}
    for name, body in (alias or {}).items():
        for alias_name, st in (((body or {}).get("aliases")) or {}).items():
            alias_members[alias_name].append(name)
            if (st or {}).get("is_write_index"):
                alias_write[name] = alias_name
    for name, alias_name in alias_write.items():
        if name not in members_of:
            family = sorted(alias_members[alias_name], key=lambda n: (created.get(n) or (explain.get(n) or {}).get("index_creation_date_millis") or 0, n))
            if name in family:
                family = family[: family.index(name) + 1]
            else:
                family = [name]
            members_of[name] = family

    def born(name: str) -> int | None:
        return (explain.get(name) or {}).get("index_creation_date_millis") or created.get(name)

    rated: dict[str, int] = {}
    mix = {"logs": 0, "k8s_logs": 0, "metrics": 0, "traces": 0}
    total = short = fallback = 0
    restart_extra = restart_indices = 0
    covered: set[str] = set()

    def add(series: str, write_name: str, rate: int, window: int, by_fallback: bool) -> None:
        nonlocal total, short, fallback
        rated[write_name] = rate
        if rate <= 0:
            return
        total += rate
        kind = ingest_kind(series, index_mode.get(write_name))
        if kind == "k8s_logs":
            mix["k8s_logs"] += rate
            kind = "logs"
        mix[kind] += rate
        if window < MIN_RATE_WINDOW_SEC:
            short += rate
        if by_fallback:
            fallback += rate

    stream_name = {names[-1]: ds.get("name") or names[-1] for ds in streams or [] for names in [[(ix or {}).get("index_name") or "" for ix in (ds.get("indices") or [])]] if names and names[-1]}
    for write_name, family in members_of.items():
        info = explain.get(write_name) or {}
        seconds = _write_phase_sec(info.get("index_creation_date_millis"), info.get("lifecycle_date_millis"), info.get("action"), collection)
        by_fallback = False
        if seconds <= 0 and write_name in stream_write and not info.get("lifecycle_date_millis"):
            # Data stream lifecycle: no ILM dates, and the write index is by definition still being written.
            start = born(write_name)
            if start and collection > start:
                seconds = (collection - int(start)) // 1000
                by_fallback = True
        generations = [(_bulk_bytes(stats, write_name), int(seconds), True)]
        later = write_name
        for older in reversed(family[:-1]):
            older_info = explain.get(older) or {}
            start, end = born(older), None
            if older_info.get("lifecycle_date_millis") and start and int(older_info["lifecycle_date_millis"]) > int(start):
                end = int(older_info["lifecycle_date_millis"])
            elif born(later):
                end = int(born(later))
            phase = (end - int(start)) // 1000 if start and end and end > int(start) else 0
            usable = (older_info.get("phase") in (None, "hot")) and phase > 0 and _counter_intact(stats, older, write_name)
            generations.append((_bulk_bytes(stats, older), int(phase), usable))
            later = older
        rate, window, _used = _window_rate(generations)
        add(stream_name.get(write_name) or write_name, write_name, rate, window, by_fallback)
        write_bulk, write_seconds = generations[0][0], generations[0][1]
        start = (counter_start or {}).get(write_name)
        if rate > 0 and start and write_seconds > 0 and collection > start:
            counted = (collection - int(start)) // 1000
            if 0 < counted < 0.9 * write_seconds:
                restart_extra += max(0, int(int(write_bulk) * (86400.0 / counted)) - int(int(write_bulk) * (86400.0 / write_seconds)))
                restart_indices += 1

    # Dated indices written by name: no data stream, no write alias, never rolled over.
    in_stream = {n for names in members_of.values() for n in names}
    write_aliases = set(alias_write.values())
    families: dict[str, list[str]] = defaultdict(list)
    for name in stats or {}:
        if name in in_stream or not _user_index(name) or _bulk_bytes(stats, name) <= 0:
            continue
        if set((((alias or {}).get(name) or {}).get("aliases")) or {}) & write_aliases or _rolled_over(explain.get(name) or {}):
            continue
        match = _DATED_NAME.match(name)
        if match and born(name):
            families[match.group("base")].append(name)
    for base, names in families.items():
        names.sort(key=lambda n: (int(born(n)), n))
        newest = names[-1]
        age = (collection - int(born(newest))) // 1000
        if age <= 0 or age > DATED_FAMILY_MAX_AGE_SEC // 1:
            continue
        generations = [(_bulk_bytes(stats, newest), int(age), True)]
        later = newest
        for older in reversed(names[:-1]):
            phase = (int(born(later)) - int(born(older))) // 1000
            generations.append((_bulk_bytes(stats, older), int(phase), phase > 0 and _counter_intact(stats, older, newest)))
            later = older
        rate, window, _used = _window_rate(generations)
        if rate > 0:
            add(base, newest, rate, window, True)
            covered.update(names)
    return {
        "rated": rated, "bytes_per_day": total, "mix": mix,
        "short_window_bytes": short, "fallback_bytes": fallback, "covered": covered,
        "restart_extra_bytes": restart_extra, "restart_indices": restart_indices,
    }


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
        recovery = _zip_json(archive, "/recovery.json") or {}

    # When each index's primary shards last started on a node. The bulk counter restarts then.
    counter_start: dict[str, int] = {}
    for name, body in (recovery.items() if isinstance(recovery, dict) else []):
        for shard in (body or {}).get("shards") or []:
            ms = shard.get("start_time_in_millis")
            if shard.get("primary") and ms:
                counter_start[name] = max(counter_start.get(name, 0), int(ms))

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
    created: dict[str, int] = {}
    index_mode: dict[str, str] = {}
    for name, meta in ((state.get("metadata") or {}).get("indices") or {}).items():
        settings = ((meta.get("settings") or {}).get("index") or {})
        if settings.get("mode"):
            index_mode[name] = str(settings.get("mode"))
        raw = settings.get("creation_date")
        if raw:
            try:
                created[name] = int(raw)
            except (TypeError, ValueError):
                pass

    rates = series_rates(stats, streams, alias, explain, created, index_mode, collection, counter_start)
    rated = rates["rated"]
    est_bytes = rates["bytes_per_day"]
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

    series_of = {
        (ix or {}).get("index_name"): ds.get("name") or ""
        for ds in streams for ix in (ds.get("indices") or [])
    }
    by_age: dict[float, float] = defaultdict(float)
    metrics_age_bytes = 0.0
    metrics_bytes = 0.0
    unknown = 0.0
    local = 0.0
    dataset = 0.0
    snapshot = 0.0
    observed: list[float] = []
    blended_bytes = 0.0       # store with no delete policy, taken at its own age
    blended_age_bytes = 0.0
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
        ms = created.get(ix) or info.get("index_creation_date_millis")
        own_age = None
        if ms:
            own_age = (captured_ms - int(ms)) / 86_400_000
        else:
            own_age = _parse_age(info.get("time_since_index_creation"))
        if own_age is not None and own_age > 0:
            observed.append(own_age)
        if age is None:
            # No delete policy covers this index. Its own age is the best evidence of how long
            # that data is kept, so it joins the weighted age at that age, by its size.
            unknown += nbytes
            if own_age is not None and own_age > 0:
                blended_bytes += nbytes
                blended_age_bytes += own_age * nbytes
                by_age[round(own_age, 3)] += nbytes
                age = own_age
        else:
            by_age[round(age, 3)] += nbytes
        if age is not None and ingest_kind(series_of.get(ix) or ix, index_mode.get(ix)) == "metrics":
            metrics_age_bytes += age * nbytes
            metrics_bytes += nbytes

    known = local - unknown                   # store whose age a policy or data stream sets
    covered = known + blended_bytes           # store that has an age at all
    weighted = sum(age * nbytes for age, nbytes in by_age.items()) / covered if covered > 0 else None
    known_share = (known / local) if local > 0 else None
    parts: list[dict] = []
    if covered > 0:
        for age, nbytes in sorted(by_age.items(), key=lambda kv: -kv[1]):
            parts.append({"days": age, "pct": round(100 * nbytes / covered, 1)})
    observed_days = max(observed) if observed else None
    if weighted and known > 0 and blended_bytes > 0:
        basis, days = "blended", weighted
    elif weighted and known > 0:
        basis, days = "policy", weighted
    elif weighted:
        basis, days = "observed", weighted
        parts = [{"days": round(weighted, 1), "pct": 100.0, "basis": "observed"}]
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
        # Size-weighted delete age of the metrics indices alone, for the TSDS retention row.
        "retention_days_metrics": round(metrics_age_bytes / metrics_bytes, 3) if metrics_bytes > 0 else None,
        "retention_basis": basis,
        # Share of local store whose age a delete policy or data stream sets. The rest is taken at
        # its own age (blended) and is what the age would be if nothing were ever deleted.
        "retention_known_share": None if known_share is None else round(known_share, 4),
        "retention_label": _retention_label(parts, basis),
        "store_gb": round(local / 1e9, 2),
        "dataset_gb": round((dataset + snapshot) / 1e9, 2),
        "snapshot_gb": round(((dataset - local) + snapshot) / 1e9, 2),
        # logs + metrics + traces is bulk_gb_day. k8s_logs is the part of logs from Kubernetes.
        "bulk_mix": {kind: round(nbytes / 1e9, 3) for kind, nbytes in rates["mix"].items()},
        # Part of bulk_gb_day rated over less than a day, and the part rated without an ILM write phase.
        "bulk_short_window_gb_day": round(rates["short_window_bytes"] / 1e9, 3),
        "bulk_fallback_gb_day": round(rates["fallback_bytes"] / 1e9, 3),
        # Extra GB/day if each write index's bytes were spread over the age of its shards' bulk
        # counter. A flag only: the quoted bulk_gb_day is esdiag's figure.
        "bulk_restart_gb_day": round(rates["restart_extra_bytes"] / 1e9, 3),
        "bulk_restart_indices": rates["restart_indices"],
        **unrated_ingest(stats, streams, alias, explain, rated, rates["covered"]),
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


def parse_diagnostics(out_dir: Path, *, captured: datetime | None = None, workers: int | None = None, exclude_ids: set[str] | None = None) -> list[dict]:
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
        if exclude_ids and str(item.get("id") or "").lower() in exclude_ids:
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
