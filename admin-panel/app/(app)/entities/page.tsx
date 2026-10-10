"use client";

import { FormEvent, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, canMutate } from "@/lib/api";
import { useApp } from "@/components/Providers";
import { EmptyState, ErrorState, LoadingState } from "@/components/States";
import { Icon } from "@/components/Icon";
import { Button, Dialog, Field, useToast } from "@/components/ui";
import { entitySchema, validationErrors, type FieldErrors } from "@/lib/schemas";

type EntityData = { price?: number | string; stock?: string | number; status?: string; [key: string]: unknown };
type Ent = { id: string; name: string; entity_type: string; description?: string; data?: EntityData; updated_at?: string };

export default function EntitiesPage() {
  const { businessId, admin } = useApp();
  const qc = useQueryClient();
  const [filter, setFilter] = useState("");
  const [type, setType] = useState("all");
  const [showAdd, setShowAdd] = useState(false);
  const [formErrors, setFormErrors] = useState<FieldErrors>({});
  const { toast } = useToast();
  const list = useQuery({
    queryKey: ["entities", businessId],
    enabled: !!businessId,
    queryFn: () => api<{ items: Ent[]; total: number }>(`/api/admin/entities/${businessId}`),
  });
  const add = useMutation({
    mutationFn: (body: { entity_type: string; name: string; description: string; data: EntityData }) =>
      api(`/api/admin/entities/${businessId}`, { method: "POST", body: JSON.stringify(body) }),
    onSuccess: async () => {
      setShowAdd(false);
      setFormErrors({});
      toast({ tone: "pos", title: "Entity created", body: "The structured record is ready for retrieval." });
      await qc.invalidateQueries({ queryKey: ["entities", businessId] });
    },
  });
  const items = useMemo(() => list.data?.items ?? [], [list.data?.items]);
  const types = useMemo(() => Array.from(new Set(items.map((item) => item.entity_type))), [items]);
  const visible = useMemo(() => items.filter((item) => {
    const matchesType = type === "all" || item.entity_type === type;
    return matchesType && `${item.name} ${item.description ?? ""}`.toLowerCase().includes(filter.toLowerCase());
  }), [filter, items, type]);

  if (!businessId || list.isLoading) return <LoadingState label="Cataloguing entities…" />;
  if (list.isError) return <ErrorState message={(list.error as Error).message} />;

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const parsed = entitySchema.safeParse({
      entity_type: String(data.get("entity_type")),
      name: String(data.get("name")).trim(),
      description: String(data.get("description") ?? "").trim(),
    });
    if (!parsed.success) {
      setFormErrors(validationErrors(parsed.error));
      return;
    }
    setFormErrors({});
    add.mutate({ ...parsed.data, description: parsed.data.description || "", data: {} });
  }

  return <div>
    <header className="page-head"><div><p className="eyebrow">Structured catalogue</p><h1>Entities</h1><p className="page-sub">Products, services, locations and events grounded in business data.</p></div>{canMutate(admin?.role) && <Button variant="primary" onClick={() => setShowAdd(true)}><Icon name="plus" className="ic ic-sm" />Add entity</Button>}</header>

    <Dialog
      open={showAdd && canMutate(admin?.role)}
      title="Add an entity"
      description="Create a searchable structured record."
      onClose={() => { setShowAdd(false); setFormErrors({}); }}
      footer={<><Button onClick={() => setShowAdd(false)}>Cancel</Button><Button variant="primary" loading={add.isPending} type="submit" form="entity-form">Create entity</Button></>}
    >
      <form id="entity-form" className="form-grid" onSubmit={submit}>
        <Field label="Type"><select className="select" name="entity_type" defaultValue="product"><option value="product">Product</option><option value="service">Service</option><option value="location">Location</option><option value="event">Event</option><option value="faq">FAQ</option></select></Field>
        <Field label="Name" required error={formErrors.name}><input className="input" name="name" /></Field>
        <div className="full"><Field label="Description" error={formErrors.description}><textarea className="textarea" name="description" /></Field></div>
        {add.isError && <p className="err full">{(add.error as Error).message}</p>}
      </form>
    </Dialog>

    <div className="tbl-toolbar mt5"><span className="input-affix search-field"><Icon name="search" className="ic ic-sm" /><input className="input" value={filter} onChange={(event) => setFilter(event.target.value)} placeholder="Search entities" /></span><div className="pilltabs" role="tablist"><button role="tab" aria-selected={type === "all"} onClick={() => setType("all")}>All {items.length}</button>{types.map((value) => <button key={value} role="tab" aria-selected={type === value} onClick={() => setType(value)}>{value} {items.filter((item) => item.entity_type === value).length}</button>)}</div></div>

    {items.length === 0 ? <EmptyState title="No entities" body="Products, services, locations and events will live here." /> : visible.length === 0 ? <EmptyState title="No matching entities" body="Change the type or search phrase." /> : <div className="grid g3">
      {visible.map((entity) => <article key={entity.id} className="card entity-card"><div className="ec-top"><span className="badge badge-outline">{entity.entity_type}</span><span className="badge badge-pos">{String(entity.data?.status ?? "active")}</span></div><div><h4>{entity.name}</h4><p className="muted" style={{ fontSize: "var(--text-xs)" }}>{entity.description || "No description supplied."}</p></div><hr className="thread-rule" /><div className="row-flex between gap3"><span className="ec-price">{entity.data?.price == null ? "On request" : `₹${entity.data.price}`}</span>{entity.data?.stock != null && <span className="badge badge-outline">{String(entity.data.stock)}</span>}</div><span className="hint">Updated {entity.updated_at ? new Date(entity.updated_at).toLocaleDateString() : "—"}</span></article>)}
    </div>}
  </div>;
}
