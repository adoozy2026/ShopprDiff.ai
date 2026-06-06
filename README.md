# Personal Shopper Agent

Multi-agent personal shopper for a 24-hour hackathon. The user describes what
they want; an **Intake** agent gates research behind one clarifying turn; a
**Search Planner** builds a candidate set across retailers; **N Researcher
agents** run in parallel — each one is a visible tile on the dashboard,
surfacing price, condition, seller, shipping, returns, known issues, scam
risk, and alternatives. A **Synthesizer** ranks them; a **Forecaster** writes a
multi-week price-trend narrative.

```
[Next.js on Vercel]
        |  realtime WS: insforge.realtime.subscribe(`intent:<id>`)
        |  writes via insforge.database.from(...).insert(...) (anon key)
        v
[Insforge Postgres + Realtime + Auth]  <----writes via REST (service role)
        ^                                          |  poll for status='ready'
        | DB triggers call realtime.publish(       |  intents every ~1.5s
        |   `intent:<id>`, '<event>', payload)     |
        |                                          |
[FastAPI orchestrator — your laptop, outbound only]
                                                   |
                                                   | google-genai SDK + tool-use loops
                                                   | google_search / url_context (built-in)
                                                   | playwright fallback (in-process)
```

Insforge does **not** expose a direct Postgres connection, so the orchestrator
talks to it via REST. Realtime updates flow the other way: DB triggers call
`realtime.publish()` whenever the pipeline writes a row, and the browser
subscribes to `intent:<id>` to react.

## Layout

```
migrations/      SQL — applied via `npx @insforge/cli db migrations up --all`
orchestrator/    FastAPI service — runs on your laptop
web/             Next.js app — deploys to Vercel
```

## One-time setup

### 1. Create the Insforge project

1. Sign in at https://insforge.dev → **Create New Project** (~3 seconds).
2. Grab the **Project ID** from the dashboard URL
   (`https://insforge.dev/dashboard/project/<id>`).
3. From the repo root, link the CLI:
   ```bash
   npx @insforge/cli link --project-id <id>
   ```
4. Apply the schema:
   ```bash
   npx @insforge/cli db migrations up --all
   ```
   This applies `migrations/<timestamp>_init.sql`.
5. From the dashboard, copy the **Project URL**, **anon key**, and
   **service role key**.

### 2. Google AI Studio (Gemini)

API key from https://aistudio.google.com/app/apikey.

### 3. Environment variables

```bash
cp .env.example .env
# fill in Insforge + Google AI Studio values
```

The web app reads `NEXT_PUBLIC_INSFORGE_*` — set the same values in Vercel
project env before deploying.

## Run locally

Two processes — one terminal each.

```bash
# orchestrator (laptop)
cd orchestrator
uv sync
uv run uvicorn app.main:app --reload --port 8787

# web
cd web
pnpm install
pnpm dev
```

Open http://localhost:3000.

Orchestrator health: `curl http://localhost:8787/healthz`.

## Deploy the web app to Vercel

```bash
cd web
npx vercel --prod
```

Or connect the repo via the Vercel dashboard. The orchestrator stays on your
laptop — that's the "self-deploy locally via a script" story.

## Fixture mode

Set `FIXTURE_MODE=true` in the orchestrator's env to bypass live web tools
and serve seeded data. Live-demo fallback if any provider rate-limits or
blocks on stage.
