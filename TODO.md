# Grafter — TODO

Status board and single source of project status. One group of related items = one branch + one PR; one commit per item.
Claude ticks a box only after its **Done when** check passed, in the same PR as the work.

Legend: `[ ]` todo · `[x]` done · 🧑 needs the user (Claude cannot do it alone) · ⚖️ decision needed

## Workflow for every item

1. `git switch -c <type>/<ids>-<slug>` from fresh `main` (e.g. `fix/0B1-0B2-paths`).
2. Write tests first, then code.
3. `ruff check . && ruff format --check . && pytest -q` green locally (`pre-commit run -a`).
4. Run the item's **Done when** check; paste the output into the PR body.
5. One commit per item, message `[<id>] <summary>`. PR title `[<ids>] <summary>`; tick the boxes in this file in the same PR.
6. User reviews and merges. Next item.

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
- [ ] **0D2** 🧑 Create GitHub App: contents RW, pull requests RW, issues RW, checks R, metadata R. Install on pilot + sandbox repos only. Private key on the VM (mode 0600), never in git.
- [ ] **0D3** 🧑 Ruleset on each pilot repo `main`: PR + 1 human approval + `test` check, no force push, App not in bypass list. App may push only to `ai/*`.
- [ ] **0D4** LiteLLM gateway on the VM, split:
  - [x] **0D4a** `deploy/litellm/` compose (LiteLLM v1.108.1 + private Postgres, D18), model routes for `models.*` (D15), `GatewayClient` (per-run key with `max_budget` + `budget_duration`, D16) and `grafter gateway check`. Done when: tests against a fake gateway pass and `docker compose config` validates.
  - [ ] **0D4b** 🧑 `docker compose up -d` on the VM with a real `.env`. Done when: `grafter gateway check` prints ok.
- [ ] **0D5** Fictional end-to-end test repo `grafter-sandbox`, split:
  - [x] **0D5a** Template in `examples/sandbox-repo/` (Python AcmeHarvest API, ~36% coverage, `test` CI job, charter protecting `invoices.py`), `deploy/grafter.sandbox.yaml` (dry run, 1 PR/night) and `scripts/create-sandbox-repo.sh`. Done when: Grafter CI keeps the template at 30–50% coverage and its charter validates against the config.
  - [ ] **0D5b** 🧑 Run `scripts/create-sandbox-repo.sh <owner>`, install the App (0D2), add the `main` ruleset. Done when: repo exists, App installed, ruleset on. All phase-1 testing happens here before the pilot.

**Gate 0 → 1:** charter validated · coverage known · thresholds recorded · VM + App + rulesets live.

---

## Phase 1 — Script pilot (tests only)

### 1A Core plumbing
- [ ] **1A1** Structured logging (JSON lines) + run id; logs in `~/.grafter/runs/<run-id>/`. Done when: `grafter run --dry-run` writes a log.
- [ ] **1A2** Run lock so two cron runs never overlap; kill switch checked at start and between tasks. Done when: tests.
- [x] **1A3** SQLite state store (D17): `runs`, `changes`, `change_deps`, `change_events` tables + numbered migrations; WAL mode + busy timeout; path from config (default `~/.grafter/grafter.db`); `grafter db backup`. Done when: tests on a temp-file DB, incl. migrate-from-empty and a concurrent reader during a write.
- [ ] **1A4** Secrets from env/file (App key, gateway key); refuse to start if a secret file sits inside a repo dir. Done when: test.

### 1B Git + GitHub
- [ ] **1B1** Git wrapper: clone/fetch into work dir, cut `ai/<id>` from fresh `main`, commit as Grafter, push. Hard check: never pushes a non-`ai/*` ref. Done when: integration test against a local bare repo.
- [ ] **1B2** GitHub App auth (JWT → installation token, auto refresh). Done when: `grafter doctor` lists repos it can see.
- [ ] **1B3** PR creation: title, body (charter citation, change type, tier, tests run + results, coverage delta, risks, tokens), labels (`grafter`, `tier-N`, `type-X`). Done when: PR opened on the sandbox repo.
- [ ] **1B4** Idempotency: skip a task if an open PR with the same fingerprint exists. Done when: second run opens nothing new.
- [ ] **1B5** `grafter doctor`: checks Docker, gateway, App permissions, rulesets present, kill switch state. Done when: all green on the VM.

### 1C Sandbox + engine
- [ ] **1C1** Sandbox image for the sandbox repo, split:
  - [x] **1C1a** `sandboxes/python/Dockerfile` (`python:3.12.15-slim-bookworm`, non-root, no secrets) + `scripts/build-sandbox.sh`. Done when: files reviewed, script syntax-checked.
  - [ ] **1C1b** 🧑 Build it and run the sandbox repo's tests inside it (`--network none`, or `grafter-gw` only). Done when: container runs the repo's tests. Needs a running Docker engine.
- [ ] **1C2** Spike: OpenHands Software Agent SDK + DockerWorkspace hello-world (MIT packages only; verify licence; update `THIRD_PARTY.md`). Done when: agent edits a file inside the container.
- [ ] **1C3** `OpenHandsEngine.run`: two steps (D15): `models.planner` writes a plan from task + charter + `AGENTS.md` + rules, then `models.coder` executes it; returns `RunResult` (changed files, summary, risks, tokens); token budget enforced. Done when: test with fake LLM + one real run on the sandbox repo.
- [ ] **1C4** Fake engine for tests (deterministic diff). Done when: full pipeline test runs offline in CI.

### 1D Work finding + pipeline
- [ ] **1D1** Task finder for `test` type: coverage in sandbox → lowest-covered files outside protected paths, each citing a charter domain. Done when: prints ranked tasks for the sandbox repo.
- [ ] **1D2** Verification gates after the engine: full suite passes in a fresh container; overall coverage did not drop and the target file improved; `guard.check` passes. Failures are recorded as rejected with a reason, no PR. Done when: tests.
- [ ] **1D3** `grafter run`: lock → kill switch → find → engine → gates → guard → branch/commit/push/PR → record; stop at `max_prs_per_night` or budget. `--dry-run` skips push/PR. Done when: dry-run, then a real run on the sandbox repo opens ≤ N PRs.
- [ ] **1D4** Nightly run report (PRs opened, rejections + reasons, tokens, cost) as a markdown file and an issue comment. Done when: report exists after a run.
- [ ] **1D5** Nightly schedule, split:
  - [x] **1D5a** `deploy/systemd/grafter.{service,timer}` (02:00, hardened, secrets via 0600 env file) + `deploy/cron/grafter.cron`. Done when: files reviewed.
  - [ ] **1D5b** 🧑 Enable the timer on the VM. Done when: one unattended night on the sandbox repo.

### 1E Scanners
- [ ] **1E1** Renovate config template, split:
  - [x] **1E1a** `deploy/renovate/renovate.json` (`ai/deps-*` branches, patch bumps grouped, no automerge). Done when: valid JSON, reviewed.
  - [ ] **1E1b** 🧑 Enable Renovate on the sandbox repo. Done when: a Renovate PR on the sandbox repo.
- [ ] **1E2** Semgrep + Trivy + OSV-Scanner in the sandbox; findings stored as candidate tasks (not acted on in phase 1). Done when: `grafter findings` lists them.

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
- [ ] **2.11** Features as issues: feature ideas filed as issues, never code.
- [ ] **2.12** Prompt-injection defences: mark untrusted content, no secret access from tools, diff check for exfiltration patterns.
- [ ] **2.13** Opt-in Tier 0 auto-merge (explicit ruleset bypass), off by default.
- [ ] **2.14** DB backups + stale branch cleanup.

**Gate 2 → 3:** 1–2 months of real results meeting thresholds.

---

## Phase 3 — Public release
- [ ] **3.1** Scrub repo and history for pilot names and data. 🧑 sign-off.
- [ ] **3.2** Docs site, quickstart with the fictional example, case study with sanitised numbers.
- [ ] **3.3** SECURITY.md review, threat model doc, release workflow (PyPI), CHANGELOG.
- [ ] **3.4** 🧑 Make the repo public.

## Phase 4 — Dashboard + next solution
- [ ] **4.1** FastAPI backend over the change DB.
- [ ] **4.2** Next.js dashboard.
- [ ] **4.3** Onboard a second solution.
- [ ] **4.4** More engine adapters (OpenCode, Claude Code, Codex CLI).
- [ ] **4.5** ⚖️ Enterprise evaluation (D2): includes whether PostgreSQL is needed (D17).
