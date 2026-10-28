---
name: splunk-xml-to-kibana
description: >-
  Migrates a Splunk dashboard (Classic Simple XML or Dashboard Studio JSON) into
  Elastic: Splunk-faithful HTML mockup → Kibana Dashboards API panels → data
  factories → MAPPING.md → deploy with user-supplied Kibana/ES URL and API key.
  Use when migrating Splunk XML/Studio to Kibana/Elastic or wiring panels to
  ES|QL + synthetic data factories.
---

# Splunk XML → Mockup → Kibana → Data Factories

Token-first. Prefer scripts over narrating. Never paste large XML/SPL/NDJSON/dashboard JSON into chat.

**Harness-agnostic** (Claude Code + Cursor). Customer paths: [`adapters/`](adapters/) — load only when relevant.

**Target:** ≤ **12 assistant turns** for a clean conversion when scripts succeed (prefer **2–4** after kickoff: fill ES|QL + `run_pipeline`). **One `USE_CASE` per chat.**

## Parameters (ask once)

| Param | Example | Meaning |
|-------|---------|---------|
| `USE_CASE` | `aws_user_investigation` | Package slug |
| `PACKAGE_ROOT` | `./elastic/<USE_CASE>/` | Artifact root |
| `STREAM` | `logs-<tenant>.<dataset>-*` | Primary index pattern |
| `DASH_ID` | `acme-aws-user-investigation` | Kibana dashboard id |
| `DATA_FACTORIES` | path or empty | Shared factory pkg; else `fields.json` |
| `ADAPTER` | `adapters/blackstone.md` or none | Customer adapter |

Never hard-code machine-absolute paths in customer outputs.

## Gate 0 — credentials (before any deploy)

Ask once: Kibana URL + API key. Echo destination before writes. Prefer env `KIBANA_URL` / `ELASTICSEARCH_URL` / `ELASTICSEARCH_API_KEY`. Never invent keys or read `.env` without user OK. Skip Gate 0 for mockup-only.

If the user already approved credentials (e.g. “use blackstone `.env`”), pass `--assume-env` to [`scripts/run_pipeline.py`](scripts/run_pipeline.py) — echo URLs, do not wait another turn.

## Round budget (hard)

| Round | Work | Load next |
|-------|------|-----------|
| R0 | Params + Gate 0 (or `--assume-env` if already approved) | adapter if any |
| R1 | `parse_inventory.py --from-audit … --name …` — **counts only** | — |
| R2 | `scaffold_use_case.py` + `panels_from_inventory.py` stub | — |
| R3 | Agent fills `esql/panels.json` + `data_factories/fields.json` | [`reference/panels.md`](reference/panels.md), [`reference/controls.md`](reference/controls.md) |
| R4–R7 | `run_pipeline.py --package … --assume-env` (mockup→factory→bulk→spotcheck→build→vega→put→rewire→mapping→tokens) | [`reference/vega.md`](reference/vega.md) **only if** sankey/custom |

**Hard stop:** Vega/UI failure → **one** diagnose batch, then `BUGS.md` fallback or ask user. No open-ended force-graph loops.

## Checklist

```
- [ ] R0 Params + Gate 0 (or --assume-env)
- [ ] R1 parse_inventory --from-audit (counts only)
- [ ] R2 scaffold + panels_from_inventory stub
- [ ] R3 Agent ES|QL + fields.json
- [ ] R4–R7 run_pipeline.py
- [ ] Confirm metrics/token_usage.json; new chat for next USE_CASE
```

## Hard rules

| Topic | Rule |
|-------|------|
| Tabs | No native Kibana tabs → one dashboard per tab + `links` nav |
| Viz map | table→`data_table`; bar/column→`xy`; single→`metric`; punchcard→`heatmap`; gauge→`gauge` |
| Inputs | Every Splunk input → Control / time / **GAP** (MATRIX in MAPPING). Ban hard-coded demo tokens in every `WHERE` |
| Force-directed | → `heatmap` + edge table; see `BUGS.md` |
| Sankey | Vega first (`ref_id` + rewire); use `deploy_vega_sankey.py` / `templates/vega/sankey.py` — never silent collapse |
| Vega fields | Alias dotted paths in ES\|QL before encode (`src`/`dst`/`count`) |
| Output | Link file paths; do not dump MAPPING/dashboard JSON in chat |

## Scripts (prefer these)

| Script | Role |
|--------|------|
| `extract_from_audit.py` | Audit JSON → `source.xml` |
| `parse_inventory.py` | XML → inventory (+ `--from-audit --name`) |
| `scaffold_use_case.py` | Package tree |
| `panels_from_inventory.py` | Stub `esql/panels.json` + `fields.json` skeleton |
| `mockup_from_inventory.py` | HTML mockup from inventory |
| `generate_from_fields.py` | `fields.json` → NDJSON |
| `build_from_catalog.py` | `esql/panels.json` → kibana JSON |
| `spotcheck_esql.py` | Batch `POST /_query` |
| `deploy_vega_sankey.py` | Shared Vega Sankey saved viz |
| `put_dashboard.py` / `rewire_vega_refs.py` | Deploy / Vega rewire |
| `render_mapping.py` | Draft MAPPING.md |
| `run_pipeline.py` | R2–R7 orchestrator (`--assume-env`, `--adapter`) |
| `estimate_package.py` | Warn oversized hand-written builders |
| `record_token_usage.py` | Append turn/run tokens → `metrics/token_usage.jsonl` + summary JSON |

Agent-authored (small): fill stub `PACKAGE_ROOT/esql/panels.json` ES|QL — see `templates/esql/panels.example.json`.

## Token usage (required artifact)

Every conversion **must** leave metrics under `PACKAGE_ROOT/metrics/`:

| File | Role |
|------|------|
| `token_usage.jsonl` | Append-only per-turn / per-hook events |
| `token_usage.json` | Rolled-up totals + vs ≤12-turn / ~70k cost-relevant goals |

**How to populate**

1. **Cursor (preferred):** set `SX2K_PACKAGE_ROOT` to the package, install [`hooks/hooks.json.example`](hooks/hooks.json.example) (`stop` / `afterAgentResponse` → `hooks/cursor_stop_record_tokens.sh`). Each agent stop appends real `input_tokens` / `output_tokens` / cache fields when the hook payload includes them.
2. **R7 manual / Claude:** if hooks are unavailable, record once at end of run:

```bash
export SX2K_PACKAGE_ROOT="$PACKAGE_ROOT"
python scripts/record_token_usage.py --package "$PACKAGE_ROOT" \
  --input-tokens N --output-tokens N \
  --cache-read-tokens N --cache-write-tokens N \
  --model "$MODEL" --source manual --note "R7 end" --print-summary
```

3. **Estimate only (last resort):**  
   `python scripts/record_token_usage.py --package "$PACKAGE_ROOT" --estimate-transcript path/to/chat.jsonl`  
   Label is `transcript_estimate` — undercounts real Cursor context; still better than nothing.

Link `metrics/token_usage.json` from ANALYSIS.md (one line). Do **not** paste full metrics into chat.

## Token rules

- One `USE_CASE` per chat; new chat after deploy URL.
- Never Read `source.xml`, `*.ndjson`, or full `splunk_queries.json` — parser + one-liners for counts.
- Batch independent tools in one turn.
- Subagents only when ≥3 independent factory domains.
- Do not open `reference/vega.md` unless sankey/force/custom viz present.
- Do not open monolithic `reference.md` (index only) — use `reference/*.md` shards.
- Always persist token counts before closing the run (hook and/or R7).

## Package layout

```
PACKAGE_ROOT/
  ANALYSIS.md  MAPPING.md  manifest.json  inventory.json  splunk_queries.json
  esql/panels.json          # agent catalog (start from panels_from_inventory stub)
  metrics/token_usage.jsonl # per-turn events
  metrics/token_usage.json  # run summary (required)
  mockup/  kibana/  data_factories/fields.json  data_factories/out/
```

## Anti-patterns

Deploy without Gate 0 · chat-only mockup · delete mockup after deploy · sticky multi-dashboard chats · pasting XML/SPL into replies · Vega force for `force_directed*` · lens `ref_id` left unrewired · absolute `/Users/…` paths in shared outputs · closing a run without `metrics/token_usage.json` · re-copying customer `build_dashboard.py` Vega instead of `templates/vega/sankey.py`

## References

- Shards: [`reference/panels.md`](reference/panels.md) · [`controls.md`](reference/controls.md) · [`vega.md`](reference/vega.md) · [`mapping.md`](reference/mapping.md)
- Index stub: [`reference.md`](reference.md) · bugs: [`BUGS.md`](BUGS.md)
- Adapter example: [`adapters/blackstone.md`](adapters/blackstone.md)
