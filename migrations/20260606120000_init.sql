-- Personal Shopper Agent — initial schema
-- See plan: §"Data model"
--
-- Insforge migration rules:
--   * filename must be <YYYYMMDDHHmmss>_<name>.sql
--   * BEGIN/COMMIT/ROLLBACK statements are rejected (the runner wraps each file
--     in its own transaction). PL/pgSQL DO blocks and CREATE OR REPLACE
--     FUNCTION ... BEGIN/END are fine — those are PL/pgSQL, not txn control.
--   * `realtime.publish(channel, event, payload)` is available from triggers
--     to push to subscribers connected via @insforge/sdk's realtime channel.

create extension if not exists "pgcrypto";
create extension if not exists "vector";

-- ============================================================
-- Enums
-- ============================================================
do $$ begin
  create type intent_status as enum ('eliciting','ready','researching','done','error');
exception when duplicate_object then null; end $$;

do $$ begin
  create type candidate_status as enum ('queued','researching','done','rejected','error');
exception when duplicate_object then null; end $$;

do $$ begin
  create type finding_status as enum ('queued','running','done','error');
exception when duplicate_object then null; end $$;

do $$ begin
  create type forecast_direction as enum ('down','flat','up','unknown');
exception when duplicate_object then null; end $$;

-- ============================================================
-- Tables
-- ============================================================
create table if not exists sessions (
  id          uuid primary key default gen_random_uuid(),
  user_id     text,
  persona     jsonb not null default '{}'::jsonb,
  created_at  timestamptz not null default now()
);

create table if not exists intents (
  id                  uuid primary key default gen_random_uuid(),
  session_id          uuid not null references sessions(id) on delete cascade,
  raw_query           text not null,
  spec                jsonb not null default '{}'::jsonb,
  status              intent_status not null default 'eliciting',
  clarifying_turns    jsonb not null default '[]'::jsonb,
  picked_up_at        timestamptz,  -- set by orchestrator when it starts processing
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now()
);
create index if not exists intents_session_idx on intents(session_id);
create index if not exists intents_status_idx  on intents(status);
create index if not exists intents_ready_unpicked_idx
  on intents(created_at) where status = 'ready' and picked_up_at is null;

create table if not exists candidates (
  id                uuid primary key default gen_random_uuid(),
  intent_id         uuid not null references intents(id) on delete cascade,
  title             text not null,
  canonical_attrs   jsonb not null default '{}'::jsonb,
  canonical_key     text,
  source            text not null,
  source_url        text not null,
  raw_price_cents   integer,
  status            candidate_status not null default 'queued',
  created_at        timestamptz not null default now()
);
create index if not exists candidates_intent_idx on candidates(intent_id);
create index if not exists candidates_canonkey_idx on candidates(canonical_key);

create table if not exists researcher_findings (
  id            uuid primary key default gen_random_uuid(),
  candidate_id  uuid not null references candidates(id) on delete cascade,
  intent_id     uuid not null references intents(id) on delete cascade,
  agent_label   text not null,
  step          text not null,
  status        finding_status not null default 'queued',
  finding       jsonb not null default '{}'::jsonb,
  log           text,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);
create index if not exists rf_candidate_idx on researcher_findings(candidate_id);
create index if not exists rf_intent_idx    on researcher_findings(intent_id);
create index if not exists rf_status_idx    on researcher_findings(status);

create table if not exists price_history (
  id            uuid primary key default gen_random_uuid(),
  canonical_key text not null,
  source        text not null,
  price_cents   integer not null,
  observed_at   timestamptz not null default now()
);
create index if not exists price_history_key_idx on price_history(canonical_key, observed_at desc);

create table if not exists forecasts (
  id              uuid primary key default gen_random_uuid(),
  canonical_key   text not null,
  horizon_weeks   integer not null,
  narrative       text not null,
  direction       forecast_direction not null default 'unknown',
  drivers         jsonb not null default '[]'::jsonb,
  generated_at    timestamptz not null default now()
);

create table if not exists recommendations (
  id                     uuid primary key default gen_random_uuid(),
  intent_id              uuid not null references intents(id) on delete cascade,
  ranked_candidate_ids   uuid[] not null default '{}',
  rationale              text not null default '',
  alternatives           jsonb not null default '[]'::jsonb,
  generated_at           timestamptz not null default now()
);
create index if not exists recs_intent_idx on recommendations(intent_id);

-- ============================================================
-- updated_at triggers
-- ============================================================
create or replace function set_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end $$;

drop trigger if exists trg_intents_updated on intents;
create trigger trg_intents_updated
  before update on intents
  for each row execute function set_updated_at();

drop trigger if exists trg_rf_updated on researcher_findings;
create trigger trg_rf_updated
  before update on researcher_findings
  for each row execute function set_updated_at();

-- ============================================================
-- Realtime publishers — push to channel `intent:<id>` whenever the
-- pipeline state changes. Browser subscribes via:
--   insforge.realtime.subscribe(`intent:${intentId}`)
-- ============================================================

create or replace function publish_intent_event()
returns trigger language plpgsql security definer as $$
declare
  evt text;
begin
  evt := case
    when tg_op = 'INSERT' then 'intent.created'
    else 'intent.updated'
  end;
  perform realtime.publish(
    'intent:' || new.id::text,
    evt,
    jsonb_build_object(
      'id', new.id,
      'status', new.status,
      'spec', new.spec,
      'clarifying_turns', new.clarifying_turns,
      'updated_at', new.updated_at
    )
  );
  return new;
end $$;

drop trigger if exists trg_intent_publish on intents;
create trigger trg_intent_publish
  after insert or update on intents
  for each row execute function publish_intent_event();

create or replace function publish_candidate_event()
returns trigger language plpgsql security definer as $$
begin
  perform realtime.publish(
    'intent:' || new.intent_id::text,
    case when tg_op = 'INSERT' then 'candidate.created' else 'candidate.updated' end,
    jsonb_build_object(
      'id', new.id,
      'intent_id', new.intent_id,
      'title', new.title,
      'source', new.source,
      'source_url', new.source_url,
      'canonical_attrs', new.canonical_attrs,
      'raw_price_cents', new.raw_price_cents,
      'status', new.status
    )
  );
  return new;
end $$;

drop trigger if exists trg_candidate_publish on candidates;
create trigger trg_candidate_publish
  after insert or update on candidates
  for each row execute function publish_candidate_event();

create or replace function publish_finding_event()
returns trigger language plpgsql security definer as $$
begin
  perform realtime.publish(
    'intent:' || new.intent_id::text,
    case when tg_op = 'INSERT' then 'finding.created' else 'finding.updated' end,
    jsonb_build_object(
      'id', new.id,
      'candidate_id', new.candidate_id,
      'intent_id', new.intent_id,
      'agent_label', new.agent_label,
      'step', new.step,
      'status', new.status,
      'finding', new.finding,
      'updated_at', new.updated_at
    )
  );
  return new;
end $$;

drop trigger if exists trg_finding_publish on researcher_findings;
create trigger trg_finding_publish
  after insert or update on researcher_findings
  for each row execute function publish_finding_event();

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
      'alternatives', new.alternatives
    )
  );
  return new;
end $$;

drop trigger if exists trg_recommendation_publish on recommendations;
create trigger trg_recommendation_publish
  after insert on recommendations
  for each row execute function publish_recommendation_event();
