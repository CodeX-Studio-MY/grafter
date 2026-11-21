# Architecture

```
Task finders (coverage, scanners, issues; improvement scout from phase 4)
                       │
Scheduler ─► Orchestrator ─► Agent workers ──► LLM gateway (LiteLLM)
                                   │
                                   ▼
                      Charter judge + CI gates ◄── grafter.charter.yaml
                                   │
                                   ▼
                    ai/* branch + PR, ai/preview rebuilt
                                   │
                                   ▼
                 Morning review (approve / reject / rework) ── Change DB
                       │                    │
                       ▼                    └─ rework ─► Agent workers
              Squash-merge into main
                       │
                       ▼
          (solution's own CI/CD — out of scope)
```

## Stack

| Layer | v1 | Later |
|---|---|---|
| Scheduler | cron | same |
| Orchestrator | Python script | Python FastAPI (+ Temporal if needed) |
| Agent workers | Python + OpenHands SDK in Docker | same |
| LLM access | LiteLLM gateway (`deploy/litellm/`, own private Postgres, D18), scoped key per run, $5/night cap; planner/reviewer/judge = Opus 5.5, coder = Sonnet 5.5 (configurable) | same |
| Review UI | GitHub PRs | Next.js dashboard (phase 6) |
| State | SQLite on local disk, WAL mode (D17) | same; PostgreSQL only if the enterprise evaluation needs it |
| Git | GitHub App | same |
| Scanners | Renovate, Trivy, OSV-Scanner (D20; Semgrep deferred, 1E3) | same |
| Sandbox | Rootless Docker | gVisor option |

## Where it runs

A Linux VM with Docker owned by the solution owner. Not GitHub Actions runners (job time limits; secrets would live in GitHub).

## Sandbox image per solution

Each solution ships a build-and-test image. Example for a .NET + React + Flutter + MSSQL solution:

- .NET SDK, Node, Flutter SDK
- MSSQL container seeded with test data
- Limitation: iOS builds cannot run on Linux; Flutter is covered by unit and widget tests only

## Engine adapter

```python
class AgentEngine(Protocol):
    def run(self, task: Task, workspace: Path, charter: Charter) -> RunResult: ...
```

Default: OpenHands. Planned adapters: OpenCode, Claude Code, Codex CLI.

### OpenHands engine (`engine: openhands`, TODO 1C3; spike: [docs/spikes/1C2-openhands.md](spikes/1C2-openhands.md))

Each task works like this:

1. **Run key.** One gateway run key is created for `[models.planner, models.coder]`. It is capped at `usd_budget_per_night / max_prs_per_night` and valid for 1 h, and it is always deleted at the end.
2. **Planner.** One chat call made from the host with `models.planner`. Its input is the task, the charter (cited domain and invariants), the repo's `AGENTS.md`, the target file and its existing tests, each with a size cap. It returns a short test plan.
3. **Coder.** An OpenHands agent running `models.coder` inside a fresh agent sandbox container. The image is `openhands.image`: the agent server, pinned by digest, plus test tools, from `sandboxes/agent-python`. The container runs only on the internal network `openhands.network`, behind a relay (1D2c). The work tree arrives as a `git archive`. The agent follows the plan and runs the repo's tests in the container. It works under limits on iterations, USD and tokens (`token_budget_per_night / max_prs_per_night`), plus a timeout.
4. **Result.** Changed files that were all CRLF get CRLF again. The container's diff comes back as a patch and is applied on the host with `git apply`, which refuses paths outside the repo. Then the pipeline continues with the real diff stats, the guard and the gates.
5. **Cost.** Cost is the gateway's spend for the run key. The pipeline adds it up, stores it per run and stops at `usd_budget_per_night`.

Nothing the agent writes runs on the host. The agent container:
- gets only the run key, never the master key or the Anthropic key;
- reaches only the gateway. `grafter-gw` is `internal`. A relay on that network and the default bridge publishes the agent server on `127.0.0.1` only, and the agent server requires a random session key that only Grafter holds;
- runs with no capabilities and `no-new-privileges`, so the image's sudo does not work.

The engine refuses to start if the network is not internal. The gates execute the new tests in the Docker runner, which is offline (1D2b). `grafter run` refuses `engine: openhands` with `runner: local`.
