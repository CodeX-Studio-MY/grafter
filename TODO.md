# Grafter — TODO

Status board and single source of project status. One group of related items = one branch + one PR; one commit per item.
Claude ticks a box only after its **Done when** check passed, in a separate `status/<date>` PR after the work merged.

Legend: `[ ]` todo · `[x]` done · 🧑 needs the user (Claude cannot do it alone) · ⚖️ decision needed

## Workflow for every item

1. `git switch -c <type>/<ids>-<slug>` from fresh `main` (e.g. `fix/0B1-0B2-paths`).
2. Write tests first, then code.
3. `ruff check . && ruff format --check . && mypy && pytest -q` green locally (`pre-commit run -a`).
4. Run the item's **Done when** check; paste the output into the PR body.
5. One commit per item, message `[<id>] <summary>`. PR title `[<ids>] <summary>`. Feature PRs never edit this file; the PR body lists the items it completes.
6. User reviews and merges.
7. A `status/<date>` PR ticks the verified boxes (with the source PR, e.g. `(#17)`) and splits items. Next item.

Never tick an unverified item. If an item is split, add sub-items instead of ticking partially.

---

## Phase 0 — Foundations

### 0A Repo hygiene
- [x] **0A1** Add `TODO.md`, link from `CLAUDE.md` and `README.md`. Done when: PR merged.
- [x] **0A2** Pre-commit hooks (ruff, ruff-format, trailing whitespace) + `ruff format --check` in CI. Done when: CI runs format check.
- [x] **0A3** mypy (strict on `src/`) in dev deps + CI. Done when: `mypy src` clean.
- [x] **0A4** pytest-cov in CI, fail under current coverage. Done when: coverage % shown in CI log.
- [x] **0A5** PR template (summary, TODO id, verification output) + `CODEOWNERS`. Done when: a new PR shows the template.
- [x] **0A6** 🧑 Ruleset on Grafter's own `main`: PR + `test` check required, no force push.

### 0B Harden the scaffold (from review of the scaffold)
- [x] **0B1** Normalise paths in `charter._matches` (`./`, `\`, `..`, case), reject paths escaping the repo, add glob support. Done when: bypass tests pass.
- [x] **0B2** Always-protected set: `grafter.charter.yaml`, `grafter.yaml`, `AGENTS.md`, `.github/`, `.grafter/`. Done when: a test proves each one is blocked.
- [x] **0B3** Tier 0 only for `dependency` patch-level bumps (add version-delta info to `Task`/`RunResult`); agent-written `security` changes are Tier 1 minimum. Done when: guard tests updated and pass.
- [x] **0B4** Guard: max diff size (files + lines) from config → reject. Done when: test.
- [x] **0B5** Guard: a `test` change may touch only test paths (per-repo `test_paths` globs) and may not delete test files or test functions. Done when: tests.
- [x] **0B6** Guard: secret scan of the diff (regex set or `detect-secrets`) → reject. Done when: test with a fake key.
- [x] **0B7** Per-repo config: `name`, `default_branch`, `language`, `test_command`, `coverage_command`, `test_paths`, `sandbox_image`. Charter paths become repo-scoped. Update example + add `grafter validate --config`. Done when: example validates.
- [x] **0B8** Config: `kill_switch` (file/env), `dry_run`, `nightly_focus` rotation, models per role (see D15), GitHub App ids (ids only; secrets via env). Done when: tests.

### 0C Pilot preparation (outside this repo)
- [ ] **0C1** 🧑 Write the pilot's `grafter.charter.yaml` + `AGENTS.md` in the PILOT repos (Claude can draft it in a pilot-repo session). Done when: `grafter validate` passes on it.
- [ ] **0C2** 🧑 Measure test coverage per pilot repo (backend, web, mobile). Keep numbers in a private note, NOT in this repo.
- [x] **0C3** ⚖️ Set gate thresholds in `docs/decisions.md`: coverage target per repo, min approval rate, max reject streak before throttling. → D14.
- [x] **0C4** ⚖️ Choose LLM provider/model + nightly budget; record as a decision. → D15 (Opus 5.5 plans/reviews/judges, Sonnet 5.5 codes; `models:` config), D16 ($5/night).
- [x] **0C5** ⚖️ Choose phase-1 state store: SQLite (simple, one host) or PostgreSQL (as in docs); record as a decision. → D17 (SQLite; PostgreSQL parked for the enterprise evaluation).

### 0D Infrastructure
- [ ] **0D1** 🧑 Provision Linux VM (Ubuntu LTS): rootless Docker, Python 3.12, git, non-root `grafter` user; nightly backup of the SQLite file (D17).
- [ ] **0D2** GitHub App, split:
  - [x] **0D2a** Create the App (org-owned, contents RW, pull requests RW, issues RW, checks R, metadata R; no webhook, no workflows/administration) and install it on the sandbox repo only. Done when: permission audit passes and `grafter doctor` sees only the installed repos. (verified 2026-11-21: App grafter-codex-studio, audit PASS, #27)
  - [ ] **0D2b** 🧑 Private key on the VM (`~/.grafter/app.pem`, mode 0600, never in git); delete the workstation copy or rotate the key (needs 0D1). Install on the pilot repos at 1F1. Done when: `grafter doctor` on the VM is green.
- [ ] **0D3** 🧑 Ruleset on each pilot repo `main`: PR + 1 human approval + `test` check, no force push, App not in bypass list. App may push only to `ai/*`.
- [ ] **0D4** LiteLLM gateway on the VM, split:
  - [x] **0D4a** `deploy/litellm/` compose (LiteLLM v1.108.1 + private Postgres, D18), model routes for `models.*` (D15), `GatewayClient` (per-run key with `max_budget` + `budget_duration`, D16) and `grafter gateway check`. Done when: tests against a fake gateway pass and `docker compose config` validates.
  - [ ] **0D4b** 🧑 `docker compose up -d` on the VM with a real `.env`. Done when: `grafter gateway check` prints ok.
- [x] **0D5** Fictional end-to-end test repo `grafter-sandbox`, split:
  - [x] **0D5a** Template in `examples/sandbox-repo/` (Python AcmeHarvest API, ~36% coverage, `test` CI job, charter protecting `invoices.py`), `deploy/grafter.sandbox.yaml` (dry run, 1 PR/night) and `scripts/create-sandbox-repo.sh`. Done when: Grafter CI keeps the template at 30–50% coverage and its charter validates against the config.
  - [x] **0D5b** Run `scripts/create-sandbox-repo.sh <owner>`, install the App (0D2), add the `main` ruleset. Done when: repo exists, App installed, ruleset on. All phase-1 testing happens here before the pilot. (verified 2026-11-21: public per D19, App installed, ruleset 27747355; App push to main rejected GH013, #27)

**Gate 0 → 1:** charter validated · coverage known · thresholds recorded · VM + App + rulesets live.

---

## Phase 1 — Script pilot (tests only)

### 1A Core plumbing
- [x] **1A1** Structured logging (JSON lines) + run id; logs in `~/.grafter/runs/<run-id>/`. Done when: `grafter run --dry-run` writes a log. (#17)
- [x] **1A2** Run lock so two cron runs never overlap; kill switch checked at start and between tasks. Done when: tests.
- [x] **1A3** SQLite state store (D17): `runs`, `changes`, `change_deps`, `change_events` tables + numbered migrations; WAL mode + busy timeout; path from config (default `~/.grafter/grafter.db`); `grafter db backup`. Done when: tests on a temp-file DB, incl. migrate-from-empty and a concurrent reader during a write.
- [x] **1A4** Secrets from env/file (App key, gateway key); refuse to start if a secret file sits inside a repo dir. Done when: test.

### 1B Git + GitHub
- [x] **1B1** Git wrapper: clone/fetch into work dir, cut `ai/<id>` from fresh `main`, commit as Grafter, push. Hard check: never pushes a non-`ai/*` ref. Done when: integration test against a local bare repo.
- [x] **1B2** GitHub App auth, split:
  - [x] **1B2a** `github.AppAuth`: RS256 JWT, installation token cached until 5 min before expiry. Done when: tests against a fake GitHub API verifying the JWT.
  - [x] **1B2b** With the real App (0D2). Done when: `grafter doctor` lists repos it can see. (verified 2026-11-21: doctor: App sees CodeX-Studio-MY/grafter-sandbox)
- [x] **1B3** PR creation, split:
  - [x] **1B3a** `GitHubClient.create_pr` (head must be `ai/*`), `ensure_labels`, `prbody.render_pr` (charter citation, type, tier, tests, coverage delta, risks, tokens, cost, fingerprint marker). Done when: tests against a fake GitHub API.
  - [x] **1B3b** Done when: a PR opened on the sandbox repo. (verified 2026-11-21, #29; sandbox PRs #1, #2)
- [x] **1B4** Idempotency, split:
  - [x] **1B4a** `find_open_pr(repo, fingerprint)` matches the exact marker on open `ai/*` PRs; fingerprint = sha256(repo|type|target). Done when: tests against a fake GitHub API.
  - [x] **1B4b** Done when: a second real run never opens a duplicate PR for an open change. (verified 2026-11-21, #29; sandbox PRs #1, #2)
- [ ] **1B5** `grafter doctor`, split:
  - [x] **1B5a** Offline checks: config, home, kill switch, lock, DB, git, Docker, engine, gateway, App secrets. Done when: tests. (#17)
  - [ ] **1B5b** 🧑 Add App permissions + rulesets checks; run on the VM. Done when: all green on the VM.

### 1C Sandbox + engine
- [x] **1C1** Sandbox image for the sandbox repo, split:
  - [x] **1C1a** `sandboxes/python/Dockerfile` (`python:3.12.15-slim-bookworm`, non-root, no secrets) + `scripts/build-sandbox.sh`. Done when: files reviewed, script syntax-checked.
  - [x] **1C1b** Build it and run the sandbox repo's tests inside it (`--network none`, or `grafter-gw` only). Done when: container runs the repo's tests. Verified locally (Docker 29.9.3): 4 passed as uid 10001, 36% coverage, network blocked. (#17)
- [x] **1C2** Spike: OpenHands Software Agent SDK + DockerWorkspace hello-world (MIT packages only; verify licence; update `THIRD_PARTY.md`). Done when: agent edits a file inside the container. (#19)
- [x] **1C3** `OpenHandsEngine.run`: two steps (D15): `models.planner` writes a plan from task + charter + `AGENTS.md` + rules, then `models.coder` executes it; returns `RunResult` (changed files, summary, risks, tokens, cost); token budget enforced. Split:
  - [x] **1C3a** Engine with per-task gateway key, patch brought back and applied with `git apply`; nothing agent-written runs on the host. Done when: tests with fake LLM + fake sandbox, and one real engine run on the sandbox repo whose diff the guard allows. (#21)
  - [x] **1C3b** `grafter run` with `engine: openhands` (needs 1D2b so gates run in a container). Done when: a real run on the sandbox repo opens a PR. (verified 2026-11-21, #29; sandbox PRs #1, #2)
- [x] **1C4** Fake engine for tests (deterministic diff). Done when: full pipeline test runs offline in CI.

### 1D Work finding + pipeline
- [x] **1D1** Task finder for `test` type: coverage in sandbox → lowest-covered files outside protected paths, each citing a charter domain. Done when: prints ranked tasks for the sandbox repo.
- [x] **1D2** Verification gates after the engine, split:
  - [x] **1D2a** `gates.run_gates`: suite passes, overall coverage did not drop, target file improved; failures carry a reason. Runs through a `CommandRunner` (`LocalRunner` for tests). Done when: tests on real pytest runs against the sandbox template.
  - [x] **1D2b** Docker `CommandRunner` so gates run in a fresh sandbox container (needs 1C1b). Never run repo commands on the host in real runs. Done when: gates pass/fail the same way inside the container. (#23)
  - [x] **1D2c** Agent container reaches only the gateway (internal network + relay, spike 1C2). Real `grafter run` with `engine: openhands` stays refused until this is done. Done when: from inside the agent container the internet is blocked and the gateway is reachable, and an engine run still passes. (#25)
- [x] **1D3** `grafter run`: lock → kill switch → find → engine → gates → guard → branch/commit/push/PR → record; stop at `max_prs_per_night` or budget. `--dry-run` skips push/PR. Split:
  - [x] **1D3a** Pipeline + CLI offline: local bare origin, fake engine, fake GitHub; ≤ N PRs, none on re-run, protected/skip edits rejected, kill switch + lock exit codes. Real runs refused with `runner: local`. Done when: offline end-to-end tests + `--dry-run`. (#17)
  - [x] **1D3b** Needs 1D2b, the App (0D2) and the sandbox repo. Done when: a real run on the sandbox repo opens ≤ N PRs. (verified 2026-11-21, #29; sandbox PRs #1, #2)
- [x] **1D4** Nightly run report (PRs opened, rejections + reasons, tokens, cost), split:
  - [x] **1D4a** Markdown `report.md` in the run dir. Done when: report exists after a dry run. (#17)
  - [x] **1D4b** Post it as an issue comment; real cost from the gateway. Done when: comment exists after a real run. (#31; sandbox issue #4)
- [ ] **1D5** Nightly schedule, split:
  - [x] **1D5a** `deploy/systemd/grafter.{service,timer}` (02:00, hardened, secrets via 0600 env file) + `deploy/cron/grafter.cron`. Done when: files reviewed.
  - [ ] **1D5b** 🧑 Enable the timer on the VM. Done when: one unattended night on the sandbox repo.

### 1E Scanners
- [ ] **1E1** Renovate config template, split:
  - [x] **1E1a** `deploy/renovate/renovate.json` (`ai/deps-*` branches, patch bumps grouped, no automerge). Done when: valid JSON, reviewed.
  - [ ] **1E1b** 🧑 Enable Renovate on the sandbox repo. Done when: a Renovate PR on the sandbox repo.
- [x] **1E2** Trivy + OSV-Scanner (offline, D20) in the sandbox; findings stored as candidate tasks (not acted on in phase 1). Done when: `grafter findings` lists them. (#33)
- [ ] **1E3** ⚖️ Code-pattern scanning (Semgrep deferred in D20): Grafter's own MIT rules, or Semgrep under an accepted licence. Done when: decision recorded in `docs/decisions.md`.

### 1F Go live on the pilot
- [ ] **1F1** 🧑 Install on pilot repos with `max_prs_per_night: 1`; dry-run 3 nights; review logs.
- [ ] **1F2** 🧑 Real runs; review PRs each morning; record approve/reject + reason.
- [ ] **1F3** `docs/operations.md`, split:
  - [x] **1F3a** Draft: host, install, config + secrets, gateway, sandbox image, database, schedule, kill switch, rollback, upgrade. Done when: draft merged.
  - [ ] **1F3b** 🧑 Follow it on a fresh VM and fix every gap. Done when: the user can follow it on a fresh VM.

**Gate 1 → 2:** coverage target met per repo (0C3) · approval rate ≥ threshold · zero guard escapes.

---

## Phase 2 — Low-risk fixes
- [ ] **2.1** Task finders: failing tests, scanner findings, CVE patches, lint errors.
- [ ] **2.2** Change lifecycle state machine (`proposed → … → merged`) with `change_events` audit log.
- [ ] **2.3** Webhook/poll reconciler: PR approved/merged/closed, `main` moved, review comments.
- [ ] **2.4** Approve flow: App squash-merges the approved PR; rebase other pending; re-test; approval void if the diff changed.
- [ ] **2.5** Reject flow (delete branch, record reason) and rework flow (review comment → agent → same branch).
- [ ] **2.6** `ai/preview` rebuild = `main` + pending branches; conflicts mark the later change `blocked`.
- [ ] **2.7** `depends_on` + cross-repo change groups (merge order backend → web → mobile).
- [ ] **2.8** Separate reviewer agent + charter judge on `models.reviewer` / `models.judge` (Opus 5.5, never the coder's prompt); judge rejections logged.
- [ ] **2.9** Drift signals: new modules, dependencies, domain vocabulary flagged in the PR.
- [ ] **2.10** Learning loop: approval rate per category; rejection comments into agent memory; throttle low-acceptance categories.
- [ ] **2.11** Features as issues: feature ideas filed as issues (`feature_changes: propose_only`, the default; code only from 4.6).
- [ ] **2.12** Prompt-injection defences: mark untrusted content, no secret access from tools, diff check for exfiltration patterns.
- [ ] **2.13** Opt-in Tier 0 auto-merge (explicit ruleset bypass), off by default.
- [ ] **2.14** DB backups + stale branch cleanup.

**Gate 2 → 3:** 1–2 months of real results meeting thresholds.

---

## Phase 3 — Normal-risk fixes (D21)
- [ ] **3.1** ⚖️ Risk tiers: what makes a change low vs normal risk (diff size, files touched, `review_paths`, business logic). Record in `docs/decisions.md`.
- [ ] **3.2** Task finders: business-logic bugs from issues labelled `grafter`, breaking dependency upgrades Renovate cannot do alone, multi-file refactors.
- [ ] **3.3** Size caps per risk tier (configurable); the PR shows its tier and why.
- [ ] **3.4** Evidence in every PR: tests added or changed, coverage delta, before/after behaviour.
- [ ] **3.5** ⚖️ Gate thresholds for phase 3 → 4 (approval rate, zero invariant breaks). Record in `docs/decisions.md`.

**Gate 3 → 4:** 1–2 months of real results meeting the 3.5 thresholds.

---

## Phase 4 — Solution self-evolution (D21, D22)
- [ ] **4.1** Charter: `feature_changes: allow_code` (default stays `propose_only`) and a `ui_ux` change type; `grafter validate` accepts both.
- [ ] **4.2** Improvement scout: scans the whole solution on a schedule for UI/UX, performance and best-practice opportunities; ranks them by charter fit; stores them as candidate tasks.
- [ ] **4.3** ⚖️ Best-practice sources: which sources count (framework release notes, official guides, pinned and cited in the PR); no unvetted web content in prompts (prompt injection).
- [ ] **4.4** Performance evidence: benchmark before/after in the sandbox; no measured gain → no PR.
- [ ] **4.5** UI/UX evidence: before/after screenshots from `ai/preview` and an accessibility check in the PR.
- [ ] **4.6** Feature code PRs (only with `allow_code`): from an accepted issue or a charter-cited idea, behind a feature flag where the solution has them; judge must pass.
- [ ] **4.7** Churn limits: evolution PRs per night/week, cooldown on recently changed files, throttle categories with rejections (2.10).

**Gate 4 → 5:** evolution PRs approved at the threshold; zero invariant breaks.

---

## Phase 5 — Public release
- [ ] **5.1** Scrub repo and history for pilot names and data. 🧑 sign-off.
- [ ] **5.2** Docs site, quickstart with the fictional example, case study with sanitised numbers.
- [ ] **5.3** SECURITY.md review, threat model doc, release workflow (PyPI), CHANGELOG.
- [ ] **5.4** 🧑 Make the repo public.

## Phase 6 — Dashboard + next solution
- [ ] **6.1** FastAPI backend over the change DB.
- [ ] **6.2** Next.js dashboard.
- [ ] **6.3** Onboard a second solution.
- [ ] **6.4** More engine adapters (OpenCode, Claude Code, Codex CLI).
- [ ] **6.5** ⚖️ Enterprise evaluation (D2): includes whether PostgreSQL is needed (D17).
