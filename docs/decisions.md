# Decisions

| # | Date | Decision | Why |
|---|---|---|---|
| D1 | 2026-11-19 | Name: **Grafter** | Grafting = approved branches joined onto `main`; ties to agriculture roots |
| D2 | 2026-11-19 | Licence: **MIT**, open-source core only | Reputation; enterprise layer parked for later evaluation |
| D3 | 2026-11-19 | **1:1** per solution, not multi-tenant | Maintaining one solution is hard enough |
| D4 | 2026-11-19 | Agent engine: **OpenHands Software Agent SDK** behind an `AgentEngine` adapter | Python, MIT, model-agnostic, Docker workspaces. Never use OpenHands' `enterprise/` directory |
| D5 | 2026-11-19 | Tier 0 auto-merge: **opt-in**, off by default | Human gate stays the default |
| D6 | 2026-11-19 | Git host v1: **GitHub only** (GitHub App) | Scope |
| D7 | 2026-11-19 | Charter: **`grafter.charter.yaml` + free-text `AGENTS.md`** | YAML = enforced rules; AGENTS.md = context |
| D8 | 2026-11-19 | Fix branches cut from **`main`**, `ai/preview` rebuilt | Approving one change never ships another |
| D9 | 2026-11-19 | Backend **Python**, dashboard **TypeScript/Next.js** | Best fit, not team familiarity |
| D10 | 2026-11-19 | v1 = Python script + cron + GitHub PRs as review UI | Prove the flow before building a dashboard |
| D11 | 2026-11-19 | Runs on a Linux VM with Docker, not GitHub Actions runners | Time limits; secrets stay off GitHub |
| D12 | 2026-11-19 | Repo **private until the pilot ends**, then public | Publish with real results |
| D13 | 2026-11-19 | No CLA | Not needed under MIT without dual licensing |
| D14 | 2026-11-20 | Gate thresholds: coverage target per repo = pilot baseline **+ 20 points, at least 60%** (line coverage); gate 1 → 2 needs **≥ 70% of the last 20** Grafter PRs approved and zero guard escapes; a change category is throttled after **3 rejections in a row** | Measurable, reachable from a weak baseline, strict enough to prove value |
| D15 | 2026-11-20 | Models per role, all configurable (`models:` in `grafter.yaml`): **planner, reviewer, judge = Claude Opus 5.5** (`claude-opus-5-5`); **coder = Claude Sonnet 5.5** (`claude-sonnet-5-5`). All calls go through LiteLLM | Reasoning and planning need the strongest model; code writing runs at half the price |
| D16 | 2026-11-20 | Hard spend cap **$5 per night** (`limits.usd_budget_per_night`), enforced as `max_budget` on the LiteLLM virtual key; the token budget stays as a second limit | About $150 a month; fits 3 PRs a night at Opus/Sonnet prices with caching |
| D17 | 2026-11-20 | State store: **SQLite**, one file on the Grafter host's local disk (default `~/.grafter/grafter.db`), WAL mode + busy timeout, nightly `.backup` copy. Only the Grafter process opens it, never sandboxes. Schema stays portable (migrations from day one, plain SQL types, no SQLite-only features). **PostgreSQL is parked for the enterprise evaluation** (D2) | 1:1, single writer, tiny data volume; nothing extra to run, secure or back up. Portable schema keeps a later move cheap |

| D18 | 2026-11-20 | LiteLLM runs with **its own private PostgreSQL** in the same compose stack (`deploy/litellm/`), no published port. It is the gateway's key/spend store only, not Grafter state (D17 stays SQLite) | LiteLLM's virtual keys and budgets require Postgres; keeps per-run scoped keys and the hard daily cap (D16) at the gateway |
| D19 | 2026-11-21 | The **sandbox repo is public** (`CodeX-Studio-MY/grafter-sandbox`, fictional content only). The GitHub App `grafter-codex-studio` is owned by the org, installed on selected repos only, with contents/pull requests/issues RW and checks/metadata R, no webhook, no workflows/administration. **Pilot repos need a paid plan** (GitHub Team, or a client org that has one) before 0D3/1F1, because rulesets on private repos are paid-only | The free org plan returns 403 for rulesets on private repos. The ruleset is GitHub's own guarantee that the App cannot push to or merge into `main`; it was proven on the sandbox (App push to `main` rejected with GH013, push to `ai/*` accepted) |
| D20 | 2026-11-21 | Phase-1 scanners are **Trivy** (dependencies, secrets, misconfiguration) and **OSV-Scanner**, both Apache-2.0, run as pinned unmodified images. **Semgrep is skipped for now**: its registry rules are under the Semgrep Rules License, not open source, which does not fit an MIT product that may be sold; code-pattern scanning is a later decision (1E3). Scanners run **offline**: databases are refreshed into a Docker volume by a step that has no repo content, and scans run with `--network none` on a copy of the repo, with the cache read-only | No repo content or dependency list ever leaves the host (OSV's online mode would send the client's package list to osv.dev); scanners never see agent-written code because they scan the fresh `main` checkout |
| D21 | 2026-11-22 | Roadmap gains two phases **before the public release**: **3 Normal-risk fixes** and **4 Solution self-evolution**. Public release becomes phase 5, dashboard + next solution phase 6. In phase 4 Grafter continuously scans the whole solution and proposes code changes that evolve it (UI/UX, backend performance, current best practice), as long as the charter's purpose and invariants hold. Every change is still a PR that a human approves or rejects; `main` stays read-only to the AI | The goal of the project is a solution that keeps evolving, not only one that stays patched. Phases 1–3 build the safety net (tests, change DB, judge, learning loop) that evolution needs; the public case study then shows the whole product |
| D22 | 2026-11-22 | Replaces "features are proposed as issues, never written as code". The charter's `feature_changes` gets a second value, **`allow_code`**: Grafter may then write new features as code PRs (phase 4 onward). The **default stays `propose_only`**, so a solution opts in explicitly in its charter, which only a human can edit. Feature and evolution PRs must cite the charter, pass the separate charter judge, carry measured evidence (benchmarks, before/after screenshots, accessibility check) and respect churn limits | The owner wants the solution to grow, and every change already passes human review. The opt-in default, the human-only charter and the judge keep scope creep and drift in check |

Prices at decision time (Anthropic API, per 1M tokens): Opus 5.5 $4 in / $20 out; Sonnet 5.5 $2 in / $10 out.

## Open

- Enterprise evaluation (D2, roadmap phase 6): PostgreSQL as state store, only if needed for many concurrent writers, a multi-user dashboard, or a database on another host.

- Coverage target numbers per repo: computed from D14 once 0C2 measures the pilot baseline (kept outside this repo).
