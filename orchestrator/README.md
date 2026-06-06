# orchestrator

Local FastAPI service that runs the agent pipeline. Talks to Insforge outbound-only over REST. Insforge does not expose a direct Postgres connection, so there is no `LISTEN/NOTIFY`; instead the service polls for `status='ready'` intents (see `app/db/poll.py`) and claims each by setting `picked_up_at`.

## Run locally

```bash
uv sync
cp ../.env.example ../.env   # fill in keys
uv run uvicorn app.main:app --reload --port 8787
```

Health check: `curl localhost:8787/healthz`
