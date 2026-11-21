# Roadmap

| Phase | When | Scope | Gate to next |
|---|---|---|---|
| 0 Foundations | Weeks 1–2 | Pilot charter, test coverage baseline, name and licence checks | Charter written, coverage known |
| 1 Script pilot | Weeks 3–6 | Python script + cron + GitHub PRs; Renovate + scanners; agent writes tests only | Tests in place on each repo |
| 2 Low-risk fixes | Months 2–3 | Bug fixes, security patches, `ai/preview`, change DB, charter judge, learning loop | 1–2 months of real results |
| 3 Normal-risk fixes | Months 4–5 | Business-logic bug fixes, breaking dependency upgrades, multi-file refactors; risk tiers and size caps; evidence in every PR (D21) | 1–2 months of real results at the phase-3 thresholds |
| 4 Solution self-evolution | Months 6+ | Grafter scans the whole solution and evolves it: UI/UX, backend performance, current best practice, and (when the charter allows it) new features as code; measured evidence, churn limits, charter judge (D21, D22) | Evolution PRs approved at the threshold; zero invariant breaks |
| 5 Public release | After pilot | Public repo, docs, SECURITY.md, credits, case study with real numbers | Repo stable, issues answered |
| 6 Dashboard + next solution | Later | Next.js dashboard, second solution, evaluate enterprise options (incl. PostgreSQL, D17) | — |

Durations are estimates. Gate thresholds: [D14](decisions.md) (coverage baseline + 20 points, at least 60%; ≥ 70% of the last 20 PRs approved; throttle after 3 rejections in a row).

## Before going public

Examples, sample charters and the case study use a fictional solution or sanitised data. No pilot-solution code, client names or client data in this repo.
