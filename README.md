# phantomlint

**The reality check for AI-coded apps.** Vibe coding ships UI faster than the backend that feeds it - `phantomlint` catches the drift between what your UI promises and what your backend can deliver.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Dependencies](https://img.shields.io/badge/dependencies-0-brightgreen.svg)](pyproject.toml)
[![Python](https://img.shields.io/badge/python-3.8%2B-blue.svg)](pyproject.toml)

Every web app is haunted. Some endpoints get called but never served. Some queries hit tables that don't exist. Some columns are written and never defined. You can't see them with type checkers or unit tests - they're alive only at runtime, in production, on someone else's screen.

**`phantomlint` exorcises them.** Static. Deterministic. 4 checks. MIT.

`phantomlint` cross-checks the three layers of a typical web app contract and tells you, in seconds, where they disagree:

```
frontend  ──calls──▶  API routes  ──queries──▶  database schema
   (fetch)              (served?)                  (table/column exists?)
```

> A **phantom feature** is any place these three layers disagree: a call with no matching route, or a query against a table or column the schema never defines. Wired in the UI, broken at runtime.

- **Zero dependencies, zero LLM, fully deterministic.** Pure static analysis. No API keys, no network, no cost.
- **Stack-agnostic.** Driven by a small config of regex patterns - works with any frontend, any backend routing style, any SQL schema.
- **CI-ready.** Machine-readable JSON, severity levels, exit codes, and a baseline file to accept known/legacy drift so the gate only fails on *new* drift.

Built and maintained by [GolemLabs.ai](https://github.com/GolemLabs-ai). If it catches a phantom in your codebase, a star helps others find it.

## What it detects

| Finding | Meaning | Severity |
|---|---|---|
| **phantom table** | backend queries a table the schema never defines | BLOCK |
| **phantom column** | backend inserts into a column the table never defines | WARN |
| **phantom endpoint** | frontend calls a route the backend never serves | WARN |
| **dead table** | schema defines a table nothing ever queries | INFO |

Phantom table/column suggestions use fuzzy matching (`payments` -> "did you mean `payment`?").

**On severity:** `phantom table` is exact and reliable (it BLOCKs). `phantom column` and `phantom endpoint` are high-recall but accept some noise on large codebases with dynamic SQL or hand-rolled routing - they default to WARN, and the **baseline** (below) is the recommended way to adopt them on an existing project: capture today's findings once, then only *new* drift surfaces.

## Install

```bash
pipx run phantomlint --demo      # zero-install: try the bundled sample in one command
pip install phantomlint          # or: uv tool install phantomlint
```

## Usage

```bash
phantomlint -c phantomlint.json            # human report, exit 1 on BLOCK
phantomlint -c phantomlint.json --json     # machine-readable
phantomlint -c phantomlint.json --strict   # exit 1 on WARN too
phantomlint -c phantomlint.json --update-baseline   # accept current findings
phantomlint --demo                         # run the bundled sample (no config needed)
```

Exit codes: `0` no drift (or only accepted/INFO/WARN without `--strict`), `1`
drift gate failed (a BLOCK, or a WARN under `--strict`), `2` usage/config error.

Try it on the bundled sample (works straight after `pip install`, no path needed):

```bash
phantomlint --demo
```

```
# phantomlint - sample-shop

schema tables: 4 | queried: 3 | endpoints: 3 | routes: 4 | columns checked: 4

## PHANTOM TABLE  (queried, not in schema)  [BLOCK] (1)
  - payments  <- api/server.js   (did you mean `payment`?)

## PHANTOM COLUMN (inserted, not in schema)  [WARN] (1)
  - orders.coupon_code  <- api/server.js

## PHANTOM ENDPOINT (called, not served)  [WARN] (1)
  - /api/refunds  <- frontend/app.jsx

## DEAD TABLE     (defined, never queried)  [INFO] (2)
  - audit_log  <- schema/schema.sql
  - payment  <- schema/schema.sql

---
BLOCK: 1 | WARN: 2 | INFO: 2 | accepted: 0
```

The backend queries `payments` but the schema only defines `payment` - that's a
phantom table, and the fuzzy matcher suggests the near-miss. Exit code is `1`
because a BLOCK finding is present.

## Config

A config describes where each layer lives. Patterns are optional - sensible defaults cover `fetch()`/`axios`, hand-rolled JS routing (`path ===`, `startsWith`, `path.match(/.../)`), and standard SQL. Override per layer for other stacks.

```json
{
  "name": "my-app",
  "root": ".",
  "frontend": { "globs": ["web/src/**/*.{jsx,tsx}"] },
  "api":      { "globs": ["api/src/**/*.js"] },
  "schema":   { "globs": ["migrations/**/*.sql"] },
  "options": {
    "table_prefix": "",
    "broad_prefixes": ["/api"],
    "ignore": ["**/node_modules/**", "**/dist/**"]
  }
}
```

- **`table_prefix`** - restrict table checks to a namespace (e.g. `app_`). Empty = all tables.
- **`broad_prefixes`** - routing prefixes the backend dispatches on, then branches internally (e.g. `/api`, `/app`). Excluded from matching so they don't mask real phantoms. Defaults to `["/api", "/app"]` when omitted.
- TOML configs are also supported on Python 3.11+.

## Baseline (accept known drift)

Legacy projects have drift you already know about. Capture it once so the gate only fails on regressions:

```bash
phantomlint -c phantomlint.json --update-baseline   # writes phantomlint.baseline.json
```

Commit the baseline. From then on, accepted findings are listed but don't fail the build.

## Use in CI

### GitHub Actions

Drop this into `.github/workflows/phantomlint.yml`:

```yaml
name: phantomlint
on: [push, pull_request]
jobs:
  drift-check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - run: pip install phantomlint
      - run: phantomlint -c phantomlint.json
```

Exit code `1` blocks the merge on any new BLOCK finding. Add `--strict` to block on WARN too.

### pre-commit hook

Add to `.pre-commit-config.yaml`:

```yaml
repos:
  - repo: local
    hooks:
      - id: phantomlint
        name: phantomlint (frontend/API/schema drift)
        entry: phantomlint -c phantomlint.json
        language: system
        pass_filenames: false
        stages: [pre-commit]
```

Catches phantom features before they get committed, not after they ship.

## Limitations (honest list)

`phantomlint` is regex + set-arithmetic. That keeps it fast and dependency-free, but it pays for it in specific stacks:

- **ORM-heavy stacks (Prisma / Drizzle / Sequelize / TypeORM):** queries are generated, not visible as raw SQL strings. Run with `--update-baseline` first to accept the generated noise, then only *new* drift surfaces. Native ORM model parsing is on the roadmap.
- **GraphQL:** not supported in v0.1. The 3-layer model (fetch -> route -> SQL) doesn't map cleanly to a GraphQL schema. Track [issue #1](https://github.com/GolemLabs-ai/phantomlint/issues) for progress.
- **Microservices / split repos:** `phantomlint` reads one repo at a time. Frontend in repo A calling backend in repo B requires running it on both with shared route conventions (or pinning OpenAPI specs, which a separate tool can do better).
- **Dynamic SQL (string concatenation, query builders):** false positives possible. `phantom column` defaults to WARN precisely because of this - tune `broad_prefixes` and use baselines.
- **NoSQL (Mongo, DynamoDB, Firestore):** no schema layer to diff against. Out of scope.

When `phantomlint` fits: hand-rolled JS/TS routing + raw SQL migrations + standard `fetch()`/`axios`. Worst case for any stack: run with `--update-baseline`, get the gate working on *new* drift, file an issue with a sample for better support.

## What it is NOT

- Not a type checker (use TypeScript/zod for payload shape).
- Not a runtime/integration test - it reads source, it does not execute.
- Not a full SQL parser - it uses high-precision patterns, favoring few false positives. Endpoint matching against hand-rolled routing is best-effort (WARN, not BLOCK) and improves as you tune patterns.

## License

MIT © GolemLabs.ai
