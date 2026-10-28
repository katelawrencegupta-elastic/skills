#!/usr/bin/env python3
"""Orchestrate SX2K R2–R7 after agent fills esql/panels.json.

Usage:
  python run_pipeline.py --package PACKAGE_ROOT --adapter blackstone --assume-env
  python run_pipeline.py --package PACKAGE_ROOT --assume-env --skip-put --skip-bulk
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPTS.parent


def _run(cmd: list[str], env: dict | None = None) -> None:
    print("+", " ".join(str(c) for c in cmd if not _looks_like_key(str(c))))
    subprocess.check_call(cmd, env=env or os.environ.copy())


def _looks_like_key(s: str) -> bool:
    # avoid echoing API keys in command traces
    if len(s) > 40 and all(c.isalnum() or c in "+/=" for c in s):
        return True
    return False


def load_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        raise SystemExit(f"Missing env file: {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def es_url_to_kb(es_url: str) -> str:
    return es_url.rstrip("/").replace(".es.", ".kb.")


def resolve_adapter_env(adapter: str, env_file: Path | None, cwd: Path) -> Path | None:
    if env_file:
        return env_file
    if adapter == "blackstone":
        # PACKAGE may be customers/blackstone/elastic/<uc>
        # Prefer customers/blackstone/.env relative to cwd/repo walks
        candidates = [
            cwd / "customers" / "blackstone" / ".env",
            cwd / "blackstone" / ".env",
            cwd / ".env",
        ]
        # walk up from cwd
        for parent in [cwd, *cwd.parents]:
            candidates.append(parent / "customers" / "blackstone" / ".env")
            candidates.append(parent / "blackstone" / ".env")
            if parent.name == "blackstone":
                candidates.append(parent / ".env")
        for c in candidates:
            if c.exists():
                return c
    return None


def es_request(es_url: str, api_key: str, method: str, path: str, body=None, content_type=None):
    url = f"{es_url.rstrip('/')}{path}"
    data = None
    headers = {
        "Authorization": f"ApiKey {api_key}",
        "Accept": "application/json",
    }
    if body is not None:
        if isinstance(body, (bytes, bytearray)):
            data = body
            headers["Content-Type"] = content_type or "application/octet-stream"
        else:
            data = json.dumps(body).encode()
            headers["Content-Type"] = content_type or "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            raw = resp.read()
            if not raw:
                return resp.status, None
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw


def ensure_data_stream(es_url: str, api_key: str, name: str) -> None:
    st, body = es_request(es_url, api_key, "PUT", f"/_data_stream/{name}")
    err = body.get("error", {}).get("type") if isinstance(body, dict) else None
    if st not in (200, 201) and err != "resource_already_exists_exception":
        raise SystemExit(f"data_stream {name}: {st} {err or body}")
    print(f"  data_stream {name}: ok")


def bulk_ndjson(es_url: str, api_key: str, index: str, path: Path) -> None:
    docs = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    lines: list[str] = []
    prefix = path.stem[:40]
    for i, d in enumerate(docs):
        lines.append(json.dumps({"create": {"_index": index, "_id": f"{prefix}-{i}"}}))
        lines.append(json.dumps(d))
    st, body = es_request(
        es_url,
        api_key,
        "POST",
        "/_bulk?refresh=true",
        ("\n".join(lines) + "\n").encode(),
        content_type="application/x-ndjson",
    )
    if st >= 300 or (isinstance(body, dict) and body.get("errors")):
        err = None
        if isinstance(body, dict):
            for item in body.get("items") or []:
                for v in item.values():
                    if isinstance(v, dict) and v.get("error"):
                        err = v["error"]
                        break
        raise SystemExit(f"bulk {index} failed: {st} {err or body}")
    print(f"  bulk {index}: {len(docs)} docs")


def stream_from_ndjson_name(name: str) -> str:
    # logs-bx.notable-default.ndjson or logs-bx_notable_default.ndjson
    stem = Path(name).stem
    return stem


def write_vega_map(package: Path, catalog: dict) -> Path | None:
    mapping: dict = {}
    for p in catalog.get("panels") or []:
        if p.get("viz") != "vega_ref":
            continue
        pid = p["id"]
        viz_id = p.get("ref_id") or f"{pid}-vega-sankey"
        mapping[pid] = {"viz_id": viz_id, "title": p.get("title") or pid}
    if not mapping:
        return None
    dest = package / "kibana" / "vega_panel_map.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(mapping, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {dest}")
    return dest


def touch_analysis_url(package: Path, kb_url: str, dash_id: str) -> None:
    path = package / "ANALYSIS.md"
    url = f"{kb_url.rstrip('/')}/app/dashboards#/view/{dash_id}"
    line = f"**Kibana:** [{dash_id}]({url})\n"
    if path.exists():
        text = path.read_text(encoding="utf-8")
        if dash_id in text and "app/dashboards" in text:
            return
        path.write_text(text.rstrip() + "\n\n" + line, encoding="utf-8")
    else:
        path.write_text(
            f"# {package.name}\n\n{line}**Mapping:** [`MAPPING.md`](MAPPING.md)\n",
            encoding="utf-8",
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--package", type=Path, required=True)
    ap.add_argument("--adapter", default="", help="e.g. blackstone")
    ap.add_argument(
        "--assume-env",
        action="store_true",
        help="Gate 0 satisfied: load env and proceed (echo URLs)",
    )
    ap.add_argument("--env-file", type=Path)
    ap.add_argument("--from-audit", type=Path)
    ap.add_argument("--name", default="", help="With --from-audit")
    ap.add_argument("--stream", default="", help="Unused for put; documented in catalog")
    ap.add_argument("--skip-extract", action="store_true")
    ap.add_argument("--skip-mockup", action="store_true")
    ap.add_argument("--skip-factory", action="store_true")
    ap.add_argument("--skip-bulk", action="store_true")
    ap.add_argument("--skip-spotcheck", action="store_true")
    ap.add_argument("--skip-put", action="store_true")
    ap.add_argument("--skip-vega", action="store_true")
    ap.add_argument("--skip-mapping", action="store_true")
    ap.add_argument("--skip-tokens", action="store_true")
    ap.add_argument("--skip-build", action="store_true")
    args = ap.parse_args()

    package = args.package.resolve()
    py = sys.executable
    cwd = Path.cwd()

    # --- Gate 0 ---
    needs_cluster = not (
        args.skip_bulk and args.skip_spotcheck and args.skip_put and args.skip_vega
    )
    es_url = os.environ.get("ELASTICSEARCH_URL", "")
    kb_url = os.environ.get("KIBANA_URL", "")
    api_key = os.environ.get("ELASTICSEARCH_API_KEY", "")

    if needs_cluster:
        if not args.assume_env and not args.env_file and not (es_url and api_key):
            print(
                "Gate 0: pass --assume-env or --env-file after user OK "
                "(or set ELASTICSEARCH_URL + ELASTICSEARCH_API_KEY).",
                file=sys.stderr,
            )
            return 2
        env_path = resolve_adapter_env(args.adapter, args.env_file, cwd)
        if args.assume_env or args.env_file:
            if not env_path:
                raise SystemExit(
                    f"Could not resolve .env for adapter={args.adapter!r}; pass --env-file"
                )
            file_env = load_env_file(env_path)
            es_url = file_env.get("ELASTICSEARCH_URL") or es_url
            kb_url = file_env.get("KIBANA_URL") or kb_url or es_url_to_kb(es_url)
            api_key = file_env.get("ELASTICSEARCH_API_KEY") or api_key
        if not kb_url and es_url:
            kb_url = es_url_to_kb(es_url)
        if not es_url or not api_key:
            raise SystemExit("Missing ELASTICSEARCH_URL or ELASTICSEARCH_API_KEY")
        print(f"Destination ES: {es_url}")
        print(f"Destination KB: {kb_url}")

    # dirs
    for sub in ("mockup", "kibana", "data_factories/out", "esql", "metrics"):
        (package / sub).mkdir(parents=True, exist_ok=True)

    # 1 extract/parse
    if args.from_audit and not args.skip_extract:
        if not args.name:
            raise SystemExit("--from-audit requires --name")
        _run(
            [
                py,
                str(SCRIPTS / "parse_inventory.py"),
                "--from-audit",
                str(args.from_audit),
                "--name",
                args.name,
                "--out-dir",
                str(package),
            ]
        )

    inv = package / "inventory.json"
    catalog_path = package / "esql" / "panels.json"

    # 3 mockup
    if not args.skip_mockup:
        if not inv.exists():
            raise SystemExit(f"missing {inv} — run parse/extract first")
        _run(
            [
                py,
                str(SCRIPTS / "mockup_from_inventory.py"),
                "--inventory",
                str(inv),
                "--out",
                str(package / "mockup"),
            ]
        )

    # 4 require catalog
    if not catalog_path.exists():
        raise SystemExit(
            f"missing {catalog_path} — run panels_from_inventory.py then edit ES|QL"
        )
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    dash_id = (catalog.get("dashboard") or {}).get("id") or package.name

    # 5 factory
    fields_json = package / "data_factories" / "fields.json"
    fields_dir = package / "data_factories" / "fields"
    out_dir = package / "data_factories" / "out"
    if not args.skip_factory:
        if fields_json.exists() or fields_dir.is_dir():
            cmd = [
                py,
                str(SCRIPTS / "generate_from_fields.py"),
                "--out",
                str(out_dir),
            ]
            if fields_json.exists():
                cmd.extend(["--fields", str(fields_json)])
            if fields_dir.is_dir() and any(fields_dir.glob("*.json")):
                cmd.extend(["--fields-dir", str(fields_dir)])
            _run(cmd)
        else:
            print("skip factory: no data_factories/fields.json")

    # 6 bulk
    if not args.skip_bulk and needs_cluster:
        for nd in sorted(out_dir.glob("*.ndjson")):
            index = stream_from_ndjson_name(nd.name)
            ensure_data_stream(es_url, api_key, index)
            bulk_ndjson(es_url, api_key, index, nd)

    # 7 spotcheck
    if not args.skip_spotcheck and needs_cluster:
        _run(
            [
                py,
                str(SCRIPTS / "spotcheck_esql.py"),
                "--catalog",
                str(catalog_path),
                "--es-url",
                es_url,
                "--api-key",
                api_key,
            ]
        )

    # 8 build
    if not args.skip_build:
        _run(
            [
                py,
                str(SCRIPTS / "build_from_catalog.py"),
                "--catalog",
                str(catalog_path),
                "--out",
                str(package / "kibana"),
            ]
        )

    vega_panels = [p for p in (catalog.get("panels") or []) if p.get("viz") == "vega_ref"]
    vega_map = write_vega_map(package, catalog)

    # 9 vega + put + rewire
    if vega_panels and not args.skip_vega and needs_cluster and not args.skip_put:
        for p in vega_panels:
            viz_id = p.get("ref_id") or f"{p['id']}-vega-sankey"
            # Prefer stream/src/dst from panel meta if present
            stream = p.get("vega_stream") or args.stream or "logs-*"
            src = p.get("vega_src") or "src"
            dst = p.get("vega_dst") or "dst"
            cmd = [
                py,
                str(SCRIPTS / "deploy_vega_sankey.py"),
                "--viz-id",
                viz_id,
                "--title",
                p.get("title") or viz_id,
                "--kibana-url",
                kb_url,
                "--api-key",
                api_key,
            ]
            if p.get("vega_esql"):
                cmd.extend(["--esql", p["vega_esql"]])
            else:
                cmd.extend(["--stream", stream, "--src", src, "--dst", dst])
            _run(cmd)

    if not args.skip_put and needs_cluster:
        body = package / "kibana" / f"{dash_id}.json"
        if not body.exists():
            raise SystemExit(f"missing dashboard JSON {body}")
        _run(
            [
                py,
                str(SCRIPTS / "put_dashboard.py"),
                "--body",
                str(body),
                "--kibana-url",
                kb_url,
                "--api-key",
                api_key,
            ]
        )
        if vega_map and not args.skip_vega:
            _run(
                [
                    py,
                    str(SCRIPTS / "rewire_vega_refs.py"),
                    "--dashboard-id",
                    dash_id,
                    "--map",
                    str(vega_map),
                    "--kibana-url",
                    kb_url,
                    "--api-key",
                    api_key,
                ]
            )
        touch_analysis_url(package, kb_url, dash_id)
        print(f"Dashboard URL: {kb_url.rstrip('/')}/app/dashboards#/view/{dash_id}")

    # 11 mapping
    if not args.skip_mapping:
        cmd = [
            py,
            str(SCRIPTS / "render_mapping.py"),
            "--inventory",
            str(inv),
            "--catalog",
            str(catalog_path),
            "--out",
            str(package / "MAPPING.md"),
        ]
        queries = package / "splunk_queries.json"
        if queries.exists():
            cmd.extend(["--queries", str(queries)])
        gaps = package / "gaps.json"
        if gaps.exists():
            cmd.extend(["--gaps", str(gaps)])
        _run(cmd)

    # 12 tokens placeholder
    if not args.skip_tokens:
        _run(
            [
                py,
                str(SCRIPTS / "record_token_usage.py"),
                "--package",
                str(package),
                "--input-tokens",
                "0",
                "--output-tokens",
                "0",
                "--source",
                "pipeline",
                "--note",
                "run_pipeline placeholder (prefer Cursor hook)",
                "--print-summary",
            ]
        )

    print("pipeline done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
