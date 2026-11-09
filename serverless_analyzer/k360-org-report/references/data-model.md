# K360 data model (load only if `fetch_org.py` fails)

## Indices
| Pattern | Use |
|---------|-----|
| `cloud-tracker-billing-*` | Daily `cost` (ECU), `type`, `sku`, `deployment.*` |
| `cloud-tracker-organization_pivot` | ARR, `pace.*`, `runout.*`, `consumption.*`, `organization.*` |
| `cloud-tracker-features-*` | `use_case`, `feature.search.*`, o11y/security AI flags |
| `cloud-tracker-deployments` | Top-level `name`, `stack_version`, `template`, `provider`, `hot_instance_type` |
| `cloud-tracker-telemetry` | Per-deployment dataset size / docs (`telemetry.totals.dataset_size_bytes`, `docs_count`) |

Auth: `Authorization: ApiKey $ELASTIC_API_KEY`. Org filter: `match_phrase` on `organization.name` (no `.keyword`).

## Family ← `type` (lowercased)
- **Search / default ECH:** see `taxonomy.json` → `search`
- **Observability:** `taxonomy.json` → `observability` (note: `integrationsserver`)
- **GenAI:** `inference*`, `agent builder`
- **Security:** no stable billing `type` — if latest `use_case=security`, attribute that deployment’s total ECU to Security; else 0 + report use_case counts

## ECH vs Serverless (per deployment)
1. Any `sku` `serverless.*` → Serverless  
2. Else dominant types in `serverless_types` + template in `serverless_templates` → Serverless  
3. Else ECH  

Skip 32-char hex `deployment.name` values.

## Overage
PRIORITY if: `pace.consumption_rate > 1` OR `runout_date <` nearest `organization.contract.end` OR (`runway_days < 90` and rate ≥ 1). Else WATCH if runway < 90.

## AI / embeddings (billing SKUs)
| SKU fragment | Meaning |
|--------------|---------|
| `inference-chat-input_<model>` / `inference-chat-output_<model>` | EIS input/output tokens. The model id is the suffix (`rainbow-sprinkles` = Elastic Managed LLM, `anthropic-claude-4-6-sonnet`, …). No suffix means the bill does not name a model. `quantity` is millions of tokens; ECU per million = `cost / quantity`. |
| `inference-embed-text-dense_*` | EIS dense embed model id after `_` (e.g. `jina-embeddings-v5-text-small`). `quantity` is millions of tokens. |
| `feature.search.genai_connector` | bool only — provider name not in K360 |
| `feature.search.elser` | In-cluster ELSER flag; dense model id not in K360 |

## ML nodes (billing SKUs) → `ml.*` in fetch output
| SKU fragment | Meaning |
|--------------|---------|
| `*.es.ml.{instance}_{region}_{ram_mb}_{nodes}` | In-cluster ML node capacity ECU (separate from EIS inference) |

Script rollup: `ml.total_ecu`, `ml.pct_of_org`, `ml.skus` (decoded labels), `ml.by_deployment`. Do not fold into `families.genai`.

`ml.vectors` — inventory for canvas ML section: deps with `vector_mappings` / ELSER / named EIS models / ML nodes; `model_counts` across vector deps; `unnamed_model` = vectors with neither ELSER nor billing model id.

## Infrastructure → `infrastructure.*`
From inventory + `Cloud-*` billing SKUs:
- `summary`: primary `provider`, `region`, `hot_instance`, `template`
- `providers` / `regions` / `hot_instance_types` / `templates` (deployment counts)
- `capacity_by_instance` / `capacity_by_tier` / `capacity_by_provider` (ECU)

Show provider + instance at the **top** of the canvas (after header).

## Storage → `storage.*`
Latest `cloud-tracker-telemetry` doc per `deployment.name`:
- `data_stored_gb` = `dataset_size_bytes / 1024³`
- `docs_count` = `telemetry.totals.docs_count`
- `avg_doc_kb` = `dataset_size_bytes / docs_count / 1024`
- `calculated_data_stored_gb` = `docs_count × avg_doc_kb / 1024²` (checksum of the above)

Org rollup sums latest per-dep snapshots; `as_of` = max snapshot date.

## Workload → `workload.*` (2 ES calls; `--skip-workload` to omit)
Source: `cloud-tracker-index_telemetry-*` (exclude `.` / `partial-` indices). Window ~21d; top ~100 indices by search + by docs.

**Search** (day-average, not peak-second):
- QPS = Σ Δ`index.search_total` / 86400
- Complexity ms = Σ Δ`search_time_ms` / Σ Δ`search_total` → Low `<50` / Medium `<200` / High
- Peak = max day QPS (outliers >4× median dropped); non-peak = median day
- `% hours/mo` = day-share proxy (`qps≥p75` → peak; idle only if a ~0-search day exists). No hourly series in K360.

**Ingest**:
- `docs_per_sec` = median daily Σ Δ`docs_count` / 86400
- `avg_doc_bytes` = storage `dataset_size_bytes / docs_count`
- `active_hours_mo` = (days with Δdocs>1k / days) × 730

## Offload → `offload.*` (no extra ES calls)
Classifies each dep: `o11y` | `logs` | `security` | `search` (billing family + name hints).

**Multi-project serverless (`full_split`) — default quote:**
- **Observability:** o11y + logs → Complete ingest+retention (+15% Premium); daily raw = `gb/(months×30.416)`; metered ≈ raw × 1.595
- **Security:** security deps → same Complete ladders as **proxy** until Security estimator linked
- **Search:** search-only GB → ES3 On-demand (storage-floored VCU × hour mix + $0.047/GB retain)
- Totals: `serverless_total_mo` / `full_ech_mo` / `save_vs_ech_{mo,yr,3yr}`; lines in `full_split.lines`
- **Anti-pattern:** `all_es3` = entire org on ES3 Search (storage-floored VCU); compare via `full_split.vs_all_es3`

Also:
- `by_kind` / `slices`: ECU + stored GB
- `scenarios` / `primary`: o11y-only retention variants (default `o11y_logs_3mo`) for sheet push
- `hybrid`: o11y SVLS only; search+security stay on ECH
- `apps_script_payload`: ES3 = **search-only** GB; O11y = o11y+logs @3mo — Sheets API (`push_estimator.py`) preferred; Apps Script fallback ([EstimatorImport.gs](apps-script/EstimatorImport.gs))
- `estimator_artifacts` (after push): local + Drive folder `artifacts/<customer>_<date>/`;
  copies named `<customer>_<date>_{Search|Observability}` (+ `.xlsx` export);
  PDF = `<customer>_<date>_Exec_report.pdf` in that **same** folder (local + Drive).
- Agent-facing slim files (from `emit_canvas_data.py` / `run_org_report.py`):
  `*_canvas.json`, `*_offload.json`, `*_summary.json` — **never load full org JSON into context**
- `--report exec|full` selects PDF stem (`*_Exec_report` / `*_Full_report`) and canvas layout
  (see `references/exec-report.md`, `references/full-report.md`)

`emit_estimator_import.py --org-json … --kind o11y|es3|both` prints clipboard JSON.

## Versions
`deployments.by_version`: `[{version, count}, …]` sorted by count desc — feed `PieChart` (deployment counts, not ECU).

## Deployments over time
`deployments.over_time`: `{ note, week: { periods, count }, month: { periods, count } }` — distinct named `deployment.name` with billing `cost` > 0 in each week/month (hex ids excluded). Feed `LineChart` + table (counts, not ECU).

## Features (latest `top_hits` per dep)
`elser`, `genai_connector`, `playground`, `vector_mappings`, `observability.ai_assistant`, `security.attack_discovery`

## Monthly average (script)
`ecu_monthly_avg` = sum(calendar-month ECU) / N months in lookback (partial months included).  
`ecu_monthly_avg_active` = same but only months with spend > 0. Series in `monthly.*`.
