"use client";

import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import {
  insforge,
  isConfigured,
  type Alternative,
  type CandidatePick,
  type CandidateRow,
  type ClarifyingTurn,
  type FindingPayload,
  type FindingRow,
  type IntentRow,
  type IntentStatus,
  type RecommendationRow,
} from "@/lib/insforge";

type Props = { intentId: string };

// The realtime SDK delivers each message with the published payload fields
// spread at the top level (alongside a `meta` object) — not nested under a
// `payload` key. See @insforge/shared-schemas socketMessageSchema (meta +
// passthrough payload).
type RealtimeMessage<T> = T & { meta?: Record<string, unknown> };

function money(cents: number | null | undefined): string {
  if (cents == null) return "—";
  return `$${(cents / 100).toFixed(0)}`;
}

export default function IntentDashboard({ intentId }: Props) {
  const [intent, setIntent] = useState<IntentRow | null>(null);
  const [candidates, setCandidates] = useState<Record<string, CandidateRow>>({});
  const [findings, setFindings] = useState<Record<string, FindingRow>>({});
  const [rec, setRec] = useState<RecommendationRow | null>(null);

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
    const onRecommendation = (m: RealtimeMessage<Partial<RecommendationRow>>) => {
      if (m.id) setRec(m as RecommendationRow);
    };

    const listeners: Array<[string, (m: never) => void]> = [
      ["intent.updated", onIntent],
      ["intent.created", onIntent],
      ["candidate.created", onCandidate],
      ["candidate.updated", onCandidate],
      ["finding.created", onFinding],
      ["finding.updated", onFinding],
      ["recommendation.created", onRecommendation],
    ];

    (async () => {
      const [{ data: intents }, { data: cs }, { data: fs }, { data: rs }] =
        await Promise.all([
          insforge.database.from("intents").select().eq("id", intentId),
          insforge.database.from("candidates").select().eq("intent_id", intentId),
          insforge.database.from("researcher_findings").select().eq("intent_id", intentId),
          insforge.database
            .from("recommendations")
            .select()
            .eq("intent_id", intentId)
            .order("generated_at", { ascending: false })
            .limit(1),
        ]);
      if (cancelled) return;
      if (intents?.[0]) setIntent(intents[0] as IntentRow);
      if (cs) setCandidates(Object.fromEntries((cs as CandidateRow[]).map((c) => [c.id, c])));
      if (fs) setFindings(Object.fromEntries((fs as FindingRow[]).map((f) => [f.id, f])));
      if (rs?.[0]) setRec(rs[0] as RecommendationRow);

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
  const findingByCandidate = useMemo(() => {
    const out: Record<string, FindingRow> = {};
    for (const f of Object.values(findings)) {
      const existing = out[f.candidate_id];
      if (!existing || (existing.updated_at ?? "") < (f.updated_at ?? "")) {
        out[f.candidate_id] = f;
      }
    }
    return out;
  }, [findings]);

  const topPickId = rec?.ranked_candidate_ids?.[0];
  const topPick = topPickId ? candidates[topPickId] : undefined;
  const topPickFinding = topPickId ? findingByCandidate[topPickId] : undefined;

  return (
    <main className="mx-auto max-w-6xl px-6 py-10">
      <header className="flex items-start justify-between gap-4 border-b border-neutral-200 pb-4">
        <UserPromptSection intent={intent} />
        <div className="shrink-0 rounded-full bg-neutral-100 px-3 py-1 text-xs text-neutral-700">
          status: <span className="font-mono">{intent?.status ?? "loading"}</span>
        </div>
      </header>

      {rec && topPick && (
        <TopPickPanel
          rec={rec}
          candidate={topPick}
          finding={topPickFinding}
          candidates={candidates}
        />
      )}

      {rec && rec.warnings && rec.warnings.length > 0 && (
        <WarningsBanner warnings={rec.warnings} />
      )}

      <section className="mt-8">
        <h2 className="mb-3 text-sm font-medium text-neutral-700">
          {candidateList.length > 0
            ? `Researchers (${candidateList.length})`
            : intent?.status === "done" || intent?.status === "error"
            ? "No candidates found"
            : "Researchers (waiting for candidates…)"}
        </h2>
        {candidateList.length === 0 ? (
          intent?.status === "done" || intent?.status === "error" ? (
            <EmptyState intent={intent} />
          ) : (
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {[0, 1, 2, 3, 4, 5].map((i) => (
                <div
                  key={i}
                  className="rounded-lg border border-dashed border-neutral-300 p-4"
                >
                  <div className="h-3 w-24 animate-pulse rounded bg-neutral-200" />
                  <div className="mt-2 h-3 w-40 animate-pulse rounded bg-neutral-100" />
                  <div className="mt-6 h-3 w-32 animate-pulse rounded bg-neutral-100" />
                </div>
              ))}
            </div>
          )
        ) : (
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {candidateList.map((c) => (
              <CandidateTile
                key={c.id}
                candidate={c}
                finding={findingByCandidate[c.id]}
                isTopPick={topPickId === c.id}
                pick={rec?.picks?.find((p) => p.candidate_id === c.id)}
              />
            ))}
          </div>
        )}
      </section>

      {rec && rec.alternatives && rec.alternatives.length > 0 && (
        <AlternativesSection alternatives={rec.alternatives} />
      )}

      <footer className="mt-12 text-xs text-neutral-400">
        intent_id: <span className="font-mono">{intentId}</span>
      </footer>
    </main>
  );
}

/** Read a field from the finding payload, falling back into spec_attrs. */
function fAttr(f: FindingPayload | undefined, key: string): string | null {
  if (!f) return null;
  const record = f as Record<string, unknown>;
  const top = record[key];
  if (typeof top === "string" && top) return top;
  const sa = f.spec_attrs;
  if (sa) {
    const v = sa[key];
    if (typeof v === "string" && v) return v;
  }
  return null;
}

function CandidateTile({
  candidate,
  finding,
  isTopPick,
  pick,
}: {
  candidate: CandidateRow;
  finding?: FindingRow;
  isTopPick: boolean;
  pick?: CandidatePick;
}) {
  const f = finding?.finding;
  const price = f?.price_cents ?? candidate.raw_price_cents ?? null;
  const status = finding?.status;
  const step = finding?.step;
  const scam = f?.scam_score;
  const scamReasons = f?.scam_reasons ?? [];
  const [showDetails, setShowDetails] = useState(false);

  const knownIssues = f?.known_issues ?? [];
  const hasDetails =
    Boolean(pick?.detail) ||
    Boolean(f?.seller_rep) ||
    knownIssues.length > 0 ||
    (scamReasons.length > 0 && scam != null && scam >= 40);

  return (
    <div
      className={`flex flex-col rounded-lg border bg-white p-4 shadow-sm transition ${
        isTopPick
          ? "border-emerald-400 ring-2 ring-emerald-100"
          : "border-neutral-200"
      }`}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-neutral-500">
          <span>{candidate.source}</span>
          {f?.configurator_steps != null && f.configurator_steps > 0 && (
            <span
              className="rounded-full bg-indigo-100 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-indigo-800"
              title={(f.configurator_history ?? [])
                .map((h, i) => `${i + 1}. ${h.action} — ${h.reason}`)
                .join("\n")}
            >
              🤖 navigated {f.configurator_steps} step{f.configurator_steps === 1 ? "" : "s"}
            </span>
          )}
        </div>
        <div className="flex shrink-0 flex-wrap items-end justify-end gap-1">
          {isTopPick && (
            <span className="rounded-full bg-emerald-100 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-emerald-800">
              top pick
            </span>
          )}
          {!isTopPick && scam != null && scam >= 40 && (
            <span
              className="rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-amber-800"
              title={scamReasons.join("; ")}
            >
              risk {scam}
            </span>
          )}
        </div>
      </div>



      <div className="mt-3 line-clamp-2 text-sm font-medium text-neutral-900">
        {f?.title || candidate.title}
      </div>

      <div className="mt-2 flex items-baseline gap-2">
        <span className="text-lg font-semibold text-neutral-900">{money(price)}</span>
        {fAttr(f, "condition") && (
          <span className="text-xs text-neutral-500">{fAttr(f, "condition")}</span>
        )}
      </div>

      {pick?.one_liner && (
        <p className="mt-2 rounded-md bg-neutral-50 px-2.5 py-1.5 text-xs leading-snug text-neutral-800">
          <span className="font-medium text-neutral-900">Why shown:</span>{" "}
          {pick.one_liner}
        </p>
      )}

      {f?.description_summary && (
        <ExpandableText
          text={f.description_summary as string}
          clampLines={3}
          className="mt-2 text-xs text-neutral-600"
        />
      )}

      <dl className="mt-3 space-y-1 text-xs text-neutral-700">
        {fAttr(f, "seller") && (
          <div className="flex justify-between gap-2">
            <dt className="shrink-0 text-neutral-500">seller</dt>
            <dd className="break-words text-right">{fAttr(f, "seller")}</dd>
          </div>
        )}
        {fAttr(f, "shipping_speed") && (
          <div className="flex justify-between gap-2">
            <dt className="shrink-0 text-neutral-500">shipping</dt>
            <dd className="break-words text-right">{fAttr(f, "shipping_speed")}</dd>
          </div>
        )}
        {fAttr(f, "return_policy") && (
          <div className="flex justify-between gap-2">
            <dt className="shrink-0 text-neutral-500">returns</dt>
            <dd className="break-words text-right">{fAttr(f, "return_policy")}</dd>
          </div>
        )}
      </dl>

      {hasDetails && (
        <div className="mt-3">
          {showDetails ? (
            <div className="space-y-2 rounded-md bg-neutral-50 p-3 text-xs text-neutral-800">
              {pick?.detail && (
                <Section label="Agent take">{pick.detail}</Section>
              )}
              {f?.seller_rep && (
                <Section label="Seller reputation">{f.seller_rep}</Section>
              )}
              {knownIssues.length > 0 && (
                <Section label="Known issues">
                  <ul className="list-disc space-y-0.5 pl-4">
                    {knownIssues.map((k, i) => (
                      <li key={i}>{k}</li>
                    ))}
                  </ul>
                </Section>
              )}
              {scam != null && scam >= 40 && scamReasons.length > 0 && (
                <Section label="Risk flags">
                  <ul className="list-disc space-y-0.5 pl-4">
                    {scamReasons.map((r, i) => (
                      <li key={i}>{r}</li>
                    ))}
                  </ul>
                </Section>
              )}
              <button
                type="button"
                onClick={() => setShowDetails(false)}
                className="mt-1 text-[11px] font-medium text-neutral-600 underline hover:text-neutral-900"
              >
                Show less
              </button>
            </div>
          ) : (
            <button
              type="button"
              onClick={() => setShowDetails(true)}
              className="text-[11px] font-medium text-neutral-600 underline hover:text-neutral-900"
            >
              Show details ↓
            </button>
          )}
        </div>
      )}

      <div className="mt-auto pt-3" />

      <div className="border-t border-neutral-100 pt-3 text-xs text-neutral-500">
        {status === "done" ? (
          <a
            href={candidate.source_url}
            target="_blank"
            rel="noreferrer noopener"
            className="font-medium text-neutral-900 underline"
          >
            View listing →
          </a>
        ) : status ? (
          <>
            <span className="font-mono">{status}</span> · {step}
          </>
        ) : (
          <span className="opacity-60">queued…</span>
        )}
      </div>
    </div>
  );
}

function TopPickPanel({
  rec,
  candidate,
  finding,
  candidates,
}: {
  rec: RecommendationRow;
  candidate: CandidateRow;
  finding?: FindingRow;
  candidates: Record<string, CandidateRow>;
}) {
  const f = finding?.finding;
  const price = f?.price_cents ?? candidate.raw_price_cents ?? null;
  return (
    <section className="mt-6 overflow-hidden rounded-lg border border-emerald-300 bg-emerald-50 p-5 shadow-sm">
      <div className="flex flex-col gap-5 sm:flex-row">

        <div className="flex-1">
          <div className="text-xs font-medium uppercase tracking-wider text-emerald-800">
            Top pick · {candidate.source}
          </div>
          <h2 className="mt-1 text-lg font-semibold text-neutral-900">
            {f?.title || candidate.title}
          </h2>
          <div className="mt-1 flex items-baseline gap-3 text-sm">
            <span className="text-2xl font-semibold text-neutral-900">
              {money(price)}
            </span>
            {fAttr(f, "condition") && (
              <span className="text-neutral-600">{fAttr(f, "condition")}</span>
            )}
            {fAttr(f, "seller") && (
              <span className="text-neutral-500">at {fAttr(f, "seller")}</span>
            )}
          </div>
          {rec.rationale && (
            <p className="mt-3 whitespace-pre-line text-sm leading-relaxed text-neutral-800">
              {rec.rationale}
            </p>
          )}
          <div className="mt-4">
            <a
              href={candidate.source_url}
              target="_blank"
              rel="noreferrer noopener"
              className="inline-flex items-center rounded-md bg-emerald-700 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-800"
            >
              Buy at {candidate.source} →
            </a>
          </div>
        </div>
      </div>

      {rec.tradeoffs && rec.tradeoffs.length > 0 && (
        <div className="mt-5 border-t border-emerald-200 pt-4">
          <div className="mb-2 text-xs font-medium uppercase tracking-wider text-emerald-800">
            Tradeoffs
          </div>
          <ul className="space-y-1.5 text-sm text-neutral-800">
            {rec.tradeoffs.map((t, i) => {
              const winner = t.winner_candidate_id
                ? candidates[t.winner_candidate_id]
                : null;
              return (
                <li key={i} className="flex items-start gap-2">
                  <span className="mt-0.5 shrink-0 rounded bg-white px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-emerald-800">
                    {t.axis}
                  </span>
                  <span>
                    {t.summary}
                    {winner && (
                      <span className="ml-1 text-neutral-500">
                        → {winner.source}
                      </span>
                    )}
                  </span>
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </section>
  );
}

function Section({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="mb-0.5 text-[10px] font-medium uppercase tracking-wide text-neutral-500">
        {label}
      </div>
      <div className="text-neutral-800">{children}</div>
    </div>
  );
}

function ExpandableText({
  text,
  clampLines = 3,
  className,
}: {
  text: string;
  clampLines?: number;
  className?: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const [overflowing, setOverflowing] = useState(false);
  const ref = useRef<HTMLParagraphElement>(null);

  useEffect(() => {
    if (!ref.current || expanded) return;
    // scrollHeight > clientHeight (accounting for 1px rounding) means the
    // visible clamp is hiding content the user might want to read.
    setOverflowing(ref.current.scrollHeight > ref.current.clientHeight + 1);
  }, [text, expanded, clampLines]);

  // Inline style is cleaner than maintaining N tailwind classes for arbitrary
  // line-clamp values; works in all evergreen browsers.
  const clampStyle = expanded
    ? undefined
    : {
        display: "-webkit-box" as const,
        WebkitBoxOrient: "vertical" as const,
        WebkitLineClamp: clampLines,
        overflow: "hidden",
      };

  return (
    <div className={className}>
      <p ref={ref} style={clampStyle} className="leading-snug">
        {text}
      </p>
      {(overflowing || expanded) && (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="mt-0.5 text-[11px] font-medium text-neutral-600 underline hover:text-neutral-900"
        >
          {expanded ? "Read less" : "Read more…"}
        </button>
      )}
    </div>
  );
}

function EmptyState({ intent }: { intent: IntentRow }) {
  const spec = intent.spec ?? {};
  const get = (k: string) => spec[k as keyof typeof spec];
  const allFields: Array<[string, unknown]> = [
    ["product", get("product_class")],
    [
      "budget",
      typeof get("budget_cents") === "number"
        ? `$${((get("budget_cents") as number) / 100).toFixed(0)}`
        : null,
    ],
    ["condition", get("condition")],
    ["must-haves", (get("must_haves") as string[] | undefined)?.join(", ")],
    ["deal-breakers", (get("deal_breakers") as string[] | undefined)?.join(", ")],
  ];
  const fields = allFields.filter(
    (pair): pair is [string, unknown] =>
      Boolean(pair[1]) && (typeof pair[1] !== "string" || pair[1].length > 0),
  );

  return (
    <div className="rounded-lg border border-dashed border-neutral-300 bg-neutral-50 p-6">
      <p className="text-sm text-neutral-800">
        {intent.status === "error"
          ? "Something went wrong while searching — see console for details."
          : "I searched but couldn't find product listings matching your query."}
      </p>
      <p className="mt-2 text-xs text-neutral-600">
        This usually happens when the query has conflicting requirements (e.g.
        asking for both &quot;brand new&quot; and &quot;gently used&quot;), or
        when the product is niche enough that Google&apos;s shopping results
        didn&apos;t surface direct buy pages. Try simplifying the prompt — the
        &quot;Refine search&quot; field above is the fastest way.
      </p>
      {fields.length > 0 && (
        <dl className="mt-4 grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1.5 text-xs">
          {fields.map(([k, v]) => (
            <Fragment key={k}>
              <dt className="text-neutral-500">{k}</dt>
              <dd className="text-neutral-800">{String(v)}</dd>
            </Fragment>
          ))}
        </dl>
      )}
    </div>
  );
}

function WarningsBanner({ warnings }: { warnings: string[] }) {
  return (
    <section className="mt-4 rounded-lg border border-amber-300 bg-amber-50 p-4">
      <div className="mb-2 text-xs font-medium uppercase tracking-wider text-amber-900">
        Honest concerns
      </div>
      <ul className="space-y-1 text-sm text-amber-900">
        {warnings.map((w, i) => (
          <li key={i} className="flex gap-2">
            <span className="select-none">⚠️</span>
            <span>{w}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function AlternativesSection({ alternatives }: { alternatives: Alternative[] }) {
  return (
    <section className="mt-8">
      <h2 className="mb-3 text-sm font-medium text-neutral-700">
        Alternatives to consider
      </h2>
      <ul className="space-y-3">
        {alternatives.map((a, i) => (
          <li
            key={i}
            className="rounded-lg border border-neutral-200 bg-white p-4 shadow-sm"
          >
            <div className="text-sm font-medium text-neutral-900">{a.title}</div>
            <p className="mt-1 text-xs text-neutral-600">{a.why_consider}</p>
          </li>
        ))}
      </ul>
    </section>
  );
}

function UserPromptSection({ intent }: { intent: IntentRow | null }) {
  const [refining, setRefining] = useState(false);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const turns = intent?.clarifying_turns ?? [];
  // Refinements = all user turns after the first (index 0 is the original prompt).
  const refinements = turns.filter((t, i) => i > 0 && t.role === "user");

  function openRefine() {
    setDraft("");
    setErr(null);
    setRefining(true);
  }

  function cancelRefine() {
    setRefining(false);
    setErr(null);
  }

  async function submitRefinement(e: React.FormEvent) {
    e.preventDefault();
    if (!intent || !draft.trim() || busy) return;
    setBusy(true);
    setErr(null);
    try {
      const nextTurns: ClarifyingTurn[] = [
        ...turns,
        { role: "user", text: draft.trim() },
      ];
      const { error } = await insforge.database
        .from("intents")
        .update({
          clarifying_turns: nextTurns,
          status: "eliciting",
          picked_up_at: null,
        })
        .eq("id", intent.id);
      if (error) throw error;
      setDraft("");
      setRefining(false);
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function undoRefinement(text: string) {
    if (!intent || busy) return;
    setBusy(true);
    setErr(null);
    try {
      // Remove the first matching user turn with this text (after index 0).
      let removed = false;
      const nextTurns = turns.filter((t, i) => {
        if (!removed && i > 0 && t.role === "user" && t.text === text) {
          removed = true;
          return false;
        }
        return true;
      });
      const { error } = await insforge.database
        .from("intents")
        .update({
          clarifying_turns: nextTurns,
          status: "eliciting",
          picked_up_at: null,
        })
        .eq("id", intent.id);
      if (error) throw error;
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="min-w-0 flex-1">
      <div className="text-xs uppercase tracking-wider text-neutral-500">User Prompt</div>
      <div className="mt-1 flex items-start gap-3">
        <h1 className="text-xl font-semibold">
          {intent?.raw_query || "Shopping in progress"}
        </h1>
        {intent && !refining && (
          <button
            type="button"
            onClick={openRefine}
            className="mt-0.5 shrink-0 rounded-md border border-neutral-300 bg-white px-2.5 py-1 text-xs font-medium text-neutral-700 hover:bg-neutral-50"
          >
            Refine search
          </button>
        )}
      </div>

      {refinements.length > 0 && (
        <ul className="mt-3 space-y-1">
          {refinements.map((r, i) => (
            <li
              key={i}
              className="flex items-center gap-2 rounded-md bg-neutral-50 px-3 py-1.5 text-sm text-neutral-800"
            >
              <span className="flex-1">{r.text}</span>
              <button
                type="button"
                onClick={() => undoRefinement(r.text)}
                disabled={busy}
                className="shrink-0 text-xs text-neutral-500 underline hover:text-neutral-900 disabled:opacity-50"
              >
                Undo
              </button>
            </li>
          ))}
        </ul>
      )}

      {refining && (
        <form onSubmit={submitRefinement} className="mt-3">
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            rows={2}
            placeholder="Add details to refine your search…"
            className="w-full rounded-md border border-neutral-300 bg-white px-3 py-2 text-sm text-neutral-900 placeholder:text-neutral-400 outline-none focus:border-neutral-900"
            autoFocus
          />
          <div className="mt-2 flex gap-2">
            <button
              type="submit"
              disabled={busy || !draft.trim()}
              className="rounded-md bg-neutral-900 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
            >
              {busy ? "Submitting…" : "Submit"}
            </button>
            <button
              type="button"
              onClick={cancelRefine}
              disabled={busy}
              className="rounded-md border border-neutral-300 bg-white px-3 py-1.5 text-sm font-medium text-neutral-700 disabled:opacity-50"
            >
              Cancel
            </button>
          </div>
        </form>
      )}

      {err && <p className="mt-2 text-xs text-red-700">{err}</p>}
    </div>
  );
}
