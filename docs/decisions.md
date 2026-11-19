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

## Open

- Numeric thresholds for each roadmap gate (coverage target, minimum approval rate)
