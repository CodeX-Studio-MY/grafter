# Tiered autonomy and guardrails

| Tier | Examples | Path |
|---|---|---|
| 0 Auto | Patch-level dependency bump (incl. an upstream CVE fix shipped as a patch release), ≤ 2 files | Auto-merge if all gates pass (opt-in). Agent-written security code is never Tier 0 |
| 1 Approve | Minor bumps, bug fixes, refactors, tests | One-click human approval |
| 2 Review | `review_paths` (migrations), auth/security logic, cross-repo groups; from phase 3, normal-risk fixes (exact split in TODO 3.1); from phase 4, UI/UX and performance evolution and feature code (`allow_code`, D22) | Full human review |
| Never | Charter, own permissions, guardrails, audit log, `protected_paths` | Locked |

Feature code and evolution changes are never Tier 0.

## Guardrails

- One PR = one change. Cap 3–5 PRs and a token budget per night.
- Rotating nightly focus (security, performance, tests, mobile; UI/UX from phase 4).
- Evidence (phases 3–4): every PR shows tests and coverage delta; performance PRs show a before/after benchmark (no measured gain, no PR); UI/UX PRs show before/after screenshots and an accessibility check.
- Churn limits (phase 4): evolution PRs per night/week, cooldown on recently changed files.
- "Best practice" comes from pinned, cited sources (official guides, release notes), never from unvetted web content.
- Tests first: on a weakly tested solution, the first job is writing tests.
- A `test` change may touch only the repo's `test_paths`, may not delete files, remove test declarations or add skips.
- Separate judges: locked test suite, reviewer agent, security scan, charter judge.
- Sandboxes hold no production secrets and no raw LLM keys (scoped gateway key only).
- Dependency and web content is untrusted input (prompt injection).
- Learning loop: approval rate per category; rejection comments feed agent memory; low-acceptance categories throttled.

## Known risks

- The agent grades its own work if tests are weak.
- Git cannot merge several repos atomically.
- Schema migrations cannot be undone with `git revert`.
- Runaway agents (dozens of PRs overnight) — the caps exist for this.
- Review fatigue: large or frequent evolution PRs get approved without a real look — size caps, churn limits and evidence exist for this.
- Trend churn: rewriting working code to follow fashion — changes must cite the charter and show a measured gain.

Safety features are never moved to a paid tier.
