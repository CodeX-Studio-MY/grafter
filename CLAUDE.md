# Grafter — instructions for Claude

Read this first in every session. Full plan: `docs/` (start with `docs/decisions.md`).

## What Grafter is
Open-source (MIT) AI maintainer embedded 1:1 into ONE software solution. Nightly it finds work, writes fixes in a sandbox, tests them, opens one `ai/*` branch + PR per change, rebuilds `ai/preview`. Humans approve; only approved branches are squash-merged into `main`. A Solution Charter (`grafter.charter.yaml` + `AGENTS.md`) keeps changes aligned with the solution's purpose (no drift).

## Current state (2026-11-19)
- Scaffold only. Works: charter model + validation, guard rules (citation/type/paths → tier), config, CLI `validate`, 9 tests.
- Not built: OpenHands engine (`engines/openhands.py` raises NotImplementedError), `grafter run`, sandbox, git/branch ops, GitHub PRs, preview rebuild, change DB, scheduler, learning loop.
- Phase: **0 — Foundations** (see `docs/roadmap.md`).

## Phase 0 tasks (in order)
1. Write the pilot solution's charter + AGENTS.md — they live in the PILOT's repos, never in this repo.
2. Measure the pilot's test coverage per repo (backend, web, mobile).
3. Set numeric gate thresholds in `docs/decisions.md` (coverage target, min approval rate).
4. Provision host (Linux VM + Docker); create GitHub App; add `main` ruleset (PR + 1 approval + `test` check, no force push).

## Phase 1 (next): script pilot
- `OpenHandsEngine.run` via OpenHands Software Agent SDK (MIT packages only, never `enterprise/`) + Docker workspace.
- `grafter run`: pick task → engine → `guard.check` → create `ai/<id>` branch, commit, push, open PR (GitHub App) → cap PRs per night.
- First job type: writing tests only.

## Fixed decisions (do not change without the user)
- MIT; 1:1 per solution; not multi-tenant; GitHub only (v1).
- Python backend; Next.js dashboard only in phase 4. v1 review UI = GitHub PRs.
- Fix branches cut from `main`; `ai/preview` rebuilt, never committed to.
- AI never pushes to `main`; Tier 0 auto-merge opt-in, default off.
- Features are proposed as issues, never written as code.
- Safety features never become paid-only.

## Rules for working in this repo
- Private until the pilot ends. NEVER commit pilot-solution code, client names or client data; use fictional `examples/`.
- Copied code: MIT/Apache only, keep its notice, list it in `THIRD_PARTY.md`. No GPL/AGPL.
- Before commit: `ruff check . && pytest -q`.
- One change per PR. Keep docs in `docs/` in sync with decisions.
- User prefers terse, code-first answers.
