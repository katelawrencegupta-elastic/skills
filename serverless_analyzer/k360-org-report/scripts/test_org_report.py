"""Offline tests for the org report. No network, no Google packages, no K360.

  cd k360-org-report/scripts && python3 -m unittest test_org_report

Elasticsearch is a fake `es_post`. Sheets and Drive are fakes that record every
call. Each test class names the defect it covers (O1–O13 and the low items).
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import emit_canvas_data  # noqa: E402
import emit_estimator_import  # noqa: E402
import estimator_layout  # noqa: E402
import fetch_org  # noqa: E402
import push_estimator  # noqa: E402
import render_org_pdf  # noqa: E402
import run_org_report  # noqa: E402

TODAY = "2026-11-13"
SKU_AWS_HOT = "Cloud-Enterprise_aws.es.datahot.c6gd_us-west-2_8192_2"
SKU_AWS_ML = "Cloud-Enterprise_aws.es.ml.c5d_us-west-2_2048_3"
SKU_AZURE_HOT = "Cloud-Enterprise_azure.es.datahot.edsv4_azure-eastus2_8192_2"
SKU_GCP_FROZEN = "Cloud-Standard_gcp.es.datafrozen.n2.68x10x95_gcp-europe-west1_61440_2"
MONTH = 30.416


# ----------------------------------------------------------------------------- fake Elasticsearch
def dep_docs(name, start, end, cost, sku=SKU_AWS_HOT, typ="elasticsearch", lines=3, template=None, version="8.17.0"):
    """One billing row a day for a deployment, `lines` billing lines each."""
    out = []
    cur = date.fromisoformat(start)
    while cur <= date.fromisoformat(end):
        out.append({"day": cur.isoformat(), "dep": name, "sku": sku, "type": typ, "cost": cost, "lines": lines, "template": template, "version": version})
        cur += timedelta(days=1)
    return out


class FakeES:
    """Answers the searches fetch_org.main sends. Terms aggregations honor the request's size and order."""

    def __init__(self, docs, pivots=None, telem=None, use_case=None, index_telemetry=None):
        self.docs = docs
        self.pivots = pivots or [{
            "organization": {"name": "Acme", "id": "org1", "ARR": 1000000.0, "AE": "A. Person", "HV": ["h1"], "segment": "ENTR", "geo": "AMER"},
            "pace": {"commit": 100}, "runout": {}, "consumption": {}, "license": "enterprise",
        }]
        self.telem = telem or {}          # name -> gb, or (gb, "YYYY-MM-DD")
        self.use_case = use_case or {}    # name -> use_case
        self.index_telemetry = index_telemetry
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, base, key, path, body):
        self.calls.append((path, body))
        if "organization_pivot" in path:
            return {"hits": {"hits": [{"_source": p} for p in self.pivots]}}
        if "billing" in path:
            return {"aggregations": self.billing(body)}
        if "features" in path:
            size = body["aggs"]["by_dep"]["terms"]["size"]
            names = sorted(self.use_case)
            return {"aggregations": {
                "use_case": {"buckets": []},
                "by_dep": {
                    "buckets": [{"key": n, "latest": {"hits": {"hits": [{"_source": {"use_case": self.use_case[n], "feature": {}}}]}}} for n in names[:size]],
                    "sum_other_doc_count": max(len(names) - size, 0),
                },
            }}
        if "index_telemetry" in path:
            return {"aggregations": self.workload(body)}
        if "cloud-tracker-telemetry" in path:
            size = body["aggs"]["by_dep"]["terms"]["size"]
            rows = []
            for name, value in self.telem.items():
                gb, day = value if isinstance(value, tuple) else (value, "2026-11-12")
                rows.append({"key": name, "latest": {"hits": {"hits": [{"_source": {
                    "@timestamp": f"{day}T00:00:00Z",
                    "telemetry": {"totals": {"dataset_size_bytes": gb * 1024**3, "docs_count": gb * 1e6}},
                }}]}}})
            return {"aggregations": {"by_dep": {"buckets": rows[:size], "sum_other_doc_count": max(len(rows) - size, 0)}}}
        if "cloud-tracker-deployments" in path:
            return {"hits": {"total": {"value": 0}, "hits": []}}
        raise AssertionError(f"unexpected search {path}")

    @staticmethod
    def _terms(docs, field, spec, sub=None, include=None):
        size = spec["size"]
        by_ecu = (spec.get("order") or {}).get("ecu") == "desc"
        groups = defaultdict(list)
        for d in docs:
            k = d.get(field)
            if k is None or (include and not re.fullmatch(include, k)):
                continue
            groups[k].append(d)
        rows = [(k, sum(x.get("lines", 1) for x in ds), sum(x["cost"] for x in ds), ds) for k, ds in groups.items()]
        rows.sort(key=(lambda r: (-r[2], r[0])) if by_ecu else (lambda r: (-r[1], r[0])))
        out = []
        for k, n, e, ds in rows[:size]:
            b = {"key": k, "doc_count": n, "ecu": {"value": e}, "qty": {"value": sum(x.get("qty", 0.0) for x in ds)}}
            if sub:
                b.update(sub(ds))
            out.append(b)
        return {"buckets": out, "sum_other_doc_count": sum(r[1] for r in rows[size:])}

    def billing(self, body):
        rng = next(f["range"]["@timestamp"] for f in body["query"]["bool"]["filter"] if "range" in f)
        lo, hi = rng["gte"][:10], rng["lte"][:10]
        docs = [d for d in self.docs if lo <= d["day"] <= hi]
        aggs = body["aggs"]
        sub_spec = aggs["by_dep"]["aggs"]

        def dep_sub(ds):
            return {
                "types": self._terms(ds, "type", sub_spec["types"]["terms"]),
                "skus": self._terms(ds, "sku", sub_spec["skus"]["terms"]),
                "ai_skus": self._terms(ds, "sku", sub_spec["ai_skus"]["terms"], include=".*inference.*"),
                "ml_skus": self._terms(ds, "sku", sub_spec["ml_skus"]["terms"], include=r".*(\.es\.ml\.|\.ml\.).*"),
                "templates": self._terms(ds, "template", sub_spec["templates"]["terms"]),
                "versions": self._terms(ds, "version", sub_spec["versions"]["terms"]),
            }

        out = {"by_dep": self._terms(docs, "dep", aggs["by_dep"]["terms"], sub=dep_sub)}
        by_day = defaultdict(list)
        for d in docs:
            by_day[d["day"]].append(d)
        buckets = []
        cur = date.fromisoformat(lo)
        day_spec = aggs["by_day"]["aggs"]["by_dep"]["terms"]
        while cur <= date.fromisoformat(hi):
            ds = by_day.get(cur.isoformat(), [])
            buckets.append({
                "key_as_string": cur.isoformat() + "T00:00:00.000Z",
                "by_dep": self._terms(ds, "dep", day_spec),
                "ecu": {"value": sum(x["cost"] for x in ds)},
            })
            cur += timedelta(days=1)
        out["by_day"] = {"buckets": buckets}
        out["types_total"] = self._terms(docs, "type", aggs["types_total"]["terms"])
        out["cloud_skus"] = self._terms(docs, "sku", aggs["cloud_skus"]["terms"], include="Cloud-.*")
        out["ai_skus"] = self._terms(docs, "sku", aggs["ai_skus"]["terms"], include=".*inference.*")
        out["inference_total"] = {"ecu": {"value": 0}, "qty": {"value": 0}}
        out["ml_skus"] = self._terms(docs, "sku", aggs["ml_skus"]["terms"], include=r".*(\.es\.ml\.|\.ml\.).*")
        return out

    def workload(self, body):
        """index_telemetry: {"idx": {name: [(search_total, search_time_ms, docs_count) per day]}, "days": [...]}."""
        it = self.index_telemetry
        if not it:
            return {"by_search": {"buckets": []}, "by_docs": {"buckets": []}}
        if "by_search" in body["aggs"]:
            names = [{"key": n} for n in it["idx"]]
            return {"by_search": {"buckets": names}, "by_docs": {"buckets": names}}
        buckets = []
        for i, day in enumerate(it["days"]):
            buckets.append({"key_as_string": f"{day}T00:00:00.000Z", "by_idx": {"buckets": [
                {"key": n, "st": {"value": rows[i][0]}, "tm": {"value": rows[i][1]}, "docs": {"value": rows[i][2]}}
                for n, rows in it["idx"].items()
            ]}})
        return {"by_day": {"buckets": buckets}}


class _FixedNow(datetime):
    fixed = datetime(2026, 11, 13, 15, 0, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.fixed


class Workdir(unittest.TestCase):
    """A private temp folder with a fake .env. Removed after each test."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="orgtest_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env = self.tmp / ".env"
        self.env.write_text("KIBANA_URL=https://fake.kb.example.invalid\nELASTIC_API_KEY=fake\n")

    def fetch(self, es, days=365, chart="week", extra=(), org="Acme", env=None):
        """Run fetch_org.main against the fake. Returns (org JSON, stderr text)."""
        out = self.tmp / "org.json"
        argv = ["fetch_org.py", "--org", org, "--env", str(env or self.env), "--days", str(days), "--chart", chart, "--out", str(out), *extra]
        err = io.StringIO()
        with mock.patch.object(fetch_org, "es_post", es), mock.patch.object(fetch_org, "datetime", _FixedNow), \
                mock.patch.object(sys, "argv", argv), contextlib.redirect_stderr(err):
            fetch_org.main()
        self.org_path = out
        return json.loads(out.read_text()), err.getvalue()


def mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def page_text(html_text: str) -> str:
    import html as _html

    body = html_text[html_text.index("</style>"):]
    return " ".join(_html.unescape(re.sub(r"<[^>]+>", " ", body)).split())


def year(name, cost, **kw):
    return dep_docs(name, "2025-11-14", TODAY, cost, **kw)


# ----------------------------------------------------------------------------- O1
class O1NotPricedLeftOffBothSides(Workdir):
    DEPS = [
        {"name": "priced", "ecu": 12000.0, "platform": "ech", "families": {"search": 12000.0}},
        {"name": "no-telemetry", "ecu": 6000.0, "platform": "ech", "families": {"search": 6000.0}},
        {"name": "svl-project", "ecu": 3000.0, "platform": "serverless", "families": {"search": 3000.0}},
    ]
    STORE = [{"name": "priced", "data_stored_gb": 500.0}, {"name": "svl-project", "data_stored_gb": 80.0}]

    def split(self, **kw):
        return fetch_org.build_serverless_offload(self.DEPS, self.STORE, 21000.0, None, lookback_days=365, **kw)

    def test_unpriced_and_serverless_deployments_are_named_not_priced_at_zero(self):
        fs = self.split()["full_split"]
        reasons = {x["name"]: x["reason"] for x in fs["not_priced"]}
        self.assertEqual(reasons, {
            "no-telemetry": fetch_org.NOT_PRICED_NO_GB,
            "svl-project": "already serverless",
        })
        months = 365 / MONTH
        self.assertEqual(fs["priced_n"], 1)
        self.assertEqual(fs["not_priced_n"], 2)
        self.assertAlmostEqual(fs["not_priced_ecu_mo"], 9000 / months, places=1)
        self.assertEqual({x["name"]: x["ecu_mo"] for x in fs["not_priced"]}["no-telemetry"], round(6000 / months, 2))

    def test_totals_cover_the_priced_set_only(self):
        off = self.split()
        fs = off["full_split"]
        months = 365 / MONTH
        self.assertAlmostEqual(fs["full_ech_mo"], 12000 / months, places=2)
        self.assertAlmostEqual(fs["org_ech_mo"], 21000 / months, places=2)
        self.assertAlmostEqual(sum(l["ech_mo"] for l in fs["lines"]), fs["full_ech_mo"], places=1)
        self.assertGreater(fs["serverless_total_mo"], 0)
        self.assertAlmostEqual(fs["save_vs_ech_mo"], fs["full_ech_mo"] - fs["serverless_total_mo"], places=2)
        self.assertAlmostEqual(fs["save_vs_ech_yr"], fs["save_vs_ech_mo"] * 12, places=1)
        self.assertAlmostEqual(fs["save_vs_ech_3yr"], fs["save_vs_ech_mo"] * 36, places=1)
        # No line keeps an ECH bill against a $0 Serverless price.
        for line in fs["lines"]:
            self.assertFalse(line["ech_mo"] > 0 and line["serverless_mo"] == 0, line)
        self.assertEqual(sum(k["n"] for k in off["by_kind"].values()), 1)
        self.assertAlmostEqual(off["hybrid"]["full_ech_mo"] if off["hybrid"] else fs["full_ech_mo"], fs["full_ech_mo"], places=2)

    def test_org_with_no_stored_gb_saves_nothing_instead_of_the_whole_bill(self):
        off = fetch_org.build_serverless_offload(self.DEPS[:2], [], 18000.0, None, lookback_days=365)
        fs = off["full_split"]
        self.assertEqual(fs["priced_n"], 0)
        self.assertEqual(fs["full_ech_mo"], 0)
        self.assertEqual(fs["save_vs_ech_mo"], 0)
        self.assertEqual(fs["not_priced_n"], 2)

    def test_summary_and_pdf_say_what_was_not_compared(self):
        es = FakeES(year("priced", 100.0) + year("no-telemetry", 40.0), telem={"priced": 500})
        org, err = self.fetch(es)
        self.assertIn("not in the ECH-vs-Serverless comparison", err)
        canvas = emit_canvas_data.build_canvas(org)
        offload = emit_canvas_data.build_offload(org)
        summary = emit_canvas_data.build_summary(org, canvas, offload, {"folder": "x"}, "exec")
        self.assertEqual(summary["not_compared_deployments"], 1)
        self.assertAlmostEqual(summary["not_compared_ecu_mo"], 40.0 * 365 / (365 / MONTH), places=1)
        joined = " ".join(summary["chat_bullets_hint"])
        self.assertIn("1 deployments (1,217 ECU/mo) are not in the Serverless comparison", joined)
        self.assertIn("on 1 compared deployments", joined)
        text = page_text(render_org_pdf.render_html(canvas, offload, "exec"))
        self.assertIn("Not compared: 1 deployments, 1,217 ECU/mo", text)
        self.assertIn("no-telemetry (1,217 ECU/mo)", text)
        self.assertIn("Total, compared deployments", text)


# ----------------------------------------------------------------------------- O2
class O2SkuRegionAndEstimatorRegion(Workdir):
    def test_aws_sku_has_no_region_prefix(self):
        self.assertEqual(fetch_org.parse_cloud_sku(SKU_AWS_ML), {"provider": "AWS", "instance": "es.ml.c5d", "region": "us-west-2", "tier": "ml"})
        self.assertEqual(fetch_org.parse_cloud_sku(SKU_AZURE_HOT)["region"], "eastus2")
        self.assertEqual(fetch_org.parse_cloud_sku(SKU_GCP_FROZEN)["region"], "europe-west1")
        self.assertEqual(fetch_org.parse_cloud_sku(SKU_GCP_FROZEN)["tier"], "datafrozen")
        self.assertIsNone(fetch_org.parse_cloud_sku("serverless.search_vcu"))
        self.assertEqual(fetch_org.cloud_sku_region("Cloud-Enterprise_azure.kibana.fsv2_azure-eastus2_1024_1"), {"provider": "AZURE", "region": "eastus2"})

    def test_aws_org_gets_its_own_region_and_no_azure_string(self):
        es = FakeES(year("a", 100.0, sku=SKU_AWS_HOT) + year("b", 10.0, sku=SKU_AWS_ML), telem={"a": 100, "b": 20})
        org, _ = self.fetch(es)
        infra = org["infrastructure"]
        self.assertEqual(infra["summary"]["provider"], "AWS")
        self.assertEqual(infra["summary"]["region"], "us-west-2")
        self.assertEqual(infra["capacity_by_provider"], [{"provider": "AWS", "ecu": org["totals"]["ecu"]}])
        self.assertTrue(infra["capacity_by_tier"])
        self.assertEqual({d["region"] for d in org["deployments"]["items"]}, {"us-west-2"})
        payload = org["offload"]["apps_script_payload"]
        self.assertEqual(payload["cloud"], {"provider": "AWS", "region": "us-west-2"})
        self.assertEqual(payload["o11y"]["cloud"], {"provider": "AWS", "region": "us-west-2", "cell": "G8"})
        self.assertNotIn("G8", payload["o11y"]["cells"])
        self.assertNotIn("C5", payload["es3"]["cells"])
        self.assertNotIn("C6", payload["es3"]["cells"])
        self.assertNotIn("AZURE", json.dumps(payload).upper())

    def test_region_option_is_matched_or_left_alone(self):
        opts = ["AWS: us-east-1 (Virginia)", "AWS: us-west-2 (Oregon)", "AZURE: us-east-2 (Virginia)", "GCP: us-central1 (Iowa)"]
        m = estimator_layout.match_region
        self.assertEqual(m("AWS", "us-west-2", opts), ("AWS: us-west-2 (Oregon)", "exact"))
        self.assertEqual(m("AZURE", "azure-eastus2", opts), ("AZURE: us-east-2 (Virginia)", "alias"))
        self.assertEqual(m("GCP", "gcp-us-central1", opts), ("GCP: us-central1 (Iowa)", "exact"))
        # No option for this region: nothing is returned, not another cloud's or another region's option.
        self.assertEqual(m("GCP", "europe-west1", opts), (None, "none"))
        self.assertEqual(m("AWS", "us-east-2", opts), (None, "none"))
        self.assertEqual(m("AWS", None, opts), (None, "none"))
        self.assertEqual(m(None, "us-west-2", opts), (None, "none"))
        self.assertEqual(estimator_layout.region_options([["x", "AWS: us-east-1 (Virginia)"], [None, "GCP: us-central1 (Iowa)", 3]]),
                         ["AWS: us-east-1 (Virginia)", "GCP: us-central1 (Iowa)"])


# ----------------------------------------------------------------------------- O3 + O7
class O3O7MonthlyDivisor(Workdir):
    def test_offload_divides_by_the_lookback_length(self):
        deps = [{"name": "a", "ecu": 9000.0, "platform": "ech", "families": {}}]
        store = [{"name": "a", "data_stored_gb": 100.0}]
        for days in (30, 90, 365):
            off = fetch_org.build_serverless_offload(deps, store, 9000.0, None, lookback_days=days)
            self.assertAlmostEqual(off["full_split"]["full_ech_mo"], 9000.0 / (days / MONTH), places=2, msg=days)
            self.assertAlmostEqual(off["by_kind"]["search"]["ecu_mo"], 9000.0 / (days / MONTH), places=2)
        self.assertAlmostEqual(fetch_org.lookback_months(365), 12.0003, places=4)

    def test_ninety_day_fetch_is_not_divided_by_twelve(self):
        es = FakeES(dep_docs("a", "2026-08-16", TODAY, 100.0), telem={"a": 100})
        org, _ = self.fetch(es, days=90)
        tot = org["totals"]
        self.assertEqual(tot["ecu"], 9000.0)
        self.assertAlmostEqual(tot["ecu_mo"], 9000.0 / (90 / MONTH), places=2)   # 3041.6, not 750
        self.assertEqual(tot["lookback_days"], 90)
        self.assertAlmostEqual(org["offload"]["full_split"]["full_ech_mo"], tot["ecu_mo"], places=2)

    def test_report_ecu_mo_and_section_ten_use_one_divisor(self):
        es = FakeES(year("a", 100.0) + year("b", 50.0), telem={"a": 100})
        org, _ = self.fetch(es)
        tot, fs = org["totals"], org["offload"]["full_split"]
        # The calendar-month keys analyze.py reads are unchanged: 13 months touched by 365 days.
        self.assertEqual(tot["months"], 13)
        self.assertAlmostEqual(tot["ecu_monthly_avg"], tot["ecu"] / 13, places=2)
        self.assertAlmostEqual(tot["ecu_mo"], tot["ecu"] / (365 / MONTH), places=2)
        self.assertAlmostEqual(fs["full_ech_mo"] + fs["not_priced_ecu_mo"], tot["ecu_mo"], places=1)
        self.assertAlmostEqual(fs["org_ech_mo"], tot["ecu_mo"], places=2)
        items = {d["name"]: d for d in org["deployments"]["items"]}
        self.assertAlmostEqual(sum(d["ecu_mo"] for d in items.values()), tot["ecu_mo"], places=1)
        canvas = emit_canvas_data.build_canvas(org)
        self.assertEqual(canvas["ecu_mo"], tot["ecu_mo"])
        self.assertEqual(canvas["ecu_monthly_avg"], tot["ecu_mo"])
        self.assertEqual(canvas["ecu_monthly_avg_calendar"], tot["ecu_monthly_avg"])
        text = page_text(render_org_pdf.render_html(canvas, emit_canvas_data.build_offload(org), "exec"))
        self.assertIn(f"{tot['ecu_mo']:,.0f} ECU / mo", text)
        self.assertIn(f"${fs['full_ech_mo']:,.0f}", text)
        self.assertIn("365 days ÷ 30.416", text)


# ----------------------------------------------------------------------------- O4
def telem_bucket(name, gb, day, docs=None):
    return {"key": name, "latest": {"hits": {"hits": [{"_source": {
        "@timestamp": f"{day}T03:00:00Z",
        "telemetry": {"totals": {"dataset_size_bytes": gb * 1024**3, "docs_count": docs if docs is not None else gb * 1000}},
    }}]}}}


class O4Storage(Workdir):
    END = date(2026, 11, 13)

    def build(self):
        buckets = [
            telem_bucket("fresh", 100, "2026-11-12"),
            telem_bucket("edge", 10, "2026-09-29"),      # 45 days: not stale
            telem_bucket("old", 50, "2026-03-12"),       # 246 days: stale
            telem_bucket("deleted", 400, "2026-11-12"),  # last bill 2026-03-01
            telem_bucket("never-billed", 7, "2026-11-12"),
        ]
        last_bill = {"fresh": "2026-11-13", "edge": "2026-08-16", "old": "2026-11-13", "deleted": "2026-03-01"}
        return fetch_org.build_storage(buckets, last_bill, self.END)

    def test_each_row_keeps_its_own_snapshot_day_and_stale_rows_are_listed(self):
        storage, _, _ = self.build()
        rows = {r["name"]: r for r in storage["by_deployment"]}
        self.assertEqual({n: r["as_of"] for n, r in rows.items()}, {"fresh": "2026-11-12", "edge": "2026-09-29", "old": "2026-03-12"})
        self.assertEqual(rows["old"]["age_days"], 246)
        self.assertEqual(storage["as_of"], "2026-11-12")
        self.assertEqual(storage["as_of_oldest"], "2026-03-12")
        self.assertEqual(storage["stale"], [{"name": "old", "as_of": "2026-03-12", "age_days": 246, "data_stored_gb": 50.0}])
        self.assertEqual(storage["stale_gb"], 50.0)
        self.assertIn("1 of 3 storage snapshots are more than 45 days older than the range end", storage["notes"][0])

    def test_deployment_with_no_recent_bill_is_out_of_org_gb(self):
        storage, nbytes, docs = self.build()
        self.assertEqual(storage["data_stored_gb"], 160.0)
        self.assertEqual(storage["deployments_with_telemetry"], 3)
        self.assertAlmostEqual(nbytes, 160 * 1024**3)
        self.assertEqual(docs, 160000)
        gone = {x["name"]: x for x in storage["not_billing"]}
        self.assertEqual(set(gone), {"deleted", "never-billed"})
        self.assertEqual(gone["deleted"]["last_billed"], "2026-03-01")
        self.assertEqual(gone["deleted"]["reason"], "no bill in the last 90 days of the range")
        self.assertEqual(gone["never-billed"]["reason"], "no bill in the range")
        self.assertEqual(storage["not_billing_gb"], 407.0)
        # "edge" last billed on the first day of the 90-day window, so it stays.
        self.assertIn("edge", {r["name"] for r in storage["by_deployment"]})

    def test_fetch_uses_one_denominator_for_search_docs_and_docs_per_sec(self):
        docs = year("search-a", 100.0) + year("obs-apm", 100.0, typ="integrationsserver") \
            + dep_docs("deleted", "2025-11-14", "2026-03-01", 50.0)
        es = FakeES(docs, telem={"search-a": 100, "obs-apm": 300, "deleted": (600, "2026-03-01"), "ghost": 1000})
        org, err = self.fetch(es)
        st = org["storage"]
        self.assertEqual(st["data_stored_gb"], 400.0)
        self.assertEqual({x["name"] for x in st["not_billing"]}, {"deleted", "ghost"})
        self.assertIn("left out of org stored GB", err)
        share = org["offload"]["search_share"]
        self.assertEqual(share, {"search_gb": 100.0, "org_stored_gb": 400.0, "share": 0.25})
        es3 = org["offload"]["apps_script_payload"]["es3"]
        self.assertEqual(es3["search_share"], share)
        self.assertEqual(es3["cells"]["C12"], 100.0)
        self.assertEqual(es3["cells"]["C13"], int(st["docs_count"] * 0.25))
        self.assertEqual(es3["cells"]["C14"], st["avg_doc_kb"])
        # The deployment that stopped billing is named with that reason on the comparison too.
        reasons = {x["name"]: x["reason"] for x in org["offload"]["full_split"]["not_priced"]}
        self.assertEqual(reasons, {"deleted": "no bill in the last 90 days of the range"})

    def test_summary_and_pdf_say_storage_is_stale(self):
        es = FakeES(year("a", 100.0) + year("b", 10.0), telem={"a": (100, "2026-03-12"), "b": (5, "2026-11-12")})
        org, err = self.fetch(es)
        self.assertIn("1 of 2 storage snapshots are more than 45 days older", err)
        canvas, offload = emit_canvas_data.build_canvas(org), emit_canvas_data.build_offload(org)
        summary = emit_canvas_data.build_summary(org, canvas, offload, {}, "exec")
        self.assertTrue(any("1 of 2 storage snapshots are older than 45 days (oldest 246 days)" in c for c in summary["caveats"]))
        text = page_text(render_org_pdf.render_html(canvas, offload, "exec"))
        self.assertIn("1 of 2 storage snapshots are more than 45 days old (oldest snapshot 2026-03-12)", text)
        self.assertIn("storage snapshots are stale", text)


# ----------------------------------------------------------------------------- O5
OBS_OPTIONS = ["AWS: us-east-1 (Virginia)", "AWS: us-west-2 (Oregon)", "AZURE: us-east-2 (Virginia)", "GCP: us-central1 (Iowa)"]


class _Req:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class FakeSheets:
    """Sheets values API. Labels come from TEMPLATE_ANCHORS unless overridden. Every write is recorded."""

    def __init__(self, version="1.22", options=None, labels=None):
        self.version = version
        self.options = OBS_OPTIONS if options is None else options
        self.labels = labels or {}
        self.updates: list[tuple[str, list[dict]]] = []
        self.reads: list[str] = []

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def _label(self, rng):
        if rng in self.labels:
            return self.labels[rng]
        for kind in ("observability", "security"):
            tab = estimator_layout.OBSERVABILITY_TAB if kind == "observability" else estimator_layout.SECURITY_TAB
            for a1, text in estimator_layout.TEMPLATE_ANCHORS[kind]["labels"].items():
                if estimator_layout.cell_range(tab, a1) == rng:
                    return text.title()
        return None

    def batchGet(self, spreadsheetId, ranges, valueRenderOption=None):
        def run():
            self.reads.append(spreadsheetId)
            out = []
            for rng in ranges:
                if rng.startswith(f"'{estimator_layout.VERSION_TAB}'"):
                    out.append({"range": rng, "values": [["Version"], ["1.19"], [self.version]]})
                else:
                    value = self._label(rng)
                    out.append({"range": rng, "values": [[value]]} if value is not None else {"range": rng})
            return {"valueRanges": out}
        return _Req(run)

    def get(self, spreadsheetId, range):
        return _Req(lambda: {"values": [["Region"]] + [[o] for o in self.options]})

    def batchUpdate(self, spreadsheetId, body):
        def run():
            self.updates.append((spreadsheetId, body["data"]))
            return {}
        return _Req(run)

    def written(self) -> dict:
        return {d["range"]: d["values"][0][0] for _sid, data in self.updates for d in data}


class FakeDrive:
    def __init__(self):
        self.copies: list[str] = []
        self.trashed: list[str] = []

    def files(self):
        return self

    def list(self, **kw):
        return _Req(lambda: {"files": []})

    def create(self, body=None, **kw):
        return _Req(lambda: {"id": "folder-1", "name": body["name"]})

    def copy(self, fileId=None, body=None, **kw):
        def run():
            self.copies.append(fileId)
            return {"id": f"copy-{len(self.copies)}", "name": body["name"]}
        return _Req(run)

    def update(self, fileId=None, body=None, **kw):
        def run():
            if (body or {}).get("trashed"):
                self.trashed.append(fileId)
            return {"id": fileId}
        return _Req(run)


class PushCase(Workdir):
    def org_json(self, es=None, **fetch_kw):
        es = es or FakeES(year("search-a", 100.0) + year("obs-apm", 60.0, typ="integrationsserver"), telem={"search-a": 100, "obs-apm": 900})
        org, _ = self.fetch(es, **fetch_kw)
        return org

    def push(self, *argv, sheets=None, drive=None, path=None):
        """Run push_estimator.main with fake Google. Returns (stdout, sheets, drive)."""
        sheets = sheets or FakeSheets()
        drive = drive or FakeDrive()
        args = ["push_estimator.py", "--org-json", str(path or self.org_path), "--artifacts-root", str(self.tmp / "art"), "--skip-xlsx-export", "--date", TODAY, *argv]
        out = io.StringIO()
        with mock.patch.object(push_estimator, "sheets_service", lambda: sheets), \
                mock.patch.object(push_estimator, "drive_service", lambda: drive), \
                mock.patch.object(sys, "argv", args), contextlib.redirect_stdout(out):
            push_estimator.main()
        return out.getvalue(), sheets, drive


class O5LayoutCheckAndGoldenTemplates(PushCase):
    def test_es3_has_no_layout_so_its_fill_is_refused(self):
        self.assertFalse(estimator_layout.layout_recorded("es3"))
        self.assertTrue(estimator_layout.layout_recorded("observability"))
        self.org_json()
        boom = mock.Mock(side_effect=AssertionError("no Google call may be made"))
        with mock.patch.object(push_estimator, "sheets_service", boom), mock.patch.object(push_estimator, "drive_service", boom), \
                mock.patch.object(sys, "argv", ["push_estimator.py", "--org-json", str(self.org_path), "--kind", "es3"]):
            with self.assertRaises(SystemExit) as ctx:
                push_estimator.main()
        self.assertIn("Search (ES3) estimator is not filled", str(ctx.exception))
        boom.assert_not_called()

    def test_kind_both_fills_observability_and_says_search_was_not_filled(self):
        self.org_json()
        out, sheets, drive = self.push("--kind", "both")
        self.assertIn("ES3 NOT FILLED", out)
        self.assertEqual(drive.copies, [estimator_layout.GOLDEN_OBSERVABILITY])
        self.assertEqual({sid for sid, _ in sheets.updates}, {"copy-1"})
        self.assertTrue(all(r.startswith("'Observability Estimator'!") for r in sheets.written()))
        meta = json.loads((self.tmp / "art" / f"Acme_{TODAY}" / "artifacts.json").read_text())
        self.assertIn("Search (ES3) estimator is not filled", meta["es3_not_filled"])
        self.assertEqual([a["kind"] for a in meta["artifacts"]], ["o11y"])

    def test_es3_copy_is_layout_checked_before_any_cell_is_written(self):
        self.org_json()
        anchors = {"es3": {"min_version": None, "labels": {"B12": "storage"}}}
        with mock.patch.dict(estimator_layout.TEMPLATE_ANCHORS, anchors):
            wrong = FakeSheets(labels={"'Estimator'!B12": "Something else"})
            drive = FakeDrive()
            with self.assertRaises(SystemExit) as ctx:
                self.push("--kind", "es3", sheets=wrong, drive=drive)
            self.assertIn("does not match the layout", str(ctx.exception))
            self.assertIn("B12 should read 'storage'", str(ctx.exception))
            self.assertEqual(wrong.updates, [])
            self.assertEqual(drive.trashed, ["copy-1"])
            right = FakeSheets(labels={"'Estimator'!B12": "Storage (GB)"})
            _out, sheets, drive = self.push("--kind", "es3", sheets=right)
            self.assertEqual(drive.copies, [estimator_layout.GOLDEN_ES3])
            written = sheets.written()
            self.assertEqual(written["'Estimator'!C12"], 100.0)
            # The provider and region cells are never written from here.
            self.assertNotIn("'Estimator'!C5", written)
            self.assertNotIn("'Estimator'!C6", written)

    def test_observability_copy_with_moved_rows_is_not_written(self):
        self.org_json()
        sheets = FakeSheets(labels={"'Observability Estimator'!D23": "Synthetic monitoring"})
        with self.assertRaises(SystemExit) as ctx:
            self.push("--kind", "o11y", sheets=sheets)
        self.assertIn("Nothing was written", str(ctx.exception))
        self.assertEqual(sheets.updates, [])

    def test_no_copy_refuses_a_golden_template_before_any_google_call(self):
        self.org_json()
        boom = mock.Mock(side_effect=AssertionError("no Google call may be made"))
        cases = [
            (["--kind", "o11y", "--no-copy"], {}),                                                   # default id is the golden
            (["--kind", "o11y", "--no-copy", "--o11y-id", f"https://docs.google.com/spreadsheets/d/{estimator_layout.GOLDEN_OBSERVABILITY}/edit"], {}),
            (["--kind", "o11y", "--no-copy", "--o11y-id", estimator_layout.GOLDEN_SECURITY], {}),
            (["--kind", "o11y", "--no-copy", "--o11y-id", "team-template"], {"O11Y_SHEET_ID": "team-template"}),
        ]
        for argv, env in cases:
            with self.subTest(argv=argv), mock.patch.dict(os.environ, env), \
                    mock.patch.object(push_estimator, "sheets_service", boom), mock.patch.object(push_estimator, "drive_service", boom), \
                    mock.patch.object(sys, "argv", ["push_estimator.py", "--org-json", str(self.org_path), *argv]):
                with self.assertRaises(SystemExit) as ctx:
                    push_estimator.main()
                self.assertIn("golden template", str(ctx.exception))
        boom.assert_not_called()
        with self.assertRaises(SystemExit):
            push_estimator.push_cells(FakeSheets(), estimator_layout.GOLDEN_ES3, "Estimator", {"C12": 1})

    def test_no_copy_writes_only_to_the_named_copy(self):
        self.org_json()
        sheets = FakeSheets()
        drive_called = mock.Mock(side_effect=AssertionError("Drive is not used with --no-copy"))
        args = ["push_estimator.py", "--org-json", str(self.org_path), "--artifacts-root", str(self.tmp / "art"), "--kind", "o11y", "--no-copy", "--o11y-id", "my-own-copy", "--date", TODAY]
        with mock.patch.object(push_estimator, "sheets_service", lambda: sheets), mock.patch.object(push_estimator, "drive_service", drive_called), \
                mock.patch.object(sys, "argv", args), contextlib.redirect_stdout(io.StringIO()):
            push_estimator.main()
        self.assertEqual({sid for sid, _ in sheets.updates}, {"my-own-copy"})

    def test_layout_check_without_a_version_tab_is_labels_only(self):
        with mock.patch.dict(estimator_layout.TEMPLATE_ANCHORS, {"es3": {"min_version": None, "labels": {"B12": "storage"}}}):
            cells, ranges = estimator_layout.layout_ranges("es3", "Estimator")
            self.assertEqual(ranges, ["'Estimator'!B12"])
            ok = estimator_layout.check_value_ranges("es3", cells, [{"values": [["Storage GB"]]}])
            self.assertEqual(ok["problems"], [])
        # The recorded estimators still need a readable version.
        cells, ranges = estimator_layout.layout_ranges("observability", "Observability Estimator")
        self.assertEqual(ranges[-1], "'Version Control'!A1:A300")
        self.assertTrue(estimator_layout.template_check("observability", {}, None)["problems"])


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class O5AppsScriptImporter(unittest.TestCase):
    """Runs references/apps-script/EstimatorImport.gs in node against a fake spreadsheet."""

    DRIVER = r"""
const fs = require("fs"), vm = require("vm");
const cfg = JSON.parse(process.argv[3]);
const writes = [], alerts = [];
function sheet(name) {
  return {
    getName: () => name,
    getRange: (a1) => ({
      setValue: (v) => writes.push([name, a1, v]),
      getDisplayValues: () => name === "Version Control" ? cfg.versionRows : cfg.options.map((o) => [o]),
      getValue: () => "",
    }),
  };
}
const ss = {
  getId: () => cfg.id,
  getSheets: () => cfg.tabs.map(sheet),
  getSheetByName: (n) => (cfg.tabs.indexOf(n) >= 0 ? sheet(n) : null),
};
const ctx = {
  SpreadsheetApp: { getActive: () => ss, getUi: () => ({ alert: (m) => alerts.push(m) }) },
  PropertiesService: { getDocumentProperties: () => ({ setProperty: () => {}, getProperty: () => null }) },
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
ctx.applyPayload_(JSON.stringify(cfg.payload));
console.log(JSON.stringify({ writes, alerts }));
"""

    def run_gs(self, payload, version="1.22", sheet_id="a-copy", options=None):
        cfg = {
            "id": sheet_id,
            "tabs": ["Observability Estimator", "Estimator_Reference", "Version Control"],
            "versionRows": [["Version"], ["1.19"], [version]],
            "options": OBS_OPTIONS if options is None else options,
            "payload": payload,
        }
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(self.DRIVER)
        self.addCleanup(os.unlink, fh.name)
        gs = SKILL / "references" / "apps-script" / "EstimatorImport.gs"
        res = subprocess.run(["node", fh.name, str(gs), json.dumps(cfg)], capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)

    BLOCK = {"sheet_tab": "Observability Estimator", "layout": "1.22", "cloud": {"provider": "AWS", "region": "us-west-2", "cell": "G8"},
             "cells": {"G23": 90, "G25": None}}

    def test_matching_layout_is_written_with_the_orgs_region(self):
        out = self.run_gs({"o11y": self.BLOCK})
        self.assertIn(["Observability Estimator", "G23", 90], out["writes"])
        self.assertIn(["Observability Estimator", "G25", ""], out["writes"])   # null is written blank
        self.assertIn(["Observability Estimator", "G8", "AWS: us-west-2 (Oregon)"], out["writes"])

    def test_other_version_or_missing_tag_stops_with_nothing_written(self):
        for payload, version in (({"o11y": self.BLOCK}, "1.23"), ({"o11y": {**self.BLOCK, "layout": None}}, "1.22"),
                                 ({"o11y": {k: v for k, v in self.BLOCK.items() if k != "layout"}}, "1.22")):
            out = self.run_gs(payload, version=version)
            self.assertEqual(out["writes"], [], payload)
            self.assertIn("Nothing was written", out["alerts"][0])

    def test_unknown_region_and_golden_template(self):
        out = self.run_gs({"o11y": {**self.BLOCK, "cloud": {"provider": "GCP", "region": "europe-west1", "cell": "G8"}}})
        self.assertNotIn("G8", [w[1] for w in out["writes"]])
        self.assertIn("region_match: none", out["alerts"][0])
        out = self.run_gs({"o11y": self.BLOCK}, sheet_id=estimator_layout.GOLDEN_OBSERVABILITY)
        self.assertEqual(out["writes"], [])
        self.assertIn("golden estimator template", out["alerts"][0])


# ----------------------------------------------------------------------------- O6
class O6HourMixAndBlankCells(PushCase):
    def test_skipped_workload_writes_the_assumption_the_price_uses(self):
        es = FakeES(year("search-a", 100.0), telem={"search-a": 5000})
        for extra in (("--skip-workload",), ()):   # skipped, and fetched with no index telemetry
            with self.subTest(extra=extra):
                org, _ = self.fetch(es, extra=extra)
                es3 = org["offload"]["apps_script_payload"]["es3"]
                self.assertEqual(es3["hour_mix"], {"peak_pct": 26.0, "nonpeak_pct": 74.0, "idle_pct": 0.0, "assumed": True})
                self.assertTrue(es3["assumed"])
                self.assertEqual((es3["cells"]["C21"], es3["cells"]["C25"], es3["cells"]["C27"]), ("26%", "74%", "0%"))
                self.assertEqual(es3["cells"]["C32"], 730)
                for cell in ("C21", "C25", "C27", "C32"):
                    self.assertIn(cell, es3["assumed_cells"])
                self.assertEqual(es3["unmeasured_cells"], ["C19", "C23", "C30"])
                # The Python price of the Search line used the same mix.
                search = next(l for l in org["offload"]["full_split"]["lines"] if l["name"] == "Search")
                self.assertEqual(search["detail"]["hour_mix"], es3["hour_mix"])
                # workload keeps None, which the sibling skills read as "not measured".
                self.assertIsNone(org["workload"]["search"]["peak"]["pct_hours_mo"])

    def test_measured_mix_is_written_as_measured(self):
        wl = {"search": {"peak": {"qps": 40.0, "complexity": "Low", "pct_hours_mo": 26.3}, "nonpeak": {"qps": 6.0, "complexity": "High", "pct_hours_mo": 73.7}, "idle_pct_hours_mo": 0.0},
              "ingest": {"docs_per_sec": 8.0, "avg_doc_bytes": 320, "active_hours_mo": 307}}
        off = {"search_share": {"search_gb": 50.0, "org_stored_gb": 200.0, "share": 0.25}}
        es3 = fetch_org.estimator_payload("Acme", "now", {"provider": "gcp", "region": "gcp-us-central1"}, {"docs_count": 4000, "avg_doc_kb": 0.3}, wl, off)["es3"]
        self.assertEqual(es3["hour_mix"], {"peak_pct": 26.3, "nonpeak_pct": 73.7, "idle_pct": 0.0, "assumed": False})
        self.assertFalse(es3["assumed"])
        self.assertEqual(es3["cells"], {"C12": 50.0, "C13": 1000, "C14": 0.3, "C19": 40.0, "C20": "simple", "C21": "26.3%",
                                        "C23": 6.0, "C24": "complex", "C25": "73.7%", "C27": "0%", "C30": 2.0, "C31": 320, "C32": 307})
        self.assertEqual(es3["cloud"]["region"], "us-central1")

    def test_no_cell_is_left_without_a_value(self):
        es = FakeES(year("search-a", 100.0), telem={})   # no storage, no workload, no Observability slice
        org, _ = self.fetch(es, extra=("--skip-workload",))
        payload = org["offload"]["apps_script_payload"]
        for block in ("es3", "o11y"):
            for cell, value in payload[block]["cells"].items():
                self.assertIsNotNone(value, f"{block} {cell}")
                self.assertNotEqual(value, "", f"{block} {cell}")
        self.assertEqual(payload["o11y"]["cells"]["G23"], 0)
        self.assertEqual(payload["o11y"]["unmeasured_cells"], ["G23", "G25"])

    def test_push_writes_a_blank_instead_of_skipping_an_empty_cell(self):
        self.assertEqual(push_estimator.cell_updates("T", {"A1": None, "A2": 0, "A3": "x"}),
                         [{"range": "'T'!A1", "values": [[""]]}, {"range": "'T'!A2", "values": [[0]]}, {"range": "'T'!A3", "values": [["x"]]}])
        org = self.org_json()
        org["offload"]["apps_script_payload"]["o11y"]["cells"]["G25"] = None   # an older org JSON
        self.org_path.write_text(json.dumps(org))
        _out, sheets, _drive = self.push("--kind", "o11y")
        written = sheets.written()
        self.assertEqual(written["'Observability Estimator'!G25"], "")
        self.assertEqual(len(written), len(org["offload"]["apps_script_payload"]["o11y"]["cells"]) + 1)   # + G8


class O2EstimatorRegionCell(PushCase):
    def test_region_cell_is_the_orgs_own_option(self):
        self.org_json()   # AWS us-west-2
        out, sheets, _ = self.push("--kind", "o11y")
        self.assertEqual(sheets.written()["'Observability Estimator'!G8"], "AWS: us-west-2 (Oregon)")
        meta = json.loads((self.tmp / "art" / f"Acme_{TODAY}" / "artifacts.json").read_text())
        self.assertEqual(meta["artifacts"][0]["region_match"], "exact")
        self.assertEqual(meta["artifacts"][0]["region_option"], "AWS: us-west-2 (Oregon)")

    def test_no_matching_option_leaves_the_template_value_and_records_none(self):
        self.org_json()
        sheets = FakeSheets(options=["AWS: us-east-1 (Virginia)", "AZURE: us-east-2 (Virginia)"])
        out, sheets, _ = self.push("--kind", "o11y", sheets=sheets)
        self.assertNotIn("'Observability Estimator'!G8", sheets.written())
        self.assertIn("NOT WRITTEN", out)
        meta = json.loads((self.tmp / "art" / f"Acme_{TODAY}" / "artifacts.json").read_text())
        self.assertEqual(meta["artifacts"][0]["region_match"], "none")
        self.assertEqual(meta["artifacts"][0]["region_wanted"], {"provider": "AWS", "region": "us-west-2"})
        saved = json.loads(self.org_path.read_text())
        summary = emit_canvas_data.build_summary(saved, emit_canvas_data.build_canvas(saved), emit_canvas_data.build_offload(saved), {}, "exec")
        self.assertTrue(any("no option for AWS us-west-2" in c for c in summary["caveats"]))

    def test_old_org_json_with_another_clouds_region_string_is_not_written(self):
        org = self.org_json()
        block = org["offload"]["apps_script_payload"]["o11y"]
        block.pop("cloud")
        org["offload"]["apps_script_payload"].pop("cloud")
        block["cells"]["G8"] = "AZURE: eastus2 (Virginia)"      # what the old fetch wrote for every org
        org["infrastructure"]["summary"]["region"] = None       # and the old fetch found no AWS region
        self.org_path.write_text(json.dumps(org))
        _out, sheets, _ = self.push("--kind", "o11y")
        self.assertNotIn("'Observability Estimator'!G8", sheets.written())
        self.assertNotIn("AZURE: eastus2 (Virginia)", sheets.written().values())

    def test_apps_script_payload_carries_cloud_not_a_region_string(self):
        org = self.org_json()
        out, notes = emit_estimator_import.import_payload(org, "both")
        self.assertNotIn("es3", out)
        self.assertTrue(any("Search (ES3) estimator is not filled" in n for n in notes))
        self.assertEqual(out["o11y"]["layout"], "1.22")
        self.assertEqual(out["o11y"]["cloud"], {"provider": "AWS", "region": "us-west-2", "cell": "G8"})
        self.assertNotIn("G8", out["o11y"]["cells"])
        with self.assertRaises(SystemExit):
            emit_estimator_import.import_payload(org, "es3")


# ----------------------------------------------------------------------------- O8
class O8FamiliesPartitionEcu(Workdir):
    def test_split_sums_to_the_deployment_ecu(self):
        types = {"elasticsearch": 80.0, "integrationsserver": 12.0, "inference-chat-input": 5.0, "something new": 2.0}
        fam = fetch_org.split_families(types, 100.0, "search")
        self.assertEqual(fam, {"search": 80.0, "observability": 12.0, "security": 0.0, "genai": 5.0, "other": 3.0})
        sec = fetch_org.split_families(types, 100.0, "security")
        self.assertEqual(sec, {"search": 0.0, "observability": 0.0, "security": 95.0, "genai": 5.0, "other": 0.0})
        for f in (fam, sec):
            self.assertAlmostEqual(sum(f.values()), 100.0)

    def test_security_deployment_is_routed_to_security(self):
        kind = fetch_org.classify_dep_kind
        # A security deployment that also bills an integrations server (observability family in the old split).
        self.assertEqual(kind("prod-1", {"security": 95.0, "observability": 0.0}), "security")
        self.assertEqual(kind("siem-prod", {"observability": 12.0}), "security")
        self.assertEqual(kind("apm-prod", {"observability": 12.0}), "o11y")
        self.assertEqual(kind("app-logs", {}), "logs")
        self.assertEqual(kind("products", {"search": 5.0}), "search")

    def test_all_security_org_shows_security_and_families_sum_to_the_total(self):
        docs = []
        for name in ("prod-a", "prod-b"):
            docs += year(name, 90.0) + year(name, 10.0, typ="integrationsserver", sku="Cloud-Enterprise_aws.integrationsserver.c5d_us-west-2_1024_1")
        es = FakeES(docs, telem={"prod-a": 1000, "prod-b": 500}, use_case={"prod-a": "security", "prod-b": "security"})
        org, _ = self.fetch(es)
        total = org["totals"]["ecu"]
        self.assertEqual(org["families"], {"search": 0.0, "observability": 0.0, "security": total, "genai": 0.0, "other": 0.0})
        self.assertAlmostEqual(sum(org["families"].values()), total, places=2)
        for d in org["deployments"]["items"]:
            self.assertAlmostEqual(sum(d["families"].values()), d["ecu"], places=2)
        by_kind = org["offload"]["by_kind"]
        self.assertEqual(by_kind["security"]["n"], 2)
        self.assertEqual(by_kind["o11y"]["n"], 0)
        sec_line = next(l for l in org["offload"]["full_split"]["lines"] if l["name"] == "Security")
        self.assertGreater(sec_line["ech_mo"], 0)
        self.assertGreater(sec_line["serverless_mo"], 0)

    def test_mixed_org_families_sum_to_one_hundred_percent(self):
        docs = year("sec", 100.0) + year("obs", 50.0, typ="integrationsserver") + year("srch", 25.0) + year("ai", 5.0, typ="inference")
        es = FakeES(docs, use_case={"sec": "security", "obs": "observability", "srch": "search"})
        org, _ = self.fetch(es)
        self.assertAlmostEqual(sum(org["families"].values()), org["totals"]["ecu"], places=2)
        self.assertAlmostEqual(org["families"]["security"], 100.0 * 365, places=2)


# ----------------------------------------------------------------------------- O9
class O9HourShares(Workdir):
    def check(self, shares):
        for v in shares:
            self.assertGreaterEqual(v, 0.0)
            self.assertLessEqual(v, 100.0)
        self.assertAlmostEqual(sum(shares), 100.0, places=6)

    def test_constant_low_qps_no_longer_goes_negative(self):
        self.assertEqual(fetch_org.hour_shares([0.5] * 20), (0.0, 0.0, 100.0))    # was 100 / -100 / 100
        self.assertEqual(fetch_org.hour_shares([5.0] * 20), (100.0, 0.0, 0.0))
        self.assertEqual(fetch_org.hour_shares([0.3]), (0.0, 0.0, 100.0))
        self.assertEqual(fetch_org.hour_shares([0.1] * 8 + [5, 10]), (20.0, 0.0, 80.0))
        self.assertEqual(fetch_org.hour_shares([]), (0.0, 0.0, 0.0))

    def test_shares_are_a_partition_for_any_series(self):
        import random

        rnd = random.Random(7)
        for _ in range(500):
            n = rnd.randint(1, 25)
            series = [rnd.choice([0.0, 0.2, 1.0, 1.0, 3.0, rnd.uniform(0, 60)]) for _ in range(n)]
            self.check(fetch_org.hour_shares(series))

    def test_fetch_workload_with_constant_low_qps(self):
        days = [(date(2026, 10, 24) + timedelta(days=i)).isoformat() for i in range(21)]
        rows = [(43200.0 * i, 432000.0 * i, 1000.0) for i in range(21)]   # 0.5 QPS every day
        es = FakeES(year("a", 10.0), telem={"a": 10}, index_telemetry={"days": days, "idx": {"idx-1": rows}})
        org, _ = self.fetch(es)
        search = org["workload"]["search"]
        shares = (search["peak"]["pct_hours_mo"], search["nonpeak"]["pct_hours_mo"], search["idle_pct_hours_mo"])
        self.assertEqual(shares, (0.0, 0.0, 100.0))
        self.check(shares)
        mix = org["offload"]["apps_script_payload"]["es3"]["hour_mix"]
        self.assertEqual((mix["peak_pct"], mix["nonpeak_pct"], mix["idle_pct"], mix["assumed"]), (0.0, 0.0, 100.0, False))

    def test_price_mix_is_normalised_to_one_hundred(self):
        bad = {"search": {"peak": {"pct_hours_mo": 100.0}, "nonpeak": {"pct_hours_mo": -100.0}, "idle_pct_hours_mo": 100.0}}   # an older org JSON
        mix = fetch_org.search_hour_mix(bad)
        self.check((mix["peak_pct"], mix["nonpeak_pct"], mix["idle_pct"]))


# ----------------------------------------------------------------------------- O10
class O10CanvasAndPdfText(Workdir):
    def org(self, **pivot_extra):
        pivot = {
            "organization": {"name": "Acme", "id": "org1", "ARR": 1250000.0, "AE": "A. Person", "HV": ["h1", "h2"], "segment": "ENTR", "geo": "AMER",
                             "contract": {"end": ["2027-01-11T00:00:00+00:00"]}},
            "pace": {"commit": 100, "consumption_rate": 0.9}, "runout": {"runway_days": 200, "runout_date": "2027-02-09T00:00:00Z"},
            "consumption": {"annual_projection": 60000}, "license": "enterprise",
        }
        pivot.update(pivot_extra)
        docs = year("search-a", 100.0) + year("obs-apm", 60.0, typ="integrationsserver") + year("tiny", 1.0)
        es = FakeES(docs, pivots=[pivot], telem={"search-a": 100, "obs-apm": 900, "tiny": 1})
        return self.fetch(es)[0]

    def test_canvas_reads_the_field_names_fetch_writes(self):
        org = self.org()
        canvas = emit_canvas_data.build_canvas(org)
        self.assertEqual(canvas["ae"], "A. Person")
        self.assertEqual(canvas["hv"], ["h1", "h2"])
        self.assertEqual(canvas["arr"], 1250000.0)
        self.assertEqual(canvas["months_n"], 13)
        summary = emit_canvas_data.build_summary(org, canvas, emit_canvas_data.build_offload(org), {"folder": "f"}, "exec")
        joined = " ".join(summary["chat_bullets_hint"])
        self.assertIn("vs ARR ~$1,250,000", joined)
        self.assertNotIn("None", joined)
        self.assertLessEqual(len(summary["chat_bullets_hint"]), 5)

    def test_monthly_by_deployment_dict_survives_the_slim(self):
        org = self.org()
        slim = emit_canvas_data._trim_monthly(org["monthly"], top_n=2)
        self.assertEqual(list(slim["by_deployment"]), ["search-a", "obs-apm"])
        self.assertEqual(len(slim["by_deployment"]["search-a"]["ecu"]), len(slim["months"]))
        self.assertIn("avg_active", slim["by_deployment"]["search-a"])

    def test_vector_model_counts_survive_the_slim(self):
        ml = {"total_ecu": 5, "vectors": {"count": 3, "with_elser": 1, "with_named_models": 1, "unnamed_model": 1,
                                         "model_counts": [{"model": "ELSER (in-cluster)", "deployments": 1}], "by_deployment": [{"name": "a"}]}}
        slim = emit_canvas_data._trim_ml(ml)["vectors"]
        self.assertEqual(slim["unnamed_model"], 1)
        self.assertEqual(slim["model_counts"], [{"model": "ELSER (in-cluster)", "deployments": 1}])
        self.assertEqual(slim["with_named_models"], 1)
        self.assertEqual(slim["items"], [{"name": "a"}])

    def test_pdf_prints_names_not_dicts(self):
        org = self.org()
        text = page_text(render_org_pdf.render_html(emit_canvas_data.build_canvas(org), emit_canvas_data.build_offload(org), "full"))
        self.assertNotIn("{'", text)
        self.assertIn("AWS Cloud", text)
        self.assertIn("us-west-2 Region", text)
        self.assertIn("2025-11-14 → 2026-11-13 (365 days)", text)
        self.assertIn("Generated by k360-org-report", text)
        self.assertEqual(render_org_pdf.range_text({"start": "2026-01-01", "end": "2026-03-31", "days": 90}), "2026-01-01 → 2026-03-31 (90 days)")

    def test_runout_sentence_compares_the_dates(self):
        after = {"runout_date": "2027-02-09", "nearest_contract_end": "2027-01-11", "runway_days": 88}
        before = {"runout_date": "2026-12-13", "nearest_contract_end": "2029-02-10", "runway_days": 30}
        self.assertEqual(render_org_pdf.runout_text(after), "runout 2027-02-09 (88d) after contract end 2027-01-11")
        self.assertEqual(render_org_pdf.runout_text(before), "runout 2026-12-13 (30d) before contract end 2029-02-10")
        self.assertEqual(render_org_pdf.runout_text({"runout_date": "2027-01-11", "nearest_contract_end": "2027-01-11"}), "runout 2027-01-11 on contract end 2027-01-11")
        self.assertEqual(render_org_pdf.runout_text({}), "no runout date")
        self.assertIn("after contract end 2027-01-11", emit_canvas_data.runout_phrase(after))
        self.assertIn("before contract end 2029-02-10", emit_canvas_data.runout_phrase(before))
        text = page_text(render_org_pdf.render_html(emit_canvas_data.build_canvas(self.org()), {}, "exec"))
        self.assertIn("runout 2027-02-09 (200d) after contract end 2027-01-11", text)
        self.assertNotIn("before 2027-01-11", text)

    def test_recommendations_follow_the_data(self):
        lines = [
            {"name": "Observability + logging", "project": "Observability (Complete)", "n": 2, "ech_mo": 1000.0, "serverless_mo": 900.0, "save_mo": 100.0},
            {"name": "Security", "project": "Security (Complete proxy)", "n": 0, "ech_mo": 0.0, "serverless_mo": 0.0, "save_mo": 0.0},
            {"name": "Search", "project": "Elasticsearch ES3 On-demand", "n": 3, "ech_mo": 5000.0, "serverless_mo": 2000.0, "save_mo": 3000.0},
        ]
        recs = render_org_pdf.recommendations({"priority": False, "watch": False}, {"lines": lines, "not_priced_n": 0}, {})
        self.assertEqual(recs[0], "P0 — Monitor burndown; no overage flag")
        self.assertIn("Move Search to Elasticsearch ES3 On-demand", recs[1])
        self.assertIn("the largest saving line", recs[1])            # Search here, not Observability
        self.assertNotIn("largest", recs[2])
        self.assertFalse(any("Security" in r for r in recs))          # no security deployments, no security line
        # Serverless dearer than ECH: the move is not recommended.
        dear = [{"name": "Search", "project": "Elasticsearch ES3 On-demand", "n": 1, "ech_mo": 100.0, "serverless_mo": 400.0, "save_mo": -300.0}]
        recs = render_org_pdf.recommendations({"priority": True, "reasons": ["consumption_rate 1.2 > 1.0"]}, {"lines": dear, "not_priced_n": 4, "not_priced_ecu_mo": 250.0}, {"stale_n": 2})
        self.assertEqual(recs[0], "P0 — Address overage / runway risk: consumption_rate 1.2 > 1.0")
        self.assertIn("Keep Search on ECH for now", recs[1])
        self.assertFalse(any("Move Search" in r for r in recs))
        self.assertIn("Size the 4 deployments left out of the comparison (250 ECU/mo)", recs[2])
        self.assertIn("storage snapshots are stale", recs[3])
        self.assertEqual(render_org_pdf.save_text({"lines": dear, "priced_n": 1, "save_vs_ech_yr": -3600.0}), "Serverless is a ballpark $3,600/yr more than ECH on 1 compared deployments.")


# ----------------------------------------------------------------------------- O11
class O11ReuseAndOrgId(unittest.TestCase):
    ORG = {"organization": {"name": "Acme Corp", "id": "org1"}, "generated_at": "2026-11-12T10:00Z", "range": {"end": "2026-11-12"}}
    NOW = date(2026, 11, 13)

    def test_another_organizations_file_stops_the_run(self):
        with self.assertRaises(SystemExit) as ctx:
            run_org_report.check_reused_org(self.ORG, "Globex", today=self.NOW)
        self.assertIn("'Acme Corp'", str(ctx.exception))
        self.assertIn("'Globex'", str(ctx.exception))
        for wrong in ("Corp", "Acme Corporation", ""):
            with self.assertRaises(SystemExit):
                run_org_report.check_reused_org(self.ORG, wrong, today=self.NOW)
        with self.assertRaises(SystemExit):
            run_org_report.check_reused_org({"organization": {}}, "Acme", today=self.NOW)

    def test_same_name_any_case_is_accepted(self):
        self.assertEqual(run_org_report.check_reused_org(self.ORG, "acme corp", today=self.NOW), [])
        notes = run_org_report.check_reused_org(self.ORG, "ACME", today=self.NOW)   # the name fetch resolves "Acme" to
        self.assertEqual(len(notes), 1)
        self.assertIn("'Acme Corp'", notes[0])

    def test_org_id_must_be_in_the_file(self):
        self.assertEqual(run_org_report.check_reused_org(self.ORG, "Acme Corp", ["org1"], today=self.NOW), [])
        with self.assertRaises(SystemExit) as ctx:
            run_org_report.check_reused_org(self.ORG, "Acme Corp", ["org9"], today=self.NOW)
        self.assertIn("org9", str(ctx.exception))

    def test_old_file_warns_with_its_fetch_date(self):
        self.assertEqual(run_org_report.check_reused_org(self.ORG, "Acme Corp", today=date(2026, 11, 19)), [])   # 7 days
        warn = run_org_report.check_reused_org(self.ORG, "Acme Corp", today=date(2026, 11, 20))
        self.assertEqual(len(warn), 1)
        self.assertIn("fetched on 2026-11-12, 8 days ago", warn[0])
        no_stamp = {"organization": {"name": "Acme Corp"}, "range": {"end": "2026-10-01"}}
        self.assertIn("2026-10-01", run_org_report.check_reused_org(no_stamp, "Acme Corp", today=self.NOW)[0])

    def test_org_id_is_passed_to_fetch(self):
        with mock.patch.object(sys, "argv", ["run_org_report.py", "--org", "Acme", "--org-id", "o1", "--org-id", "o2", "--skip-workload", "--days", "90"]):
            ap_args = None
            real = run_org_report.fetch_command

            made: list[Path] = []

            def capture(args, scripts, out):
                nonlocal ap_args
                ap_args = args
                made.append(Path(out).parent)
                raise SystemExit("stop after the fetch command")

            with mock.patch.object(run_org_report, "fetch_command", capture), mock.patch.object(os, "chdir", lambda p: None):
                with self.assertRaises(SystemExit):
                    run_org_report.main()
            # The fetch goes to a folder of its own, and a failed fetch leaves nothing behind.
            self.assertNotEqual(made[0], Path("/tmp"))
            self.assertFalse(made[0].exists())
        cmd = real(ap_args, HERE, Path("/x/org.json"))
        self.assertEqual(cmd[cmd.index("--out") + 1], "/x/org.json")
        self.assertEqual([cmd[i + 1] for i, a in enumerate(cmd) if a == "--org-id"], ["o1", "o2"])
        self.assertIn("--skip-workload", cmd)
        self.assertEqual(cmd[cmd.index("--days") + 1], "90")

    def test_no_shared_tmp_file(self):
        for name in ("run_org_report.py", "push_estimator.py", "emit_canvas_data.py", "emit_estimator_import.py", "render_org_pdf.py", "fetch_org.py"):
            self.assertNotIn("/tmp/org_report.json", (HERE / name).read_text().replace("A fixed /tmp/org_report.json was", ""), name)
        self.assertNotIn("/tmp/org_report.json", (SKILL / "SKILL.md").read_text())
        self.assertNotIn("/tmp/org_report.json", (SKILL / "references" / "apps-script" / "EstimatorImport.gs").read_text())


# ----------------------------------------------------------------------------- O12
class O12SkillName(unittest.TestCase):
    def test_front_matter_name_is_the_folder_name(self):
        text = (SKILL / "SKILL.md").read_text()
        front = text.split("---")[1]
        self.assertIn(f"name: {SKILL.name}", front.splitlines())
        self.assertEqual(SKILL.name, "k360-org-report")

    def test_old_name_is_gone(self):
        for path in [SKILL / "SKILL.md", *sorted((SKILL / "references").rglob("*")), *sorted(HERE.glob("*.py"))]:
            if path.is_file() and path.name != Path(__file__).name and path.suffix in (".md", ".py", ".gs", ".json", ".txt"):
                self.assertNotIn("cloud-tracker-org-report", path.read_text(), path)


# ----------------------------------------------------------------------------- O13
class O13RosterTruncation(Workdir):
    def test_every_deployment_terms_uses_one_size_and_ecu_order(self):
        es = FakeES(year("a", 10.0), telem={"a": 10})
        self.fetch(es)
        bodies = {path.split("/")[0]: body for path, body in es.calls}
        bill = bodies["cloud-tracker-billing-*"]["aggs"]
        size = fetch_org.DEP_TERMS_SIZE
        self.assertEqual(size, 300)
        self.assertEqual(bill["by_dep"]["terms"], {"field": "deployment.name", "size": size, "order": {"ecu": "desc"}})
        self.assertEqual(bill["by_day"]["aggs"]["by_dep"]["terms"], {"field": "deployment.name", "size": size, "order": {"ecu": "desc"}})
        self.assertEqual(bodies["cloud-tracker-features-*"]["aggs"]["by_dep"]["terms"], {"field": "deployment.name", "size": size})
        self.assertEqual(bodies["cloud-tracker-telemetry"]["aggs"]["by_dep"]["terms"], {"field": "deployment.name", "size": size})
        self.assertEqual(bodies["cloud-tracker-deployments"]["size"], size)

    def test_big_spender_with_few_lines_stays_on_the_daily_roster(self):
        # 320 deployments: 319 small ones with many billing lines a day, one big spender with a single line.
        docs = dep_docs("big-spender", "2026-08-01", TODAY, 5000.0, lines=1)
        for i in range(319):
            docs += dep_docs(f"small-{i:03d}", "2026-10-15", TODAY, 1.0, lines=50)
        org, err = self.fetch(FakeES(docs), days=120, chart="day")
        for window in ("30", "90"):
            self.assertIn("big-spender", org["chart"]["billed_windows"][window]["names"], window)
        self.assertEqual(org["chart"]["billed_window"], org["chart"]["billed_windows"]["30"])
        self.assertIn("big-spender", org["monthly"]["by_deployment"])
        self.assertEqual(org["deployments"]["items"][0]["name"], "big-spender")
        # 320 names do not fit in 300: the output says which aggregations were cut, and stderr warns.
        self.assertEqual(org["truncated"], ["billing.by_dep", "billing.by_day.by_dep"])
        self.assertIn("WARN: billing.by_dep holds more than 300 deployments", err)
        self.assertIn("WARN: billing.by_day.by_dep holds more than 300 deployments", err)
        # The saved rosters agree with each other: every name on a billed window is in monthly.by_deployment.
        self.assertLessEqual(set(org["chart"]["billed_windows"]["90"]["names"]), set(org["monthly"]["by_deployment"]))
        canvas = emit_canvas_data.build_canvas(org)
        self.assertEqual(canvas["truncated"], org["truncated"])
        summary = emit_canvas_data.build_summary(org, canvas, emit_canvas_data.build_offload(org), {}, "exec")
        self.assertTrue(any("truncated" in c for c in summary["caveats"]))

    def test_small_org_reports_nothing_truncated(self):
        org, err = self.fetch(FakeES(year("a", 10.0) + year("b", 5.0), telem={"a": 10}))
        self.assertEqual(org["truncated"], [])
        self.assertNotIn("holds more than", err)

    def test_billed_window_meaning_is_unchanged(self):
        docs = dep_docs("old", "2026-06-01", "2026-08-31", 10.0) + dep_docs("mid", "2026-09-01", "2026-10-20", 10.0) + dep_docs("new", "2026-10-01", "2026-11-10", 10.0)
        org, _ = self.fetch(FakeES(docs), days=200, chart="day")
        w30, w90 = org["chart"]["billed_windows"]["30"], org["chart"]["billed_windows"]["90"]
        # The window ends on the last day with a bill (the 10th), not on the fetch day.
        self.assertEqual((w30["start"], w30["end"], w30["days"]), ("2026-10-12", "2026-11-10", 30))
        self.assertEqual(w30["names"], ["mid", "new"])
        self.assertEqual((w90["start"], w90["end"], w90["days"]), ("2026-08-13", "2026-11-10", 90))
        self.assertEqual(w90["names"], ["mid", "new", "old"])


# ----------------------------------------------------------------------------- low items
class LowKibanaUrl(Workdir):
    def test_url_must_be_https_without_credentials(self):
        ok = fetch_org.es_base_and_key({"KIBANA_URL": "https://x.kb.us-east-1.aws.example.io/", "ELASTIC_API_KEY": "k"})
        self.assertEqual(ok, ("https://x.es.us-east-1.aws.example.io", "k"))
        for url, why in (("http://x.kb.example.io", "must be https"), ("x.kb.example.io", "must be https"),
                         ("https://user:pw@x.kb.example.io", "username or password"), ("https://user@x.kb.example.io", "username or password")):
            with self.assertRaises(SystemExit) as ctx:
                fetch_org.es_base_and_key({"KIBANA_URL": url, "ELASTIC_API_KEY": "k"})
            self.assertIn(why, str(ctx.exception))
        with self.assertRaises(SystemExit):
            fetch_org.es_base_and_key({"KIBANA_URL": "https://x.kb.example.io"})

    def test_fetch_stops_before_any_search_on_a_plain_http_url(self):
        env = self.tmp / "bad.env"
        env.write_text("KIBANA_URL=http://fake.kb.example.invalid\nELASTIC_API_KEY=fake\n")
        es = FakeES([])
        with self.assertRaises(SystemExit) as ctx:
            self.fetch(es, env=env)
        self.assertIn("must be https", str(ctx.exception))
        self.assertEqual(es.calls, [])


def pivot(name, oid):
    return {"organization": {"name": name, "id": oid}, "pace": {}}


class LowOrgNameLookup(Workdir):
    def test_fallback_query_is_a_literal_prefix(self):
        q = fetch_org.org_name_query("Foo Corp")
        self.assertEqual(q, {"wildcard": {"organization.name": {"value": "Foo Corp*", "case_insensitive": True}}})
        self.assertEqual(fetch_org.org_name_query("A*B?C\\D")["wildcard"]["organization.name"]["value"], "A\\*B\\?C\\\\D*")

    def test_exact_or_unique_prefix_only(self):
        choose = fetch_org.choose_pivots
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(choose([pivot("Foo Corp", "1"), pivot("Foo Corp Europe", "2")], "foo corp"), [pivot("Foo Corp", "1")])
            self.assertEqual(choose([pivot("GlaxoSmithKline LLC", "1")], "GlaxoSmithKline"), [pivot("GlaxoSmithKline LLC", "1")])
        self.assertIn("org → GlaxoSmithKline LLC", err.getvalue())
        with self.assertRaises(SystemExit) as ctx:     # a single stray hit that is neither exact nor a prefix
            choose([pivot("Corp Holdings", "9")], "Foo Corp")
        self.assertIn("No organization is named 'Foo Corp'", str(ctx.exception))
        with self.assertRaises(SystemExit) as ctx:     # several organizations start with it
            choose([pivot("Foo Corp Europe", "2"), pivot("Foo Corp Asia", "3")], "Foo")
        self.assertIn("Several organizations start with 'Foo'", str(ctx.exception))

    def test_fetch_refuses_a_stray_hit(self):
        es = FakeES(year("a", 1.0), pivots=[pivot("Corp Holdings", "9")])
        with self.assertRaises(SystemExit) as ctx:
            self.fetch(es, org="Foo Corp")
        self.assertIn("No organization is named", str(ctx.exception))
        self.assertFalse(any("billing" in path for path, _ in es.calls))
        for _path, body in es.calls:
            self.assertNotIn("query_string", json.dumps(body))


class LowFileModes(PushCase):
    def test_customer_files_are_private(self):
        org = self.org_json()
        self.assertEqual(mode(self.org_path), 0o600)
        out_dir = self.tmp / "emit"
        with mock.patch.object(sys, "argv", ["emit_canvas_data.py", "--org-json", str(self.org_path), "--out-dir", str(out_dir)]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            emit_canvas_data.main()
        info = json.loads(out.getvalue())
        self.assertEqual(mode(out_dir), 0o700)
        for key in ("canvas", "offload", "summary"):
            self.assertEqual(mode(info[key]), 0o600, key)
        self.push("--kind", "o11y")
        folder = self.tmp / "art" / f"Acme_{TODAY}"
        self.assertEqual(mode(folder), 0o700)
        for path in folder.iterdir():
            self.assertEqual(mode(path), 0o600, path.name)
        self.assertEqual({p.name for p in folder.iterdir()}, {"artifacts.json", "README.md", f"Acme_{TODAY}_org_report.json"})
        self.assertEqual(org["organization"]["name"], "Acme")


# ----------------------------------------------------------------------------- sibling reader
class SiblingReaderStillWorks(Workdir):
    """analyze.build_account in k360-account-analysis reads the org JSON this skill writes."""

    def test_build_account_reads_the_new_shape(self):
        sibling = SKILL.parent / "k360-account-analysis" / "scripts"
        if not (sibling / "analyze.py").is_file():
            self.skipTest("k360-account-analysis is not beside this skill")
        sys.path.insert(0, str(sibling))
        self.addCleanup(sys.path.remove, str(sibling))
        try:
            import analyze  # type: ignore
        except Exception as ex:  # noqa: BLE001 - the sibling is another skill; its import errors are not this suite's
            self.skipTest(f"analyze.py did not import: {ex}")
        docs = year("search-a", 100.0) + year("obs-apm", 60.0, typ="integrationsserver") + year("sec", 30.0) \
            + dep_docs("deleted", "2025-11-14", "2026-03-01", 5.0)
        es = FakeES(docs, telem={"search-a": 100, "obs-apm": (900, "2026-03-12"), "sec": 50, "deleted": 10}, use_case={"sec": "security"})
        org, _ = self.fetch(es, chart="day")
        acc = analyze.build_account(org, None, when=date(2026, 11, 13))
        self.assertEqual(acc["ecu"]["months_in_lookback"], 13)
        self.assertAlmostEqual(acc["ecu"]["total_lookback_ech_named"], org["totals"]["ecu"], places=2)
        self.assertEqual(acc["storage"]["data_stored_gb"], org["storage"]["data_stored_gb"])
        self.assertEqual(acc["deployments"]["active"]["names"], org["chart"]["billed_windows"]["90"]["names"])
        self.assertEqual(acc["solutions"]["totals"]["security"]["deployments"], 1)


if __name__ == "__main__":
    unittest.main()
