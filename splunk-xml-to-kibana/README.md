# splunk-xml-to-kibana

Portable Agent Skill: migrate Splunk dashboards to Elastic — mockup → ES|QL panels → factories → `MAPPING.md` → deploy.

Works in **Claude Code** and **Cursor**. Token target: **≤12 assistant turns** per clean conversion; **one `USE_CASE` per chat**.

## Layout

```
SKILL.md                 # slim round budget + hard rules (~1.5–2k tokens)
reference.md             # index only — do not load whole
reference/
  panels.md controls.md vega.md mapping.md
adapters/blackstone.md   # optional customer adapter
scripts/
  parse_inventory.py scaffold_use_case.py mockup_from_inventory.py
  build_from_catalog.py render_mapping.py spotcheck_esql.py
  put_dashboard.py rewire_vega_refs.py estimate_package.py
templates/
  mockup/ MAPPING.md factories/ esql/panels.example.json panels.schema.json
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
python scripts/scaffold_use_case.py --use-case my_dash --out ./elastic
cp dashboard.xml ./elastic/my_dash/source.xml
python scripts/parse_inventory.py --source ./elastic/my_dash/source.xml --out-dir ./elastic/my_dash
python scripts/mockup_from_inventory.py --inventory ./elastic/my_dash/inventory.json --out ./elastic/my_dash/mockup
# edit ./elastic/my_dash/esql/panels.json (start from templates/esql/panels.example.json)
python scripts/build_from_catalog.py --catalog ./elastic/my_dash/esql/panels.json --out ./elastic/my_dash/kibana
python scripts/put_dashboard.py --body ./elastic/my_dash/kibana/<id>.json
python scripts/render_mapping.py --inventory ./elastic/my_dash/inventory.json \
  --catalog ./elastic/my_dash/esql/panels.json --out ./elastic/my_dash/MAPPING.md \
  --queries ./elastic/my_dash/splunk_queries.json
python scripts/estimate_package.py --package ./elastic/my_dash
```

## Token rules (summary)

- Progressive disclosure: open one `reference/*.md` shard per round
- Agent writes small `esql/panels.json`; scripts build HTML/Kibana/MAPPING
- Never paste XML / full SPL / NDJSON / dashboard JSON into chat
- Subagents only when ≥3 factory domains

## Sharing

Share this folder/repo. Keep customer `.env` and PII out. Use `adapters/` for customer paths.
