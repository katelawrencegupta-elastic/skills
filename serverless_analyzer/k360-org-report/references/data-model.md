# K360 data model (load only if `fetch_org.py` fails)

## Indices
| Pattern | Use |
|---------|-----|
| `cloud-tracker-billing-*` | Daily `cost` (ECU), `type`, `sku`, `deployment.*` |
| `cloud-tracker-organization_pivot` | ARR, `pace.*`, `runout.*`, `consumption.*`, `organization.*` |
| `cloud-tracker-features-*` | `use_case`, `feature.search.*`, o11y/security AI flags |
| `cloud-tracker-deployments` | Top-level `name`, `stack_version`, `template`, `provider`, `hot_instance_type` |
| `cloud-tracker-telemetry` | Per-deployment dataset size / docs (`telemetry.totals.dataset_size_bytes`, `docs_count`) |

Auth: `Authorization: ApiKey $ELASTIC_API_KEY`. `KIBANA_URL` must be https and must not carry a username or password; the fetch stops otherwise.

Org lookup: `match_phrase` on `organization.name` (no `.keyword`), then a case-insensitive `wildcard` prefix (`<name>*`, with `*`, `?` and `\` escaped). The name is never sent through `query_string`. A hit is used only when its name equals `--org` in any case, or when exactly one organization's name starts with it. Any other hit stops the run. After the lookup every search filters on `organization.id`.

Every terms aggregation on `deployment.name` has size 300 (`DEP_TERMS_SIZE`). The two billing ones are ordered by ECU, so a big spender with few billing lines is not dropped. When `sum_other_doc_count` is above 0 the aggregation's name is added to `truncated` in the output (`billing.by_dep`, `billing.by_day.by_dep`, `features.by_dep`, `telemetry.by_dep`, `deployments.inventory`) and a WARN is printed. `truncated` is `[]` when nothing was cut.

## Family ← `type` (lowercased) → `families`, `deployments.items[].families`
- **Search / default ECH:** see `taxonomy.json` → `search`
- **Observability:** `taxonomy.json` → `observability` (note: `integrationsserver`)
- **GenAI:** `inference*`, `agent builder`
- **Other:** any other type, and ECU that no listed type explains
- **Security:** no stable billing `type`. If the latest `use_case` is `security`, that deployment's search, observability and other ECU are Security. Its inference stays GenAI.

The five families partition ECU. They sum to the deployment's `ecu`, and the org `families` sum to `totals.ecu`.

## ECH vs Serverless (per deployment) → `deployments.items[].platform`
1. Any `sku` `serverless.*` with cost → Serverless
2. Else any `type` in `serverless_types` with cost → Serverless
3. Else template in `serverless_templates` and no `Cloud-*` SKU cost → Serverless
4. Else ECH

Skip 32-char hex `deployment.name` values.

## Burndown → `burndown.*`
From the contract pivot (`pace`, `runout`, `consumption`, `organization.contract`):

| Key | Source |
|-----|--------|
| `consumption_rate`, `commit`, `required` | `pace.*` |
| `runway_days`, `runout_date`, `ecu_balance` | `runout.runway_days`, `runout.runout_date` (day), `runout.balance` |
| `daily_30d`, `annual_projection` | `consumption.30d.daily`, `consumption.annual_projection` |
| `nearest_contract_end` | earliest date in `organization.contract.end` |
| `level`, `priority`, `watch`, `reasons` | the rule below; `reasons` lists each test that fired |

`priority` when `consumption_rate > 1`, or `runout_date < nearest_contract_end`, or `runway_days < 90` with rate ≥ 1. `watch` when `runway_days < 90` with rate < 1. `level` is `PRIORITY: OVERAGE`, `WATCH` or `OK`. The report compares `runout_date` with `nearest_contract_end` and says before, after or on; runout after the contract end is not a reason.

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

## Cloud SKUs
`Cloud-<plan>_<provider>.es.<tier>.<instance>_<region>_<ram_mb>_<zones>`. AWS regions have no provider prefix (`Cloud-Enterprise_aws.es.ml.c5d_us-west-2_2048_3`). Azure and GCP regions do (`azure-eastus2`, `gcp-europe-west1`). The parser accepts both and returns the region without the prefix. A Cloud SKU that is not an `es.<tier>` capacity SKU (Kibana, integrations server, legacy instance names) still gives a deployment its provider and region; it does not enter the capacity tables.

## Infrastructure → `infrastructure.*`
From inventory + `Cloud-*` billing SKUs:
- `summary`: primary `provider`, `region`, `hot_instance`, `template`
- `providers` / `regions` / `hot_instance_types` / `templates` (deployment counts)
- `capacity_by_instance` / `capacity_by_tier` / `capacity_by_provider` (ECU)

Show provider + instance at the **top** of the canvas (after header).

## Storage → `storage.*`
Latest `cloud-tracker-telemetry` doc per `deployment.name`. The query has no time filter, so each row says how old it is:
- `data_stored_gb` = `dataset_size_bytes / 1024³`
- `docs_count` = `telemetry.totals.docs_count`
- `avg_doc_kb` = `dataset_size_bytes / docs_count / 1024`
- `calculated_data_stored_gb` = `docs_count × avg_doc_kb / 1024²` (checksum of the above)
- `as_of` = that deployment's snapshot day; `age_days` = range end − `as_of`

Org rollup sums the kept rows. `as_of` is the newest snapshot day and `as_of_oldest` the oldest.

| Key | Meaning |
|-----|---------|
| `stale` | Kept rows whose snapshot is more than `stale_after_days` (45) days older than the range end: `name`, `as_of`, `age_days`, `data_stored_gb`. They still count in org GB. `stale_gb` sums them. |
| `not_billing` | Deployments with a snapshot and no bill in the last `billing_window_days` (90) days of the range: `name`, `as_of`, `data_stored_gb`, `last_billed`, `reason`. They are **not** in `data_stored_gb`, `docs_count` or `by_deployment`. `not_billing_gb` sums them. |
| `notes` | The first note states the stale count when there is one. |

## Workload → `workload.*` (2 ES calls; `--skip-workload` to omit)
Source: `cloud-tracker-index_telemetry-*` (exclude `.` / `partial-` indices). Window ~21d; top ~100 indices by search + by docs.

**Search** (day-average, not peak-second):
- QPS = Σ Δ`index.search_total` / 86400
- Complexity ms = Σ Δ`search_time_ms` / Σ Δ`search_total` → Low `<50` / Medium `<200` / High
- Peak = max day QPS (outliers >4× median dropped); non-peak = median day
- `% hours/mo` = day-share proxy. Idle: a day at or below 1 QPS and at or below p10. Peak: any other day at or above p75. Non-peak: the rest. Each share is 0–100 and the three sum to 100. No hourly series in K360.
- With `--skip-workload` or no index telemetry the shares are `null`. The Search price and the estimator cells then use 26 / 74 / 0, marked `assumed`.

**Ingest**:
- `docs_per_sec` = median daily Σ Δ`docs_count` / 86400
- `avg_doc_bytes` = storage `dataset_size_bytes / docs_count`
- `active_hours_mo` = (days with Δdocs>1k / days) × 730

## Offload → `offload.*` (no extra ES calls)
Classifies each compared dep: `security` | `o11y` | `logs` | `search` (billing family + name hints, tested in that order).

A deployment is compared when its `platform` is `ech` and it has stored GB in `storage.by_deployment`. The rest are in `full_split.not_priced` and on neither side.

ECH $/mo = lookback ECU ÷ `lookback_months`, where `lookback_months` = `--days` ÷ 30.416. ECU is the billed `cost`; 1 ECU is $1.

**Multi-project serverless (`full_split`) — default quote:**
- **Observability:** o11y + logs → Complete ingest+retention (+15% Premium); daily raw = `gb/(months×30.416)`; metered ≈ raw × 1.595
- **Security:** security deps → same Complete ladders as **proxy** until Security estimator linked
- **Search:** search-only GB → ES3 On-demand (storage-floored VCU × hour mix + $0.047/GB retain)
- Totals: `serverless_total_mo` / `full_ech_mo` / `save_vs_ech_{mo,yr,3yr}`; lines in `full_split.lines` (each with `n` deployments and `gb`)
- `full_ech_mo` is the ECH bill of the compared deployments (`priced_n`). `org_ech_mo` is the whole org.
- `not_priced`: `[{name, ecu, ecu_mo, reason}]`, largest first. `reason` is `no stored GB in telemetry`, `already serverless`, or the `storage.not_billing` reason. `not_priced_n`, `not_priced_ecu_mo`, `not_priced_by_reason` (`{reason: {n, ecu_mo}}`) total it.
- `lookback_days`, `lookback_months`: the divisor used.
- **Anti-pattern:** `all_es3` = entire org on ES3 Search (storage-floored VCU); compare via `full_split.vs_all_es3`

Also:
- `by_kind` / `slices`: ECU + stored GB of the compared deployments
- `search_share`: `{search_gb, org_stored_gb, share}`. `org_stored_gb` is `storage.data_stored_gb`. This one share scales Search docs and ingest docs/sec.
- `scenarios` / `primary`: o11y-only retention variants (default `o11y_logs_3mo`) for sheet push
- `hybrid`: o11y SVLS only; search+security stay on ECH. Its `full_ech_mo` is the compared deployments too.
- `apps_script_payload`: ES3 = **search-only** GB; O11y = o11y+logs @3mo — Sheets API (`push_estimator.py`) preferred; Apps Script fallback ([EstimatorImport.gs](apps-script/EstimatorImport.gs))
  - `cloud`: `{provider, region}` from `infrastructure.summary`, region without a provider prefix. Each block repeats it: `o11y.cloud.cell` is `G8`; `es3.cloud.provider_cell` / `region_cell` are `C5` / `C6`. No region string is in `cells`.
  - `es3.layout` is `null`: no ES3 layout is recorded, so the block is not filled. `o11y.layout` is `1.22`.
  - `es3.hour_mix`: `{peak_pct, nonpeak_pct, idle_pct, assumed}`, the mix the Python price used. `es3.assumed` is true when any cell is an assumption. `es3.assumed_cells` and `unmeasured_cells` name them. `o11y.unmeasured_cells` names `G23` / `G25` when there is no Observability slice.
  - Every cell has a value. Not measured is 0.
- `estimator_artifacts` (after push): local + Drive folder `serverless_analyzer/diag_artifacts/<customer>_<date>/`;
  copy named `<customer>_<date>_Observability` (+ `.xlsx` export);
  PDF = `<customer>_<date>_Exec_report.pdf` in that **same** folder (local + Drive).
  - `artifacts[]`: each filled copy with `region_match` (`exact`, `alias`, `none`), `region_option`, `region_wanted`.
  - `es3_not_filled`: why the Search estimator was not filled, or `null`.
- Agent-facing slim files (from `emit_canvas_data.py` / `run_org_report.py`):
  `*_canvas.json`, `*_offload.json`, `*_summary.json` — **never load full org JSON into context**
  - Canvas: `ae`, `hv`, `arr` (from the pivot's `AE`, `HV`, `ARR`), `ecu_mo`, `lookback_days`, `lookback_months`, `ecu_monthly_avg` (same value as `ecu_mo`), `ecu_monthly_avg_calendar`, `months_n` (calendar months), `truncated`. `monthly.by_deployment` is `{name: {ecu, avg, avg_active}}` for the top 8. `ml.vectors` keeps `with_elser`, `with_named_models`, `unnamed_model`, `model_counts`. `storage` keeps `stale` and `not_billing` (largest 15 each) with exact `stale_n` and `not_billing_n`.
  - Offload: `full_split` with `not_priced` (largest 25; the counts cover all), `search_share`.
  - Summary: `ecu_mo`, `compared_deployments`, `not_compared_deployments`, `not_compared_ecu_mo`, `caveats`, `chat_bullets_hint` (at most 5; the last one is the caveats).
- `--report exec|full` selects PDF stem (`*_Exec_report` / `*_Full_report`) and canvas layout
  (see `references/exec-report.md`, `references/full-report.md`)

`emit_estimator_import.py --org-json … --kind o11y|both` prints clipboard JSON for the Apps Script importer: the Observability block with its `layout` tag and `cloud`. `--kind es3` stops until an ES3 layout is recorded.

## Versions
`deployments.by_version`: `[{version, count}, …]` sorted by count desc — feed `PieChart` (deployment counts, not ECU).

## Deployments over time
`deployments.over_time`: `{ note, week: { periods, count }, month: { periods, count } }` — distinct named `deployment.name` with billing `cost` > 0 in each week/month (hex ids excluded). Feed `LineChart` + table (counts, not ECU).

## Billed windows → `chart.billed_window`, `chart.billed_windows`
`chart.billed_windows` is `{"30": {...}, "90": {...}}`. Each entry is `{start, end, days, names}`: the named deployments with a bill on the last 30 or 90 billed days. The window ends on the last day with a positive org total, and days before the org's first bill are not counted. `chart.billed_window` is the 30-day entry, kept for older readers. The names come from the per-day roster, which has the same size and ECU order as the lookback roster.

## Features (latest `top_hits` per dep)
`elser`, `genai_connector`, `playground`, `vector_mappings`, `observability.ai_assistant`, `security.attack_discovery`

## Monthly figures (script)
`totals.ecu_mo` and `deployments.items[].ecu_mo` = lookback ECU ÷ `totals.lookback_months` (`totals.lookback_days` ÷ 30.416). This is the report's "ECU / mo" and the §10 divisor.

`totals.ecu_monthly_avg` and `deployments.items[].ecu_monthly_avg` = sum(calendar-month ECU) / `totals.months`, the number of calendar months the range touches (13 for 365 days; partial months included). `k360-account-analysis` reads these two and `totals.months`; they are unchanged.

`ecu_monthly_avg_active` = same but only months with spend > 0. Series in `monthly.*`. `monthly.by_deployment` is `{name: {months, ecu, avg, avg_active}}`.

## Top-level keys
`generated_at`, `range` (`start`, `end`, `days`), `organization`, `license`, `burndown`, `infrastructure`, `storage`, `workload`, `offload`, `deployments`, `families`, `use_case_feature_docs`, `ai`, `ml`, `chart`, `monthly`, `totals`, `truncated`.
