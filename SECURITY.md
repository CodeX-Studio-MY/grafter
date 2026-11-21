# Security policy

Grafter holds write access to repositories and runs AI-written code. Please report vulnerabilities privately through GitHub's "Report a vulnerability" (Security tab). Do not open public issues for security problems.

## Defaults we will not weaken

- The AI cannot push to the production branch.
- Every merge into production needs a human approving review (Tier 0 auto-merge is opt-in).
- Sandboxes hold no production secrets and no raw LLM keys.
- The Solution Charter and protected paths are read-only to the AI.
- New features are written as code only if the charter opts in (`feature_changes: allow_code`); only a human can edit the charter.
