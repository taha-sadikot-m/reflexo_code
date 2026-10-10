"use client";

import { FormEvent, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, canMutate } from "@/lib/api";
import { useApp } from "@/components/Providers";
import { EmptyState, ErrorState, LoadingState } from "@/components/States";
import { Icon } from "@/components/Icon";
import { Button, Dialog, Drawer, Field, useToast } from "@/components/ui";
import { knowledgeSchema, validationErrors, type FieldErrors } from "@/lib/schemas";
import { queryKeys } from "@/lib/queries/keys";

type Entry = {
  id: string;
  question?: string;
  answer: string;
  category: string;
  source?: string;
  confidence?: number;
  usage_count?: number;
  updated_at?: string;
  created_at?: string;
  rrf_score?: number;
};
type Gap = {
  id: string;
  question?: string;
  context?: string;
  status?: string;
  occurrence_count?: number;
  last_occurred_at?: string;
  answer?: string;
  conversation_id?: string;
};
type GapFilter = "open" | "escalated" | "resolved" | "ignored" | "all";

const CATEGORIES = ["general", "product", "policy", "hours"] as const;

function statusBadge(status?: string) {
  if (status === "resolved") return "badge-pos";
  if (status === "escalated") return "badge-warn";
  if (status === "ignored") return "badge-outline";
  return "badge-warn";
}

export default function KnowledgePage() {
  const { businessId, admin } = useApp();
  const mutate = canMutate(admin?.role);
  const qc = useQueryClient();
  const [tab, setTab] = useState<"entries" | "gaps" | "search">("entries");
  const [filter, setFilter] = useState("");
  const [qtext, setQtext] = useState("");
  const [gapFilter, setGapFilter] = useState<GapFilter>("open");
  const [showAdd, setShowAdd] = useState(false);
  const [editing, setEditing] = useState<Entry | null>(null);
  const [selected, setSelected] = useState<Entry | null>(null);
  const [resolving, setResolving] = useState<Gap | null>(null);
  const [formErrors, setFormErrors] = useState<FieldErrors>({});
  const { toast } = useToast();

  const list = useQuery({
    queryKey: queryKeys.knowledge(businessId),
    enabled: !!businessId,
    queryFn: () => api<{ items: Entry[]; total: number }>(`/api/admin/knowledge/${businessId}`),
  });
  const gaps = useQuery({
    queryKey: [...queryKeys.gaps(businessId), gapFilter],
    enabled: !!businessId,
    queryFn: () =>
      api<{ items: Gap[] }>(`/api/admin/conversations/${businessId}/gaps?status=${gapFilter}`),
  });

  function invalidateKb() {
    return Promise.all([
      qc.invalidateQueries({ queryKey: queryKeys.knowledge(businessId) }),
      qc.invalidateQueries({ queryKey: queryKeys.gaps(businessId) }),
    ]);
  }

  const add = useMutation({
    mutationFn: (body: { question?: string; answer: string; category: string }) =>
      api(`/api/admin/knowledge/${businessId}`, { method: "POST", body: JSON.stringify(body) }),
    onSuccess: async () => {
      setShowAdd(false);
      setFormErrors({});
      toast({ tone: "pos", title: "Knowledge entry added", body: "The answer is now available to semantic search." });
      await invalidateKb();
    },
  });
  const update = useMutation({
    mutationFn: (body: { id: string; question?: string; answer: string; category: string }) =>
      api(`/api/admin/knowledge/${businessId}/${body.id}`, {
        method: "PUT",
        body: JSON.stringify({ question: body.question, answer: body.answer, category: body.category }),
      }),
    onSuccess: async () => {
      toast({ tone: "pos", title: "Entry updated" });
      setEditing(null);
      setSelected(null);
      await invalidateKb();
    },
  });
  const deactivate = useMutation({
    mutationFn: (id: string) => api(`/api/admin/knowledge/${businessId}/${id}`, { method: "DELETE" }),
    onSuccess: async () => {
      toast({ tone: "warn", title: "Entry deactivated" });
      setSelected(null);
      await invalidateKb();
    },
  });
  const resolve = useMutation({
    mutationFn: (body: { id: string; answer: string; category: string }) =>
      api(`/api/admin/knowledge/${businessId}/gaps/${body.id}/resolve`, {
        method: "POST",
        body: JSON.stringify({ answer: body.answer, category: body.category }),
      }),
    onSuccess: async () => {
      toast({ tone: "pos", title: "Gap resolved", body: "A knowledge entry was created from this answer." });
      setResolving(null);
      setTab("entries");
      await invalidateKb();
    },
  });
  const ignore = useMutation({
    mutationFn: (id: string) =>
      api(`/api/admin/knowledge/${businessId}/gaps/${id}/ignore`, { method: "POST" }),
    onSuccess: async () => {
      toast({ tone: "warn", title: "Gap ignored" });
      await invalidateKb();
    },
  });
  const search = useMutation({
    mutationFn: (query: string) =>
      api<{ results: Entry[] }>(`/api/admin/knowledge/${businessId}/search-test`, {
        method: "POST",
        body: JSON.stringify({ query }),
      }),
  });

  const visible = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    return (list.data?.items ?? []).filter((entry) =>
      `${entry.question ?? ""} ${entry.answer} ${entry.category}`.toLowerCase().includes(needle),
    );
  }, [filter, list.data?.items]);

  if (!businessId || list.isLoading) return <LoadingState label="Reading the knowledge loom…" />;
  if (list.isError) return <ErrorState message={(list.error as Error).message} />;
  const items = list.data?.items ?? [];
  const gapItems = gaps.data?.items ?? [];

  function parseEntryForm(form: HTMLFormElement) {
    const data = new FormData(form);
    return knowledgeSchema.safeParse({
      question: String(data.get("question") ?? "").trim(),
      answer: String(data.get("answer") ?? "").trim(),
      category: String(data.get("category") ?? "general"),
    });
  }

  function submitEntry(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const parsed = parseEntryForm(event.currentTarget);
    if (!parsed.success) {
      setFormErrors(validationErrors(parsed.error));
      return;
    }
    setFormErrors({});
    add.mutate(parsed.data);
  }

  function submitEdit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!editing) return;
    const parsed = parseEntryForm(event.currentTarget);
    if (!parsed.success) {
      setFormErrors(validationErrors(parsed.error));
      return;
    }
    setFormErrors({});
    update.mutate({ id: editing.id, ...parsed.data });
  }

  function submitResolve(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!resolving) return;
    const data = new FormData(event.currentTarget);
    const answer = String(data.get("answer") ?? "").trim();
    const category = String(data.get("category") ?? "general");
    if (answer.length < 2) {
      setFormErrors({ answer: "Answer is required" });
      return;
    }
    setFormErrors({});
    resolve.mutate({ id: resolving.id, answer, category });
  }

  return (
    <div>
      <header className="page-head">
        <div>
          <p className="eyebrow">Memory</p>
          <h1>Knowledge base</h1>
          <p className="page-sub">Verified answers the agent can retrieve, and questions it could not.</p>
        </div>
        {mutate && (
          <Button variant="primary" onClick={() => setShowAdd(true)}>
            <Icon name="plus" className="ic ic-sm" /> Add entry
          </Button>
        )}
      </header>

      <div className="tabs" role="tablist" aria-label="Knowledge sections">
        <button role="tab" aria-selected={tab === "entries"} onClick={() => setTab("entries")}>
          Entries <span className="tab-count">{list.data?.total ?? items.length}</span>
        </button>
        <button role="tab" aria-selected={tab === "gaps"} onClick={() => setTab("gaps")}>
          Knowledge gaps <span className="tab-count">{gapItems.length}</span>
        </button>
        <button role="tab" aria-selected={tab === "search"} onClick={() => setTab("search")}>
          Semantic search test
        </button>
      </div>

      <Dialog
        open={showAdd && mutate}
        title="New knowledge entry"
        description="Question and answer are embedded after validation."
        onClose={() => {
          setShowAdd(false);
          setFormErrors({});
        }}
        footer={
          <>
            <Button onClick={() => setShowAdd(false)}>Cancel</Button>
            <Button variant="primary" loading={add.isPending} form="knowledge-entry-form" type="submit">
              Save entry
            </Button>
          </>
        }
      >
        <form id="knowledge-entry-form" className="form-grid" onSubmit={submitEntry}>
          <Field label="Category">
            <select className="select" name="category">
              {CATEGORIES.map((cat) => (
                <option key={cat} value={cat}>
                  {cat}
                </option>
              ))}
            </select>
          </Field>
          <div className="full">
            <Field label="Customer question" error={formErrors.question}>
              <input className="input" name="question" placeholder="What might a customer ask?" />
            </Field>
          </div>
          <div className="full">
            <Field label="Verified answer" required error={formErrors.answer}>
              <textarea className="textarea" name="answer" placeholder="Write the answer the agent may use" />
            </Field>
          </div>
          {add.isError && <p className="err full">{(add.error as Error).message}</p>}
        </form>
      </Dialog>

      <Dialog
        open={!!editing}
        title="Edit knowledge entry"
        onClose={() => {
          setEditing(null);
          setFormErrors({});
        }}
        footer={
          <>
            <Button onClick={() => setEditing(null)}>Cancel</Button>
            <Button variant="primary" loading={update.isPending} form="knowledge-edit-form" type="submit">
              Save changes
            </Button>
          </>
        }
      >
        {editing && (
          <form id="knowledge-edit-form" className="form-grid" onSubmit={submitEdit}>
            <Field label="Category">
              <select className="select" name="category" defaultValue={editing.category}>
                {CATEGORIES.map((cat) => (
                  <option key={cat} value={cat}>
                    {cat}
                  </option>
                ))}
              </select>
            </Field>
            <div className="full">
              <Field label="Customer question" error={formErrors.question}>
                <input className="input" name="question" defaultValue={editing.question ?? ""} />
              </Field>
            </div>
            <div className="full">
              <Field label="Verified answer" required error={formErrors.answer}>
                <textarea className="textarea" name="answer" defaultValue={editing.answer} />
              </Field>
            </div>
            {update.isError && <p className="err full">{(update.error as Error).message}</p>}
          </form>
        )}
      </Dialog>

      <Dialog
        open={!!resolving}
        title="Resolve knowledge gap"
        description={resolving?.question}
        onClose={() => {
          setResolving(null);
          setFormErrors({});
        }}
        footer={
          <>
            <Button onClick={() => setResolving(null)}>Cancel</Button>
            <Button variant="primary" loading={resolve.isPending} form="gap-resolve-form" type="submit">
              Save to knowledge base
            </Button>
          </>
        }
      >
        {resolving && (
          <form id="gap-resolve-form" className="form-grid" onSubmit={submitResolve}>
            {resolving.context && <p className="muted full">{resolving.context}</p>}
            <Field label="Category">
              <select className="select" name="category" defaultValue="general">
                {CATEGORIES.map((cat) => (
                  <option key={cat} value={cat}>
                    {cat}
                  </option>
                ))}
              </select>
            </Field>
            <div className="full">
              <Field label="Verified answer" required error={formErrors.answer}>
                <textarea className="textarea" name="answer" placeholder="The answer the agent should use next time" />
              </Field>
            </div>
            {resolve.isError && <p className="err full">{(resolve.error as Error).message}</p>}
          </form>
        )}
      </Dialog>

      {tab === "entries" && (
        <>
          <div className="tbl-toolbar mt5">
            <span className="input-affix search-field">
              <Icon name="search" className="ic ic-sm" />
              <input
                className="input"
                value={filter}
                onChange={(event) => setFilter(event.target.value)}
                placeholder="Search questions and answers"
              />
            </span>
            <span className="badge badge-outline">{visible.length} shown</span>
          </div>
          {items.length === 0 ? (
            <EmptyState title="Empty loom" body="Add an entry, then search-test it to confirm retrieval." />
          ) : visible.length === 0 ? (
            <EmptyState title="No matching entries" body="Try a broader question, answer, or category." />
          ) : (
            <div className="stack gap3 mt4">
              {visible.map((entry) => (
                <button
                  type="button"
                  key={entry.id}
                  className="card card-pad kb-entry"
                  onClick={() => setSelected(entry)}
                >
                  <div className="row-flex between gap3">
                    <b>{entry.question || "Untitled question"}</b>
                    <span className="badge badge-outline">{entry.category}</span>
                  </div>
                  <p className="muted kb-answer-preview mt2">{entry.answer}</p>
                  <div className="row-flex gap3 mt3 faint">
                    <span>Used {entry.usage_count ?? 0}</span>
                    <span>{entry.source ?? "admin"}</span>
                    <span>{entry.updated_at ? new Date(entry.updated_at).toLocaleDateString() : ""}</span>
                  </div>
                </button>
              ))}
            </div>
          )}
        </>
      )}

      <Drawer
        open={!!selected}
        title={selected?.question || "Knowledge entry"}
        description={selected ? `${selected.category} · ${selected.source ?? "admin"}` : undefined}
        onClose={() => setSelected(null)}
        footer={
          mutate && selected ? (
            <>
              <Button
                variant="danger"
                loading={deactivate.isPending}
                onClick={() => deactivate.mutate(selected.id)}
              >
                Deactivate
              </Button>
              <Button
                variant="primary"
                onClick={() => {
                  setEditing(selected);
                }}
              >
                Edit
              </Button>
            </>
          ) : undefined
        }
      >
        {selected && (
          <div className="stack gap4">
            <div>
              <div className="eyebrow">Question</div>
              <p className="mt2">{selected.question || "—"}</p>
            </div>
            <div>
              <div className="eyebrow">Answer</div>
              <p className="mt2 kb-answer-full">{selected.answer}</p>
            </div>
            <div className="dl">
              <div>
                <dt>Used</dt>
                <dd>{selected.usage_count ?? 0}</dd>
              </div>
              <div>
                <dt>Confidence</dt>
                <dd>{selected.confidence == null ? "—" : `${Math.round(selected.confidence * 100)}%`}</dd>
              </div>
              <div>
                <dt>Updated</dt>
                <dd>{selected.updated_at ? new Date(selected.updated_at).toLocaleString() : "—"}</dd>
              </div>
            </div>
          </div>
        )}
      </Drawer>

      {tab === "gaps" && (
        <div className="mt5">
          <div className="row-flex gap2 wrap mb4">
            {(["open", "escalated", "resolved", "ignored", "all"] as GapFilter[]).map((value) => (
              <button
                key={value}
                className={`btn ${gapFilter === value ? "btn-primary" : ""}`}
                onClick={() => setGapFilter(value)}
              >
                {value}
              </button>
            ))}
          </div>
          {gaps.isLoading ? (
            <LoadingState />
          ) : gaps.isError ? (
            <ErrorState message={(gaps.error as Error).message} />
          ) : gapItems.length === 0 ? (
            <EmptyState title="No gaps in this filter" body="Unanswered customer questions will appear here." />
          ) : (
            <div className="grid g2">
              {gapItems.map((gap) => {
                const status = gap.status || "open";
                const openish = status === "open" || status === "escalated";
                return (
                  <article className="card card-sunk card-pad" key={gap.id}>
                    <div className="row-flex between gap3">
                      <b>{gap.question || "Unlabelled question"}</b>
                      <span className={`badge ${statusBadge(status)}`}>{status}</span>
                    </div>
                    {gap.context && <p className="muted mt2">{gap.context}</p>}
                    <p className="hint mt3">
                      Asked {gap.occurrence_count ?? 1} time{(gap.occurrence_count ?? 1) === 1 ? "" : "s"}
                      {gap.last_occurred_at ? ` · last ${new Date(gap.last_occurred_at).toLocaleString()}` : ""}
                    </p>
                    {status === "resolved" && gap.answer && <p className="mt3">{gap.answer}</p>}
                    {mutate && openish && (
                      <div className="row-flex gap2 mt4">
                        <Button size="sm" variant="primary" onClick={() => setResolving(gap)}>
                          Resolve
                        </Button>
                        <Button size="sm" variant="ghost" loading={ignore.isPending} onClick={() => ignore.mutate(gap.id)}>
                          Ignore
                        </Button>
                      </div>
                    )}
                  </article>
                );
              })}
            </div>
          )}
        </div>
      )}

      {tab === "search" && (
        <section className="card mt5">
          <div className="card-head">
            <div>
              <h3>Semantic search test</h3>
              <p>Run a customer query exactly as the agent would.</p>
            </div>
          </div>
          <div className="card-body stack gap4">
            <form
              className="row-flex gap2 wrap"
              onSubmit={(event) => {
                event.preventDefault();
                if (qtext.trim()) search.mutate(qtext.trim());
              }}
            >
              <input
                className="input grow"
                value={qtext}
                onChange={(event) => setQtext(event.target.value)}
                required
                placeholder="Try a customer question"
              />
              <button className={`btn btn-primary ${search.isPending ? "btn-loading" : ""}`} disabled={search.isPending}>
                Search
              </button>
            </form>
            {search.isError && <p className="err">{(search.error as Error).message}</p>}
            {(search.data?.results ?? []).map((result) => (
              <article className="card card-sunk card-pad" key={result.id}>
                <div className="row-flex between gap3">
                  <b>{result.question || "Match"}</b>
                  <span className="badge badge-brand mono">{(result.rrf_score ?? 0).toFixed(3)}</span>
                </div>
                <p className="muted mt2">{result.answer}</p>
              </article>
            ))}
            {search.isSuccess && search.data.results.length === 0 && (
              <EmptyState title="No semantic matches" body="This query may represent a knowledge gap." />
            )}
          </div>
        </section>
      )}
    </div>
  );
}
