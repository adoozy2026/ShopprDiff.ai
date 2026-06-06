-- Recommendations enrichment: structured synth output beyond a single rationale.
--
-- Adds three jsonb columns that the synthesizer fills:
--   picks       — [{candidate_id, score, one_liner, detail?}] per ranked candidate
--                  so each tile can render *why* it's on screen.
--   tradeoffs   — [{axis, winner_candidate_id, summary}] for axis-by-axis insight
--                  (price vs. returns vs. shipping vs. seller trust, etc.)
--   warnings    — [string] honest concerns surfaced to the user (no returns,
--                  thin seller history, variant mismatch, etc.)
--
-- Existing rationale/alternatives columns stay; this is purely additive.

alter table recommendations
  add column if not exists picks      jsonb not null default '[]'::jsonb,
  add column if not exists tradeoffs  jsonb not null default '[]'::jsonb,
  add column if not exists warnings   jsonb not null default '[]'::jsonb;

-- Republish the recommendation event with the new fields so the dashboard
-- gets them via realtime without an extra REST hop.
create or replace function publish_recommendation_event()
returns trigger language plpgsql security definer as $$
begin
  perform realtime.publish(
    'intent:' || new.intent_id::text,
    'recommendation.created',
    jsonb_build_object(
      'id', new.id,
      'intent_id', new.intent_id,
      'ranked_candidate_ids', new.ranked_candidate_ids,
      'rationale', new.rationale,
      'alternatives', new.alternatives,
      'picks', new.picks,
      'tradeoffs', new.tradeoffs,
      'warnings', new.warnings
    )
  );
  return new;
end $$;
