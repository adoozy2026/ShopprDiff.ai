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
  canonical_attrs: Record<string, unknown>;
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
  finding: Record<string, unknown>;
  updated_at: string;
};
