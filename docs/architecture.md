# Architecture

```
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

| Layer | v1 | Later (phase 4) |
|---|---|---|
| Scheduler | cron | same |
| Orchestrator | Python script | Python FastAPI (+ Temporal if needed) |
| Agent workers | Python + OpenHands SDK in Docker | same |
| LLM access | LiteLLM gateway, scoped key per sandbox, $5/night cap; planner/reviewer/judge = Opus 5.5, coder = Sonnet 5.5 (configurable) | same |
| Review UI | GitHub PRs | Next.js dashboard |
| State | PostgreSQL (container on the VM, from phase 1) | same |
| Git | GitHub App | same |
| Scanners | Renovate, Semgrep, Trivy, OSV-Scanner | same |
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
