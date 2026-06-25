#!/usr/bin/env python3
"""Compare Headroom per-request dumps (see headroom/dump.py).

Scans the dump directory (default ``~/.headroom/dumps``, or ``$HEADROOM_DUMP_DIR``)
where each request is stored as three files sharing a stem::

    <stem>.original.json    pre-compression body  {model, provider, messages}
    <stem>.compressed.json  post-compression body
    <stem>.meta.json        token counts, ratio, byte sizes, transforms

Subcommands
-----------
    summary        Table of every request + aggregate savings (default).
    show [WHICH]   Diff original vs compressed for one request, highlighting
                   what compression removed. WHICH = a stem/id substring, or
                   "latest" (default).
    top [N]        The N requests with the highest token savings.

Stdlib only — no dependencies. Colors auto-disable when not a TTY or with
--no-color.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
from pathlib import Path


# ---- presentation helpers ---------------------------------------------------

class C:
    """ANSI colors; blanked out when disabled."""

    RESET = "\033[0m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"

    @classmethod
    def disable(cls) -> None:
        for k in ("RESET", "DIM", "BOLD", "RED", "GREEN", "YELLOW", "CYAN"):
            setattr(cls, k, "")


def _dump_dir() -> Path:
    raw = os.environ.get("HEADROOM_DUMP_DIR", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".headroom" / "dumps"


def _stems(dump_dir: Path) -> list[Path]:
    """Return sorted unique stems (paths without the .<kind>.json suffix)."""
    stems: set[Path] = set()
    for meta in dump_dir.glob("*.meta.json"):
        stems.add(Path(str(meta)[: -len(".meta.json")]))
    return sorted(stems, key=lambda p: p.name)


def _load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _fmt_int(n) -> str:
    try:
        return f"{int(n):,}"
    except Exception:
        return str(n)


def _ratio_color(r: float) -> str:
    if r >= 0.4:
        return C.GREEN
    if r >= 0.15:
        return C.YELLOW
    return C.DIM


# ---- content extraction (for the diff) --------------------------------------

def _blocks_to_lines(content) -> list[str]:
    """Flatten a message ``content`` (str or list of blocks) to text lines."""
    out: list[str] = []
    if isinstance(content, str):
        out.extend(content.splitlines() or [""])
        return out
    if isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                out.append(str(b))
                continue
            t = b.get("type")
            if t == "text":
                out.extend((b.get("text") or "").splitlines())
            elif t == "tool_use":
                out.append(f"[tool_use {b.get('name')} input=]")
                out.extend(json.dumps(b.get("input", {}), ensure_ascii=False, indent=2).splitlines())
            elif t == "tool_result":
                inner = b.get("content")
                out.append(f"[tool_result tool_use_id={b.get('tool_use_id')}]")
                out.extend(_blocks_to_lines(inner))
            else:
                out.extend(json.dumps(b, ensure_ascii=False, indent=2).splitlines())
        return out
    out.append(str(content))
    return out


def _messages_to_lines(messages) -> list[str]:
    lines: list[str] = []
    for i, m in enumerate(messages or []):
        role = m.get("role", "?") if isinstance(m, dict) else "?"
        lines.append(f"=== message[{i}] role={role} ===")
        lines.extend(_blocks_to_lines(m.get("content") if isinstance(m, dict) else m))
    return lines


# ---- subcommands ------------------------------------------------------------

def cmd_summary(dump_dir: Path, as_json: bool) -> int:
    stems = _stems(dump_dir)
    if not stems:
        print(f"No dumps found in {dump_dir}")
        return 0

    rows = []
    tot_before = tot_after = 0
    for stem in stems:
        meta = _load(Path(f"{stem}.meta.json"))
        if not meta:
            continue
        before = int(meta.get("tokens_before", 0) or 0)
        after = int(meta.get("tokens_after", 0) or 0)
        tot_before += before
        tot_after += after
        rows.append(
            {
                "id": stem.name,
                "model": meta.get("model", "?"),
                "tokens_before": before,
                "tokens_after": after,
                "tokens_saved": before - after,
                "ratio": float(meta.get("compression_ratio", 0) or 0),
                "transforms": meta.get("transforms_applied", []),
            }
        )

    if as_json:
        print(json.dumps({"dump_dir": str(dump_dir), "requests": rows,
                          "totals": {"tokens_before": tot_before, "tokens_after": tot_after,
                                     "tokens_saved": tot_before - tot_after}}, ensure_ascii=False, indent=2))
        return 0

    print(f"{C.BOLD}Dump dir:{C.RESET} {dump_dir}   ({len(rows)} requests)\n")
    print(f"{C.DIM}{'id':<34} {'model':<22} {'before':>9} {'after':>9} {'saved':>9}  ratio{C.RESET}")
    for r in rows:
        rc = _ratio_color(r["ratio"])
        print(
            f"{r['id']:<34} {str(r['model'])[:22]:<22} "
            f"{_fmt_int(r['tokens_before']):>9} {_fmt_int(r['tokens_after']):>9} "
            f"{rc}{_fmt_int(r['tokens_saved']):>9} {r['ratio']*100:5.1f}%{C.RESET}"
        )
    saved = tot_before - tot_after
    agg = (saved / tot_before) if tot_before else 0.0
    print(f"\n{C.BOLD}TOTAL{C.RESET}  before={_fmt_int(tot_before)}  after={_fmt_int(tot_after)}  "
          f"{C.GREEN}saved={_fmt_int(saved)} ({agg*100:.1f}%){C.RESET}")
    return 0


def cmd_top(dump_dir: Path, n: int) -> int:
    stems = _stems(dump_dir)
    rows = []
    for stem in stems:
        meta = _load(Path(f"{stem}.meta.json"))
        if not meta:
            continue
        rows.append((int(meta.get("tokens_saved", 0) or 0), stem.name, meta))
    rows.sort(reverse=True)
    for saved, name, meta in rows[: max(1, n)]:
        print(f"{C.GREEN}{_fmt_int(saved):>9} saved{C.RESET}  {name}  "
              f"({_fmt_int(meta.get('tokens_before'))}→{_fmt_int(meta.get('tokens_after'))}, "
              f"{float(meta.get('compression_ratio',0))*100:.1f}%)")
    return 0


def cmd_show(dump_dir: Path, which: str, context: int) -> int:
    stems = _stems(dump_dir)
    if not stems:
        print(f"No dumps found in {dump_dir}")
        return 1
    if which in ("", "latest"):
        target = stems[-1]
    else:
        matches = [s for s in stems if which in s.name]
        if not matches:
            print(f"No dump matching '{which}'. Available ids:")
            for s in stems[-10:]:
                print(f"  {s.name}")
            return 1
        target = matches[-1]

    meta = _load(Path(f"{target}.meta.json")) or {}
    orig = _load(Path(f"{target}.original.json")) or {}
    comp = _load(Path(f"{target}.compressed.json")) or {}

    print(f"{C.BOLD}{target.name}{C.RESET}")
    print(f"  model={meta.get('model')}  "
          f"tokens {_fmt_int(meta.get('tokens_before'))}→{_fmt_int(meta.get('tokens_after'))}  "
          f"{C.GREEN}saved {_fmt_int(meta.get('tokens_saved'))} "
          f"({float(meta.get('compression_ratio',0))*100:.1f}%){C.RESET}")
    print(f"  transforms: {', '.join(meta.get('transforms_applied', [])) or '(none)'}\n")

    a = _messages_to_lines(orig.get("messages", []))
    b = _messages_to_lines(comp.get("messages", []))
    diff = difflib.unified_diff(a, b, fromfile="original", tofile="compressed", n=context, lineterm="")
    removed = added = 0
    for line in diff:
        if line.startswith("---") or line.startswith("+++"):
            print(f"{C.BOLD}{line}{C.RESET}")
        elif line.startswith("@@"):
            print(f"{C.CYAN}{line}{C.RESET}")
        elif line.startswith("-"):
            removed += 1
            print(f"{C.RED}{line}{C.RESET}")
        elif line.startswith("+"):
            added += 1
            print(f"{C.GREEN}{line}{C.RESET}")
        else:
            print(f"{C.DIM}{line}{C.RESET}")
    print(f"\n{C.RED}- {removed} lines removed{C.RESET}   {C.GREEN}+ {added} lines added{C.RESET}")
    return 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Compare Headroom request dumps.")
    p.add_argument("--dir", help="Dump dir (default: $HEADROOM_DUMP_DIR or ~/.headroom/dumps)")
    p.add_argument("--no-color", action="store_true", help="Disable ANSI colors")
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("summary", help="Table of all requests + totals")
    sp.add_argument("--json", action="store_true", help="Machine-readable output")

    st = sub.add_parser("show", help="Diff one request (original vs compressed)")
    st.add_argument("which", nargs="?", default="latest", help="id substring or 'latest'")
    st.add_argument("-C", "--context", type=int, default=2, help="Diff context lines")

    tp = sub.add_parser("top", help="Requests with the highest token savings")
    tp.add_argument("n", nargs="?", type=int, default=10)

    args = p.parse_args(argv)
    if args.no_color or not sys.stdout.isatty():
        C.disable()

    dump_dir = Path(args.dir).expanduser() if args.dir else _dump_dir()
    if not dump_dir.exists():
        print(f"Dump dir does not exist: {dump_dir}")
        print("Enable dumping (on by default) and run some requests through the proxy first.")
        return 1

    if args.cmd == "show":
        return cmd_show(dump_dir, args.which, args.context)
    if args.cmd == "top":
        return cmd_top(dump_dir, args.n)
    # default + "summary"
    return cmd_summary(dump_dir, getattr(args, "json", False))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
