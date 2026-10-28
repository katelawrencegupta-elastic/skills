# splunk-xml-to-kibana — known bugs

## Open

### BUG-1: Kibana Vega force / particle graphs unreliable on Dashboards API

| Field | Value |
|---|---|
| **Status** | Open |
| **Severity** | High (blocks Splunk-parity for `force_directed*`) |
| **Skill impact** | Skill defaults `force_directed*` → `heatmap` + edge table (GAP) |
| **GitHub** | https://github.com/katelawrencegupta-elastic/skills/issues/2 |
| **Repro package** | Customer AWS blast-radius style dashboards (VPC edges + Vega) |

**Summary**

Splunk `force_directed_viz` / Verlet particle graphs need a node-link force layout.
Kibana Dashboards + Vega embeds on Elastic Cloud serverless do not yet provide a
stable recipe via the Dashboards API (`2023-10-31`).

**Observed failures** (Blast Radius, Oct 2026)

| Attempt | Result |
|---|---|
| `vis` + `config.ref_id` to classic Vega SO | Stored as `lens` → “visualization type not found” (needs SO rewire to `visualization`) |
| Mark-level `force` + `linkpath` / path on `datum.source.x` | `reading 'id'`; `Invalid SVG path, incorrect parameter type` |
| Data-transform `force` + `rule` lookup + `static: false` | Still failed to render a usable particle graph on target cluster |
| Dotted ES|QL fields (e.g. `cloud.account.name`) in Vega aggregates | `Cannot read properties of undefined (reading '…')` |

**Current skill workaround**

- P0: Dashboards `heatmap` (src×dest `COUNT`) + companion edge `data_table`
- Keep Splunk title; document GAP in `MAPPING.md`
- Do not block conversions on inventing a force recipe

**Desired fix**

Document and ship a **proven** Kibana-native or Vega recipe that:

1. Renders an animated or settled node-link particle graph from ES|QL edge aggregates
2. Binds dashboard ES|QL Controls (`?params`) and time range
3. Deploys cleanly via Dashboards API (inline or saved viz + correct SO type)
4. Survives serverless Kibana versions used by Elastic Cloud customers

Then flip skill P0 from heatmap → that recipe and close this bug.

**References**

- Skill: `SKILL.md` § Custom viz → Vega; `reference.md` § Force-directed → heatmap
- See adapter notes for customer-specific GAP examples
