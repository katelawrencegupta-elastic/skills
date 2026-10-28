# splunk-xml-to-kibana

Portable Agent Skill: migrate Splunk dashboards to Elastic — mockup → ES|QL panels → factories → `MAPPING.md` → deploy.

Works in **Claude Code** and **Cursor**. Token target: **≤12 assistant turns** per clean conversion (prefer **2–4** after kickoff via stub + `run_pipeline`); **one `USE_CASE` per chat**.

## Layout

```
SKILL.md                 # slim round budget + hard rules (~1.5–2k tokens)
reference.md             # index only — do not load whole
reference/
  panels.md controls.md vega.md mapping.md
adapters/blackstone.md   # optional customer adapter
scripts/
  extract_from_audit.py parse_inventory.py panels_from_inventory.py
  scaffold_use_case.py mockup_from_inventory.py generate_from_fields.py
  build_from_catalog.py spotcheck_esql.py deploy_vega_sankey.py
  put_dashboard.py rewire_vega_refs.py render_mapping.py
  run_pipeline.py estimate_package.py record_token_usage.py
hooks/
  cursor_stop_record_tokens.sh
  hooks.json.example
templates/
  mockup/ MAPPING.md factories/ esql/ vega/ metrics/
```

## Install

```bash
# Claude Code
ln -s "$(pwd)" ~/.claude/skills/splunk-xml-to-kibana

# Cursor
ln -s "$(pwd)" ~/.cursor/skills/splunk-xml-to-kibana
```

## Quick start (script path)

```bash
PKG=./elastic/my_dash
python scripts/scaffold_use_case.py --use-case my_dash --out ./elastic --dash-id my-dash

# From Splunk audit export (Classic or Studio):
python scripts/parse_inventory.py \
  --from-audit path/to/dashboard_to_migrate.json \
  --name my_dashboard_view_name \
  --out-dir "$PKG"

# Stub catalog + fields skeleton (agent then fills ES|QL):
python scripts/panels_from_inventory.py \
  --inventory "$PKG/inventory.json" \
  --out "$PKG/esql/panels.json" \
  --dash-id my-dash \
  --stream 'logs-demo.events-*'

# Edit esql/panels.json + data_factories/fields.json, then:
python scripts/run_pipeline.py --package "$PKG" --assume-env --env-file path/to/.env

# Offline (no cluster):
python scripts/run_pipeline.py --package "$PKG" \
  --skip-bulk --skip-spotcheck --skip-put --skip-vega --skip-tokens
```

Blackstone adapter:

```bash
python scripts/run_pipeline.py \
  --package customers/blackstone/elastic/<use_case> \
  --adapter blackstone --assume-env
```

### Cursor hook (real per-turn counts)

1. `export SX2K_PACKAGE_ROOT=/absolute/path/to/elastic/<use_case>`
2. Merge [`hooks/hooks.json.example`](hooks/hooks.json.example) into the project `.cursor/hooks.json` or `~/.cursor/hooks.json` (adjust `command` path to the skill install).
3. Each `stop` / `afterAgentResponse` appends to `metrics/token_usage.jsonl` and refreshes `metrics/token_usage.json`.

## Token rules (summary)

- Progressive disclosure: open one `reference/*.md` shard per round
- Stub via `panels_from_inventory.py`; agent fills ES|QL only
- Prefer `run_pipeline.py` over hand-rolled setup scripts
- Sankey → `deploy_vega_sankey.py` / `templates/vega/sankey.py` (do not copy customer builders)
- Never paste XML / full SPL / NDJSON / dashboard JSON into chat
- Persist `metrics/token_usage.json` every run (hook and/or R7)

## Sharing

Share this folder/repo. Keep customer `.env` and PII out. Use `adapters/` for customer paths.
