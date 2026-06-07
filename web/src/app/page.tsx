"use client";

import { Fragment, useState } from "react";
import { useRouter } from "next/navigation";
import { insforge, isConfigured } from "@/lib/insforge";

// A field inside a term's secondary-input prompt.
type PromptField = {
  key: string;
  placeholder: string;
  prefix?: string; // e.g. "$" shown before the input
  before?: string; // e.g. "to" shown between two fields
};

// Some terms are ambiguous without a value (a budget, a rating, a number of
// days, etc). Those carry a `prompt` describing the inline inputs to collect and
// a `build` that turns the entered values into the text appended to the query.
type PromptSpec = {
  title: string;
  fields: PromptField[];
  hint?: string;
  // When true the chip can be confirmed with every field blank (the value is an
  // optional refinement); otherwise at least one field must be filled.
  optional?: boolean;
  build: (values: Record<string, string>) => string;
};

// A term is either a plain phrase that appends as-is, or one that opens a prompt.
type Term = string | { label: string; prompt: PromptSpec };

const termLabel = (t: Term): string => (typeof t === "string" ? t : t.label);
const termPrompt = (t: Term): PromptSpec | null =>
  typeof t === "string" ? null : t.prompt;

// Strip a leading "$" and surrounding whitespace from a money field value.
const money = (v: string | undefined) =>
  (v ?? "").trim().replace(/^\$+/, "").trim();
const val = (v: string | undefined) => (v ?? "").trim();

// Example preference terms users can click to append to their prompt, grouped by
// category. Each chip disappears once used so the suggestion list shrinks as the
// prompt is built up. Terms with a `prompt` collect extra info before appending.
const EXAMPLE_GROUPS: { label: string; terms: Term[] }[] = [
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
      {
        label: "within my budget",
        prompt: {
          title: "Set your budget range",
          fields: [
            { key: "min", prefix: "$", placeholder: "min" },
            { key: "max", prefix: "$", placeholder: "max", before: "to" },
          ],
          hint: "Enter a min, a max, or both — leave one blank for an open-ended range.",
          build: (v) => {
            const min = money(v.min);
            const max = money(v.max);
            if (min && max) return `within my budget of $${min}–$${max}`;
            if (max) return `within my budget of up to $${max}`;
            if (min) return `within my budget of at least $${min}`;
            return "within my budget";
          },
        },
      },
      "price-match guarantee",
      "financing available",
    ],
  },
  {
    label: "Seller",
    terms: [
      {
        label: "prefer a trusted retailer",
        prompt: {
          title: "Preferred retailer",
          optional: true,
          fields: [{ key: "retailer", placeholder: "e.g. Amazon, Best Buy" }],
          hint: "Name a store, or leave blank for any trusted retailer.",
          build: (v) =>
            val(v.retailer)
              ? `prefer to buy from ${val(v.retailer)}`
              : "prefer a trusted retailer",
        },
      },
      {
        label: "highly rated seller",
        prompt: {
          title: "Minimum seller rating",
          fields: [{ key: "rating", placeholder: "4.5" }],
          hint: "Minimum star rating out of 5.",
          build: (v) =>
            val(v.rating)
              ? `highly rated seller (${val(v.rating)}★+)`
              : "highly rated seller",
        },
      },
      "sold/shipped by the brand",
    ],
  },
  {
    label: "Shipping & pickup",
    terms: [
      "free shipping",
      {
        label: "fast shipping",
        prompt: {
          title: "Maximum shipping time",
          fields: [{ key: "days", placeholder: "3" }],
          hint: "Greatest number of days until it ships.",
          build: (v) =>
            val(v.days) ? `ships within ${val(v.days)} days` : "ships quickly",
        },
      },
      {
        label: "local pickup available",
        prompt: {
          title: "Local pickup",
          optional: true,
          fields: [{ key: "location", placeholder: "city or ZIP" }],
          hint: "Add a location, or leave blank for any nearby pickup.",
          build: (v) =>
            val(v.location)
              ? `local pickup available near ${val(v.location)}`
              : "local pickup available",
        },
      },
    ],
  },
  {
    label: "Returns & warranty",
    terms: [
      {
        label: "free returns",
        prompt: {
          title: "Return window",
          fields: [{ key: "days", placeholder: "30" }],
          hint: "Minimum number of days to return for free.",
          build: (v) =>
            val(v.days) ? `free ${val(v.days)}-day returns` : "free returns",
        },
      },
      {
        label: "includes a warranty",
        prompt: {
          title: "Warranty length",
          optional: true,
          fields: [{ key: "length", placeholder: "e.g. 1 year" }],
          hint: "Minimum warranty length, or leave blank for any warranty.",
          build: (v) =>
            val(v.length)
              ? `includes at least a ${val(v.length)} warranty`
              : "includes a warranty",
        },
      },
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
  // Label of the term whose secondary-input prompt is open, plus its field values.
  const [openTerm, setOpenTerm] = useState<string | null>(null);
  const [fieldValues, setFieldValues] = useState<Record<string, string>>({});
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

  function openPrompt(label: string) {
    setOpenTerm(label);
    setFieldValues({});
  }

  function closePrompt() {
    setOpenTerm(null);
    setFieldValues({});
  }

  function confirmPrompt(spec: PromptSpec, label: string) {
    appendText(spec.build(fieldValues) || label, label);
    closePrompt();
  }

  // Whether the open prompt has enough input to be submitted.
  function canSubmit(spec: PromptSpec) {
    if (spec.optional) return true;
    return spec.fields.some((f) => (fieldValues[f.key] ?? "").trim() !== "");
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
    g.terms.some((t) => !usedTerms.includes(termLabel(t))),
  );

  return (
    <main className="mx-auto max-w-2xl px-6 py-24">
      <h1 className="text-3xl font-semibold tracking-tight">
        Hi, I&apos;m your personal Shoppr Jeff{" "}
        <span className="inline-block animate-[wave_1.8s_ease-in-out_infinite] origin-[70%_70%]">👋</span>
      </h1>
      <p className="mt-2 text-sm text-neutral-500">
        Tell me what you&apos;re looking for and I&apos;ll get to work —
        scouring the web to research your product, comparing prices across
        dozens of retailers to find the best deal, and recommending the most
        trustworthy sellers so you can buy with confidence.
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
                  !usedTerms.includes(termLabel(t)) &&
                  termLabel(t) !== openTerm,
              );
              const openInThisGroup = group.terms.find(
                (t) => termLabel(t) === openTerm,
              );
              const openSpec = openInThisGroup
                ? termPrompt(openInThisGroup)
                : null;
              if (terms.length === 0 && !openSpec) return null;
              return (
                <div key={group.label}>
                  <p className="text-[11px] font-medium uppercase tracking-wide text-neutral-400">
                    {group.label}
                  </p>
                  <div className="mt-1.5 flex flex-wrap gap-2">
                    {terms.map((term) => {
                      const label = termLabel(term);
                      const spec = termPrompt(term);
                      return (
                        <button
                          key={label}
                          type="button"
                          onClick={() =>
                            spec ? openPrompt(label) : appendTerm(label)
                          }
                          className="rounded-full border border-neutral-300 bg-white px-3 py-1 text-xs text-neutral-700 shadow-sm transition hover:border-neutral-900 hover:bg-neutral-900 hover:text-white"
                        >
                          + {label}
                          {spec ? "…" : ""}
                        </button>
                      );
                    })}
                  </div>
                  {openSpec && openTerm && (
                    <div className="mt-2 rounded-lg border-2 border-indigo-400 bg-indigo-50 p-3 shadow-sm">
                      <p className="text-xs font-semibold uppercase tracking-wide text-indigo-700">
                        {openSpec.title}
                      </p>
                      <div className="mt-2 flex flex-wrap items-center gap-2">
                        {openSpec.fields.map((f, i) => (
                          <Fragment key={f.key}>
                            {f.before && (
                              <span className="text-sm font-medium text-indigo-600">
                                {f.before}
                              </span>
                            )}
                            <div className="flex items-center rounded-md border border-indigo-300 bg-white px-2 shadow-sm focus-within:border-indigo-600 focus-within:ring-1 focus-within:ring-indigo-400">
                              {f.prefix && (
                                <span className="text-sm font-medium text-indigo-500">
                                  {f.prefix}
                                </span>
                              )}
                              <input
                                autoFocus={i === 0}
                                value={fieldValues[f.key] ?? ""}
                                onChange={(e) =>
                                  setFieldValues((prev) => ({
                                    ...prev,
                                    [f.key]: e.target.value,
                                  }))
                                }
                                onKeyDown={(e) => {
                                  if (e.key === "Enter") {
                                    e.preventDefault();
                                    if (canSubmit(openSpec))
                                      confirmPrompt(openSpec, openTerm);
                                  }
                                }}
                                placeholder={f.placeholder}
                                className="w-28 bg-transparent px-1 py-1 text-sm font-medium text-indigo-900 placeholder:font-normal placeholder:text-indigo-300 outline-none"
                              />
                            </div>
                          </Fragment>
                        ))}
                        <button
                          type="button"
                          onClick={() => confirmPrompt(openSpec, openTerm)}
                          disabled={!canSubmit(openSpec)}
                          className="rounded-md bg-indigo-600 px-3 py-1 text-xs font-medium text-white transition hover:bg-indigo-700 disabled:opacity-50"
                        >
                          Add
                        </button>
                        <button
                          type="button"
                          onClick={closePrompt}
                          className="rounded-md border border-indigo-300 bg-white px-3 py-1 text-xs font-medium text-indigo-600 transition hover:border-indigo-600"
                        >
                          Cancel
                        </button>
                      </div>
                      {openSpec.hint && (
                        <p className="mt-1.5 text-[11px] text-indigo-500">
                          {openSpec.hint}
                        </p>
                      )}
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
