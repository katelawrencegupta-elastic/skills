#!/usr/bin/env python3
"""Record token usage for a Splunk→Kibana conversion run.

Writes under PACKAGE_ROOT/metrics/:
  token_usage.jsonl   # one JSON object per turn / event (append-only)
  token_usage.json    # rolled-up summary for the run

Sources:
  1) Cursor stop / afterAgentResponse hook JSON on stdin (--from-hook)
  2) Manual / Claude CLI flags (--input-tokens etc.)
  3) Optional transcript estimate (--estimate-transcript path.jsonl)

Env:
  SX2K_PACKAGE_ROOT  default package root when --package omitted

Usage:
  echo "$HOOK_JSON" | python record_token_usage.py --from-hook --package ./elastic/my_dash
  python record_token_usage.py --package ./elastic/my_dash \\
    --input-tokens 120000 --output-tokens 4000 --cache-read-tokens 100000 \\
    --model claude --source manual --note "R7 end"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cost_relevant(input_tokens: int, output_tokens: int, cache_read: int, cache_write: int) -> int:
    """Approx non-cache input + output (clamp at 0)."""
    uncached = max(0, input_tokens - cache_read - cache_write)
    return uncached + output_tokens


def load_hook_event(raw: str) -> dict:
    data = json.loads(raw)
    # Cursor may wrap; accept flat or nested
    if "input_tokens" not in data and isinstance(data.get("payload"), dict):
        data = {**data, **data["payload"]}
    return data


def event_from_hook(data: dict, source: str = "cursor_hook") -> dict:
    inp = int(data.get("input_tokens") or 0)
    out = int(data.get("output_tokens") or 0)
    cr = int(data.get("cache_read_tokens") or data.get("cache_read") or 0)
    cw = int(data.get("cache_write_tokens") or data.get("cache_write") or 0)
    return {
        "ts": utc_now(),
        "source": source,
        "hook_event": data.get("hook_event_name") or data.get("event") or "stop",
        "conversation_id": data.get("conversation_id") or "",
        "generation_id": data.get("generation_id") or "",
        "model": data.get("model") or data.get("model_id") or "",
        "status": data.get("status") or "",
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_tokens": cr,
        "cache_write_tokens": cw,
        "cost_relevant_tokens": cost_relevant(inp, out, cr, cw),
        "billed_input_incl_cache": inp,
        "note": data.get("note") or "",
    }


def estimate_transcript(path: Path) -> dict:
    """Rough estimate: visible chars/4; Method-B sum of per-turn growing context (capped)."""
    user_chars = asst_chars = 0
    turns = 0
    cum = 50_000
    in_sum = out_sum = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        role = row.get("role") or ""
        msg = row.get("message") or row
        content = msg.get("content") if isinstance(msg, dict) else msg
        if content is None:
            c = 0
        elif isinstance(content, str):
            c = len(content)
        else:
            c = len(json.dumps(content, ensure_ascii=False))
        if role == "user":
            user_chars += c
            cum += c
        elif role == "assistant":
            turns += 1
            asst_chars += c
            in_sum += min(cum, 800_000) // 4
            out_sum += c // 4
            cum += c
        else:
            cum += c
    visible = (user_chars + asst_chars) // 4
    return {
        "ts": utc_now(),
        "source": "transcript_estimate",
        "hook_event": "estimate",
        "conversation_id": path.stem,
        "generation_id": "",
        "model": "",
        "status": "estimated",
        "input_tokens": in_sum,
        "output_tokens": out_sum,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cost_relevant_tokens": visible,  # lower-bound proxy
        "billed_input_incl_cache": in_sum,
        "assistant_turns": turns,
        "visible_tokens_est": visible,
        "note": f"estimated from {path.name}; undercounts real Cursor context",
    }


def summarize(events: list[dict]) -> dict:
    def sumf(key: str) -> int:
        return sum(int(e.get(key) or 0) for e in events)

    turns = len([e for e in events if e.get("source") != "run_meta"])
    # Prefer summing hook turns; if one transcript estimate exists, use its turn count
    for e in events:
        if e.get("assistant_turns"):
            turns = max(turns, int(e["assistant_turns"]))
    return {
        "updated_at": utc_now(),
        "event_count": len(events),
        "assistant_turns_est": turns,
        "totals": {
            "input_tokens": sumf("input_tokens"),
            "output_tokens": sumf("output_tokens"),
            "cache_read_tokens": sumf("cache_read_tokens"),
            "cache_write_tokens": sumf("cache_write_tokens"),
            "cost_relevant_tokens": sumf("cost_relevant_tokens"),
            "billed_input_incl_cache": sumf("billed_input_incl_cache"),
        },
        "target": {
            "max_assistant_turns": 12,
            "cost_relevant_tokens_goal": 70000,
            "billed_incl_cache_goal": 250000,
        },
        "within_turn_target": turns <= 12 if turns else None,
        "events_tail": events[-5:],
    }


def write_metrics(package: Path, event: dict) -> tuple[Path, Path]:
    metrics = package / "metrics"
    metrics.mkdir(parents=True, exist_ok=True)
    jsonl = metrics / "token_usage.jsonl"
    summary_path = metrics / "token_usage.json"

    with jsonl.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")

    events = []
    for line in jsonl.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    summary = summarize(events)
    summary["package"] = str(package)
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return jsonl, summary_path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--package",
        type=Path,
        default=Path(os.environ.get("SX2K_PACKAGE_ROOT") or "."),
        help="PACKAGE_ROOT (or set SX2K_PACKAGE_ROOT)",
    )
    ap.add_argument("--from-hook", action="store_true", help="Read Cursor hook JSON from stdin")
    ap.add_argument("--estimate-transcript", type=Path, help="Estimate from Cursor/Claude jsonl")
    ap.add_argument("--input-tokens", type=int, default=None)
    ap.add_argument("--output-tokens", type=int, default=None)
    ap.add_argument("--cache-read-tokens", type=int, default=0)
    ap.add_argument("--cache-write-tokens", type=int, default=0)
    ap.add_argument("--model", default="")
    ap.add_argument("--conversation-id", default="")
    ap.add_argument("--source", default="manual")
    ap.add_argument("--note", default="")
    ap.add_argument("--print-summary", action="store_true")
    args = ap.parse_args()

    package = args.package.resolve()

    if args.from_hook:
        raw = sys.stdin.read()
        if not raw.strip():
            # fail open for hooks — empty stdin
            print("{}", flush=True)
            return 0
        try:
            event = event_from_hook(load_hook_event(raw))
        except json.JSONDecodeError:
            print("{}", flush=True)
            return 0
    elif args.estimate_transcript:
        event = estimate_transcript(args.estimate_transcript)
    elif args.input_tokens is not None and args.output_tokens is not None:
        inp, out = args.input_tokens, args.output_tokens
        cr, cw = args.cache_read_tokens, args.cache_write_tokens
        event = {
            "ts": utc_now(),
            "source": args.source,
            "hook_event": "manual",
            "conversation_id": args.conversation_id,
            "generation_id": "",
            "model": args.model,
            "status": "recorded",
            "input_tokens": inp,
            "output_tokens": out,
            "cache_read_tokens": cr,
            "cache_write_tokens": cw,
            "cost_relevant_tokens": cost_relevant(inp, out, cr, cw),
            "billed_input_incl_cache": inp,
            "note": args.note,
        }
    else:
        ap.error("Provide --from-hook, --estimate-transcript, or --input-tokens and --output-tokens")
        return 2

    jsonl, summary_path = write_metrics(package, event)
    # Cursor hooks expect JSON on stdout; empty permission object is fine
    out = {
        "ok": True,
        "wrote": str(jsonl),
        "summary": str(summary_path),
        "last": {
            "input_tokens": event.get("input_tokens"),
            "output_tokens": event.get("output_tokens"),
            "cost_relevant_tokens": event.get("cost_relevant_tokens"),
        },
    }
    print(json.dumps(out), flush=True)
    if args.print_summary:
        print(summary_path.read_text(encoding="utf-8"), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
