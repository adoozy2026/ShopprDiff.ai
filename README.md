# Personal Shopper Agent

> **Elevator Pitch:** You paste a single sentence — "used iPhone 15 Pro, unlocked, under $700" — and a coordinated team of AI agents fans out across the internet, vets every listing for price, seller reputation, return policy, and scam risk, then returns a ranked recommendation that tells you *why* the top pick wins and *what you'd trade off* by choosing an alternative. It's the research your most obsessive friend would do, compressed into under two minutes.

---

## Overview

Personal Shopper Agent is a multi-agent retail research system built during a 24-hour hackathon. A user describes what they want to buy in plain language; an autonomous pipeline of specialized LLM agents discovers candidate listings across retailers, deep-dives each one in parallel, detects scams, and synthesizes a holistic shopping recommendation — complete with tradeoff analysis, honest warnings, and price-trend forecasts.

The system is split into two cooperating services:

| Layer | Tech | Runs on |
|---|---|---|
| **Frontend** | Next.js 16 + Tailwind CSS + `@insforge/sdk` realtime | Vercel (or `localhost:3000`) |
| **Orchestrator** | FastAPI + Google Gemini (`google-genai`) + Playwright | Your laptop (outbound-only) |
| **Backend** | InsForge (Postgres, Realtime, Auth) | Managed cloud (`insforge.dev`) |

```
User → [Next.js UI] → InsForge DB (insert intent)
                            ↕ realtime WebSocket
       [FastAPI Orchestrator] polls for new intents
            ├── Intake Agent        (clarifies the query)
            ├── Search Planner      (discovers candidate URLs via Google Search)
            ├── N × Researcher      (parallel: extracts price, condition, seller, scam signals)
            │     └── Configurator  (Playwright browser agent for dynamic pricing pages)
            ├── Synthesizer         (ranks, compares tradeoffs, surfaces warnings)
            └── Forecaster          (multi-week price-trend narrative)
```

Every stage writes progress back to InsForge. Database triggers publish events over WebSocket, and the dashboard animates each researcher tile in real time — the user watches the agents work.

---

## Usage Scenario

**You're looking for a used iPhone 15 Pro, 256 GB, unlocked, under $700, from a US-based seller.**

1. **Submit your query.** Type your request into the search bar. Optionally click preference chips (condition, shipping, returns, match scope) to refine.
2. **Intake clarifies (if needed).** The Intake agent may ask one short follow-up — "Do you have a color preference?" — then locks in a structured shopping spec.
3. **Agents fan out.** The Search Planner fires Google Search queries (broad + retailer-scoped) and surfaces 4–8 candidate product URLs. Each candidate gets its own Researcher agent tile on the dashboard.
4. **Live research.** Each Researcher fetches the listing page, extracts price/condition/seller/shipping/returns, checks seller reputation, scans for known product issues, and runs a scam-risk heuristic — all visible step-by-step on screen. For dynamic-pricing retailers (Apple, Best Buy, Dell), a Playwright browser agent automatically configures the product to get the real price.
5. **Recommendation lands.** The Synthesizer ranks every candidate, writes a per-listing one-liner explaining *why* it's shown, maps out axis-by-axis tradeoffs (price vs. return policy vs. seller trust vs. shipping speed), and flags honest warnings ("Listing 2 has no returns — risky for a used phone"). A Forecaster adds a price-trend narrative so you know whether to buy now or wait.
6. **You decide.** The dashboard highlights a Top Pick with a rationale tied to *your* stated priorities, plus alternatives ("if you can wait two weeks, refurb prices drop 12%").

---

## Design Goals

1. **Advisor, not a sorted list.** The system produces opinionated, tradeoff-aware recommendations — not a generic price-sorted grid. Each recommendation references the user's actual deal-breakers and preferences.

2. **Transparent research.** Every agent's work is visible in real time. The user sees each researcher tile progress through steps (fetching → reputation check → known issues → scam evaluation), building trust that the system is doing thorough work.

3. **Scam and risk detection.** A rule-based scam scorer flags too-good-to-be-true pricing, no-return policies, overseas shippers when the user wants US-only, and negative seller reputation signals — all surfaced with explainable reasons.

4. **Dynamic-price awareness.** A Playwright-based browser agent handles retailers where the real price only appears after configuration (Apple "From $X,XXX", Best Buy variant selectors, Newegg "Add to cart for price"). Static scraping alone misses these.

5. **Minimal user effort.** One sentence in, full recommendation out. The Intake agent gates behind at most one clarifying question so the user isn't interrogated.

6. **Decoupled architecture.** The orchestrator runs on your laptop (outbound-only — no inbound ports, no tunnels). InsForge provides the database, auth, and realtime pub/sub as a managed service. The frontend deploys to Vercel. Each layer scales and deploys independently.

7. **Graceful degradation.** Fixture mode (`FIXTURE_MODE=true`) serves seeded mock data when API providers rate-limit or go down — the demo never breaks on stage. Skeleton mode works even before InsForge is wired up.

---

## Feature Expansion Roadmap

We're building toward a world where this agent doesn't just *research* — it *shops* for you. Here's what's coming:

### Near-Term

- **Price alerts & monitoring.** Persist price history per canonical product key and notify users when prices drop below their budget. The `price_history` and `forecasts` tables are already in the schema.
- **Multi-intent sessions.** Research several products in one session ("I need a phone AND a case AND a screen protector") with shared context across intents.
- **User accounts & saved searches.** InsForge Auth is wired but unused — enable login so users can revisit past research and track price changes over time.
- **Broader retailer coverage.** Expand the Configurator's domain list and add retailer-specific extractors for Amazon, Walmart, Target, and international marketplaces.

### Mid-Term

- **One-click purchase.** Deep-link directly to the retailer checkout with pre-filled configuration. For supported retailers, use affiliate APIs to complete the purchase without leaving the dashboard.
- **Collaborative shopping.** Share a research session with a friend or partner. Real-time cursor presence and voting on candidates ("I like this one").
- **Mobile app.** A lightweight React Native client that pushes notifications when research completes or prices change.
- **Review aggregation.** Pull in user reviews from multiple sources, summarize sentiment per product, and surface review-backed warnings alongside scam signals.

### Long-Term Vision

- **Autonomous purchasing agent.** With user-defined rules and budget guardrails, the agent buys the item when price and conditions are met — fully hands-off shopping.
- **Cross-category intelligence.** Learn from completed research sessions to improve recommendations over time. "Users who bought X at this price point were satisfied 94% of the time."
- **Enterprise / B2B procurement.** Adapt the pipeline for bulk purchasing, vendor comparison, and compliance-aware sourcing.
- **International expansion.** Multi-currency support, cross-border shipping estimation, and region-aware scam detection.

---

## Project Structure

```
README.md               ← you are here
migrations/             SQL migrations (applied via InsForge CLI)
scripts/                One-shot setup helpers (realtime channel bootstrap)
orchestrator/           FastAPI service — agent pipeline
  app/
    agents/             Intake, Planner, Researcher, Configurator, Synthesizer, Scam
    db/                 InsForge REST client + poller
    tools/              Web scraping, Google Search, URL context, Playwright fallback
web/                    Next.js frontend — real-time dashboard
  src/
    app/                Pages: home (search), intent/[id] (live dashboard)
    lib/                InsForge SDK client
```

## Quick Start

### Prerequisites

- Python ≥ 3.12 with [uv](https://docs.astral.sh/uv/)
- Node.js with [pnpm](https://pnpm.io/)
- An [InsForge](https://insforge.dev) project (free tier works)
- A [Google AI Studio](https://aistudio.google.com/app/apikey) API key (Gemini)

### 1. Clone & configure

```bash
git clone https://github.com/adoozy2026/insforge-hack.git
cd insforge-hack
cp .env.example .env
# Fill in INSFORGE_PROJECT_URL, INSFORGE_ANON_KEY,
# INSFORGE_SERVICE_ROLE_KEY, and GOOGLE_API_KEY
```

### 2. Set up the backend

```bash
npx @insforge/cli link --project-id <your-project-id>
npx @insforge/cli db migrations up --all
./scripts/bootstrap-realtime.sh
```

### 3. Run locally (two terminals)

```bash
# Terminal 1 — orchestrator
cd orchestrator
uv sync
uv run uvicorn app.main:app --reload --port 8787

# Terminal 2 — web
cd web
pnpm install
pnpm dev
```

Open [http://localhost:3000](http://localhost:3000) and start shopping.

### 4. Deploy

```bash
cd web
npx vercel --prod
```

The orchestrator stays on your machine — no inbound ports required.

---

## Tech Stack

| Component | Technology |
|---|---|
| Frontend | Next.js 16, React 19, Tailwind CSS 4, TypeScript |
| Backend-as-a-Service | InsForge (Postgres, Realtime, Auth) |
| Orchestrator | FastAPI, Python 3.12, Pydantic |
| LLM | Google Gemini (3.5 Flash for research, 2.5 Pro for synthesis) |
| Web Scraping | Playwright (browser agent), Gemini URL Context (static) |
| Search | Google Search (via Gemini grounded tool use) |
| Deployment | Vercel (frontend), local machine (orchestrator) |

---

## License

Built during a 24-hour hackathon. Reach out for licensing inquiries.
