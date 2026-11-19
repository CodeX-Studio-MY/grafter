# Tiered autonomy and guardrails

| Tier | Examples | Path |
|---|---|---|
| 0 Auto | Patch-level dependency bump (incl. an upstream CVE fix shipped as a patch release), ≤ 2 files | Auto-merge if all gates pass (opt-in). Agent-written security code is never Tier 0 |
| 1 Approve | Minor bumps, bug fixes, refactors, tests | One-click human approval |
| 2 Review | `review_paths` (migrations), auth/security logic, cross-repo groups | Full human review |
| Never | Charter, own permissions, guardrails, audit log, `protected_paths` | Locked |

## Guardrails

- One PR = one change. Cap 3–5 PRs and a token budget per night.
- Rotating nightly focus (security, performance, tests, mobile).
- Tests first: on a weakly tested solution, the first job is writing tests.
- Separate judges: locked test suite, reviewer agent, security scan, charter judge.
- Sandboxes hold no production secrets and no raw LLM keys (scoped gateway key only).
- Dependency and web content is untrusted input (prompt injection).
- Learning loop: approval rate per category; rejection comments feed agent memory; low-acceptance categories throttled.

## Known risks

- The agent grades its own work if tests are weak.
- Git cannot merge several repos atomically.
- Schema migrations cannot be undone with `git revert`.
- Runaway agents (dozens of PRs overnight) — the caps exist for this.

Safety features are never moved to a paid tier.
