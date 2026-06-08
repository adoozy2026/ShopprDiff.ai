import { createClient } from "@insforge/sdk";

const baseUrl = process.env.NEXT_PUBLIC_INSFORGE_PROJECT_URL ?? "";
const anonKey = process.env.NEXT_PUBLIC_INSFORGE_ANON_KEY ?? "";

export const insforge = createClient({
  baseUrl,
  anonKey,
});

export function isConfigured(): boolean {
  return Boolean(baseUrl) && Boolean(anonKey);
}

/**
 * Route an image URL through the orchestrator's pass-through proxy so it
 * loads with a Referer matching its own origin — bypassing the hotlink
 * protection that most retailer CDNs enforce against cross-origin <img>
 * fetches. The orchestrator URL defaults to localhost for the local dev
 * setup; override with NEXT_PUBLIC_ORCHESTRATOR_URL when deploying.
 */
export function imgProxy(url: string | null | undefined): string | undefined {
  if (!url) return undefined;
  const base =
    process.env.NEXT_PUBLIC_ORCHESTRATOR_URL?.replace(/\/$/, "") ??
    "http://localhost:8787";
  return `${base}/img?url=${encodeURIComponent(url)}`;
}

// --- Domain types: mirror migrations/<ts>_init.sql ---
export type IntentStatus = "eliciting" | "ready" | "researching" | "done" | "error";
export type CandidateStatus = "queued" | "researching" | "done" | "rejected" | "error";
export type FindingStatus = "queued" | "running" | "done" | "error";

export type ClarifyingTurn = { role: "user" | "assistant"; text: string };

export type IntentRow = {
  id: string;
  session_id: string;
  raw_query: string;
  spec: Record<string, unknown>;
  status: IntentStatus;
  clarifying_turns: ClarifyingTurn[];
  updated_at: string;
};

export type CandidateRow = {
  id: string;
  intent_id: string;
  title: string;
  canonical_attrs: Record<string, unknown>; // legacy, now dynamic via spec_attrs
  source: string;
  source_url: string;
  raw_price_cents: number | null;
  status: CandidateStatus;
};

export type FindingRow = {
  id: string;
  candidate_id: string;
  intent_id: string;
  agent_label: string;
  step: string;
  status: FindingStatus;
  finding: FindingPayload;
  updated_at: string;
};

export type FindingPayload = {
  // Universal core fields (always extracted)
  title?: string | null;
  price_cents?: number | null;
  shipping_cost_cents?: number | null;
  // Dynamic attributes extracted based on intake spec categories.
  // Fields like condition, seller, return_policy, etc. live here now
  // instead of at the top level.
  spec_attrs?: Record<string, string | number | null>;
  // Enrichment from OG meta / pipeline steps (not LLM-extracted)
  image_url?: string | null;
  description_summary?: string | null;
  seller_rep?: string | null;
  known_issues?: string[];
  scam_score?: number;
  scam_reasons?: string[];
  confidence?: string;
  // When the browser-agent configurator ran on this candidate.
  configurator_steps?: number;
  configurator_history?: { action: string; reason: string }[];
};

export type Alternative = { title: string; why_consider: string };

export type CandidatePick = {
  candidate_id: string;
  score: number;
  one_liner: string;
  detail?: string | null;
};

export type TradeoffInsight = {
  axis: string;
  winner_candidate_id?: string | null;
  summary: string;
};

export type RecommendationRow = {
  id: string;
  intent_id: string;
  ranked_candidate_ids: string[];
  rationale: string;
  alternatives: Alternative[];
  picks: CandidatePick[];
  tradeoffs: TradeoffInsight[];
  warnings: string[];
  generated_at: string;
};
