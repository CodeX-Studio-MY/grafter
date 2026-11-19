# Grafter — instructions for Claude

Read this first in every session. Full plan: `docs/` (start with `docs/decisions.md`).

## What Grafter is
Open-source (MIT) AI maintainer embedded 1:1 into ONE software solution. Nightly it finds work, writes fixes in a sandbox, tests them, opens one `ai/*` branch + PR per change, rebuilds `ai/preview`. Humans approve; only approved branches are squash-merged into `main`. A Solution Charter (`grafter.charter.yaml` + `AGENTS.md`) keeps changes aligned with the solution's purpose (no drift).

## Current state and next work
- **`TODO.md` is the single source of status.** Read it at session start; the first unticked item without 🧑/⚖️ is next.
- Phase: **0 — Foundations** (see `docs/roadmap.md`). Scaffold only: charter model, guard, config, CLI `validate`.
- Pilot charter + AGENTS.md live in the PILOT's repos, never in this repo.
- OpenHands: MIT packages only, never `enterprise/`. Phase 1 job type: writing tests only.

## Definition of done (every TODO item)
- One TODO group per branch + PR (e.g. 0B1–0B2), one commit per item tagged `[<id>]`. Branch cut from `main`.
- Tests first; `ruff check . && ruff format --check . && pytest -q` green.
- Item's "Done when" check run; its output pasted in the PR body.
- Box ticked in `TODO.md` in the same PR. Never tick an unverified item.
- 🧑 items: prepare everything possible, then tell the user exactly what to do.
- ⚖️ items: present options + a recommendation; record the decision in `docs/decisions.md`.

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
- Before commit: `ruff check . && ruff format --check . && pytest -q` (or `pre-commit run -a`).
- One TODO group per PR. Keep docs in `docs/` in sync with decisions.
- User prefers terse, code-first answers.
- Dev machine is Windows; the Grafter host is Linux. Keep paths POSIX in code (`PurePosixPath`), test both separators.
