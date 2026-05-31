# Security Policy

## Reporting a Vulnerability

If you discover a security issue in this project, please report it privately
to **security@golemlabs.ai** (or open a GitHub Security Advisory). Do not open a
public issue for an undisclosed vulnerability. We aim to acknowledge reports
within 5 business days.

## Threat model

This is a static, offline, zero-dependency, zero-network linter. It reads source
files only and never executes them, makes no network calls, and stores no
credentials.

Because a config can be supplied by an untrusted source (e.g. a fork PR that
edits `phantomlint.json` in CI), phantomlint enforces several guardrails so a config
alone cannot read or wedge the host:

- **Path containment.** Every scanned file must resolve to a path inside
  `root`. Globs that are absolute or contain `..` are rejected with a clean
  error, and symlinks that escape `root` are skipped - a config cannot read
  files outside the project (no `../../etc/...` exfiltration into CI logs).
- **Per-file size cap.** Files larger than `options.max_file_bytes` (default
  ~2 MB) are skipped with a stderr note, so a giant migration cannot OOM the
  runner. Column parsing is linear, not quadratic.
- **ReDoS guard.** Pattern matching runs under a wall-clock timeout
  (`options.regex_timeout_seconds`, default 2s) on POSIX; on timeout the match
  is skipped with a warning. Built-in default patterns are linear; the guard
  protects against catastrophic backtracking in user-supplied config regexes.

The most plausible remaining class of issue is a crafted input file causing a
crash. Reports in that area are welcome.
