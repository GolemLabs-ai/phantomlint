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

## Supported stacks (v0.1.1)

`phantomlint` works on three layers expressed as **plain text**: a frontend that calls endpoints with `fetch()`/`axios`, a backend that mounts routes, and a database whose schema is defined in raw SQL `CREATE TABLE` statements.

**Routing patterns parsed in v0.1.1:**

- Hand-rolled JS/TS dispatch: `path === '/route'`, `path.startsWith('/route')`, `path.match(/regex/)`
- **Express Router**: `router.get|post|put|patch|delete|all('/route', handler)` (also `app.get|...`)

**Schema layer:**

- Raw SQL `CREATE TABLE` migrations (Postgres / MySQL / SQLite dialects)

Ground-truth on three public OSS repos (v0.1.1):

| Stack | Result |
|---|---|
| React + Express + raw SQL ([vandenn/fullstack-react-blog](https://github.com/vandenn/fullstack-react-blog)) | 10 routes extracted, 14 columns checked, 1 baseline-acceptable FP from `ON CONFLICT DO UPDATE SET` UPSERT |
| Next.js + Prisma ([prisma/fullstack-prisma-nextjs-blog](https://github.com/prisma/fullstack-prisma-nextjs-blog)) | 0 BLOCK findings (import statements no longer trip the SQL `FROM` regex). Schema layer silent (Prisma DSL not parsed - see roadmap). |
| Django + DRF ([vintasoftware/django-react-boilerplate](https://github.com/vintasoftware/django-react-boilerplate)) | 1 BLOCK (down from 11 in v0.1.0 - Python `from X import Y` no longer matched as SQL `FROM`). Schema layer silent (Django models.py not parsed - see roadmap). |

## Limitations (honest list)

- **ORM schema parsing (Prisma / Drizzle / Sequelize / TypeORM / Django models):** the schema layer doesn't read `schema.prisma`, `models.py`, or TypeScript decorators. Tables are still queried correctly through `--update-baseline` for raw SQL parts, but the phantom-column check is silent on ORM-modeled tables. Native parsers are on the v0.2 roadmap.
- **GraphQL:** not supported. The 3-layer model (fetch → route → SQL) doesn't map cleanly to one GraphQL schema. v0.2 candidate.
- **Microservices / split repos:** `phantomlint` reads one repo at a time. Cross-repo frontend ↔ backend needs each side scanned separately (or pinned OpenAPI specs, which a separate tool can do better).
- **Dynamic SQL (string concatenation, query builders):** false positives possible. `phantom column` defaults to WARN precisely for this - tune `broad_prefixes` and use baselines.
- **NoSQL (Mongo, DynamoDB, Firestore):** no schema layer to diff against. Out of scope.

Worst case for any stack: run `--update-baseline` once, get the gate working on *new* drift, file an issue with a sample for better support. PRs welcome.

## What it is NOT

- Not a type checker (use TypeScript/zod for payload shape).
- Not a runtime/integration test - it reads source, it does not execute.
- Not a full SQL parser - it uses high-precision patterns, favoring few false positives. Endpoint matching against hand-rolled routing is best-effort (WARN, not BLOCK) and improves as you tune patterns.

## License

MIT © GolemLabs.ai
