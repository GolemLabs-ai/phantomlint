"""Engine + CLI regression suite for phantomlint.

Run with pytest, or directly:  python tests/test_drift.py   (no pytest needed).
Tests use bare asserts and never `return` a value (no PytestReturnNotNoneWarning).
"""
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from phantomlint import core  # noqa: E402
from phantomlint import cli  # noqa: E402

SAMPLE_CFG = ROOT / "examples" / "sample.phantomlint.json"
SAMPLE_ROOT = ROOT / "examples" / "sample"


def run_sample():
    cfg = core.load_config(SAMPLE_CFG)
    return core.run(cfg, SAMPLE_ROOT)


# --- original contract --------------------------------------------------------
def test_sample_drift():
    res = run_sample()
    found = {(f["kind"], f["id"]) for f in res["findings"]}

    expected = {
        ("phantom_table", "payments"),
        ("phantom_column", "orders.coupon_code"),
        ("phantom_endpoint", "/api/refunds"),
        ("dead_table", "audit_log"),
    }
    missing = expected - found
    assert not missing, f"missed: {missing}"

    must_not = {
        ("phantom_endpoint", "/api/users"),
        ("phantom_endpoint", "/api/orders/:param"),
        ("phantom_table", "users"),
        ("phantom_table", "orders"),
    }
    false_pos = must_not & found
    assert not false_pos, f"false positives: {false_pos}"


def test_sample_fuzzy_suggestion():
    """The bundled sample must surface the fuzzy 'did you mean payment?' hint."""
    res = run_sample()
    pt = next(f for f in res["findings"]
              if f["kind"] == "phantom_table" and f["id"] == "payments")
    assert pt.get("suggestion") == "did you mean `payment`?", pt


def test_baseline_suppresses():
    res = run_sample()
    keys = {f["key"] for f in res["findings"]}
    cfg = core.load_config(SAMPLE_CFG)
    res2 = core.run(cfg, SAMPLE_ROOT, baseline=keys)
    assert all(f["accepted"] for f in res2["findings"]), "baseline did not accept all"


# --- identifier handling ------------------------------------------------------
def _schema_only(sql, table_def_root, extra_files=None):
    """Build a throwaway project with one schema file, return (tables, cols)."""
    d = Path(table_def_root)
    (d / "schema").mkdir(parents=True, exist_ok=True)
    (d / "schema" / "s.sql").write_text(sql, encoding="utf-8")
    cfg = {"schema": {"globs": ["schema/**/*.sql"]}}
    return core.extract_schema(d, cfg)


def test_schema_qualified_tables():
    """CREATE TABLE public.accounts -> 'accounts', FROM public.accounts -> 'accounts'."""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "schema").mkdir()
        (d / "api").mkdir()
        (d / "schema" / "s.sql").write_text(
            "CREATE TABLE public.accounts (id INTEGER, email TEXT);", encoding="utf-8"
        )
        (d / "api" / "q.js").write_text(
            'db.prepare("SELECT email FROM public.accounts WHERE id = 1");',
            encoding="utf-8",
        )
        cfg = {
            "schema": {"globs": ["schema/**/*.sql"]},
            "api": {"globs": ["api/**/*.js"]},
        }
        res = core.run(cfg, d)
        tables = res["stats"]["schema_tables"]
        phantoms = [f for f in res["findings"] if f["kind"] == "phantom_table"]
        assert tables == 1, res["stats"]
        # accounts exists and is queried -> NO phantom, NO 'public' nonsense table
        assert not phantoms, f"schema-qualified table wrongly flagged: {phantoms}"
        kinds = {f["id"] for f in res["findings"]}
        assert "public" not in kinds, f"'public' leaked as a table: {kinds}"


def test_temp_and_bracketed_tables():
    with tempfile.TemporaryDirectory() as td:
        tables, cols = _schema_only(
            "CREATE TEMP TABLE staging (id INT, val TEXT);\n"
            "CREATE UNLOGGED TABLE fast (id INT);\n"
            "CREATE GLOBAL TEMPORARY TABLE gt (id INT);\n"
            "CREATE TABLE [bracketed] (id INT, name TEXT);\n",
            td,
        )
        assert "staging" in tables, tables
        assert "fast" in tables, tables
        assert "gt" in tables, tables
        assert "bracketed" in tables, tables
        assert "val" in cols.get("staging", set()), cols
        assert "name" in cols.get("bracketed", set()), cols


def test_ctas_no_contamination():
    """CREATE TABLE ... AS SELECT must not steal the next table's columns."""
    with tempfile.TemporaryDirectory() as td:
        tables, cols = _schema_only(
            "CREATE TABLE summary AS SELECT * FROM orders;\n"
            "CREATE TABLE other (password TEXT, secret TEXT);\n",
            td,
        )
        assert "summary" in tables, tables
        # summary has no parsed body -> must NOT borrow other's columns
        assert cols.get("summary", set()) == set(), cols.get("summary")
        assert "password" in cols.get("other", set()), cols
        # LIKE form also bails
        tables2, cols2 = _schema_only(
            "CREATE TABLE clone (LIKE orders);\n"
            "CREATE TABLE real (col_a TEXT);\n",
            td,
        )
        assert cols2.get("clone", set()) == set(), cols2.get("clone")


def test_sql_string_literal_comment():
    """A DEFAULT '-- x' must not eat subsequent columns (quote-aware stripping)."""
    with tempfile.TemporaryDirectory() as td:
        tables, cols = _schema_only(
            "CREATE TABLE t (\n"
            "  id INTEGER PRIMARY KEY,\n"
            "  note TEXT DEFAULT '-- not a comment',\n"
            "  important_col TEXT\n"
            ");",
            td,
        )
        assert "important_col" in cols.get("t", set()), cols.get("t")


# --- frontend / routing -------------------------------------------------------
def test_js_commented_fetch_not_phantom():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "frontend").mkdir()
        (d / "api").mkdir()
        (d / "frontend" / "a.jsx").write_text(
            'fetch("/api/real");\n'
            '// fetch("/api/commented");\n'
            '/* fetch("/api/blockcommented"); */\n',
            encoding="utf-8",
        )
        (d / "api" / "s.js").write_text('if (path.startsWith("/api")) {}', encoding="utf-8")
        cfg = {
            "frontend": {"globs": ["frontend/**/*.jsx"]},
            "api": {"globs": ["api/**/*.js"]},
            "options": {"broad_prefixes": []},
        }
        res = core.run(cfg, d)
        phantom_eps = {f["id"] for f in res["findings"] if f["kind"] == "phantom_endpoint"}
        assert "/api/commented" not in phantom_eps, phantom_eps
        assert "/api/blockcommented" not in phantom_eps, phantom_eps


def test_single_segment_prefix_serves():
    """startsWith('/health') must serve a fetch('/health') call (len>=1 prefix)."""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "frontend").mkdir()
        (d / "api").mkdir()
        (d / "frontend" / "a.jsx").write_text('fetch("/health");', encoding="utf-8")
        (d / "api" / "s.js").write_text('if (path.startsWith("/health")) {}', encoding="utf-8")
        cfg = {
            "frontend": {"globs": ["frontend/**/*.jsx"]},
            "api": {"globs": ["api/**/*.js"]},
            "options": {"broad_prefixes": []},
        }
        res = core.run(cfg, d)
        phantom_eps = {f["id"] for f in res["findings"] if f["kind"] == "phantom_endpoint"}
        assert "/health" not in phantom_eps, phantom_eps


def test_regex_to_skeleton_nested_groups():
    """Nested capture groups collapse to one :param, no stray ')' segment."""
    skel = core.regex_to_skeleton(r"^/api/users/((\d+)|me)$")
    assert skel == "/api/users/:param", skel
    assert ")" not in skel, skel


# --- Bug #1: import lines must NOT leak into the SQL FROM extractor -----------
def test_bug1_python_import_not_sql_from():
    """`from django.contrib import auth` / `import X from "next"` must NOT be
    parsed as SQL `FROM ...` and turn into a phantom_table finding.

    Regression for Bug #1: extract_table_refs scanned raw text with the
    `\\bFROM` regex, so Python/TS import declarations produced false positives
    like `phantom_table: contrib` and `phantom_table: "next"`.
    """
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "schema").mkdir()
        (d / "api").mkdir()
        (d / "schema" / "s.sql").write_text(
            "CREATE TABLE users (id INT, email TEXT);", encoding="utf-8"
        )
        # Mix of Python-style + TS/JS ES-module imports + a Prisma-ish `from`.
        # The lone real SQL on the last line (`FROM users`) is what should be
        # extracted; everything else must be silently ignored.
        (d / "api" / "handler.ts").write_text(
            "from django.contrib import auth\n"
            "from rest_framework.decorators import api_view\n"
            'import X from "next";\n'
            'import { prisma } from "@prisma/client";\n'
            'import * as ns from "react";\n'
            'export { foo } from "./bar";\n'
            'import "side-effect-only";\n'
            'db.exec("SELECT id FROM users WHERE id = 1");\n',
            encoding="utf-8",
        )
        cfg = {
            "schema": {"globs": ["schema/**/*.sql"]},
            "api": {"globs": ["api/**/*.ts"]},
        }
        res = core.run(cfg, d)
        phantoms = {f["id"] for f in res["findings"] if f["kind"] == "phantom_table"}
        # Tokens that USED to leak from import declarations:
        for bad in (
            "contrib", "rest_framework", "decorators", "next",
            "@prisma/client", "react", "./bar", "side-effect-only", "client",
        ):
            assert bad not in phantoms, (
                f"import-line leak: '{bad}' surfaced as phantom_table; "
                f"all phantoms={phantoms}"
            )
        # And the legitimate `FROM users` must still resolve cleanly (no phantom).
        assert "users" not in phantoms, (
            f"`users` wrongly flagged despite schema CREATE TABLE; phantoms={phantoms}"
        )


# --- Bug #2: Express router endpoints must be extracted -----------------------
def test_bug2_express_router_endpoints():
    """router.get('/x') / app.post('/y') / router.use('/z') must register as
    served routes so frontend fetches to those paths are NOT phantom_endpoints.

    Regression for Bug #2: extract_routes recognised only `path === '/x'`,
    `startsWith('/x')`, and `path.match(/regex/)`. Standard Express idioms
    returned zero routes, producing false-positive phantom_endpoint findings.
    """
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "frontend").mkdir()
        (d / "api").mkdir()
        (d / "schema").mkdir()
        (d / "schema" / "s.sql").write_text(
            "CREATE TABLE users (id INT);", encoding="utf-8"
        )
        (d / "frontend" / "app.jsx").write_text(
            'fetch("/api/users");\n'
            'fetch("/api/orders");\n'
            'fetch("/api/orders/123");\n'      # served by :id param route
            'fetch("/api/admin/dashboard");\n'  # served by router.use prefix mount
            'fetch("/api/legacy");\n',          # genuinely not served -> phantom
            encoding="utf-8",
        )
        (d / "api" / "routes.js").write_text(
            "const router = express.Router();\n"
            "router.get('/api/users', listUsers);\n"
            'router.post("/api/orders", createOrder);\n'
            "router.put(`/api/orders/:id`, updateOrder);\n"
            "router.delete('/api/orders/:id', deleteOrder);\n"
            "app.patch('/api/users/:id', patchUser);\n"
            "router.use('/api/admin', adminRouter);\n"
            "module.exports = router;\n",
            encoding="utf-8",
        )
        cfg = {
            "frontend": {"globs": ["frontend/**/*.jsx"]},
            "api": {"globs": ["api/**/*.js"]},
            "schema": {"globs": ["schema/**/*.sql"]},
            # No broad_prefixes drop: we want `/api/...` routes to count.
            "options": {"broad_prefixes": []},
        }
        res = core.run(cfg, d)
        routes_count = res["stats"]["routes"]
        # 6 declared routes; /api/orders/:id appears twice (PUT+DELETE) but the
        # path is the same -> 5 unique paths registered.
        assert routes_count >= 5, (
            f"Express router extraction failed: expected >=5 routes, got "
            f"{routes_count}; stats={res['stats']}"
        )
        phantom_eps = {
            f["id"] for f in res["findings"] if f["kind"] == "phantom_endpoint"
        }
        # These must NOT be phantom (Express router serves them):
        for served in (
            "/api/users", "/api/orders", "/api/orders/:param",
            "/api/admin/dashboard",
        ):
            assert served not in phantom_eps, (
                f"Express-served endpoint '{served}' wrongly flagged as phantom; "
                f"all phantoms={phantom_eps}"
            )
        # And the genuinely-unserved one is still caught (sanity: extraction
        # didn't accidentally swallow EVERY fetch as served):
        assert "/api/legacy" in phantom_eps, (
            f"legitimately phantom '/api/legacy' was lost; phantoms={phantom_eps}"
        )


# --- path safety --------------------------------------------------------------
def test_path_traversal_rejected():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "in").mkdir()
        outside = d.parent / "dl_outside_secret.sql"
        try:
            outside.write_text("CREATE TABLE stolen_passwords (x TEXT);", encoding="utf-8")
            cfg = {"schema": {"globs": ["../dl_outside_secret.sql"]}}
            raised = False
            try:
                list(core.iter_files(d, ["../dl_outside_secret.sql"], []))
            except core.ConfigError:
                raised = True
            assert raised, "traversal glob with '..' was not rejected"
            # absolute glob also rejected
            raised_abs = False
            try:
                list(core.iter_files(d, [str(outside)], []))
            except core.ConfigError:
                raised_abs = True
            assert raised_abs, "absolute glob was not rejected"
        finally:
            if outside.exists():
                outside.unlink()


def test_symlink_escape_skipped():
    if not hasattr(os, "symlink"):
        return  # platform without symlinks; skip silently
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "schema").mkdir()
        secret = d.parent / "dl_secret_target.sql"
        secret.write_text("CREATE TABLE leaked (x TEXT);", encoding="utf-8")
        link = d / "schema" / "evil.sql"
        try:
            os.symlink(secret, link)
        except (OSError, NotImplementedError):
            secret.unlink()
            return
        try:
            files = list(core.iter_files(d, ["schema/**/*.sql"], []))
            assert link.resolve() not in {f.resolve() for f in files}, \
                "escaping symlink was followed"
        finally:
            if secret.exists():
                secret.unlink()


def test_oversized_file_skipped():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "schema").mkdir()
        big = d / "schema" / "big.sql"
        big.write_text("CREATE TABLE t (id INT);\n" + ("x" * 5000), encoding="utf-8")
        files = list(core.iter_files(d, ["schema/**/*.sql"], [], max_bytes=100))
        assert files == [], "oversized file was not skipped"


# --- baseline / config robustness --------------------------------------------
def test_baseline_char_split_rejected():
    with tempfile.TemporaryDirectory() as td:
        bp = Path(td) / "b.baseline.json"
        # 'accepted' is a string, not a list -> must be ignored, not char-split
        bp.write_text(json.dumps({"accepted": "phantom_table:payments"}), encoding="utf-8")
        acc = core.load_baseline(bp)
        assert acc == set(), f"string baseline was char-split: {sorted(acc)[:5]}"
        # valid list still works
        bp.write_text(json.dumps({"accepted": ["phantom_table:payments"]}), encoding="utf-8")
        acc2 = core.load_baseline(bp)
        assert acc2 == {"phantom_table:payments"}, acc2


def test_malformed_config_clean_exit():
    with tempfile.TemporaryDirectory() as td:
        bad = Path(td) / "bad.json"
        bad.write_text('{ "name": "x", }', encoding="utf-8")  # trailing comma
        rc = cli.main(["-c", str(bad)])
        assert rc == 2, f"malformed config should exit 2, got {rc}"
        # missing config
        rc2 = cli.main(["-c", str(Path(td) / "nope.json")])
        assert rc2 == 2, f"missing config should exit 2, got {rc2}"
        # not-a-dict config
        notdict = Path(td) / "list.json"
        notdict.write_text("[1, 2, 3]", encoding="utf-8")
        rc3 = cli.main(["-c", str(notdict)])
        assert rc3 == 2, f"non-dict config should exit 2, got {rc3}"


def test_bom_config_loads():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "schema").mkdir()
        (d / "schema" / "s.sql").write_text("CREATE TABLE t (id INT);", encoding="utf-8")
        cfgp = d / "c.json"
        cfgp.write_text(
            json.dumps({"name": "bom", "root": ".", "schema": {"globs": ["schema/**/*.sql"]}}),
            encoding="utf-8-sig",  # write WITH a BOM
        )
        cfg = core.load_config(cfgp)
        assert cfg["name"] == "bom", cfg


def test_null_subkeys_coerced():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "schema").mkdir()
        (d / "schema" / "s.sql").write_text("CREATE TABLE t (id INT);", encoding="utf-8")
        cfgp = d / "c.json"
        cfgp.write_text(json.dumps({
            "name": "n", "root": ".",
            "schema": {"globs": ["schema/**/*.sql"]},
            "options": {"broad_prefixes": None, "ignore": None},
        }), encoding="utf-8")
        cfg = core.load_config(cfgp)
        # should not raise on iteration of coerced-away null sub-keys
        rc = cli.main(["-c", str(cfgp)])
        assert rc == 0, f"null sub-keys broke the run (rc {rc})"


def test_invalid_config_regex_clean_exit():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "schema").mkdir()
        (d / "schema" / "s.sql").write_text("CREATE TABLE t (id INT);", encoding="utf-8")
        cfgp = d / "c.json"
        cfgp.write_text(json.dumps({
            "name": "n", "root": ".",
            "schema": {"globs": ["schema/**/*.sql"], "table_def": ["("]},  # bad regex
        }), encoding="utf-8")
        rc = cli.main(["-c", str(cfgp)])
        assert rc == 2, f"invalid config regex should exit 2, got {rc}"


# --- false-green --------------------------------------------------------------
def test_false_green_missing_root_exit2():
    with tempfile.TemporaryDirectory() as td:
        cfgp = Path(td) / "c.json"
        cfgp.write_text(json.dumps({
            "name": "n", "root": "does_not_exist_xyz",
            "schema": {"globs": ["**/*.sql"]},
        }), encoding="utf-8")
        rc = cli.main(["-c", str(cfgp)])
        assert rc == 2, f"missing root should exit 2 (not green 0), got {rc}"


def test_false_green_zero_file_warning():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        cfgp = d / "c.json"
        cfgp.write_text(json.dumps({
            "name": "n", "root": ".",
            "schema": {"globs": ["migrations/**/*.sql"]},  # matches nothing
        }), encoding="utf-8")
        warns = core.layer_warnings(d, core.load_config(cfgp))
        assert any("0 files" in w for w in warns), f"no zero-file warning: {warns}"
        # a layer that is simply absent (no globs) must NOT warn
        cfg_absent = {"schema": {"globs": []}}
        assert core.layer_warnings(d, cfg_absent) == [], "absent layer warned"


def test_gitignore_does_not_block_baseline():
    """README says 'commit the baseline' -> .gitignore must NOT ignore it."""
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "*.baseline.json" not in gi, ".gitignore still blocks *.baseline.json"


# --- runner -------------------------------------------------------------------
def _all_tests():
    return [
        v for k, v in sorted(globals().items())
        if k.startswith("test_") and callable(v)
    ]


if __name__ == "__main__":
    import contextlib
    import io

    failures = 0
    real_out = sys.stdout
    for fn in _all_tests():
        # Tests exercise the CLI, which prints reports/errors; mute that noise so
        # the direct-run summary stays clean (pytest captures this automatically).
        buf_out, buf_err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                fn()
            real_out.write(f"PASS {fn.__name__}\n")
        except AssertionError as e:
            failures += 1
            real_out.write(f"FAIL {fn.__name__}: {e}\n")
        except Exception as e:  # noqa: BLE001
            failures += 1
            real_out.write(f"ERROR {fn.__name__}: {type(e).__name__}: {e}\n")
    real_out.write("-" * 40 + "\n")
    if failures:
        real_out.write(f"{failures} test(s) failed\n")
        sys.exit(1)
    real_out.write("ALL TESTS PASSED\n")
