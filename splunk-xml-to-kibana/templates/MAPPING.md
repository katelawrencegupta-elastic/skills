# <Title> — Splunk → Elastic mapping

**Splunk source:** `<app>` / `<dashboard_id>`  
**Elastic target:** `<kibana_dashboard_id>`  
**Demo tokens:** …  
**Package:** `elastic/<use_case>/`  
**Splunk mockup:** `elastic/<use_case>/mockup/index.html`

## 0. Audit / DIMA JSON used

| JSON / XML file | Used? | How |
|-----------------|:-----:|-----|
| `source.xml` / `dashboard_to_migrate.json` | | |
| `macros.json` | | |
| `lookup_samples.json` | | |

## 0b. Data factories (demo generation)

| Script | Role |
|--------|------|
| `data_factories/generate_*.py` | |

| Factory NDJSON | → ES index/stream | → panels |
|----------------|-------------------|----------|

## 1. Inputs → Controls matrix (required)

| Splunk input / token | Elastic Control / time / GAP | Drives (`?param`) | Why |
|----------------------|------------------------------|-------------------|-----|

## 2. Knowledge objects

### Lookups

### Macros

### Datamodels → streams

## 3. Panel-by-panel mapping

### <panel title> — `<elastic_panel_id>`

**SPL**

```spl
```

**ES|QL**

```esql
```

**Viz:** Splunk X → Kibana `vis` type  
**Why:** …

## 4. Data layout

| Elastic resource | Feeds panels |
|------------------|--------------|

## 5. Gaps / non-goals

| Item | Reason |
|------|--------|

## 6. Quick reference

| Splunk pattern | Elastic pattern |
|----------------|-----------------|

## 7. Links

- Mockup: `mockup/index.html`
- ANALYSIS.md
- `splunk_queries.json`
- Kibana dashboard URL
