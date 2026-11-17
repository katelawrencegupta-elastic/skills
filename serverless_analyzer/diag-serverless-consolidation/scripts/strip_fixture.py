"""Copy a diagnostic account down to the fields a consolidation run reads.

Usage:
  python3 strip_fixture.py --account PATH --infra PATH --out DIR --replace Token=acct-a

The replacement runs on every string after the unused fields are dropped.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
K360 = HERE.parents[1] / "k360-account-analysis" / "scripts"
sys.path.insert(0, str(K360))


class TrackDict(dict):
    def __init__(self, data, path, seen):
        super().__init__()
        self._path = path
        self._seen = seen
        seen.add(path)
        for key, value in data.items():
            super().__setitem__(key, _wrap(value, path + (("k", key),), seen))

    def __getitem__(self, key):
        self._seen.add(self._path + (("k", key),))
        return super().__getitem__(key)

    def get(self, key, default=None):
        self._seen.add(self._path + (("k", key),))
        return super().get(key, default)

    def __contains__(self, key):
        self._seen.add(self._path + (("k", key),))
        return dict.__contains__(self, key)


class TrackList(list):
    def __init__(self, data, path, seen):
        super().__init__(_wrap(value, path + (("i", index),), seen) for index, value in enumerate(data))
        self._path = path
        self._seen = seen
        seen.add(path)

    def __getitem__(self, index):
        if isinstance(index, slice):
            start, stop, step = index.indices(len(self))
            for item in range(start, stop, step):
                self._seen.add(self._path + (("i", item),))
            return list.__getitem__(self, index)
        self._seen.add(self._path + (("i", index),))
        return list.__getitem__(self, index)

    def __iter__(self):
        for index, value in enumerate(list.__iter__(self)):
            self._seen.add(self._path + (("i", index),))
            yield value


def _wrap(value, path, seen):
    if isinstance(value, dict):
        return TrackDict(value, path, seen)
    if isinstance(value, list):
        return TrackList(value, path, seen)
    return value


def _used(path, seen) -> bool:
    if path in seen:
        return True
    size = len(path)
    return any(item[:size] == path for item in seen)


def _prune(node, path, seen):
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            child = path + (("k", key),)
            if _used(child, seen):
                out[key] = _prune(value, child, seen)
        return out
    if isinstance(node, list):
        # Keep indexes aligned with any parallel list. An unread element becomes null.
        return [
            _prune(value, path + (("i", index),), seen) if _used(path + (("i", index),), seen) else None
            for index, value in enumerate(node)
        ]
    return node


_URL = re.compile(r"https?://[^\s\"'<>]*google[^\s\"'<>]*", re.IGNORECASE)
_HOME = re.compile(r"/Users/[^\s\"'<>]*")
_HEX_ID = re.compile(r"\b[0-9a-f]{32}\b", re.IGNORECASE)
_ACCT = re.compile(r"acct-[abc][A-Za-z0-9_\- ,\.]*")
_DROP_KEYS = {"ARR"}


def scrub_fixture(node, case_id: str):
    """Remove commercial figures, live links, local paths, and leftover account tokens."""
    tokens: list[str] = []
    ids: list[str] = []

    def collect(value):
        if isinstance(value, str):
            tokens.extend(_ACCT.findall(value))
            ids.extend(_HEX_ID.findall(value))
        elif isinstance(value, dict):
            for key, item in value.items():
                if key == "id" and isinstance(item, str) and _HEX_ID.fullmatch(item):
                    ids.append(item)
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(node)
    pairs: list[tuple[str, str]] = []
    for index, token in enumerate(sorted(set(tokens), key=len, reverse=True)):
        pairs.append((token, f"dep-{index:02d}"))
    for index, ident in enumerate(sorted(set(ids))):
        pairs.append((ident, f"dep-id-{index:02d}"))

    def rewrite(value):
        if isinstance(value, str):
            text = _URL.sub("https://example.invalid/", value)
            text = _HOME.sub(f"artifacts/{case_id}", text)
            for old, new in pairs:
                text = text.replace(old, new)
            return text
        if isinstance(value, dict):
            out = {}
            for key, item in value.items():
                if key in _DROP_KEYS:
                    continue
                if key == "contract":
                    continue
                new_key = rewrite(key) if isinstance(key, str) else key
                out[new_key] = rewrite(item)
            return out
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        return value

    return rewrite(node)


def _swap(node, pairs: list[tuple[re.Pattern, str]]):
    if isinstance(node, str):
        text = node
        for pattern, repl in pairs:
            text = pattern.sub(repl, text)
        if "@" in text and re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+", text):
            text = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+", "user@example.invalid", text)
        return text
    if isinstance(node, dict):
        return {_swap(key, pairs) if isinstance(key, str) else key: _swap(value, pairs) for key, value in node.items()}
    if isinstance(node, list):
        return [_swap(value, pairs) for value in node]
    return node


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", required=True)
    parser.add_argument("--infra", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--replace", action="append", default=[])
    args = parser.parse_args()
    from consolidation import build_consolidation
    from render_pdf import render_consolidation_html

    account = json.loads(Path(args.account).read_text())
    infra = json.loads(Path(args.infra).read_text())
    seen_account: set = set()
    seen_infra: set = set()
    tracked_account = _wrap(copy.deepcopy(account), (), seen_account)
    tracked_infra = _wrap(copy.deepcopy(infra), (), seen_infra)
    consolidation = build_consolidation(tracked_account, tracked_infra)
    tracked_account["consolidation"] = consolidation
    render_consolidation_html(tracked_account)
    stripped_account = _prune(account, (), seen_account)
    stripped_infra = _prune(infra, (), seen_infra)
    pairs = []
    for item in args.replace:
        old, new = item.split("=", 1)
        pairs.append((re.compile(re.escape(old), re.IGNORECASE), new))
    pairs.sort(key=lambda pair: -len(pair[0].pattern))
    stripped_account = _swap(stripped_account, pairs)
    stripped_infra = _swap(stripped_infra, pairs)
    case_id = Path(args.out).name
    stripped_account = scrub_fixture(stripped_account, case_id)
    stripped_infra = scrub_fixture(stripped_infra, case_id)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "diags_account.json").write_text(json.dumps(stripped_account))
    (out / "infra.json").write_text(json.dumps(stripped_infra))
    print(f"wrote {out} account={ (out / 'diags_account.json').stat().st_size } infra={(out / 'infra.json').stat().st_size}")


if __name__ == "__main__":
    main()
