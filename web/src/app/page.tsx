"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { insforge, isConfigured } from "@/lib/insforge";

export default function Home() {
  const [q, setQ] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const router = useRouter();

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!q.trim() || submitting) return;
    setSubmitting(true);
    setErr(null);

    if (!isConfigured()) {
      // Skeleton mode: route to the dashboard with a fake id so we can still
      // demo the UI shell before Insforge is wired up.
      router.push(`/intent/${crypto.randomUUID()}?q=${encodeURIComponent(q)}`);
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
          raw_query: q,
          // Intake agent runs while status='eliciting'. It either asks one
          // clarifying question or flips to 'ready' if it has enough info.
          status: "eliciting",
          clarifying_turns: [{ role: "user", text: q }],
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
          className="w-full rounded-lg border border-neutral-300 bg-white px-4 py-3 text-sm shadow-sm outline-none focus:border-neutral-900"
        />
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
