# splunk-xml-to-kibana

Portable Agent Skill: migrate Splunk dashboards (Classic Simple XML or Dashboard Studio) to Elastic — mockup → ES|QL panels → data factories → `MAPPING.md` → deploy.

Works in **Claude Code** and **Cursor** (same `SKILL.md` format).

## Layout

```
splunk-xml-to-kibana/
  SKILL.md                 # parameterized workflow (no customer absolute paths)
  reference.md             # Dashboards API snippets, Vega, MAPPING template detail
  BUGS.md                  # platform limitations (e.g. Vega force)
  adapters/
    blackstone.md          # optional one-page customer adapter
  scripts/
    parse_inventory.py     # XML → inventory.json + splunk_queries.json
    put_dashboard.py       # PUT/GET Dashboards API
    rewire_vega_refs.py    # lens → visualization SO rewire
    scaffold_use_case.py   # create PACKAGE_ROOT skeleton
  templates/
    mockup/                # Splunk-faithful HTML starter
    MAPPING.md
    factories/minimal_ndjson.py
    vega_panel_map.example.json
```

## Install

### Claude Code

```bash
git clone https://github.com/katelawrencegupta-elastic/skills.git
ln -s "$(pwd)/skills/splunk-xml-to-kibana" ~/.claude/skills/splunk-xml-to-kibana
```

Or copy the folder into a project: `.claude/skills/splunk-xml-to-kibana/`.

### Cursor

```bash
mkdir -p ~/.cursor/skills
ln -s /path/to/skills/splunk-xml-to-kibana ~/.cursor/skills/splunk-xml-to-kibana
```

### claude.ai

Zip this folder (must include `SKILL.md` at the zip root) → Settings → Features → upload skill.  
Note: claude.ai code execution may have limited network — treat Gate 0 deploy as optional; mockup + MAPPING still apply.

## Quick start

```bash
# 1) Scaffold
python scripts/scaffold_use_case.py --use-case my_dash --out ./elastic

# 2) Drop Splunk XML in place, parse
cp /path/to/dashboard.xml ./elastic/my_dash/source.xml
python scripts/parse_inventory.py --source ./elastic/my_dash/source.xml --out-dir ./elastic/my_dash

# 3) Follow SKILL.md checklist (mockup → ES|QL → factories → MAPPING → Gate 0 deploy)
python scripts/put_dashboard.py --body ./elastic/my_dash/kibana/my-dash.json
```

## Parameters

Resolved per conversion (see `SKILL.md`):

| Param | Meaning |
|-------|---------|
| `USE_CASE` | Package slug |
| `PACKAGE_ROOT` | `./elastic/<USE_CASE>/` by default |
| `STREAM` / `DASH_ID` | Data + Kibana ids |
| `DATA_FACTORIES` | Shared package path, or skill templates |
| `ADAPTER` | e.g. `adapters/blackstone.md` when applicable |

## Sharing

- **Share this skill folder / git repo** with Claude and Cursor users.
- Keep customer trees, `.env`, and PII **out** of the skill.
- Customer-specific env and naming go in `adapters/<name>.md`.

## License

Internal / team use unless otherwise noted by the owners.
