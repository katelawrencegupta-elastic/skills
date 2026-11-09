# One-time Sheets + Drive setup

`<skill>` is this skill's directory, whether Claude or Cursor loaded it.

```bash
python3 -m venv ~/.config/cloud-tracker-sheets/venv
~/.config/cloud-tracker-sheets/venv/bin/pip install -r \
  <skill>/scripts/requirements-sheets.txt
# client_secret.json → ~/.config/cloud-tracker-sheets/
# Enable Sheets API + Drive API
~/.config/cloud-tracker-sheets/venv/bin/python \
  <skill>/scripts/sheets_auth.py
```

Env: `ES3_SHEET_ID`, `O11Y_SHEET_ID`, `CLOUD_TRACKER_SHEETS_PY`, `CHROME_PATH`.
Re-auth if token predates Drive scopes. Apps Script fallback: [EstimatorImport.gs](apps-script/EstimatorImport.gs).
