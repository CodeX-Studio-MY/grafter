# Overview

Grafter is an open-source (MIT) AI maintainer embedded into one software solution, so the solution keeps improving, fixing bugs and patching security on its own, without drifting from its core purpose.

- **Pitch:** Continuous AI that grafts only what you approve.
- **Model:** 1:1. One Grafter instance per solution. Not multi-tenant, not a shared platform.
- **Core promise:** evolve without drifting. A postharvest system stays a postharvest system.
- **Metaphor:** each change is a scion (a cutting); `main` is the rootstock; only approved scions are grafted on.

## Scope

| Decision | Choice |
|---|---|
| Deployment model | 1:1, one instance per solution |
| Multi-tenant | No |
| Repos per solution | Many (API, web, mobile) |
| Licence | MIT |
| Models | Any (BYOK, local via vLLM/Ollama, routed through LiteLLM) |
| Git host (v1) | GitHub only |
| Production deploy | Out of scope; Grafter stops at merging into `main` |
| Branch names | Configurable |
| `main` | Read-only to the AI, enforced by a GitHub ruleset |

**Autonomy boundary:** the AI works without human input from finding work through writing, testing and branching. A human decision is required at one point only: merging into `main`. Tier 0 auto-merge is opt-in and off by default.

## Not in scope

- Multi-solution or multi-tenant operation
- Deploying to production
- Writing new features as code unless the charter opts in with `feature_changes: allow_code` (phase 4, D22); by default features are proposed as issues only
