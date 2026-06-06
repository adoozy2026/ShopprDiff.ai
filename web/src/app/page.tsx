"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { insforge, isConfigured } from "@/lib/insforge";

// Clicking this chip opens an inline budget input instead of appending a fixed
// phrase, so the appended text can include the amount the user enters.
const BUDGET_TERM = "within my budget";

// Example preference terms users can click to append to their prompt, grouped by
// category. Each chip disappears once used so the suggestion list shrinks as the
// prompt is built up.
const EXAMPLE_GROUPS: { label: string; terms: string[] }[] = [
  {
    label: "Condition",
    terms: [
      "brand new / sealed",
      "like-new condition",
      "open-box",
      "certified refurbished",
      "gently used",
    ],
  },
  {
    label: "Price & deals",
    terms: [
      BUDGET_TERM,
      "price-match guarantee",
      "financing available",
    ],
  },
  {
    label: "Seller",
    terms: [
      "prefer a trusted retailer",
      "highly rated seller (4.5★+)",
      "sold/shipped by the brand",
    ],
  },
  {
    label: "Shipping & pickup",
    terms: [
      "free shipping",
      "ships within 3 days",
      "local pickup available",
    ],
  },
  {
    label: "Returns & warranty",
    terms: [
      "free 30-day returns",
      "includes a warranty",
    ],
  },
  {
    label: "Specifics",
    terms: [
      "latest model",
      "in stock now",
      "unlocked / carrier-free",
      "original packaging & accessories",
      "energy efficient",
      "eco-friendly / sustainable",
    ],
  },
  {
    label: "Match scope",
    terms: [
      "exact item only",
      "exact brand only",
      "include comparable products",
      "show similar alternatives",
      "any brand is fine",
    ],
  },
];

export default function Home() {
  const [q, setQ] = useState("");
  const [usedTerms, setUsedTerms] = useState<string[]>([]);
  const [budgetOpen, setBudgetOpen] = useState(false);
  const [budget, setBudget] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const router = useRouter();

  // Append `text` to the prompt and mark `markTerm` as used so its chip hides.
  function appendText(text: string, markTerm: string) {
    setQ((prev) => {
      const trimmed = prev.trimEnd();
      if (!trimmed) return text;
      const sep = /[,.;]$/.test(trimmed) ? " " : ", ";
      return trimmed + sep + text;
    });
    setUsedTerms((prev) => [...prev, markTerm]);
  }

  function appendTerm(term: string) {
    appendText(term, term);
  }

  function confirmBudget() {
    const raw = budget.trim();
    const amount = raw.replace(/^\$+/, "").trim();
    appendText(
      amount ? `within my budget of $${amount}` : BUDGET_TERM,
      BUDGET_TERM,
    );
    setBudget("");
    setBudgetOpen(false);
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!q.trim() || submitting) return;
    setSubmitting(true);
    setErr(null);

    const query = q.trim();

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

  const hasRemainingTerms = EXAMPLE_GROUPS.some((g) =>
    g.terms.some((t) => !usedTerms.includes(t)),
  );

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

        <button
          type="submit"
          disabled={submitting || !q.trim()}
          className="w-full rounded-lg bg-neutral-900 px-4 py-3 text-sm font-medium text-white shadow-sm transition hover:bg-neutral-800 disabled:opacity-50"
        >
          {submitting ? "Starting…" : "Start shopping"}
        </button>

        {hasRemainingTerms && (
          <div className="space-y-3">
            <div>
              <p className="text-xs font-medium text-neutral-500">
                Add a preference (click to append):
              </p>
              <p className="mt-0.5 text-xs text-neutral-400">
                These are all optional shortcuts for common criteria — feel free
                to skip them and type anything that matters to you directly into
                the prompt above.
              </p>
            </div>
            {EXAMPLE_GROUPS.map((group) => {
              const terms = group.terms.filter(
                (t) =>
                  !usedTerms.includes(t) && !(t === BUDGET_TERM && budgetOpen),
              );
              const showBudgetInput =
                group.terms.includes(BUDGET_TERM) && budgetOpen;
              if (terms.length === 0 && !showBudgetInput) return null;
              return (
                <div key={group.label}>
                  <p className="text-[11px] font-medium uppercase tracking-wide text-neutral-400">
                    {group.label}
                  </p>
                  <div className="mt-1.5 flex flex-wrap gap-2">
                    {terms.map((term) => (
                      <button
                        key={term}
                        type="button"
                        onClick={() =>
                          term === BUDGET_TERM
                            ? setBudgetOpen(true)
                            : appendTerm(term)
                        }
                        className="rounded-full border border-neutral-300 bg-white px-3 py-1 text-xs text-neutral-700 shadow-sm transition hover:border-neutral-900 hover:bg-neutral-900 hover:text-white"
                      >
                        + {term}
                      </button>
                    ))}
                  </div>
                  {showBudgetInput && (
                    <div className="mt-2 flex flex-wrap items-center gap-2">
                      <span className="text-xs text-neutral-500">
                        What&apos;s your budget?
                      </span>
                      <div className="flex items-center rounded-lg border border-neutral-300 bg-white px-2 shadow-sm focus-within:border-neutral-900">
                        <span className="text-sm text-neutral-500">$</span>
                        <input
                          autoFocus
                          value={budget}
                          onChange={(e) => setBudget(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === "Enter") {
                              e.preventDefault();
                              confirmBudget();
                            }
                          }}
                          inputMode="decimal"
                          placeholder="700"
                          className="w-24 bg-transparent px-1 py-1 text-sm text-neutral-900 placeholder:text-neutral-400 outline-none"
                        />
                      </div>
                      <button
                        type="button"
                        onClick={confirmBudget}
                        disabled={!budget.trim()}
                        className="rounded-full bg-neutral-900 px-3 py-1 text-xs font-medium text-white disabled:opacity-50"
                      >
                        Add
                      </button>
                      <button
                        type="button"
                        onClick={() => {
                          setBudget("");
                          setBudgetOpen(false);
                        }}
                        className="rounded-full border border-neutral-300 bg-white px-3 py-1 text-xs text-neutral-600 transition hover:border-neutral-900"
                      >
                        Cancel
                      </button>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
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
