# Reference — ES|QL Controls

Load at R3 when mapping Splunk inputs → `pinned_panels`.

Put Controls on the dashboard root as `pinned_panels` (not inside `panels`).
`variable_name` becomes `?variable_name` in every panel / Vega ES|QL query.

```json
{
  "title": "Example",
  "time_range": { "from": "now-15m", "to": "now" },
  "pinned_panels": [
    {
      "type": "esql_control",
      "id": "ctl_target_ip",
      "width": "medium",
      "grow": false,
      "config": {
        "title": "AWS Analysis Target IP",
        "variable_name": "target_ip",
        "variable_type": "values",
        "control_type": "STATIC_VALUES",
        "available_options": ["10.0.0.10", "10.0.0.20"],
        "selected_options": ["10.0.0.10"],
        "single_select": true
      }
    }
  ],
  "panels": []
}
```

- `control_type` may be `VALUES_FROM_QUERY` with `esql_query` returning the option column.
- Required: `variable_type` (`values` | `fields` | …) — omitting fails validation.
- Cast with `TO_STRING(?param)` / `TO_IP(?param)` when the field is keyword/ip.
- Time: dashboard `time_range` → `?_tstart` / `?_tend` in every query.
- Every Splunk input → Control, time picker, or explicit **GAP** in MAPPING.md.
- **Ban:** hard-coding demo token values in every `WHERE` instead of `?params`.
