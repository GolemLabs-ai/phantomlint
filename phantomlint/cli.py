"""CLI: phantomlint -c config.json [--json] [--strict] [--update-baseline].

Exit codes: 0 = no drift (or only accepted/INFO/WARN without --strict),
1 = drift gate failed (BLOCK, or WARN under --strict), 2 = usage/config error.
"""
import argparse
import json
import sys
from pathlib import Path

from . import __version__, core

SEV_ORDER = {"BLOCK": 0, "WARN": 1, "INFO": 2}
KIND_LABEL = {
    "phantom_table": "PHANTOM TABLE  (queried, not in schema)",
    "phantom_column": "PHANTOM COLUMN (inserted, not in schema)",
    "phantom_endpoint": "PHANTOM ENDPOINT (called, not served)",
    "dead_table": "DEAD TABLE     (defined, never queried)",
}

# Path to the sample bundled inside the package (for `phantomlint --demo`).
_DEMO_CONFIG = Path(__file__).resolve().parent / "examples" / "sample.phantomlint.json"


def render_text(result):
    s = result["stats"]
    out = [f"# phantomlint - {result['name']}", ""]
    out.append(
        f"schema tables: {s['schema_tables']} | queried: {s['queried_tables']} | "
        f"endpoints: {s['endpoints']} | routes: {s['routes']} | columns checked: {s['columns_checked']}"
    )
    out.append("")
    active = [f for f in result["findings"] if not f["accepted"]]
    accepted = [f for f in result["findings"] if f["accepted"]]
    by_kind = {}
    for f in active:
        by_kind.setdefault(f["kind"], []).append(f)
    if not active:
        out.append("OK - no drift (outside baseline).")
    for kind in ("phantom_table", "phantom_column", "phantom_endpoint", "dead_table"):
        items = by_kind.get(kind)
        if not items:
            continue
        out.append(f"## {KIND_LABEL[kind]}  [{items[0]['severity']}] ({len(items)})")
        for f in sorted(items, key=lambda x: x["id"]):
            line = f"  - {f['id']}"
            if f["where"]:
                line += f"  <- {f['where'][0]}"
            if f.get("suggestion"):
                line += f"   ({f['suggestion']})"
            out.append(line)
        out.append("")
    if accepted:
        out.append(f"({len(accepted)} accepted via baseline, not shown)")
    counts = _counts(active)
    out.append("---")
    out.append(f"BLOCK: {counts['BLOCK']} | WARN: {counts['WARN']} | INFO: {counts['INFO']}"
               f" | accepted: {len(accepted)}")
    return "\n".join(out)


def _counts(findings):
    c = {"BLOCK": 0, "WARN": 0, "INFO": 0}
    for f in findings:
        c[f["severity"]] = c.get(f["severity"], 0) + 1
    return c


def _err(msg):
    """Print a clean one-line error to stderr (no traceback) and return 2."""
    sys.stderr.write(f"phantomlint: error: {msg}\n")
    return 2


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="phantomlint",
        description="Static linter that catches phantom features "
        "(frontend/API/schema drift) before they ship.",
    )
    ap.add_argument("-c", "--config", help="path to config (.json or .toml)")
    ap.add_argument("--root", help="base dir for globs (default: config file's dir)")
    ap.add_argument("--json", action="store_true", help="machine-readable JSON output")
    ap.add_argument("--strict", action="store_true", help="exit 1 on WARN too (not just BLOCK)")
    ap.add_argument("--baseline", help="baseline file of accepted findings (default: <config>.baseline.json)")
    ap.add_argument("--update-baseline", action="store_true", help="write all current findings to baseline and exit 0")
    ap.add_argument("--demo", action="store_true", help="run the bundled sample (no config needed)")
    ap.add_argument("--version", action="version", version=f"phantomlint {__version__}")
    args = ap.parse_args(argv)

    if not args.config and not args.demo:
        return _err("one of -c/--config or --demo is required")

    # --- resolve config path (bundled sample for --demo) --------------------
    if args.demo:
        cfg_path = _DEMO_CONFIG
        if not cfg_path.exists():
            return _err(
                "bundled sample not found in the installed package "
                f"(expected {cfg_path})"
            )
    else:
        cfg_path = Path(args.config)

    # --- load config (clean errors, no traceback) ---------------------------
    try:
        cfg = core.load_config(cfg_path)
    except core.ConfigError as e:
        return _err(str(e))

    try:
        root = (
            Path(args.root)
            if args.root
            else (cfg_path.parent / cfg.get("root", ".")).resolve()
        )
    except (TypeError, ValueError) as e:
        return _err(f"invalid root: {e}")

    # --- false-GREEN guard: a missing root is a hard error, not silent OK ---
    if not root.exists():
        return _err(f"root does not exist: {root}")
    if not root.is_dir():
        return _err(f"root is not a directory: {root}")

    baseline_path = (
        Path(args.baseline) if args.baseline else cfg_path.with_suffix(".baseline.json")
    )

    # --- warn (don't fail) when a configured layer matches zero files -------
    try:
        for w in core.layer_warnings(root, cfg):
            sys.stderr.write(f"phantomlint: warning: {w}\n")
    except core.ConfigError as e:
        return _err(str(e))

    if args.update_baseline:
        try:
            result = core.run(cfg, root, baseline=set())
        except core.ConfigError as e:
            return _err(str(e))
        keys = sorted({f["key"] for f in result["findings"]})
        try:
            baseline_path.write_text(
                json.dumps({"accepted": keys}, indent=2), encoding="utf-8"
            )
        except OSError as e:
            return _err(f"cannot write baseline {baseline_path}: {e}")
        print(f"[phantomlint] baseline written: {baseline_path} ({len(keys)} accepted)")
        return 0

    try:
        baseline = core.load_baseline(baseline_path)
        result = core.run(cfg, root, baseline=baseline)
    except core.ConfigError as e:
        return _err(str(e))

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(render_text(result))

    active = [f for f in result["findings"] if not f["accepted"]]
    counts = _counts(active)
    if counts["BLOCK"] > 0:
        return 1
    if args.strict and counts["WARN"] > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
