"use client";

import { useEffect, useMemo, useState } from "react";
import {
  insforge,
  isConfigured,
  type CandidateRow,
  type ClarifyingTurn,
  type FindingRow,
  type IntentRow,
  type IntentStatus,
} from "@/lib/insforge";

type Props = { intentId: string };

// The realtime SDK delivers each message with the published payload fields
// spread at the top level (alongside a `meta` object) — not nested under a
// `payload` key. See @insforge/shared-schemas socketMessageSchema (meta +
// passthrough payload).
type RealtimeMessage<T> = T & { meta?: Record<string, unknown> };

export default function IntentDashboard({ intentId }: Props) {
  const [intent, setIntent] = useState<IntentRow | null>(null);
  const [candidates, setCandidates] = useState<Record<string, CandidateRow>>({});
  const [findings, setFindings] = useState<Record<string, FindingRow>>({});

  useEffect(() => {
    if (!isConfigured()) return;
    const channel = `intent:${intentId}`;
    let cancelled = false;

    const onIntent = (
      m: RealtimeMessage<{
        status?: IntentStatus;
        spec?: Record<string, unknown>;
        clarifying_turns?: ClarifyingTurn[];
      }>,
    ) => {
      setIntent((prev) => {
        if (!prev) return prev;
        return {
          ...prev,
          ...(m.status ? { status: m.status } : null),
          ...(m.spec ? { spec: m.spec } : null),
          ...(m.clarifying_turns ? { clarifying_turns: m.clarifying_turns } : null),
        };
      });
    };
    const onCandidate = (m: RealtimeMessage<Partial<CandidateRow>>) => {
      if (m.id) setCandidates((prev) => ({ ...prev, [m.id!]: m as CandidateRow }));
    };
    const onFinding = (m: RealtimeMessage<Partial<FindingRow>>) => {
      if (m.id) setFindings((prev) => ({ ...prev, [m.id!]: m as FindingRow }));
    };

    // insforge.realtime is a module-level singleton, so every listener we add
    // must be removed on cleanup — otherwise they accumulate across remounts
    // (React strict mode, intent navigation) and fire duplicate updates.
    const listeners: Array<[string, (m: never) => void]> = [
      ["intent.updated", onIntent],
      ["intent.created", onIntent],
      ["candidate.created", onCandidate],
      ["candidate.updated", onCandidate],
      ["finding.created", onFinding],
      ["finding.updated", onFinding],
    ];

    (async () => {
      // Initial hydrate so a refresh / direct nav shows existing state.
      const [{ data: intents }, { data: cs }, { data: fs }] = await Promise.all([
        insforge.database.from("intents").select().eq("id", intentId),
        insforge.database.from("candidates").select().eq("intent_id", intentId),
        insforge.database.from("researcher_findings").select().eq("intent_id", intentId),
      ]);
      if (cancelled) return;
      if (intents?.[0]) setIntent(intents[0] as IntentRow);
      if (cs) setCandidates(Object.fromEntries((cs as CandidateRow[]).map((c) => [c.id, c])));
      if (fs) setFindings(Object.fromEntries((fs as FindingRow[]).map((f) => [f.id, f])));

      const res = await insforge.realtime.subscribe(channel);
      if (cancelled) return;
      if (!res.ok) {
        console.error("realtime subscribe failed:", res.error);
        return;
      }

      for (const [event, cb] of listeners) insforge.realtime.on(event, cb);
    })();

    return () => {
      cancelled = true;
      for (const [event, cb] of listeners) insforge.realtime.off(event, cb);
      insforge.realtime.unsubscribe(channel);
    };
  }, [intentId]);

  const candidateList = Object.values(candidates);
  const findingsByCandidate = useMemo(() => {
    const out: Record<string, FindingRow[]> = {};
    for (const f of Object.values(findings)) {
      (out[f.candidate_id] ??= []).push(f);
    }
    return out;
  }, [findings]);

  return (
    <main className="mx-auto max-w-6xl px-6 py-10">
      <header className="flex items-baseline justify-between border-b border-neutral-200 pb-4">
        <div>
          <div className="text-xs uppercase tracking-wider text-neutral-500">Intent</div>
          <h1 className="mt-1 text-xl font-semibold">
            {intent?.raw_query || "Shopping in progress"}
          </h1>
        </div>
        <div className="rounded-full bg-neutral-100 px-3 py-1 text-xs text-neutral-700">
          status: <span className="font-mono">{intent?.status ?? "loading"}</span>
        </div>
      </header>

      <IntakeSection intent={intent} />

      <section className="mt-8">
        <h2 className="mb-3 text-sm font-medium text-neutral-700">
          {candidateList.length > 0
            ? `Researchers (${candidateList.length})`
            : "Researchers (waiting for candidates…)"}
        </h2>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {candidateList.length === 0
            ? [0, 1, 2, 3, 4, 5].map((i) => (
                <div
                  key={i}
                  className="rounded-lg border border-dashed border-neutral-300 p-4"
                >
                  <div className="h-3 w-24 animate-pulse rounded bg-neutral-200" />
                  <div className="mt-2 h-3 w-40 animate-pulse rounded bg-neutral-100" />
                  <div className="mt-6 h-3 w-32 animate-pulse rounded bg-neutral-100" />
                </div>
              ))
            : candidateList.map((c) => {
                const steps = findingsByCandidate[c.id] ?? [];
                const latest = steps.at(-1);
                return (
                  <div key={c.id} className="rounded-lg border border-neutral-200 bg-white p-4 shadow-sm">
                    <div className="text-xs text-neutral-500">{c.source}</div>
                    <div className="mt-1 line-clamp-2 text-sm font-medium">{c.title}</div>
                    <div className="mt-3 text-xs text-neutral-700">
                      {c.raw_price_cents != null
                        ? `$${(c.raw_price_cents / 100).toFixed(2)}`
                        : "—"}
                    </div>
                    <div className="mt-4 border-t border-neutral-100 pt-3 text-xs text-neutral-500">
                      {latest ? (
                        <>
                          <span className="font-mono">{latest.status}</span>{" "}
                          · {latest.step}
                        </>
                      ) : (
                        <span className="opacity-60">queued…</span>
                      )}
                    </div>
                  </div>
                );
              })}
        </div>
      </section>

      <footer className="mt-12 text-xs text-neutral-400">
        intent_id: <span className="font-mono">{intentId}</span>
      </footer>
    </main>
  );
}

function IntakeSection({ intent }: { intent: IntentRow | null }) {
  const [reply, setReply] = useState("");
  const [sending, setSending] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const turns = intent?.clarifying_turns ?? [];
  const lastTurn = turns.at(-1);
  const awaitingUser = intent?.status === "eliciting" && lastTurn?.role === "assistant";

  if (!intent) return null;
  if (turns.length === 0 && intent.status !== "eliciting") return null;

  async function send(e: React.FormEvent) {
    e.preventDefault();
    if (!intent || !reply.trim() || sending) return;
    setSending(true);
    setErr(null);
    try {
      const nextTurns: ClarifyingTurn[] = [
        ...turns,
        { role: "user", text: reply.trim() },
      ];
      const { error } = await insforge.database
        .from("intents")
        .update({
          clarifying_turns: nextTurns,
          picked_up_at: null, // re-arm for the orchestrator's poller
        })
        .eq("id", intent.id);
      if (error) throw error;
      setReply("");
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setSending(false);
    }
  }

  return (
    <section className="mt-6 rounded-lg border border-neutral-200 bg-neutral-50 p-4">
      <h2 className="mb-3 text-sm font-medium text-neutral-700">Intake</h2>
      <div className="space-y-2">
        {turns.map((t, i) => (
          <div
            key={i}
            className={`max-w-[80%] rounded-md px-3 py-2 text-sm ${
              t.role === "user"
                ? "ml-auto bg-neutral-900 text-white"
                : "bg-white text-neutral-900 shadow-sm"
            }`}
          >
            {t.text}
          </div>
        ))}
      </div>
      {awaitingUser && (
        <form onSubmit={send} className="mt-4 flex gap-2">
          <input
            value={reply}
            onChange={(e) => setReply(e.target.value)}
            placeholder="Type your answer…"
            className="flex-1 rounded-md border border-neutral-300 bg-white px-3 py-2 text-sm text-neutral-900 placeholder:text-neutral-400 outline-none focus:border-neutral-900"
            autoFocus
          />
          <button
            type="submit"
            disabled={sending || !reply.trim()}
            className="rounded-md bg-neutral-900 px-3 py-2 text-sm font-medium text-white disabled:opacity-50"
          >
            {sending ? "Sending…" : "Send"}
          </button>
        </form>
      )}
      {err && <p className="mt-3 text-xs text-red-700">{err}</p>}
    </section>
  );
}
