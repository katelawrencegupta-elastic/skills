# Field Engineering Skills

Three agent skills for Elastic account work. Each one packages a repeatable workflow so the agent follows the same steps, produces the same output format, and spends as few tokens as possible doing it.

| Skill | Runs in | What it produces | Needs |
|---|---|---|---|
| [account-status-update](#account-status-update) | Claude (`.skill`) | Account status summary + activity timeline, optionally as a Google Doc | Slack, Gmail, Google Calendar (Drive for docs) |
| [customer-tech-research](#customer-tech-research) | Claude (`.skill`) | Confidence-rated tech-stack report + Elastic opportunity assessment | Web search, web fetch |
| [splunk-xml-to-kibana](#splunk-xml-to-kibana) | Cursor Agent | Splunk dashboard migrated to Kibana: mockup, ES\|QL panels, data factories, `MAPPING.md`, deploy | Kibana/ES URL + API key, `data_factories` package |

---

## Repository layout

```
account-status-update/
  account-status-update.skill        # zip: SKILL.md
customer-tech-research/
  customer-tech-research.skill       # zip: SKILL.md + references/industry-angles.md
splunk-xml-to-kibana/
  SKILL.md                           # workflow, gates, layout, MAPPING.md rules
  reference.md                       # Dashboards API snippets, factory notes, MAPPING.md template
  README.md                          # Cursor install notes
.cursor/rules/commits.mdc            # commit conventions for agents working in this repo
```

## Installing

**Claude skills (`.skill`).** Open the `.skill` file in Claude and click **Save skill**. A `.skill` file is a zip, so you can rename it to `.zip` to inspect or edit its contents, then re-zip it with the `.skill` extension.

**Cursor skill.** Copy or symlink the folder into your personal skills directory, then start a new Agent chat:

```bash
mkdir -p ~/.cursor/skills
ln -s "$(pwd)/splunk-xml-to-kibana" ~/.cursor/skills/splunk-xml-to-kibana
```

---

## How these skills save tokens

The same few principles show up in all three skills:

1. **Fewer round trips.** Every assistant turn re-reads the whole conversation, so the number of turns costs more than the size of any single result. The skills batch independent tool calls into one message and set an explicit round budget.
2. **Load only what's needed.** Descriptions are written so the skill triggers on its own. Heavy reference material sits in separate files (`references/industry-angles.md`, `reference.md`) and is read only when a step needs it.
3. **Filter before reading.** Queries exclude noise at the source (Gmail category/subject filters, year cutoffs, specific tool names), and results are screened from snippets or metadata before any full fetch.
4. **Hard caps and early exits.** Fixed page sizes, fetch limits, per-page token budgets, and stop conditions keep a run from drifting.
5. **Work offline, not in chat.** Large payloads (oversized tool results, Splunk XML, SPL) are parsed by scripts and saved to files. Only compact summaries come back into the conversation.
6. **Fixed output templates.** Each skill has an exact report or file structure, so the agent spends nothing designing a format and the output is the same every run.

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

**Usage:** In Cursor Agent, give it Splunk dashboard XML (or a DIMA/audit export such as `dashboard_to_migrate.json`, plus `macros.json` and `lookup_samples.json` when the SPL uses them) and ask to migrate it to Kibana.

**How it works**

1. **Gate 0, credentials.** Asks once for the Kibana URL and API key before any cluster write. It never guesses, reuses another project's key, or reads `.env` without permission. Mockup-only work skips this gate.
2. **Parse offline.** A local script turns the XML into a compact panel inventory (kind, title, theme, tabs, inputs, panels). Full SPL goes to `splunk_queries.json`, not the chat.
3. **Mockup.** An HTML mockup that matches Splunk's look, with real chart types.
4. **Panel map.** Splunk chart types map to Kibana 9.4+ `vis` types (data_table, xy, metric, gauge, heatmap, markdown). Studio tabs become one dashboard per tab joined by a `links` panel, because Kibana has no native tabs.
5. **Data factories.** Every panel gets a P0 data producer from the shared `data_factories` package, called through thin per-use-case wrappers. Each panel's ES|QL is validated with `POST /_query`.
6. **MAPPING.md (required).** For every panel: original SPL, ES|QL, chart type, and why. Also covers inputs, macros, lookups, data layout, the DIMA files that were used, and gaps.
7. **Deploy.** Templates, data streams and lookups, then bulk data, then `PUT /api/dashboards/{id}` per tab. Prints dashboard URLs, which factory feeds each panel, and the `MAPPING.md` path. If you edit dashboards in the Kibana UI, it exports them and redeploys from the export.

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

## Contributing

Agents working in this repo follow `.cursor/rules/commits.mdc`: Conventional Commits, explicit `git add <paths>`, and never commit secrets, customer data, or generated artifacts. After editing a Claude skill, re-zip its folder as `<name>.skill` so the installable bundle stays in sync with the source.
