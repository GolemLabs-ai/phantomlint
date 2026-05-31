# Changelog

All notable changes to phantomlint are documented in this file.

## [0.1.1] - 2026-05-31

### Fixed
- **Bug #1 (parser):** Python/TS `import ... from X` and `from X import Y` statements no longer leak phantom table names into SQL `FROM` detection. `_strip_import_lines()` now removes import lines before the SQL `FROM` regex runs.
  - Retest impact: django-react-boilerplate 11 BLOCK -> 1 BLOCK (91% FP reduction). prisma-nextjs-blog 2 BLOCK -> 0 BLOCK (100% BLOCK reduction).
- **Bug #2 (parser):** Express `router.get/post/put/delete(...)` mounts in `server/routes/*.js` are now extracted as server-side routes. Previously 0 routes extracted; raw-SQL React+Express repo now correctly surfaces 14 routes with `:param` normalization.
  - Retest impact: react-blog-raw-sql routes extracted 0 -> 10 across `router.get`/`post`/`put`/`delete`.

### Notes
- Residual BLOCK findings in retest are pre-existing edge cases out of Bug #1/#2 scope: SQL keyword `set` from `ON CONFLICT DO UPDATE SET`, and comment-text `from the database`/`from the backend` in Python docstrings. Both are baseline-acceptable.

## [0.1.0] - 2026-05-31

### Initial release
- Static cross-layer linter for phantom features in web app contracts.
- 4 checks: phantom_table (BLOCK), phantom_column (WARN), phantom_endpoint (WARN), dead_table (INFO).
- Baseline file support for legacy drift acceptance.
- Zero dependencies, zero LLM, deterministic.
- Fuzzy matching suggestions for phantom names.
- CLI: `phantomlint -c config.json [--json] [--strict] [--demo] [--update-baseline]`.
- Bundled sample (sample-shop) runnable via `phantomlint --demo`.

### Note
Project was renamed from `driftlint` to `phantomlint` on 2026-05-31 before first public release. The "phantom features" terminology is core to the README and findings vocabulary, so the package name aligns. No prior public PyPI releases existed under the previous name.
