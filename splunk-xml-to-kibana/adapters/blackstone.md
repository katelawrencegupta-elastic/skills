# Adapter — Blackstone POC

Load this file **only** when converting dashboards under the Blackstone customer tree. Do not bake these paths into shared `SKILL.md` outputs for other customers.

## Parameters (override skill defaults)

| Param | Blackstone value |
|-------|------------------|
| Customer root | `customers/blackstone/` |
| `PACKAGE_ROOT` | `customers/blackstone/elastic/<USE_CASE>/` |
| Gate 0 `.env` | `customers/blackstone/.env` (ask before use; echo URL) |
| Pipeline | `run_pipeline.py --adapter blackstone --assume-env` after user OK |
| Audit extract | `parse_inventory.py --from-audit …/dashboard_to_migrate.json --name <view>` |
| Typical destination | klgdimapoc Elastic Cloud (confirm each run) |
| `DATA_FACTORIES` | repo-root `data_factories/` (`pip install -e` or `sys.path` → `data_factories/src`) |
| Stream naming | `logs-bx.<dataset>-*` / tags like `bx-poc` |
| Dashboard ids | `bx-<slug>` |
| Shared helpers | `customers/blackstone/elastic/common.py` (`load_env`, `Api`, `es_url_to_kb`) |
| Factory catalog | `customers/blackstone/elastic/data_factories/factories.yaml` (if present) |

## DIMA / Splunk audit inputs

Under the Blackstone Splunk audit export (paths vary by laptop checkout):

| Artifact | Role |
|----------|------|
| `dashboard_to_migrate.json` | Primary Classic/Studio `eai:data` |
| `macros.json` | Expand macros → `macros_resolved.json` |
| `lookup_samples.json` | Often truncated; factory stand-ins OK |

## Worked packages (local examples, not shipped in skill)

| Package | Notes |
|---------|--------|
| `elastic/hdsi_user_investigation/` | Lookups + macros + Controls lessons |
| `elastic/secops_aws_blast_radius/` | Vega Sankey + force→heatmap + `rewire_vega_panel_refs` |
| `elastic/aws_user_investigation/` | Classic form, username Control, RFC1918 GAP |
| `elastic/employee_safety/` | Multi-tab → multi-dashboard + links nav |
| `elastic/ioc_threat_hunt/`, `elastic/network_egress_exfil/`, `elastic/dns_hunt/` | Hunt-style tables |

## Conventions

- Prefer skill pipeline: extract → `panels_from_inventory` → edit ES|QL/`fields.json` → `run_pipeline.py --adapter blackstone --assume-env`
- Thin wrappers: `PACKAGE_ROOT/data_factories/generate_*.py` → `data_factories.<domain>.*` only when shared domain factories beat `fields.json`
- Setup scripts: legacy `setup_*.py` / `build_dashboard.py` OK; new packages should use `run_pipeline.py` + `deploy_vega_sankey.py`
- Vega Sankey: `templates/vega/sankey.py` — do not re-copy `secops_aws_blast_radius/build_dashboard.py`
- External corporate IP lookups (netbox/zscaler/zpa): document **GAP**; RFC1918 exclude is the usual POC stand-in
- Never commit `customers/blackstone/.env` or push `customers/` to public remotes

### Gate 0 + `--assume-env`

Ask once. When the user says to use Blackstone `.env` / klgdimapoc, run:

```bash
python scripts/run_pipeline.py \
  --package customers/blackstone/elastic/<USE_CASE> \
  --adapter blackstone --assume-env
```

Pipeline resolves `customers/blackstone/.env`, echoes ES/KB URLs, then deploys. Do not wait for a second confirmation.

## When not to use this adapter

Any non-Blackstone engagement — use skill Parameters only (`PACKAGE_ROOT`, `STREAM`, `DASH_ID`).
