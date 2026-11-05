# Setup

## Dependencies
- **Parent skill** `~/.cursor/skills/k360-org-report/` — provides `scripts/fetch_org.py` (K360 fetch) and `scripts/sheets_auth.py` (OAuth). Override location with `K360_ORG_SKILL_DIR`.
- **`.env`** in the working directory: `KIBANA_URL`, `ELASTIC_API_KEY` (read-only API key on `cloud-tracker-*` is enough; `_cat` privileges not required).
- **Sheets venv** (only for the ECH > 50 % step):
  ```bash
  python3 -m venv ~/.config/cloud-tracker-sheets/venv
  ~/.config/cloud-tracker-sheets/venv/bin/pip install -r ~/.cursor/skills/k360-org-report/scripts/requirements-sheets.txt
  # client_secret.json → ~/.config/cloud-tracker-sheets/ ; enable Sheets + Drive APIs
  ~/.config/cloud-tracker-sheets/venv/bin/python ~/.cursor/skills/k360-org-report/scripts/sheets_auth.py
  ```
  Override interpreter with `CLOUD_TRACKER_SHEETS_PY`. If the venv is missing the pipeline still produces the report and records `sizing.estimators.error`.

- **es3-estimator venv** (Search sizing). The library and models stay in the clone; this venv only runs them. scikit-learn must be 1.7.2, the version the pickled models were trained with.
  ```bash
  python3 -m venv ~/.config/es3-estimator/venv
  ~/.config/es3-estimator/venv/bin/pip install pandas numpy joblib 'scikit-learn==1.7.2'
  ```
  `ES3_ESTIMATOR_DIR` defaults to `~/Documents/GItHub/es3-estimator-main` (the directory that contains `estimator/lib`). `ES3_ESTIMATOR_PY` overrides the interpreter.

## Env overrides
`O11Y_SHEET_ID`, `SECURITY_SHEET_ID` (golden template ids) · `K360_ORG_SKILL_DIR` · `CLOUD_TRACKER_SHEETS_PY` · `ES3_ESTIMATOR_DIR` · `ES3_ESTIMATOR_PY`.

## Admin console (diagnostic consolidation skill)
The account-summary skill passes `--skip-diags --skip-sizing` and does not use this. `diag-serverless-consolidation` needs `FOUND_ADMIN_API_KEY` in the same `.env`. `FOUND_ADMIN_URL` defaults to `https://admin.found.no` (`http://` is rewritten to `https://`). The key must be allowed to search deployments for the organization and to capture Elasticsearch diagnostics. The pull also requires the Aviatrix VPN tunnel: an `openvpn` process, which Aviatrix starts when the tunnel connects. The menu-bar app alone is not enough. `--skip-vpn-check` skips that gate. Without bundles the quote falls back to K360 stored GB ÷ assumed retention, and the notes say K360 does not provide a raw ingest total. `--diags-dir` reads a folder of zips and does not call the admin API. `--skip-diags` selects that backup on purpose. `--k360-ingest` writes the backup as well when the bundles succeed.

## Python
Analysis scripts are stdlib-only (Python ≥ 3.10). `size_serverless.py` and `upload_pdfs.py` must run under the Sheets venv (`google-api-python-client`, `google-auth-oauthlib`).

- **Chrome** (PDF export): Google Chrome or Chromium in `/Applications`, or set `CHROME_PATH`. If Chrome is missing the HTML is still written and `exports.<kind>.error` records why; use `--skip-pdf` to disable.
