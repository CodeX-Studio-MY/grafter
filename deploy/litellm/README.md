# LiteLLM gateway

Routes every model call Grafter makes (D15) and enforces the nightly spend cap (D16).
Grafter keeps the master key. Each run gets its own virtual key, limited to the models
it needs and capped in USD. Sandboxes only ever see that run key.

The Postgres container is LiteLLM's own key and spend store (D18). It has no published
port and is not Grafter state. Grafter's records live in its SQLite file (D17). Losing
the gateway database loses spend history and open run keys, nothing else.

## First start (on the Grafter VM)

```bash
cd deploy/litellm
cp .env.example .env && chmod 600 .env
# fill in .env: ANTHROPIC_API_KEY, LITELLM_MASTER_KEY (starts with sk-), LITELLM_SALT_KEY, POSTGRES_PASSWORD
docker compose up -d
docker compose ps            # both services healthy
```

## Check it works (TODO 0D4b)

```bash
export LITELLM_MASTER_KEY=...   # same value as in .env
grafter gateway check --config ~/.grafter/grafter.yaml
```

The check calls the liveness probe and creates a key valid for 1 hour, capped at $0.05, for
`models.coder`. It sends one tiny prompt, then deletes the key.

## Operate

| Task | Command |
|---|---|
| Logs | `docker compose logs -f litellm` |
| Stop | `docker compose down` (data kept in the `litellm-db` volume) |
| Upgrade | change the pinned image tag, `docker compose pull && docker compose up -d` |
| Rotate master key | edit `.env`, `docker compose up -d`, update Grafter's env |
| Rotate Anthropic key | edit `.env`, `docker compose up -d` |

Never change `LITELLM_SALT_KEY` after first start; it encrypts stored credentials.
Port 4000 is bound to `127.0.0.1` only. Sandboxes reach the gateway over the `grafter-gw`
Docker network (1C1).
