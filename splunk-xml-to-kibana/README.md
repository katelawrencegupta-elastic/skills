# Splunk XML → Kibana (Cursor Agent Skill)

Token-first Cursor Agent skill that migrates a Splunk dashboard (Classic Simple XML or Dashboard Studio) into Elastic:

1. Parse Splunk → panel inventory  
2. Splunk-faithful HTML mockup site → `elastic/<use_case>/mockup/` (required artifact; optional `/visualize` preview)  
3. Kibana Dashboards API panels (ES|QL `vis` types)  
4. Data factories for demo/synthetic data  
5. **`MAPPING.md`** — SPL ↔ ES|QL + why, DIMA/audit JSON used, factories  
6. Deploy with user-supplied Kibana/ES URL + API key (Gate 0)

## Install (Cursor Desktop)

Copy this folder into your personal skills directory:

```bash
mkdir -p ~/.cursor/skills
cp -R . ~/.cursor/skills/splunk-xml-to-kibana
```

Or clone and symlink:

```bash
git clone <this-repo-url> ~/src/splunk-xml-to-kibana
ln -s ~/src/splunk-xml-to-kibana ~/.cursor/skills/splunk-xml-to-kibana
```

Restart or start a new Agent chat so Cursor picks up the skill. Trigger by asking to migrate a Splunk dashboard XML to Kibana/Elastic.

## Contents

| File | Purpose |
|------|---------|
| `SKILL.md` | Main skill instructions (workflow, Gate 0, package layout, MAPPING.md) |
| `reference.md` | Dashboards API panel snippets, factory notes, MAPPING.md template |

## Expected inputs (DIMA / Splunk audit)

Minimum: dashboard XML (or `dashboard_to_migrate.json` with `eai:data`).  
When SPL uses macros/lookups: `macros.json`, optionally `lookup_samples.json`.

## Outputs (per use case)

Typical package under a customer repo:

```
# Generators: data_factories package (pip install -e …/data_factories)
elastic/<use_case>/
  ANALYSIS.md
  MAPPING.md
  manifest.json
  splunk_queries.json
  mockup/           # required: Splunk HTML site (index.html + optional tabs/)
  kibana/
  data_factories/   # thin wrappers + local out/
  setup_*.py
```

## Worked examples (Blackstone POC)

Examples live in customer repos (not this skill package), e.g.:

- `elastic/hdsi_user_investigation/` — lookups + macros + datamodels + MAPPING.md  
- `elastic/network_egress_exfil/` — lookups + macros  
- `elastic/ioc_threat_hunt/`, `elastic/secops_aws_blast_radius/`

## Sharing

This Cursor-hosted copy is **not public**. Manage who can access it from the repo page on Cursor. Teammates install via clone + the steps above.

## License

Internal / team use unless otherwise noted by the owners.
