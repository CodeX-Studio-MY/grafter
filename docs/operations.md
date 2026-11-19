# Operations (draft)

How to install and run Grafter on its host. **Draft (TODO 1F3a):** it gets finished and tested on a fresh VM in 1F3b. Commands marked *(phase 1, in progress)* don't exist yet.

## 1. Host (TODO 0D1)

- Ubuntu LTS VM, owned by the solution owner. Not a GitHub Actions runner (D11).
- Rootless Docker, Python 3.12, git.
- A non-root user `grafter`. Everything Grafter writes lives in `/home/grafter/.grafter/` (`home` in `grafter.yaml`).
- With rootless Docker, put `DOCKER_HOST=unix:///run/user/<uid>/docker.sock` in the env file (step 3).

## 2. Install Grafter

```bash
sudo mkdir -p /opt/grafter && sudo chown grafter: /opt/grafter
sudo -iu grafter
git clone https://github.com/<owner>/grafter /opt/grafter/src
python3.12 -m venv /opt/grafter/.venv
/opt/grafter/.venv/bin/pip install /opt/grafter/src
```

## 3. Config and secrets

| File | Mode | Holds |
|---|---|---|
| `~/.grafter/grafter.yaml` | 0644 | Config, ids only. Start from `deploy/grafter.sandbox.yaml`. Grafter rejects secret-looking keys. |
| `~/.grafter/env` | **0600** | `LITELLM_MASTER_KEY`, `GRAFTER_GITHUB_APP_PRIVATE_KEY_FILE=/home/grafter/.grafter/app.pem`, `DOCKER_HOST` |
| `~/.grafter/app.pem` | **0600** | GitHub App private key (0D2). Never inside a git repo; Grafter refuses one that is. |

```bash
grafter validate /path/to/solution/grafter.charter.yaml --config ~/.grafter/grafter.yaml
```

## 4. Gateway (TODO 0D4b)

Follow [deploy/litellm/README.md](../deploy/litellm/README.md), then:

```bash
set -a; . ~/.grafter/env; set +a
grafter gateway check --config ~/.grafter/grafter.yaml
```

## 5. Sandbox image (TODO 1C1b)

```bash
/opt/grafter/src/scripts/build-sandbox.sh python
```

The image has no secrets and runs as a non-root user. At run time it only gets the `grafter-gw` network to the gateway.

## 6. Database

SQLite at `~/.grafter/grafter.db` (D17).

```bash
grafter db migrate --config ~/.grafter/grafter.yaml          # (phase 1, in progress)
grafter db backup ~/.grafter/backups/grafter-$(date +%F).db --config ~/.grafter/grafter.yaml
```

Back the file up nightly, and copy it off the host now and then.

## 7. Schedule (TODO 1D5b)

systemd (preferred):

```bash
sudo cp /opt/grafter/src/deploy/systemd/grafter.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now grafter.timer
systemctl list-timers grafter.timer
journalctl -u grafter.service -n 200
```

cron alternative: `crontab -u grafter /opt/grafter/src/deploy/cron/grafter.cron`.

Start with `dry_run: true` and `max_prs_per_night: 1` for a few nights (1F1). Read the logs in `~/.grafter/runs/<run-id>/` before you turn dry run off.

## 8. Daily checks

```bash
grafter doctor --config ~/.grafter/grafter.yaml               # (phase 1, in progress)
```

Then review the night's PRs on GitHub. Approve, reject, or comment to ask for rework.

## 9. Stop Grafter (kill switch)

Either of these stops the next run, and a running one between tasks:

```bash
touch ~/.grafter/STOP            # remove the file to resume
# or set GRAFTER_STOP=1 in ~/.grafter/env
```

For a full stop, also run `sudo systemctl disable --now grafter.timer`.

## 10. Roll back a merged change

- Grafter never pushes to `main`. Every change arrived through a squash-merged PR, so roll back by opening a revert PR for that merge commit on GitHub.
- **Schema migrations cannot be undone with `git revert`.** That's why they sit in `review_paths` (Tier 2). Roll forward with a new migration.
- Close any open `ai/*` PRs that depend on the reverted change.

## 11. Upgrade

```bash
cd /opt/grafter/src && git pull && /opt/grafter/.venv/bin/pip install .
grafter db migrate --config ~/.grafter/grafter.yaml
/opt/grafter/src/scripts/build-sandbox.sh python
```

To upgrade the gateway, see its README.
