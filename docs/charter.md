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
allowed_change_types: [bugfix, security, performance, test, refactor, docs, dependency]  # ui_ux from TODO 4.1
feature_changes: propose_only  # default; allow_code lets Grafter write features as code (phase 4, D22)
repos:                      # optional extra rules per repo, on top of the global ones
  <repo-name>:              # must match a `repos[].name` in grafter.yaml
    protected_paths: [..]
    review_paths: [..]
```

Path rules for a change in repo `X` = global rules + `repos.X` rules. A repo with no entry gets only the global rules. A path may not be both protected and review, including across global and per-repo rules. `grafter validate CHARTER --config grafter.yaml` also checks that every `repos:` key names a configured repo.

Each repo is configured in `grafter.yaml` (`repos[]`: `name`, `github`, `default_branch`, `language`, `test_command`, `coverage_command`, `test_paths`, `sandbox_image`); see [grafter.example.yaml](../grafter.example.yaml).

See [examples/acme-harvest](../examples/acme-harvest) (fictional).

## Enforcement

1. **Cite the charter.** Every change names the purpose, domain or invariant it serves. No citation → rejected.
2. **Separate judge.** An agent that did not write the change scores charter alignment. Rejected changes are logged with the reason, never dropped silently.
3. **Features are issues by default.** With `feature_changes: propose_only` (the default) new capabilities arrive as issues only. `allow_code` (phase 4, D22) lets Grafter write them as code PRs; they still cite the charter, pass the judge and need human approval.
4. **Locked.** The charter and protected paths are read-only to the AI.
   Always protected, even if the charter omits them: `grafter.charter.yaml`, `grafter.yaml`, `**/AGENTS.md`, `.github/`, `.grafter/`.
5. **Drift signals.** New modules, dependencies and domain vocabulary are flagged and tracked.
