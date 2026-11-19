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

Prices at decision time (Anthropic API, per 1M tokens): Opus 5.5 $4 in / $20 out; Sonnet 5.5 $2 in / $10 out.

## Open

- Enterprise evaluation (D2, roadmap phase 4): PostgreSQL as state store, only if needed for many concurrent writers, a multi-user dashboard, or a database on another host.

- Coverage target numbers per repo: computed from D14 once 0C2 measures the pilot baseline (kept outside this repo).
