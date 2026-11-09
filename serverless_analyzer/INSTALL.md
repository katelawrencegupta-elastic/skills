# Install

These three skills run from a project directory that holds `.env`. The guided installer checks the machine, links the skills, and can create the virtualenvs. It does not print or invent API keys.

```bash
cd serverless_analyzer
python3 install.py           # ask at each step
python3 install.py --check   # report what is present and change nothing
python3 install.py --yes     # safe defaults: link missing skills, install packages, do not open a browser
```

Python 3.10 or newer is required. The analysis scripts use the standard library. Google Sheets and the Search estimator each have their own virtualenv.

## What you need

| Piece | Used by | Where it lives |
|---|---|---|
| `KIBANA_URL`, `ELASTIC_API_KEY` | Every K360 fetch | `.env` in the project you run from |
| `FOUND_ADMIN_API_KEY` | A fresh diagnostic pull | Same `.env`. Not used when `--diags-dir` points at zips already on disk |
| Sheets virtualenv | Estimator copies and PDF upload | `~/.config/cloud-tracker-sheets/venv` |
| OAuth client | Sheets and Drive sign-in | `~/.config/cloud-tracker-sheets/client_secret.json` |
| es3-estimator | Search quotes only | Clone plus `~/.config/es3-estimator/venv` |
| Google Chrome | PDF print | `/Applications`, or `CHROME_PATH` |
| Aviatrix VPN | A fresh admin-console pull | An `openvpn` process. `--diags-dir` does not need it |

Keep `.env` and `artifacts/` out of git. A run directory is created mode `0700`, and the customer files inside it are mode `0600`.

## 1. Link the skills

The same skill folders work for Claude and Cursor. The runners look beside the skill that is running, then in `~/.claude/skills` and `~/.cursor/skills`. `K360_ORG_SKILL_DIR`, `K360_ACCOUNT_SKILL_DIR`, and `DIAG_SKILL_DIR` override those locations.

From this repository's `skills/` directory:

```bash
mkdir -p ~/.claude/skills ~/.cursor/skills
for skill in k360-org-report k360-account-analysis diag-serverless-consolidation; do
  ln -s "$(pwd)/serverless_analyzer/$skill" ~/.claude/skills/$skill
  ln -s "$(pwd)/serverless_analyzer/$skill" ~/.cursor/skills/$skill
done
```

If a name is already a real directory, the installer leaves it alone unless you confirm a replacement. A symlink that already points at this repo is left as it is.

## 2. Project `.env`

Create the file in the project you will `cd` to, not in this skill folder. A read-only key on `cloud-tracker-*` is enough. `_cat` privileges are not required.

```bash
# .env
KIBANA_URL=https://your-kibana-url
ELASTIC_API_KEY=
FOUND_ADMIN_API_KEY=
```

`KIBANA_URL` must be `https` and must not include a username or password. `FOUND_ADMIN_URL` defaults to `https://admin.found.no`. Another host is used only when it is listed in `FOUND_ADMIN_HOST_ALLOWLIST`. A URL with a username, password, query, fragment, or path is refused.

The installer can write this template with empty values. It will not overwrite an existing `.env`.

## 3. Google Sheets and Drive

Enable the Sheets API and the Drive API on the Google Cloud project. Download a Desktop OAuth client and save it as `~/.config/cloud-tracker-sheets/client_secret.json`.

```bash
python3 -m venv ~/.config/cloud-tracker-sheets/venv
~/.config/cloud-tracker-sheets/venv/bin/pip install -r \
  k360-org-report/scripts/requirements-sheets.txt
~/.config/cloud-tracker-sheets/venv/bin/python \
  k360-org-report/scripts/sheets_auth.py
```

The sign-in opens a browser and writes `token.json` next to the client file. Run it again if the token predates the Drive scopes. `CLOUD_TRACKER_SHEETS_PY` overrides the interpreter. `SHEETS_CLIENT_SECRET` and `SHEETS_TOKEN` override the two files.

Golden Observability and Security templates are copied. They are never edited. `O11Y_SHEET_ID` and `SECURITY_SHEET_ID` override the built-in template ids. The org report also accepts `ES3_SHEET_ID`.

## 4. Search estimator

Skip this section when the account has no Search workload. The library stays in the clone. The virtualenv only runs it. scikit-learn must stay at 1.7.2, the version the pickled models were trained with.

```bash
python3 -m venv ~/.config/es3-estimator/venv
~/.config/es3-estimator/venv/bin/pip install pandas numpy joblib 'scikit-learn==1.7.2'
```

`ES3_ESTIMATOR_DIR` defaults to `~/Documents/GItHub/es3-estimator-main`, the directory that contains `estimator/lib`. `ES3_ESTIMATOR_PY` overrides the interpreter.

## 5. Chrome

PDF export uses headless Chrome. Install Google Chrome or Chromium, or set `CHROME_PATH`. When Chrome is missing, the HTML is still written and the export records why. `--skip-pdf` skips the print.

## Run from the project

`<skill>` is that skill's directory. After the links above, either `~/.claude/skills/<skill>` or `~/.cursor/skills/<skill>` works.

```bash
cd <project with .env>

python <skill>/scripts/run_org_report.py \
  --org "ORG NAME" --report exec

python <skill>/scripts/run_account_analysis.py \
  --org "ORG NAME" --skip-diags --skip-sizing

python <skill>/scripts/run_consolidation.py \
  --org "ORG NAME" \
  --reuse artifacts/<customer>_<date>/<customer>_<date>_org_report.json \
  --diags-dir artifacts/<org_id>_diags_<date>
```

`--report full` expands the same K360 fetch. A diagnostic run without `--diags-dir` calls the admin API and needs the Aviatrix tunnel. `--skip-vpn-check` skips that reachability check.
