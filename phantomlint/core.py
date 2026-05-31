"""Core engine: extract layers, diff them, score severity. Stack-agnostic (config-driven)."""
import difflib
import fnmatch
import json
import re
import sys
from pathlib import Path

# --- sensible defaults (override per-layer in config) -----------------------
# Identifier fragment used inside SQL patterns: a quoted/bracketed/plain name,
# optionally schema-qualified. The capture group grabs the WHOLE dotted reference
# (e.g. public.accounts); _last_segment() then reduces it to the final part.
_ID = r"""(?:"[^"]+"|\[[^\]]+\]|`[^`]+`|[A-Za-z_]\w*)"""
_QUALIFIED = r"""(?:{id}\s*\.\s*)*{id}""".format(id=_ID)
# Optional table modifiers in CREATE TABLE: TEMP/TEMPORARY/UNLOGGED, with an
# optional GLOBAL/LOCAL scope word in front (GLOBAL TEMPORARY, LOCAL TEMP).
_TABLE_MODS = r"""(?:(?:GLOBAL|LOCAL)\s+)?(?:TEMP(?:ORARY)?|UNLOGGED)\s+"""

DEFAULTS = {
    "frontend": {
        "endpoint_patterns": [
            r"""fetch\(\s*[`'"]([^`'"]+)""",
            r"""axios(?:\.\w+)?\(\s*[`'"]([^`'"]+)""",
        ],
    },
    "api": {
        "route_exact": [r"""(?:path|pathname)\s*===?\s*[`'"](/[^`'"]+)"""],
        "route_prefix": [r"""startsWith\(\s*[`'"](/[^`'"]+)"""],
        "route_regex": [r"""\.match\(\s*/((?:\\.|[^/\\])*)/[gimsuy]*\s*\)"""],
        "table_ref": [r"""\b(?:FROM|INTO|UPDATE|JOIN|REFERENCES)\s+(""" + _QUALIFIED + r""")"""],
        "insert_cols": [
            r"""INSERT\s+INTO\s+(""" + _QUALIFIED + r""")\s*\(([^;]*?)\)\s*VALUES"""
        ],
    },
    "schema": {
        "table_def": [
            r"""CREATE\s+""" + r"""(?:""" + _TABLE_MODS + r""")?"""
            + r"""TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(""" + _QUALIFIED + r""")"""
        ],
        "alter_add": [
            r"""ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(""" + _QUALIFIED + r""")\s+ADD\s+"""
            r"""(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?(""" + _ID + r""")"""
        ],
    },
    "options": {
        "table_prefix": "",
        "broad_prefixes": ["/api", "/app"],
        "ignore": ["**/node_modules/**", "**/dist/**", "**/build/**", "**/.git/**"],
        "max_file_bytes": 2_000_000,
        "regex_timeout_seconds": 2,
    },
}
_SQL_CONSTRAINT = re.compile(
    r"^\s*(PRIMARY|FOREIGN|UNIQUE|CHECK|CONSTRAINT|KEY|INDEX|EXCLUDE|LIKE)\b",
    re.IGNORECASE,
)
# Columns referenced by an index must exist -> treat them as known schema columns.
_IDX_COLS = re.compile(
    r"""CREATE\s+(?:UNIQUE\s+)?INDEX(?:\s+CONCURRENTLY)?(?:\s+IF\s+NOT\s+EXISTS)?\s+"""
    + _ID + r"""\s+ON\s+(""" + _QUALIFIED + r""")\s*\(([^)]*)\)""",
    re.IGNORECASE,
)
# Shell-migration helper: run_alter "col" ... and the table it targets.
_RUN_ALTER = re.compile(r"""run_alter\s+["']([a-z_]\w*)["']""", re.IGNORECASE)
_SH_TABLE = re.compile(
    r"""ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(""" + _QUALIFIED + r""")""", re.IGNORECASE
)


def _last_segment(ident):
    """Reduce a (possibly schema-qualified, quoted/bracketed) identifier to its
    final unquoted segment, lowercased. `public."Accounts"` -> 'accounts'."""
    if ident is None:
        return ""
    # split on dots that are not inside quotes/brackets
    parts, buf, quote = [], [], None
    for ch in ident:
        if quote:
            buf.append(ch)
            if (quote == '"' and ch == '"') or (quote == '`' and ch == '`') or (
                quote == '[' and ch == ']'
            ):
                quote = None
        elif ch in '"`[':
            quote = ch
            buf.append(ch)
        elif ch == ".":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    last = parts[-1].strip()
    return last.strip('`"[]').strip().lower()


# --- quote-aware SQL comment stripping --------------------------------------
def _strip_sql_comments(txt):
    """Remove -- line and /* */ block comments, but NOT when they appear inside a
    single- or double-quoted string literal (a DEFAULT '-- x' must survive)."""
    out = []
    i, n = 0, len(txt)
    while i < n:
        ch = txt[i]
        if ch == "'" or ch == '"':
            # consume a string literal, honoring '' / "" doubled-quote escapes
            q = ch
            out.append(ch)
            i += 1
            while i < n:
                c = txt[i]
                out.append(c)
                if c == q:
                    if i + 1 < n and txt[i + 1] == q:  # doubled quote = escaped
                        out.append(txt[i + 1])
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if ch == "-" and i + 1 < n and txt[i + 1] == "-":
            j = txt.find("\n", i)
            if j < 0:
                break
            i = j  # keep the newline
            continue
        if ch == "/" and i + 1 < n and txt[i + 1] == "*":
            j = txt.find("*/", i + 2)
            out.append(" ")
            i = (j + 2) if j >= 0 else n
            continue
        out.append(ch)
        i += 1
    return "".join(out)


# --- quote-aware JS/JSX comment stripping -----------------------------------
def _strip_js_comments(txt):
    """Remove // line and /* */ block comments from JS/JSX/TS, but NOT inside
    string or template literals. Conservative: does not parse regex literals,
    which is acceptable because we only care about string-bearing call sites."""
    out = []
    i, n = 0, len(txt)
    while i < n:
        ch = txt[i]
        if ch in "'\"`":
            q = ch
            out.append(ch)
            i += 1
            while i < n:
                c = txt[i]
                out.append(c)
                if c == "\\" and i + 1 < n:  # escaped char
                    out.append(txt[i + 1])
                    i += 2
                    continue
                if c == q:
                    i += 1
                    break
                i += 1
            continue
        if ch == "/" and i + 1 < n and txt[i + 1] == "/":
            j = txt.find("\n", i)
            if j < 0:
                break
            i = j
            continue
        if ch == "/" and i + 1 < n and txt[i + 1] == "*":
            j = txt.find("*/", i + 2)
            out.append(" ")
            i = (j + 2) if j >= 0 else n
            continue
        out.append(ch)
        i += 1
    return "".join(out)


# --- ReDoS guard: wall-clock timeout around finditer ------------------------
class _RegexTimeout(Exception):
    pass


def _safe_finditer(rx, text, timeout):
    """Yield matches of rx in text, aborting (and warning) if matching exceeds
    `timeout` wall-clock seconds. Guards against catastrophic backtracking in
    user-supplied config patterns. Built-in defaults are linear/safe.

    Uses SIGALRM on POSIX; on platforms without it (or non-main threads) the
    timeout is a no-op and matching runs unguarded."""
    try:
        import signal
    except ImportError:
        signal = None
    use_alarm = (
        timeout
        and timeout > 0
        and signal is not None
        and hasattr(signal, "SIGALRM")
        and hasattr(signal, "setitimer")
        and sys.platform != "win32"
    )
    if not use_alarm:
        yield from rx.finditer(text)
        return

    def _handler(signum, frame):
        raise _RegexTimeout()

    try:
        old = signal.signal(signal.SIGALRM, _handler)
    except (ValueError, OSError):
        # not in main thread -> cannot install handler; run unguarded
        yield from rx.finditer(text)
        return
    try:
        signal.setitimer(signal.ITIMER_REAL, float(timeout))
        # Materialize under the alarm; finditer is lazy so iterate eagerly.
        matches = list(rx.finditer(text))
    except _RegexTimeout:
        sys.stderr.write(
            "phantomlint: warning: regex timed out after "
            f"{timeout}s, skipping a pattern match (possible ReDoS in config)\n"
        )
        matches = []
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        try:
            signal.signal(signal.SIGALRM, old)
        except (ValueError, OSError):
            pass
    yield from matches


# --- config -----------------------------------------------------------------
class ConfigError(Exception):
    """Raised for any user-facing configuration problem (clean message, no traceback)."""


def load_config(path):
    p = Path(path)
    try:
        # utf-8-sig transparently strips a UTF-8 BOM if present.
        text = p.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise ConfigError(f"{p}: config file not found")
    except IsADirectoryError:
        raise ConfigError(f"{p}: is a directory, not a config file")
    except OSError as e:
        raise ConfigError(f"{p}: cannot read config ({e})")

    if p.suffix == ".toml":
        try:
            import tomllib
        except ModuleNotFoundError:
            raise ConfigError(
                "TOML config needs Python 3.11+ (tomllib). Use a .json config instead."
            )
        try:
            cfg = tomllib.loads(text)
        except Exception as e:
            raise ConfigError(f"{p}: invalid TOML ({e})")
    else:
        try:
            cfg = json.loads(text)
        except json.JSONDecodeError as e:
            raise ConfigError(f"{p}: invalid JSON ({e})")

    if not isinstance(cfg, dict):
        raise ConfigError(
            f"{p}: config must be a JSON object, got {type(cfg).__name__}"
        )
    _coerce_null_subkeys(cfg)
    return cfg


def _coerce_null_subkeys(cfg):
    """Turn explicit nulls for list-valued option keys into [] so downstream
    iteration never hits None. A config author writing "ignore": null is treated
    the same as omitting the key (falls back to defaults)."""
    opts = cfg.get("options")
    if not isinstance(opts, dict):
        return
    for k in ("broad_prefixes", "ignore", "globs"):
        if k in opts and opts[k] is None:
            del opts[k]
    for layer in ("frontend", "api", "schema"):
        lay = cfg.get(layer)
        if isinstance(lay, dict) and lay.get("globs") is None:
            lay["globs"] = []


def _merged(cfg, layer, key):
    return cfg.get(layer, {}).get(key) or DEFAULTS.get(layer, {}).get(key) or []


def _opt(cfg, key):
    v = cfg.get("options", {}).get(key)
    if v is None:
        return DEFAULTS["options"].get(key)
    return v


def _compile(patterns, flags=0):
    """Compile config-provided regex patterns, surfacing a clean error (not a
    raw re.error traceback) when an author ships a broken pattern."""
    out = []
    for p in patterns:
        try:
            out.append(re.compile(p, flags))
        except re.error as e:
            raise ConfigError(f"invalid regex pattern {p!r}: {e}")
    return out


# --- file walking -----------------------------------------------------------
def iter_files(root, globs, ignore, max_bytes=None):
    """Yield files under `root` matching `globs`, with hard path containment:
    every yielded file resolves to a path inside root.resolve(). Rejects globs
    that are absolute or contain '..'. Skips symlinks that escape root and files
    larger than `max_bytes`."""
    base = Path(root).resolve()
    if max_bytes is None:
        max_bytes = DEFAULTS["options"]["max_file_bytes"]
    seen = set()
    for g in globs:
        if not isinstance(g, str):
            continue
        if Path(g).is_absolute() or ".." in Path(g).parts:
            raise ConfigError(
                f"glob {g!r} is absolute or escapes the project root with '..' "
                "(globs must stay inside root)"
            )
        try:
            matches = base.glob(g)
        except (ValueError, NotImplementedError) as e:
            raise ConfigError(f"invalid glob {g!r}: {e}")
        for f in matches:
            try:
                if not f.is_file():
                    continue
            except OSError:
                continue
            # Containment: resolved path must be base itself or under base. This
            # also rejects symlinks (in-repo or not) that escape the root.
            try:
                real = f.resolve()
            except OSError:
                continue
            if real != base and base not in real.parents:
                continue
            try:
                rel = f.resolve().relative_to(base).as_posix()
            except ValueError:
                continue
            if any(
                fnmatch.fnmatch(rel, ig) or fnmatch.fnmatch("/" + rel, ig)
                for ig in ignore
            ):
                continue
            if real in seen:
                continue
            try:
                if real.stat().st_size > max_bytes:
                    sys.stderr.write(
                        f"phantomlint: skipping {rel}: exceeds max_file_bytes "
                        f"({max_bytes})\n"
                    )
                    continue
            except OSError:
                continue
            seen.add(real)
            yield f


def _read(p):
    try:
        return p.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def _rel(f, root):
    """Path of f relative to root, as posix. Never emits an absolute or
    out-of-root path: falls back to the basename if containment is somehow lost."""
    base = Path(root).resolve()
    try:
        return f.resolve().relative_to(base).as_posix()
    except ValueError:
        return Path(f).name


# --- extractors -------------------------------------------------------------
def normalize_endpoint(raw):
    if not raw.startswith("/"):
        return ""
    raw = raw.split("?")[0]
    raw = re.sub(r"\$\{[^}]+\}", ":param", raw)   # JS template ${id}
    raw = re.sub(r":\w+", ":param", raw)           # :id style
    return raw.rstrip("/") or "/"


def regex_to_skeleton(src):
    """Collapse a route regex source into a path skeleton, depth-aware so nested
    capture groups become a single :param segment (no stray ')' fragments)."""
    s = src.lstrip("^").rstrip("$")
    # Depth-aware replacement of the OUTERMOST parenthesised groups with /:param/
    out, depth, i, n = [], 0, 0, len(s)
    while i < n:
        ch = s[i]
        if ch == "\\" and i + 1 < n:  # escaped char, copy verbatim at depth 0
            if depth == 0:
                out.append(s[i:i + 2])
            i += 2
            continue
        if ch == "(":
            if depth == 0:
                out.append("/:param/")
            depth += 1
            i += 1
            continue
        if ch == ")":
            if depth > 0:
                depth -= 1
            i += 1
            continue
        if depth == 0:
            out.append(ch)
        i += 1
    s = "".join(out)
    s = s.replace("\\/", "/")
    s = re.sub(r"//+", "/", s)
    res = []
    for seg in s.strip("/").split("/"):
        if not seg:
            continue
        res.append(":param" if re.search(r"[\\\[\]{}+*?.$^|]", seg) else seg)
    return "/" + "/".join(res) if res else ""


def count_files(root, cfg, layer):
    """Number of files a layer's globs actually match (after ignore + containment
    + size cap). Used to surface scanned-file counts and warn on zero-match globs."""
    globs = cfg.get(layer, {}).get("globs", [])
    ignore = _opt(cfg, "ignore")
    max_bytes = _opt(cfg, "max_file_bytes")
    return sum(1 for _ in iter_files(root, globs, ignore, max_bytes))


def layer_warnings(root, cfg):
    """Return a list of warning strings for layers that declare globs but match
    zero files (a likely path/glob typo). A layer with NO globs is intentionally
    absent and produces no warning."""
    warns = []
    for layer in ("frontend", "api", "schema"):
        globs = cfg.get(layer, {}).get("globs", [])
        if not globs:
            continue
        if count_files(root, cfg, layer) == 0:
            warns.append(
                f"layer '{layer}' has globs but matched 0 files "
                f"(check root/globs): {globs}"
            )
    return warns


def extract_endpoints(root, cfg):
    pats = _compile(_merged(cfg, "frontend", "endpoint_patterns"))
    globs = cfg.get("frontend", {}).get("globs", [])
    ignore = _opt(cfg, "ignore")
    max_bytes = _opt(cfg, "max_file_bytes")
    timeout = _opt(cfg, "regex_timeout_seconds")
    eps = {}
    for f in iter_files(root, globs, ignore, max_bytes):
        txt = _strip_js_comments(_read(f))
        for rx in pats:
            for m in _safe_finditer(rx, txt, timeout):
                ep = normalize_endpoint(m.group(1))
                if ep.startswith("/"):
                    eps.setdefault(ep, set()).add(_rel(f, root))
    return eps


def extract_routes(root, cfg):
    broad = {b.rstrip("/") for b in _opt(cfg, "broad_prefixes")}
    ex = _compile(_merged(cfg, "api", "route_exact"))
    pre = _compile(_merged(cfg, "api", "route_prefix"))
    rgx = _compile(_merged(cfg, "api", "route_regex"))
    globs = cfg.get("api", {}).get("globs", [])
    ignore = _opt(cfg, "ignore")
    max_bytes = _opt(cfg, "max_file_bytes")
    timeout = _opt(cfg, "regex_timeout_seconds")
    routes = {}

    def add(path, kind):
        p = path.split("?")[0].rstrip("/")
        if p and p not in broad and routes.get(p) != "exact":
            routes[p] = kind

    for f in iter_files(root, globs, ignore, max_bytes):
        txt = _strip_js_comments(_read(f))
        for rx in ex:
            for m in _safe_finditer(rx, txt, timeout):
                add(m.group(1), "exact")
        for rx in pre:
            for m in _safe_finditer(rx, txt, timeout):
                add(m.group(1), "prefix")
        for rx in rgx:
            for m in _safe_finditer(rx, txt, timeout):
                src = m.group(1)
                skel = regex_to_skeleton(src).rstrip("/")
                if skel:
                    add(skel, "exact" if src.rstrip().endswith("$") else "prefix")
    return routes


def extract_table_refs(root, cfg):
    prefix = _opt(cfg, "table_prefix")
    pats = _compile(_merged(cfg, "api", "table_ref"), re.IGNORECASE)
    globs = cfg.get("api", {}).get("globs", [])
    ignore = _opt(cfg, "ignore")
    max_bytes = _opt(cfg, "max_file_bytes")
    timeout = _opt(cfg, "regex_timeout_seconds")
    refs = {}
    for f in iter_files(root, globs, ignore, max_bytes):
        txt = _strip_js_comments(_read(f))
        for rx in pats:
            for m in _safe_finditer(rx, txt, timeout):
                t = _last_segment(m.group(1))
                if not t:
                    continue
                if prefix and not t.startswith(prefix):
                    continue
                refs.setdefault(t, set()).add(_rel(f, root))
    return refs


def extract_insert_columns(root, cfg):
    """{table: {column: set(files)}} from INSERT INTO t (a,b,...) VALUES - high precision."""
    pats = _compile(_merged(cfg, "api", "insert_cols"), re.IGNORECASE | re.DOTALL)
    globs = cfg.get("api", {}).get("globs", [])
    ignore = _opt(cfg, "ignore")
    max_bytes = _opt(cfg, "max_file_bytes")
    timeout = _opt(cfg, "regex_timeout_seconds")
    cols = {}
    for f in iter_files(root, globs, ignore, max_bytes):
        txt = _strip_js_comments(_read(f))
        for rx in pats:
            for m in _safe_finditer(rx, txt, timeout):
                t = _last_segment(m.group(1))
                if not t:
                    continue
                collist = m.group(2)
                # Skip non-literal column lists (template / concat / quoted) so a
                # dynamically-built INSERT cannot leak its literal fragments.
                if "${" in collist or "+" in collist or '"' in collist:
                    continue
                for c in collist.split(","):
                    c = c.strip().strip('`"\'').lower()
                    if re.fullmatch(r"[a-z_]\w*", c):
                        cols.setdefault(t, {}).setdefault(c, set()).add(_rel(f, root))
    return cols


def extract_schema(root, cfg):
    """Return ({table: [files]}, {table: set(columns)}) parsed from CREATE TABLE,
    ALTER TABLE ADD, CREATE INDEX, and shell-migration helpers."""
    defpats = _compile(_merged(cfg, "schema", "table_def"), re.IGNORECASE)
    altpats = _compile(_merged(cfg, "schema", "alter_add"), re.IGNORECASE)
    globs = cfg.get("schema", {}).get("globs", [])
    ignore = _opt(cfg, "ignore")
    max_bytes = _opt(cfg, "max_file_bytes")
    timeout = _opt(cfg, "regex_timeout_seconds")
    tables, columns = {}, {}
    for f in iter_files(root, globs, ignore, max_bytes):
        raw = _read(f)
        txt = _strip_sql_comments(raw)
        for rx in defpats:
            for m in _safe_finditer(rx, txt, timeout):
                t = _last_segment(m.group(1))
                if not t:
                    continue
                tables.setdefault(t, []).append(_rel(f, root))
                columns.setdefault(t, set()).update(_parse_columns(txt, m.end()))
        # ALTER ADD COLUMN scanned on RAW text so commented-out / idempotent-rewrite
        # ALTERs (columns that exist in prod but were no-op'd in the .sql) are recovered.
        for rx in altpats:                      # ALTER TABLE t ADD COLUMN c
            for m in _safe_finditer(rx, raw, timeout):
                t = _last_segment(m.group(1))
                c = _last_segment(m.group(2))
                if t and c:
                    columns.setdefault(t, set()).add(c)
        # CREATE INDEX ... ON t(cols): every indexed column must exist -> harvest it.
        for m in _safe_finditer(_IDX_COLS, txt, timeout):
            t = _last_segment(m.group(1))
            if not t:
                continue
            for c in m.group(2).split(","):
                c = c.strip().strip('`"\'').split(" ")[0].lower()
                if re.fullmatch(r"[a-z_]\w*", c):
                    columns.setdefault(t, set()).add(c)
        # Shell-helper ALTERs (e.g. run_alter "col" ...) attributed to EVERY table named
        # by an ALTER TABLE in the same migration file. Deliberate over-attribution
        # (cross-product): may suppress a real phantom_column on the wrong table, but
        # never over-flags - favors few false positives, consistent with this tool.
        helper_cols = [mm.group(1).lower() for mm in _RUN_ALTER.finditer(raw)]
        if helper_cols:
            helper_tabs = {
                _last_segment(mm.group(1)) for mm in _SH_TABLE.finditer(raw)
            }
            for tb in helper_tabs:
                if tb:
                    columns.setdefault(tb, set()).update(helper_cols)
    # Operator-asserted columns that exist in prod but are unparseable from migrations
    # (e.g. idempotent rewrites collapsed to prose). Optional config escape hatch.
    for t, cs in cfg.get("schema", {}).get("extra_columns", {}).items():
        columns.setdefault(t.lower(), set()).update(c.lower() for c in cs)
    return tables, columns


def _parse_columns(txt, start):
    """Read the CREATE TABLE ... ( ... ) body from position `start` and extract
    column names. Only parses when the next non-whitespace char after the table
    name is '(' -- bails on CREATE TABLE ... AS SELECT and CREATE TABLE ... LIKE
    so it never grabs the next table's column body (CTAS contamination guard)."""
    # The next non-ws token after the table name must be '(' for a column list.
    k = start
    n = len(txt)
    while k < n and txt[k].isspace():
        k += 1
    if k >= n or txt[k] != "(":
        return set()  # AS SELECT / LIKE / partition-of / no body -> no columns
    i = k
    depth = 0
    j = i
    for j in range(i, n):
        if txt[j] == "(":
            depth += 1
        elif txt[j] == ")":
            depth -= 1
            if depth == 0:
                break
    body = txt[i + 1:j]
    # O(n) split on top-level commas (no char-by-char string accumulation).
    cols, d, seg_start = set(), 0, 0
    parts = []
    for idx, ch in enumerate(body):
        if ch == "(":
            d += 1
        elif ch == ")":
            d -= 1
        elif ch == "," and d == 0:
            parts.append(body[seg_start:idx])
            seg_start = idx + 1
    parts.append(body[seg_start:])
    for line in parts:
        line = line.strip()
        if not line or _SQL_CONSTRAINT.match(line):
            continue
        name = line.split()[0].strip('`"\'').lower()
        if re.fullmatch(r"[a-z_]\w*", name):
            cols.add(name)
    return cols


# --- matching ---------------------------------------------------------------
def _seg(p):
    return [x for x in p.strip("/").split("/") if x]


def _match(a, b, n):
    return all(a[i] == b[i] or a[i] == ":param" or b[i] == ":param" for i in range(n))


def endpoint_served(ep, routes):
    e = _seg(ep)
    for r, kind in routes.items():
        rs = _seg(r)
        if kind == "exact":
            if len(rs) == len(e) and _match(e, rs, len(e)):
                return True
        # Prefix routes match when every segment of the (>=1-segment) route prefix
        # lines up with the endpoint's leading segments. Single-segment prefixes
        # like /health or /login are legitimate; noise is controlled via
        # broad_prefixes (which are dropped from `routes` before matching).
        elif 1 <= len(rs) <= len(e) and _match(e, rs, len(rs)):
            return True
    return False


def suggest(name, candidates, n=1):
    return difflib.get_close_matches(name, list(candidates), n=n, cutoff=0.6)


# --- baseline ---------------------------------------------------------------
def load_baseline(path):
    """Load the set of accepted finding keys from a baseline file. Validates that
    'accepted' is a list of strings; warns and ignores a malformed value rather
    than char-splitting a string into bogus single-character keys."""
    p = Path(path)
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as e:
        raise ConfigError(f"{p}: invalid baseline ({e})")
    if not isinstance(data, dict):
        sys.stderr.write(
            f"phantomlint: warning: baseline {p} is not an object; ignoring it\n"
        )
        return set()
    accepted = data.get("accepted", [])
    if not isinstance(accepted, list) or not all(isinstance(x, str) for x in accepted):
        sys.stderr.write(
            f"phantomlint: warning: baseline {p} 'accepted' must be a list of "
            "strings; ignoring it\n"
        )
        return set()
    return set(accepted)


def finding_key(kind, ident):
    return f"{kind}:{ident}"


# --- run --------------------------------------------------------------------
def run(cfg, root, baseline=None):
    baseline = baseline or set()
    endpoints = extract_endpoints(root, cfg)
    routes = extract_routes(root, cfg)
    table_refs = extract_table_refs(root, cfg)
    insert_cols = extract_insert_columns(root, cfg)
    sch_tables, sch_cols = extract_schema(root, cfg)
    prefix = _opt(cfg, "table_prefix")

    schema_set = set(sch_tables)
    ref_set = set(table_refs)
    schema_scope = {t for t in schema_set if not prefix or t.startswith(prefix)}

    findings = []

    def add(kind, ident, where, sev, hint=None):
        f = {"kind": kind, "id": ident, "where": sorted(where)[:3] if where else [],
             "severity": sev, "key": finding_key(kind, ident)}
        if hint:
            f["suggestion"] = hint
        f["accepted"] = f["key"] in baseline
        findings.append(f)

    # 1. phantom table - backend queries it, not defined in schema
    for t in sorted(ref_set - schema_set):
        s = suggest(t, schema_set)
        add("phantom_table", t, table_refs.get(t, set()), "BLOCK",
            f"did you mean `{s[0]}`?" if s else None)

    # 2. dead table - defined in schema, never queried (within prefix scope only)
    for t in sorted(schema_scope - ref_set):
        add("dead_table", t, sch_tables.get(t, []), "INFO")

    # 3. phantom column - INSERT into a column not in the schema (table exists)
    for t, cols in sorted(insert_cols.items()):
        if t not in sch_cols:
            continue
        for c in sorted(set(cols) - sch_cols[t]):
            s = suggest(c, sch_cols[t])
            add("phantom_column", f"{t}.{c}", cols[c], "WARN",
                f"did you mean `{t}.{s[0]}`?" if s else None)

    # 4. phantom endpoint - frontend calls it, backend doesn't serve it (advisory)
    for ep in sorted(ep for ep in endpoints if not endpoint_served(ep, routes)):
        add("phantom_endpoint", ep, endpoints[ep], "WARN")

    return {
        "name": cfg.get("name", "project"),
        "stats": {
            "schema_tables": len(schema_set), "queried_tables": len(ref_set),
            "endpoints": len(endpoints), "routes": len(routes),
            "columns_checked": sum(len(c) for c in insert_cols.values()),
            "files_scanned": {
                "frontend": count_files(root, cfg, "frontend"),
                "api": count_files(root, cfg, "api"),
                "schema": count_files(root, cfg, "schema"),
            },
        },
        "findings": findings,
    }
