# Branch and change model

Every change branch is cut from `main`, never from preview, so approving one change never ships another.

```
main ──●────────────●──────────►   protected; AI has no write access
       ├─ ai/fix-101 ─┘ approved → squash-merged
       ├─ ai/fix-102 ─┐
       └─ ai/fix-103 ─┤ pending
ai/preview = main + fix-102 + fix-103   (rebuilt, never committed to directly)
```

```yaml
# grafter.yaml
production_branch: main
preview_branch: ai/preview
fix_branch_prefix: ai/
```

| Event | Action |
|---|---|
| Approve | Squash-merge that branch only; rebase other pending branches; re-test; rebuild preview |
| Reject | Delete branch; rebuild preview |
| Rework | Reviewer comment → agent → same branch |
| `main` moved | Mark pending stale; rebase; re-test |
| Rebase changed the diff | Approval void; ask again |
| Pending fixes conflict | Later one `blocked` until the first is decided |
| Depends on another fix | `depends_on`; cannot be approved first |
| Spans repos | Change group, merged in order: backend (backward-compatible) → web → mobile |

## Who merges

A GitHub ruleset on `main` accepts changes only through a PR with a human approving review. The GitHub App can push only to `ai/*`. After approval the App squash-merges through the PR. Opt-in Tier 0 auto-merge requires an explicit ruleset bypass for the App.

## Lifecycle

```
proposed → building → testing → pending_review → approved → merged
pending_review → rejected
               → rework → testing
               → stale → rebased → testing
```

## State

Git is the source of truth for code; the database for status. Webhooks reconcile direct pushes and merges.

```sql
-- SQLite (D17); portable SQL types only. JSON columns hold JSON text.
changes(id, repo, branch, base_sha, head_sha, status, risk_tier, category,
        charter_ref, summary, risks json, test_results json,
        group_id, decided_by, decided_at, merged_sha)
change_deps(change_id, depends_on)
change_events(change_id, from_status, to_status, actor, note, at)
review_feedback(change_id, author, comment, at)
```
