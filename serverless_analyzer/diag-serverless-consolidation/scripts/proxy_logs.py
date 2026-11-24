"""Search workload from the service proxy request logs.

The query is the one in esql.txt (service-proxy-requests-*). The proxy logs sit behind a Kibana
(`PROXY_LOGS_URL`) and are read with `PROXY_LOGS_API_KEY`. A multi-day call of the whole query times
out at the gateway, so the per-minute and per-hour stages run one UTC day at a time and the last stage
(peak and non-peak windows, percentiles, complexity) runs here on the hourly rows. The arithmetic is
the same as the query's. Percentiles use linear interpolation, where Elasticsearch uses an
approximation, so a figure can differ in the last digit.

The filter is the deployment, not the cluster name: `handling_deployment` is the deployment id the
rest of the pipeline already carries. Monitoring and browser traffic is dropped by user agent.
"""

from __future__ import annotations

import json
import math
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from constants import HOURS_PER_MONTH, MODERATE_MAX_MS, SIMPLE_MAX_MS

INDEX = "logging-*:service-proxy-requests-*"
DEFAULT_DAYS = 7
# Socket timeout of one ES|QL call. A call already in flight when the deadline passes can run this long.
REQUEST_TIMEOUT_S = 300
# A span that keeps failing is halved at most this many times (24 h, 12 h, 6 h, 3 h). Halving is for a
# gateway that times out on a big span; it is not a way to keep retrying a cluster that is down.
MAX_SPLIT_DEPTH = 3
# Peak window from the query: weekdays (Mon=1 .. Sun=7), UTC hours 13 up to but not including 23.
PEAK_WEEKDAYS = 5
PEAK_START_HOUR = 13
PEAK_END_HOUR = 23
# Not customer queries: Metricbeat and the admin tooling poll every deployment, browsers are people in Kibana.
BLOCKED_USER_AGENTS = ("Elastic-Metricbeat*", "Mozilla*", "fac/*")
_ID = re.compile(r"^[0-9a-f]{32}$")


def proxy_settings(env: dict) -> tuple[str, str]:
    url = (env.get("PROXY_LOGS_URL") or "").strip().rstrip("/")
    key = (env.get("PROXY_LOGS_API_KEY") or "").strip()
    if not url or not key:
        raise ValueError("PROXY_LOGS_URL and PROXY_LOGS_API_KEY are not set in .env")
    if not url.startswith("https://"):
        raise ValueError("PROXY_LOGS_URL must be https")
    return url, key


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # the key must never follow a redirect
        return None


def run_esql(base: str, key: str, query: str, timeout: int = REQUEST_TIMEOUT_S) -> dict:
    """One ES|QL call through Kibana's internal search route. Returns the raw response."""
    req = urllib.request.Request(
        base + "/internal/search/esql",
        data=json.dumps({"params": {"query": query}}).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "kbn-xsrf": "true", "x-elastic-internal-origin": "kibana"},
    )
    req.add_unredirected_header("Authorization", f"ApiKey {key}")
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout) as resp:
            return raw_response(json.load(resp))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"proxy logs HTTP {exc.code}: {exc.read(300)!r}") from exc


def raw_response(body) -> dict:
    """The ES|QL result inside Kibana's envelope, or an error that says why it cannot be trusted.

    A result with no `rawResponse` is not an empty day. A partial result (`is_partial`) or one where a
    cross-cluster search skipped or failed a cluster has undercounted requests, and an undercounted QPS
    would be quoted as measured. Both raise `RuntimeError`, which `_read_span` retries and, if it keeps
    happening, reports against the region instead of quoting it.
    """
    raw = body.get("rawResponse") if isinstance(body, dict) else None
    if not isinstance(raw, dict):
        raise RuntimeError("proxy logs response has no rawResponse")
    if raw.get("is_partial"):
        raise RuntimeError("proxy logs returned a partial result (is_partial), so request counts may be low")
    clusters = raw.get("_clusters") if isinstance(raw.get("_clusters"), dict) else {}
    short = {name: int(clusters.get(name) or 0) for name in ("skipped", "failed", "partial")}
    if any(short.values()):
        said = ", ".join(f"{count} {name}" for name, count in short.items() if count)
        raise RuntimeError(f"proxy logs searched fewer clusters than asked ({said}), so request counts may be low")
    return raw


def _in_list(ids: list[str]) -> str:
    clean = [str(i).strip().lower() for i in ids]
    for value in clean:
        if not _ID.match(value):
            raise ValueError(f"not a deployment id: {value!r}")
    return ", ".join(f'"{value}"' for value in clean)


def hourly_query(deployment_ids: list[str], start_at: datetime, end_at: datetime | None = None) -> str:
    """The query's minute and hour stages for a span of whole hours (a UTC day by default). One row per hour.

    Hours are the query's own grouping, so any span that starts and ends on an hour gives the same rows as
    the whole day cut into pieces.
    """
    start = start_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = (end_at or (start_at + timedelta(days=1))).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    blocked = " OR ".join(f'user_agent LIKE "{pattern}"' for pattern in BLOCKED_USER_AGENTS)
    return f'''FROM {INDEX}
| WHERE @timestamp >= "{start}" AND @timestamp < "{end}"
 AND handling_deployment IN ({_in_list(deployment_ids)})
 AND request_source == "external"
 AND NOT ({blocked})
| EVAL
 minute = DATE_TRUNC(1 minute, @timestamp),
 hour = DATE_TRUNC(1 hour, @timestamp),
 is_search = CASE(action == "search" OR request_path LIKE "*/_search*" OR request_path LIKE "*/_msearch*" OR request_path LIKE "*/_count*" OR request_path LIKE "*/_pit*", 1, 0),
 search_latency_ms = CASE(is_search == 1, response_time, null)
| INLINE STATS search_latency_p90_ms = PERCENTILE(search_latency_ms, 90), search_latency_p99_ms = PERCENTILE(search_latency_ms, 99) BY hour
| STATS search_requests = SUM(is_search), search_latency_p90_ms = MAX(search_latency_p90_ms), search_latency_p99_ms = MAX(search_latency_p99_ms) BY minute
| EVAL minute_search_qps = search_requests / 60.0, hour = DATE_TRUNC(1 hour, minute)
| STATS search_requests = SUM(search_requests), peak_minute_search_qps = MAX(minute_search_qps), search_latency_p90_ms = MAX(search_latency_p90_ms), search_latency_p99_ms = MAX(search_latency_p99_ms) BY hour
| SORT hour'''


def _hour_rows(raw: dict) -> list[dict]:
    names = [col["name"] for col in raw.get("columns") or []]
    rows = []
    for values in raw.get("values") or []:
        row = dict(zip(names, values))
        if not row.get("hour"):
            continue
        rows.append({
            "hour": datetime.fromisoformat(str(row["hour"]).replace("Z", "+00:00")).astimezone(timezone.utc),
            "search_requests": float(row.get("search_requests") or 0),
            "peak_minute_search_qps": float(row.get("peak_minute_search_qps") or 0),
            "p90": None if row.get("search_latency_p90_ms") is None else float(row["search_latency_p90_ms"]),
            "p99": None if row.get("search_latency_p99_ms") is None else float(row["search_latency_p99_ms"]),
        })
    return rows


def percentile(values: list[float], pct: float) -> float | None:
    data = sorted(v for v in values if v is not None)
    if not data:
        return None
    if len(data) == 1:
        return data[0]
    rank = (len(data) - 1) * pct / 100.0
    low, high = math.floor(rank), math.ceil(rank)
    return data[low] + (data[high] - data[low]) * (rank - low)


def window_of(hour: datetime) -> str:
    weekday = hour.isoweekday()
    peak = weekday <= PEAK_WEEKDAYS and PEAK_START_HOUR <= hour.hour < PEAK_END_HOUR
    return "peak" if peak else "non_peak"


def complexity_of(ms: float | None) -> str | None:
    if ms is None:
        return None
    return "simple" if ms <= SIMPLE_MAX_MS else "moderate" if ms <= MODERATE_MAX_MS else "complex"


def summarize(hours: list[dict], total_hours: int | None = None) -> dict:
    """The query's last stages over hourly rows: one result per window, plus the hour shares.

    Only an hour with requests is a row, so `total_hours` is every hour in the window. Each band's
    share is its active hours over that total, and what is left is idle. Percentiles and averages run
    over a band's active hours, because idle is priced as its own state. Without `total_hours` the
    share is over the rows given.
    """
    by_window: dict[str, list[dict]] = {"peak": [], "non_peak": []}
    for row in hours:
        if row["search_requests"] <= 0:
            continue
        by_window[window_of(row["hour"])].append(row)
    total = max(int(total_hours or 0), sum(len(rows) for rows in by_window.values())) or len(hours)
    out: dict[str, dict] = {}
    for name, rows in by_window.items():
        if not rows:
            continue
        avg = [r["search_requests"] / 3600.0 for r in rows]
        peak_minute = [r["peak_minute_search_qps"] for r in rows]
        p90 = percentile([r["p90"] for r in rows if r["p90"] is not None], 50)
        p99 = percentile([r["p99"] for r in rows if r["p99"] is not None], 50)
        p95_minute = percentile(peak_minute, 95)
        out[name] = {
            # The unrounded p90. Complexity is bucketed from this, so the sheet and the library bucket the same
            # figure `complexity_of` did: 250.4 ms is moderate, not the "250" it displays as.
            "p90_ms": p90,
            "hours": len(rows),
            "pct_of_hours": round(100.0 * len(rows) / total) if total else 0,
            "recommended_qps": math.ceil(p95_minute) if p95_minute is not None else 0,
            "query_complexity": complexity_of(p90),
            "avg_qps": round(sum(avg) / len(avg), 1),
            "p95_hourly_qps": round(percentile(avg, 95), 1),
            "p95_peak_minute_qps": round(p95_minute, 1) if p95_minute is not None else None,
            "max_peak_minute_qps": round(max(peak_minute), 1),
            "typical_p90_latency_ms": None if p90 is None else round(p90),
            "typical_p99_latency_ms": None if p99 is None else round(p99),
        }
    return out


def _past(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def _read_span(base, key, deployment_ids, start, end, run, attempts: int = 3, pause: float = 5.0,
               deadline: float | None = None, depth: int = 0) -> list[dict]:
    """One span of hours. A gateway timeout or dropped connection is retried, then the span is split in half.

    `deadline` is a `time.monotonic()` instant. Past it no further call is made and no further wait is
    taken: the span raises, and the caller records the region as unread. A span is halved at most
    `MAX_SPLIT_DEPTH` times. Without a deadline the retries and halving are bounded only by those two.
    """
    last: Exception | None = None
    for attempt in range(attempts):
        if _past(deadline):
            raise RuntimeError(f"proxy logs exceeded their time budget before {start:%Y-%m-%d %H:00} UTC was read")
        try:
            return _hour_rows(run(base, key, hourly_query(deployment_ids, start, end)))
        except (RuntimeError, OSError) as exc:
            last = exc
            if attempt + 1 < attempts:
                wait = pause * (attempt + 1)
                if deadline is not None:
                    wait = max(0.0, min(wait, deadline - time.monotonic()))
                time.sleep(wait)
    hours = int((end - start).total_seconds() // 3600)
    if hours <= 1 or depth >= MAX_SPLIT_DEPTH or _past(deadline):
        raise RuntimeError(f"proxy logs failed for {start:%Y-%m-%d %H:00} UTC: {last}")
    mid = start + timedelta(hours=hours // 2)
    return (_read_span(base, key, deployment_ids, start, mid, run, attempts, pause, deadline, depth + 1)
            + _read_span(base, key, deployment_ids, mid, end, run, attempts, pause, deadline, depth + 1))


def search_workload(
    base: str, key: str, deployment_ids: list[str], *, days: int = DEFAULT_DAYS, now: datetime | None = None,
    run=run_esql, workers: int = 1, deadline: float | None = None,
) -> dict:
    """The Search workload the estimators read, from the last `days` complete UTC days.

    `search.peak` and `search.nonpeak` carry qps (the query's recommended_qps), complexity_ms (typical
    p90 latency), complexity (simple, moderate or complex) and pct_hours_mo. The month is split by the
    share of every hour in the window that had requests in each band. An hour with no requests is idle,
    so idle is whatever the two bands leave of the window.
    """
    if not deployment_ids:
        raise ValueError("no Search deployments to read from the proxy logs")
    if days < 1:
        raise ValueError("days must be at least 1")
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    spans = [(today - timedelta(days=back), today - timedelta(days=back - 1)) for back in range(days, 0, -1)]

    def read(span: tuple[datetime, datetime]) -> list[dict]:
        return _read_span(base, key, deployment_ids, span[0], span[1], run, deadline=deadline)

    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            parts = list(pool.map(read, spans))
    else:
        parts = [read(span) for span in spans]
    hours: list[dict] = [row for part in parts for row in part]
    if not hours:
        raise RuntimeError("the proxy logs returned no external search or request traffic for these deployments")
    result = summarize(hours, total_hours=days * 24)
    if not result:
        raise RuntimeError("the proxy logs returned no Search requests in the window for these deployments")

    def band(name: str) -> dict:
        # A band with no active hour is idle, not missing: no QPS, no hour share. The Search sheet and
        # the es3 library both skip a band whose qps is None and write 0 QPS at a 0 share.
        row = result.get(name)
        if row is None:
            return {"qps": None, "complexity": None, "complexity_ms": None, "pct_hours_mo": 0.0}
        return {
            "qps": float(row["recommended_qps"]),
            "complexity": row["query_complexity"],
            "complexity_ms": float(row.get("p90_ms") if row.get("p90_ms") is not None else (row["typical_p90_latency_ms"] or 0)),
            "pct_hours_mo": float(row["pct_of_hours"]),
        }

    peak, nonpeak = band("peak"), band("non_peak")
    return {
        "search": {"peak": peak, "nonpeak": nonpeak, "idle_pct_hours_mo": max(0.0, round(100.0 - peak["pct_hours_mo"] - nonpeak["pct_hours_mo"], 1))},
        "source": "service-proxy-requests",
        "window_days": days,
        "window_start": (today - timedelta(days=days)).strftime("%Y-%m-%d"),
        "window_end": (today - timedelta(days=1)).strftime("%Y-%m-%d"),
        "deployments": list(deployment_ids),
        # Hours with requests, the hours the shares count. rows_returned also counts zero-request rows.
        "sampled_hours": sum(int(row.get("hours") or 0) for row in result.values()),
        "rows_returned": len(hours),
        "window_hours": days * 24,
        "detail": result,
    }
