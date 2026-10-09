# Custom Claude Skills — Field Engineering

Two skills for Elastic account work. Install each one by opening its `.skill` file in Claude and clicking **Save skill**. You can also rename a `.skill` file to `.zip` to inspect or edit what's inside.

---

## account-status-update

**What it does:** Builds an account status summary and activity timeline for one named account over a time window. It pulls from Slack, Gmail, and Google Calendar, and can optionally save the result as a Google Doc.

**Usage:**
```
status update <account> [period] [googledoc=y|n]
```
Examples: `status update GE Vernova 3m`, `status update Merck 2w googledoc=y`

The period accepts `d`, `w`, `m`, or `y` (default `1m`). The doc option defaults to no. The skill also triggers on informal requests like "what's been happening with Pfizer?"

**How it works:**
- It runs in batched rounds: one round of parallel searches, an optional round of follow-ups, and a publish step. This keeps token cost down.
- Long windows are split into parallel time slices. High-volume accounts get finer Slack slicing.
- The follow-up round only runs on specific triggers: truncated email threads, a quiet customer channel, an empty calendar result, or a full results page.
- Noise is filtered out before synthesis. That covers invite emails, auto-replies, notifications, and duplicates across sources.

**Output:** A fixed-format report containing:
- a status line (On track / Watch / At risk) with the AE
- a summary of up to 120 words
- a timeline table (daily rows for windows of a month or less, weekly rows for longer ones)
- open items and risks
- meetings in the next 7 days

**Requires:** Slack, Gmail, and Google Calendar connectors. The Google Drive connector is only needed for `googledoc=y`.

**Maintain:** Edit the account map (§5 in `SKILL.md`) to add accounts, aliases, domains, Slack channel IDs, `high` volume flags, and AE owners.

---

## customer-tech-research

**What it does:** Researches a customer's public technology footprint across search, observability, and security tooling. It produces a confidence-rated intelligence report and an Elastic opportunity assessment.

**Usage:** Ask in plain language, for example "research Moody's tech stack", "what tools does TIAA use?", or "prep for a Regeneron meeting". You can specify the industry and focus area; otherwise the skill infers the industry and covers all three areas.

**How it works:**
- It starts with three parallel searches: vendor case studies, job postings, and security press. A fourth, industry-specific query only runs if the first three return weak signal.
- Results that don't name a specific tool tied to the customer are discarded, as is anything from 2022 or earlier.
- Fetching is kept cheap. Pages are ranked by source type (PDFs and vendor sites first, the customer's own site last). There's a hard cap of 5 fetches, and the skill bails early on pages that are mostly navigation.
- Each finding gets a confidence rating:
  - **HIGH** means two or more independent sources.
  - **MEDIUM** means one directly attributed source.
  - **LOW** means the tool was inferred.

**Output:** A fixed-format report containing:
- an executive summary
- for each of search, observability, and security: confirmed tools, likely tools, and intelligence gaps
- an Elastic opportunity section covering stack gaps, the strongest entry point, and competitive risk
- a list of sources

**Requires:** Web search and web fetch.

**Maintain:** Update `references/industry-angles.md` to change the per-vertical query templates, priority vendor sites, compliance frameworks, and common tool patterns.
