"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { insforge, isConfigured } from "@/lib/insforge";

// Example preference terms users can click to append to their prompt. Each chip
// disappears once used so the suggestion list shrinks as the prompt is built up.
const EXAMPLE_TERMS = [
  "like-new condition",
  "prefer a trusted retailer",
  "ships within 3 days",
  "free 30-day returns",
  "under $700",
  "includes a warranty",
  "unlocked / carrier-free",
  "in stock now",
];

export default function Home() {
  const [q, setQ] = useState("");
  const [usedTerms, setUsedTerms] = useState<string[]>([]);
  const [comparable, setComparable] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const router = useRouter();

  function appendTerm(term: string) {
    setQ((prev) => {
      const trimmed = prev.trimEnd();
      if (!trimmed) return term;
      const sep = /[,.;]$/.test(trimmed) ? " " : ", ";
      return trimmed + sep + term;
    });
    setUsedTerms((prev) => [...prev, term]);
  }

  function buildQuery() {
    const base = q.trim();
    const note = comparable
      ? "Also find comparable items."
      : "Do not include comparable items; focus only on the exact item described.";
    return base ? `${base}\n\n${note}` : note;
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!q.trim() || submitting) return;
    setSubmitting(true);
    setErr(null);

    const query = buildQuery();

    if (!isConfigured()) {
      // Skeleton mode: route to the dashboard with a fake id so we can still
      // demo the UI shell before Insforge is wired up.
      router.push(`/intent/${crypto.randomUUID()}?q=${encodeURIComponent(query)}`);
      return;
    }

    try {
      const { data: sessions, error: sErr } = await insforge.database
        .from("sessions")
        .insert({})
        .select();
      if (sErr || !sessions?.[0]) throw sErr ?? new Error("session insert failed");

      const { data: intents, error: iErr } = await insforge.database
        .from("intents")
        .insert({
          session_id: sessions[0].id,
          raw_query: query,
          // Intake agent runs while status='eliciting'. It either asks one
          // clarifying question or flips to 'ready' if it has enough info.
          status: "eliciting",
          clarifying_turns: [{ role: "user", text: query }],
        })
        .select();
      if (iErr || !intents?.[0]) throw iErr ?? new Error("intent insert failed");

      router.push(`/intent/${intents[0].id}`);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setErr(msg);
      setSubmitting(false);
    }
  }

  const remainingTerms = EXAMPLE_TERMS.filter((t) => !usedTerms.includes(t));

  return (
    <main className="mx-auto max-w-2xl px-6 py-24">
      <h1 className="text-3xl font-semibold tracking-tight">
        Personal shopper agent
      </h1>
      <p className="mt-2 text-sm text-neutral-500">
        Tell me what you&apos;re shopping for. I&apos;ll dispatch a team of agents to
        research it.
      </p>

      <form onSubmit={submit} className="mt-8 space-y-3">
        <textarea
          value={q}
          onChange={(e) => setQ(e.target.value)}
          rows={4}
          placeholder="e.g. used iPhone 15 Pro 256GB, prefer unlocked, under $700, 90%+ battery"
          className="w-full rounded-lg border border-neutral-300 bg-white px-4 py-3 text-sm text-neutral-900 placeholder:text-neutral-400 shadow-sm outline-none focus:border-neutral-900"
        />

        {remainingTerms.length > 0 && (
          <div>
            <p className="text-xs font-medium text-neutral-500">
              Add a preference (click to append):
            </p>
            <div className="mt-2 flex flex-wrap gap-2">
              {remainingTerms.map((term) => (
                <button
                  key={term}
                  type="button"
                  onClick={() => appendTerm(term)}
                  className="rounded-full border border-neutral-300 bg-white px-3 py-1 text-xs text-neutral-700 shadow-sm transition hover:border-neutral-900 hover:bg-neutral-900 hover:text-white"
                >
                  + {term}
                </button>
              ))}
            </div>
          </div>
        )}

        <label className="flex items-center gap-2 text-sm text-neutral-700 select-none">
          <input
            type="checkbox"
            checked={comparable}
            onChange={(e) => setComparable(e.target.checked)}
            className="h-4 w-4 rounded border-neutral-300 text-neutral-900 focus:ring-neutral-900"
          />
          Find comparable items
        </label>

        <button
          type="submit"
          disabled={submitting || !q.trim()}
          className="rounded-lg bg-neutral-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
        >
          {submitting ? "Starting…" : "Start shopping"}
        </button>
      </form>

      {!isConfigured() && (
        <p className="mt-8 rounded-md bg-amber-50 px-3 py-2 text-xs text-amber-900">
          Insforge env vars not set — see README. Submissions will route the UI
          but no data persists yet.
        </p>
      )}
      {err && (
        <p className="mt-4 rounded-md bg-red-50 px-3 py-2 text-xs text-red-800">
          {err}
        </p>
      )}
    </main>
  );
}
