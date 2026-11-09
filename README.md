# Field Engineering Skills

Six agent skills for Elastic account work. Each one packages a repeatable workflow so the agent follows the same steps, produces the same output format, and spends as few tokens as possible doing it.

| Skill | Runs in | What it produces | Needs |
|---|---|---|---|
| [account-status-update](#account-status-update) | Claude (`.skill`) | Account status summary + activity timeline, optionally as a Google Doc | Slack, Gmail, Google Calendar (Drive for docs) |
| [customer-tech-research](#customer-tech-research) | Claude (`.skill`) | Confidence-rated tech-stack report + Elastic opportunity assessment | Web search, web fetch |
| [splunk-xml-to-kibana](#splunk-xml-to-kibana) | Claude Code + Cursor | Splunk dashboard migrated to Kibana: mockup, ES\|QL panels, data factories, `MAPPING.md`, deploy | Kibana/ES URL + API key (optional shared `data_factories`) |
| [k360-org-report](#k360-org-report) | Cursor | K360 org fetch, exec or full report, Sheets auth | K360 `.env`, Sheets venv |
| [k360-account-analysis](#k360-account-analysis) | Cursor | K360 account summary: footprint, ECU, storage, burndown, projections | K360 `.env`, `k360-org-report` |
| [diag-serverless-consolidation](#diag-serverless-consolidation) | Cursor | Serverless consolidation priced from diagnostic bundles only | Account-summary artifacts, admin diagnostics, Sheets venv, es3-estimator |

---

## Repository layout

```
account-status-update/
  account-status-update.skill        # zip: SKILL.md
customer-tech-research/
  customer-tech-research.skill       # zip: SKILL.md + references/industry-angles.md
splunk-xml-to-kibana/
  SKILL.md                           # slim round budget (≤12 turns; Claude Code + Cursor)
  reference/                         # panels, controls, vega, mapping shards
  adapters/                          # optional customer one-pagers (e.g. blackstone.md)
  scripts/                           # parse, mockup, catalog build, mapping, spotcheck, deploy
  templates/                         # mockup, MAPPING, factories, esql/panels.example.json
  README.md                          # install + script quick start
serverless_analyzer/
  k360-org-report/                   # K360 org fetch; exec or full report
  k360-account-analysis/             # account summary; --skip-diags --skip-sizing
  diag-serverless-consolidation/     # diag bulk + measured retention → estimator read-back
.cursor/rules/commits.mdc            # commit conventions for agents working in this repo
```

## Installing

**Claude skills (`.skill`).** Open the `.skill` file in Claude and click **Save skill**. A `.skill` file is a zip, so you can rename it to `.zip` to inspect or edit its contents, then re-zip it with the `.skill` extension.

**Claude Code (filesystem skill).** Symlink or copy folders that ship `SKILL.md` (not only `.skill` zips):

```bash
mkdir -p ~/.claude/skills
ln -s "$(pwd)/splunk-xml-to-kibana" ~/.claude/skills/splunk-xml-to-kibana
ln -s "$(pwd)/serverless_analyzer/k360-org-report" ~/.claude/skills/k360-org-report
ln -s "$(pwd)/serverless_analyzer/k360-account-analysis" ~/.claude/skills/k360-account-analysis
ln -s "$(pwd)/serverless_analyzer/diag-serverless-consolidation" ~/.claude/skills/diag-serverless-consolidation
```

**Cursor skill.** Copy or symlink the folder into your personal skills directory, then start a new Agent chat:

```bash
mkdir -p ~/.cursor/skills
ln -s "$(pwd)/splunk-xml-to-kibana" ~/.cursor/skills/splunk-xml-to-kibana
ln -s "$(pwd)/serverless_analyzer/k360-org-report" ~/.cursor/skills/k360-org-report
ln -s "$(pwd)/serverless_analyzer/k360-account-analysis" ~/.cursor/skills/k360-account-analysis
ln -s "$(pwd)/serverless_analyzer/diag-serverless-consolidation" ~/.cursor/skills/diag-serverless-consolidation
```

The serverless skills also have a guided installer. From `serverless_analyzer/`, run `python3 install.py`. The steps are `serverless_analyzer/INSTALL.md` and the script catalog is `serverless_analyzer/README.md`.

---

## How these skills save tokens

The same few principles show up in these skills:

1. **Fewer round trips.** Every assistant turn re-reads the whole conversation, so the number of turns costs more than the size of any single result. The skills batch independent tool calls into one message and set an explicit round budget.
2. **Load only what's needed.** Descriptions are written so the skill triggers on its own. Heavy reference material sits in separate files (`references/industry-angles.md`, `reference/*.md` shards) and is read only when a step needs it.
3. **Filter before reading.** Queries exclude noise at the source (Gmail category/subject filters, year cutoffs, specific tool names), and results are screened from snippets or metadata before any full fetch.
4. **Hard caps and early exits.** Fixed page sizes, fetch limits, per-page token budgets, and stop conditions keep a run from drifting.
5. **Work offline, not in chat.** Large payloads (oversized tool results, Splunk XML, SPL) are parsed by scripts and saved to files. Only compact summaries come back into the conversation.
6. **Fixed output templates.** Each skill has an exact report or file structure, so the agent spends nothing designing a format and the output is the same every run.
7. **Catalog / script codegen.** Artifact-heavy skills (e.g. splunk-xml-to-kibana) have the agent edit a small JSON catalog; scripts emit HTML, Kibana JSON, and draft MAPPING — avoid pasting multi-hundred-line builders into chat.
8. **One task per chat** for long workflows (one Splunk `USE_CASE` per conversion chat). Sticky multi-dashboard threads dominate token cost.

---

## account-status-update

**What it does:** Builds a status summary and activity timeline for one named account over a time window, using Slack, Gmail, and Google Calendar. It can optionally save the result as a Google Doc.

**Usage**

```
status update <account> [period] [googledoc=y|n]
```

Examples: `status update GE Vernova 3m`, `status update Merck 2w googledoc=y`. The period takes `d`, `w`, `m`, or `y` (default `1m`), and the doc option defaults to no. It also triggers on informal requests such as "what's been happening with Pfizer?"

**How it works**

| Round | What happens | When |
|---|---|---|
| R0 | Load the skill and its tools in one message | Always |
| R1 | **Gather:** every Slack, Gmail, and Calendar call for all time slices, in one message | Always |
| R2 | **Follow-up:** truncated threads, channel fallback, empty-result retries, next pages, in one message | Only when a trigger fires |
| R3 / R4 | **Publish:** report + `create_file`, then the doc link | Doc runs only |

1. **Parse.** Reads the account, period, and doc flag from the command. Resolves the account against the account map (§5), which holds aliases, domain, Slack channel, volume flag, and AE. If one alias contains another (for example "Vernova" inside "GE Vernova"), only the shorter one is searched.
2. **Slice.** Long windows are split into parallel time slices instead of paginating one after another. High-volume accounts get finer Slack slices.
3. **Gather.** Slack keyword search (concise format, 300-char context, 20 results), Gmail `search_threads` (minimal thread view, 15 per page), and one Calendar `list_events` call.
4. **Follow up only on triggers:** Gmail threads with more than 5 messages (max 3, minimal format), no hits from the customer's Slack channel, an empty calendar, no results anywhere, or a slice that filled its page.
5. **Synthesize.** Drops noise and cross-source duplicates, marks unconfirmed meetings, and writes a fixed report: a status line (On track / Watch / At risk) with the AE, a summary of up to 120 words, a timeline (daily rows up to 1 month, weekly after that), open items and risks, and meetings in the next 7 days.

**Token optimizations**

- **Round budget:** 2–3 rounds for a plain run, 4–5 with a doc. Before R1 the agent writes down its planned call count and checks the message contains exactly that many calls.
- **Tool loading is batched with skill loading.** The description includes the exact `tool_search` query so both happen in one turn.
- **Gmail exclusions** (promotions, social, updates, invites, auto-replies, newsletters) cut Gmail tokens by about 40% in testing without losing signal.
- **Calendar is capped** at one call covering the last 30 days plus the next 7, because event payloads are the heaviest per item and older meetings already show up in email and Slack.
- **Oversized results are never read whole.** A one-shot Python extraction prints one line per event.
- **No channel calls in R1.** Keyword search already returns channel messages that name the account; the channel is only queried as a fallback.
- **No prose between rounds.** The report is the only text the agent writes.

**Maintain:** Edit the account map in `SKILL.md` §5 to add accounts, aliases, domains, Slack channel IDs, `high` volume flags, and AE owners.

---

## customer-tech-research

**What it does:** Researches a customer's public technology footprint across search, observability (logs, metrics, traces, APM), and security (SIEM, EDR, IAM, vulnerability management). It produces a confidence-rated intelligence report and an Elastic opportunity assessment.

**Usage:** Ask in plain language, for example "research Moody's tech stack", "what tools does TIAA use?", or "prep for a Regeneron meeting". You can name the industry and focus area; otherwise the skill infers the industry and covers all three areas.

**How it works**

1. **Search.** Three queries run in parallel: vendor case studies (PDFs preferred), job postings that name tools, and security press. A fourth, industry-specific query from `references/industry-angles.md` runs only if the first three return fewer than 3 useful snippets.
2. **Pre-filter.** Discards any result that doesn't name a specific tool tied to the customer, and anything from 2022 or earlier.
3. **Claim from snippets.** A snippet with a tool name, a direct customer link, and an attributable source/date is recorded as MEDIUM confidence without fetching the page.
4. **Rank and fetch.** Remaining URLs are ranked by token efficiency (PDFs, then vendor sites, then press, then job boards, then the customer's own site) and fetched within hard limits.
5. **Extract.** Takes the tool name plus 1–2 sentences of context and stops reading.
6. **Verify.** **HIGH** = 2+ independent sources, **MEDIUM** = 1 directly attributed source, **LOW** = inferred. Syndicated copies of the same content don't count as independent.
7. **Report.** A fixed template: executive summary; confirmed tools, likely tools, and gaps for each of the three areas; Elastic opportunity (stack gaps, strongest entry point, competitive risk); and sources.

**Token optimizations**

- **Conditional fourth query:** skipped when the first three already give enough signal.
- **Snippet-only claims:** many findings never need a page fetch.
- **Fetch caps:** at most 5 URLs, at most 2 from the customer's own domain, and fetching stops once 3 tool claims are confirmed.
- **Domain blocklist:** script-heavy sites with duplicated navigation (confluent.io, medium.com, substack.com, dev.to, hashnode.com, and vendor `/blog/`, `/resources/`, `/events/` paths) are never fetched.
- **Nav timeout:** if 800 tokens go by with no customer or tool name, the page is dropped.
- **Duplicate-nav detection:** when a nav block repeats, only the content between the first and second copy is read.
- **300-token content budget** per page after the nav.
- **No progress tracker or task list.** The skill runs straight to the report.

**Maintain:** Update `references/industry-angles.md` to change per-vertical query templates, priority vendor sites, compliance frameworks, and common tool patterns.

---

## splunk-xml-to-kibana

**What it does:** Migrates a Splunk dashboard (Classic Simple XML or Dashboard Studio JSON) into Elastic. It builds a mockup that looks like the original Splunk dashboard, Kibana dashboards built through the Dashboards API with ES|QL panels, synthetic data factories that populate every panel, a required `MAPPING.md`, and deploys everything to a cluster you choose.

**Usage:** In Claude Code or Cursor, give it Splunk dashboard XML (or a DIMA/audit export such as `dashboard_to_migrate.json`, plus `macros.json` and `lookup_samples.json` when the SPL uses them) and ask to migrate it to Kibana. Optional customer adapters live under `adapters/` (e.g. Blackstone).

**How it works**

1. **Parameters + Gate 0.** Resolves `PACKAGE_ROOT` / `USE_CASE` / streams; asks once for Kibana URL and API key before any cluster write. Never guesses credentials or reads `.env` without permission. Mockup-only work skips Gate 0.
2. **Parse offline.** `scripts/parse_inventory.py` turns XML into a compact inventory. Full SPL goes to `splunk_queries.json`, not the chat.
3. **Mockup.** HTML under `PACKAGE_ROOT/mockup/` (from `templates/mockup/` if needed).
4. **Panel map.** Splunk chart types map to Kibana 9.4+ `vis` types. Studio tabs → one dashboard per tab + `links` nav. Inputs → Controls (`?params`).
5. **Data factories.** Shared package when available, else `templates/factories/`. Every panel needs a P0 producer; validate with `POST /_query`.
6. **MAPPING.md (required).** SPL ↔ ES|QL + why, Controls matrix, gaps. Template: `templates/MAPPING.md`.
7. **Deploy.** `scripts/put_dashboard.py`; Vega panels via saved viz + `scripts/rewire_vega_refs.py`.

**Token optimizations**

- **"Token-first" rule:** prefer scripts and APIs over narration, and never paste large XML into chat.
- **Compact inventory:** only the parsed panel table comes back into context. SPL, macros, and dashboard JSON live in files.
- **Split reference:** `reference.md` (API snippets, factory notes, MAPPING.md template) is opened only when building or deploying panels.
- **Files over chat:** `MAPPING.md` and analysis are written to disk and linked, never pasted.
- **Short mockup prose:** the mockup step keeps text to 3 sentences or fewer.
- **Reusable data factories:** generators come from a shared package rather than being rewritten per dashboard.
- **Export instead of regenerate:** after UI edits, dashboards are exported and redeployed instead of rebuilt from templates.

**Maintain:** Edit `SKILL.md` for workflow rules and the chart-type mapping, and `reference.md` for Dashboards API panel snippets and the `MAPPING.md` template. Factory implementations live in the separate `data_factories` repo.

---

## k360-org-report

**What it does:** Fetches one Elastic Cloud organization from K360 and writes the exec or full org report. `fetch_org.py` and `sheets_auth.py` are the parent dependencies for the account summary and the diagnostic consolidation.

**Usage:** `python scripts/run_org_report.py --org "ORG NAME" --report exec`. `--report full` expands the same fetch. `--reuse` skips a second K360 query.

**Maintain:** Workflow rules live in `SKILL.md`. Setup is `references/setup.md`. OAuth files stay in `~/.config/cloud-tracker-sheets/` and are not in this folder.

## k360-account-analysis

**What it does:** Runs one Elastic Cloud account through K360 and stops at the account summary: ECH versus Serverless mix, cloud and instance footprint, versions, ECU, storage, solution mix, ML and inference, a burndown check, 3- and 6-month projections, and up to five performance and five cost recommendations. It does not pull diagnostic bundles or quote Serverless.

**Usage:** In Cursor, ask for an account summary, for example "run this skill for Colgate-Palmolive". The script resolves the K360 name and exits if the match is not unique. The run passes `--skip-diags --skip-sizing`.

**How it works**

1. **One pipeline run.** `scripts/run_account_analysis.py` fetches the org, fills storage when telemetry is missing, then analyzes the footprint.
2. **No sizing step.** Estimator copies and the consolidation PDF belong to `diag-serverless-consolidation`.
3. **Write the summary.** Account PDF, then a canvas and at most six chat bullets from the brief.

**Token optimizations**

- **One compact brief.** The command prints `brief <path>`. The agent does not open `account.json`, the markdown report, the org report, or infra.
- **Name resolution fails closed.** A fuzzy name does not fetch a different customer.

**Maintain:** Workflow rules live in `SKILL.md`. The parent fetch is `k360-org-report` in this repo (`K360_ORG_SKILL_DIR` overrides the path). Setup is `references/setup.md`.

## diag-serverless-consolidation

**What it does:** Prices a Serverless consolidation from Elasticsearch diagnostic bundles for deployments that billed in the latest month. A decommissioned deployment and its last snapshot stay out. Ingest is bulk GB/day. Retention is the measured age, rounded up to a whole month. A solution with no stored GB and no bulk is skipped because it has no workload. The run writes two PDFs from the same bulk: the rounded month, and 30 days written as 1 month. It also writes `compressioncalc_accounted_for`, bulk multiplied by the compression ratio, as its own rounded and 30-day pair. That pair does not replace the bulk quote. Each PDF links to its own estimator sheet. Measured-retention PDFs go in `full_retention`. 30-day PDFs go in `30d`. The page after the cover puts horizontal bars for cutting the deployment count by 0, 25, 50, 75, and 100% down the left column, with the project count and monthly dollars on each row, and the component tables on the right. A later page, Deployment consolidation analysis across metered ingest rates, lists each deployment's metered ingest rate and marks $0.50 rows in red. The search-power chart is the Appendix, the last page, and is not added to the quote. A data-egress page lists hosted data in, data out, and inter-node. Data out carries over outside the estimator; inter-node does not.

**Usage:** Ask for a diagnostic consolidation, a per-cluster Serverless price, or a 2–5 project pack. Reuse the account-summary org report when that folder already exists.

**How it works**

1. **One pipeline run** of `serverless_analyzer/diag-serverless-consolidation/scripts/run_consolidation.py`. It reuses the account-summary fetch, then pulls and prices the bundles. `--diags-dir` reuses zips already on disk. The active month is the latest lookback month, including a partial month.
2. **Two sheets, two PDFs.** The rounded PDF writes the measured months, rounded up. The 30-day PDF writes 1 month on a separate sheet and leaves that sheet at 1 month. The deployment-count bars are priced again at that scenario's month.
3. **Quote the read-back.** Show the arithmetic beside D32 (Observability), E74 (Security), or the es3 on-demand total (Search). The metered-rate table and the search-power chart are shown beside that quote. Deployments with no bundle add no ingest.

**Maintain:** Workflow rules live in `SKILL.md`. The arithmetic is `references/calculations.md`. The cell map is `references/sizing-cells.md`. Setup stays in `serverless_analyzer/k360-account-analysis/references/setup.md`. The quote scripts live in this skill. The account fetch stays in `serverless_analyzer/k360-account-analysis`.

---

## Contributing

Agents working in this repo follow `.cursor/rules/commits.mdc`: Conventional Commits, explicit `git add <paths>`, and never commit secrets, customer data, or generated artifacts. After editing a Claude skill, re-zip its folder as `<name>.skill` so the installable bundle stays in sync with the source.
