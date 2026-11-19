# Solution Charter

A human-owned charter defines what the solution is and is not. Every change Grafter makes must serve it.

Two files live in the solution's main repo:

| File | Role | Enforced? |
|---|---|---|
| `grafter.charter.yaml` | Purpose, domains, non-goals, invariants, paths, allowed change types | Yes |
| `AGENTS.md` (per repo) | Architecture, conventions, domain terms, history | No, context only |

## Schema

```yaml
solution: <name>
purpose: <one sentence>
core_domains: [..]
users: [..]
non_goals: [..]
invariants: [..]            # must never break
protected_paths: [..]       # never touched by the AI
review_paths: [..]          # allowed, always Tier 2 (e.g. db/migrations/)
allowed_change_types: [bugfix, security, performance, test, refactor, docs, dependency]
feature_changes: propose_only
```

See [examples/acme-harvest](../examples/acme-harvest) (fictional).

## Enforcement

1. **Cite the charter.** Every change names the purpose, domain or invariant it serves. No citation → rejected.
2. **Separate judge.** An agent that did not write the change scores charter alignment. Rejected changes are logged with the reason, never dropped silently.
3. **Features never become code.** New capabilities arrive as issues only.
4. **Locked.** The charter and protected paths are read-only to the AI.
   Always protected, even if the charter omits them: `grafter.charter.yaml`, `grafter.yaml`, `**/AGENTS.md`, `.github/`, `.grafter/`.
5. **Drift signals.** New modules, dependencies and domain vocabulary are flagged and tracked.
