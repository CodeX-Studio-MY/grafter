# Grafter

> Continuous AI that grafts only what you approve.

Grafter is an open-source AI maintainer you embed into **one** software solution. Every night it finds work, writes fixes, tests them and opens one branch per change. Humans approve; only approved branches are grafted onto `main`. A **Solution Charter** keeps every change aligned with the solution's purpose, so the system evolves without drifting (no 长歪了).

**Status:** pre-alpha · private pilot · not ready for use.

## Core ideas

| Concept | In one line |
|---|---|
| 1:1 | One Grafter instance serves exactly one solution (which may span several repos). Not multi-tenant. |
| Charter | `grafter.charter.yaml` (enforced rules) + `AGENTS.md` (context). Read-only to the AI. |
| Scion branches | Every change is its own `ai/*` branch, cut from `main`. |
| Preview | `ai/preview` = `main` + all pending branches, rebuilt, never committed to directly. |
| Human gate | `main` only accepts PRs with a human approving review. Tier 0 auto-merge is opt-in. |
| Stops at main | Deployment belongs to the solution's own CI/CD. |

## How it works

```
schedule → find work → write fix (sandbox) → tests + reviewer + charter judge
        → ai/<change> branch + PR → ai/preview rebuilt → human approves → squash-merge to main
```

## Docs

- [TODO / project status](TODO.md)
- [Overview](docs/overview.md)
- [Decisions](docs/decisions.md)
- [Solution Charter](docs/charter.md)
- [Architecture](docs/architecture.md)
- [Branch and change model](docs/branch-model.md)
- [Tiered autonomy and guardrails](docs/autonomy.md)
- [Roadmap](docs/roadmap.md)
- [Operations (draft)](docs/operations.md)
- [Landscape and prior art](docs/landscape.md)

## Development

```bash
pip install -e ".[dev]"
grafter validate examples/acme-harvest/grafter.charter.yaml
pytest
```

## Licence

[MIT](LICENSE) © 2026 CodeX Studio. Third-party credits: [THIRD_PARTY.md](THIRD_PARTY.md).
