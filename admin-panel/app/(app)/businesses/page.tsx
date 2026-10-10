"use client";

import Link from "next/link";
import { useState } from "react";
import { canMutate } from "@/lib/api";
import { useApp } from "@/components/Providers";
import { EmptyState, ErrorState, LoadingState } from "@/components/States";
import { ist } from "@/lib/format";
import { Icon } from "@/components/Icon";
import { useAdminQuery } from "@/lib/queries/hooks";
import { queryKeys } from "@/lib/queries/keys";
import { Button, Drawer } from "@/components/ui";

type Item = {
  id: string;
  name: string;
  plan: string;
  plan_status?: string;
  is_active: boolean;
  onboarding_completed?: boolean;
  owner_name?: string;
  owner_phone: string;
  message_limit?: number;
  messages_used?: number;
  vertical?: string;
  created_at?: string;
};

export default function BusinessesPage() {
  const { admin } = useApp();
  const [search, setSearch] = useState("");
  const [plan, setPlan] = useState("");
  const [status, setStatus] = useState("");
  const [selected, setSelected] = useState<Item | null>(null);
  const params = new URLSearchParams({ limit: "100" });
  if (plan) params.set("plan", plan);
  if (status) params.set("status", status);
  const q = useAdminQuery<{ items: Item[]; total: number }>(
    queryKeys.businesses(params.toString()),
    `/api/admin/businesses?${params}`,
  );
  const detail = useAdminQuery<Item & {
    channels?: { id: string; channel: string; channel_display_name?: string; is_active: boolean; error_message?: string }[];
    stats?: { messages_7d?: number; convos_7d?: number };
  }>(
    queryKeys.business(selected?.id || ""),
    `/api/admin/businesses/${selected?.id || ""}`,
    !!selected,
  );
  if (q.isLoading) return <LoadingState />;
  if (q.isError) return <ErrorState message={(q.error as Error).message} />;
  const allItems = q.data?.items || [];
  const needle = search.trim().toLocaleLowerCase();
  const items = needle
    ? allItems.filter((item) => [item.name, item.owner_name, item.owner_phone, item.vertical].some((value) => value?.toLocaleLowerCase().includes(needle)))
    : allItems;
  const active = allItems.filter((item) => item.is_active).length;
  const onboarding = allItems.filter((item) => item.is_active && !item.onboarding_completed).length;
  const used = allItems.reduce((sum, item) => sum + (item.messages_used || 0), 0);
  const quota = allItems.reduce((sum, item) => sum + (item.message_limit || 0), 0);
  return (
    <div>
      <header className="page-head">
        <div>
          <h1>Businesses</h1>
          <p className="page-sub">Every tenant, its owner, plan, quota and onboarding progress.</p>
        </div>
        <div className="page-head-actions">
          {canMutate(admin?.role) && admin?.role === "super_admin" && (
            <Link className="btn btn-primary" href="/businesses/new"><Icon name="plus" className="ic ic-sm" />New business</Link>
          )}
        </div>
      </header>
      <div className="grid g4">
        <article className="card stat card-selvedge"><div className="eyebrow">Total businesses</div><b>{q.data?.total || 0}</b><span className="stat-delta flat">Across all plans</span></article>
        <article className="card stat"><div className="eyebrow">Active</div><b>{active}</b><span className="stat-delta flat">{onboarding} onboarding</span></article>
        <article className="card stat"><div className="eyebrow">Messages used</div><b>{used.toLocaleString("en-IN")}</b><span className="stat-delta flat">Current billing counters</span></article>
        <article className="card stat"><div className="eyebrow">Quota capacity</div><b>{quota ? `${Math.round((used / quota) * 100)}%` : "—"}</b><span className="stat-delta flat">{quota ? `${quota.toLocaleString("en-IN")} messages` : "No limits configured"}</span></article>
      </div>
      <div className="tbl-toolbar mt5">
        <span className="input-affix search-field">
          <Icon name="search" className="ic ic-sm" />
          <input className="input" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search name, owner or vertical" />
        </span>
        <select className="select" aria-label="Filter by plan" value={plan} onChange={(event) => setPlan(event.target.value)} style={{ width: 140 }}>
          <option value="">All plans</option><option value="free">Free</option><option value="starter">Starter</option><option value="growth">Growth</option><option value="premium">Premium</option>
        </select>
        <select className="select" aria-label="Filter by status" value={status} onChange={(event) => setStatus(event.target.value)} style={{ width: 150 }}>
          <option value="">All statuses</option><option value="active">Active</option><option value="onboarding">Onboarding</option><option value="inactive">Inactive</option>
        </select>
        {(search || plan || status) && <button className="btn btn-ghost" onClick={() => { setSearch(""); setPlan(""); setStatus(""); }}>Clear filters</button>}
      </div>
      {items.length === 0 ? (
        <EmptyState title="No businesses" body="Create a tenant to start weaving conversations." />
      ) : (
        <div className="tablewrap">
          <table className="tbl">
            <thead><tr><th>Business</th><th>Owner</th><th>Plan</th><th className="cell-num">Messages</th><th>Quota</th><th>Channels</th><th>Status</th><th /></tr></thead>
            <tbody>
              {items.map((business) => {
                const quotaPercent = business.message_limit ? Math.min(100, Math.round(((business.messages_used || 0) / business.message_limit) * 100)) : 0;
                const state = !business.is_active ? "Inactive" : !business.onboarding_completed ? "Onboarding" : "Active";
                return (
                  <tr key={business.id}>
                    <td className="cell-strong"><div className="row-flex gap2"><span className="avatar avatar-sm">{business.name.slice(0, 2).toUpperCase()}</span><div><Link href={`/businesses/${business.id}`}>{business.name}</Link><div className="faint" style={{ fontSize: "var(--text-2xs)" }}>{business.vertical || "General"}</div></div></div></td>
                    <td>{business.owner_name || business.owner_phone}<div className="faint mono" style={{ fontSize: "var(--text-2xs)" }}>{business.owner_name ? business.owner_phone : ist(business.created_at)}</div></td>
                    <td><span className="badge badge-outline">{business.plan}</span></td>
                    <td className="cell-num mono">{(business.messages_used || 0).toLocaleString("en-IN")}</td>
                    <td style={{ minWidth: 130 }}><div className={`bar ${quotaPercent > 85 ? "bar-neg" : quotaPercent > 70 ? "bar-accent" : ""}`}><i style={{ width: `${quotaPercent}%` }} /></div><span className="hint">{business.message_limit ? `${quotaPercent}% of ${business.message_limit.toLocaleString("en-IN")}` : "Unlimited"}</span></td>
                    <td><span className="badge badge-outline faint">View detail</span></td>
                    <td><span className={`badge ${state === "Active" ? "badge-pos" : state === "Onboarding" ? "badge-accent" : "badge-neg"}`}>{state}</span></td>
                    <td className="cell-actions"><Button size="sm" onClick={() => setSelected(business)}>Inspect</Button></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <div className="tbl-foot"><span>Showing {items.length} of {q.data?.total || 0} businesses</span><span className="mono">API limit 100</span></div>
        </div>
      )}
      <Drawer
        open={!!selected}
        title={selected?.name || "Business"}
        description={selected ? `${selected.plan} · ${selected.vertical || "general"}` : undefined}
        onClose={() => setSelected(null)}
        footer={selected && <><Button onClick={() => setSelected(null)}>Close</Button><Link className="btn btn-primary" href={`/businesses/${selected.id}`}>Open full profile</Link></>}
      >
        {detail.isLoading ? <LoadingState label="Loading business profile…" /> : detail.isError ? <ErrorState message={(detail.error as Error).message} onRetry={() => detail.refetch()} /> : detail.data && (
          <div className="stack gap5">
            <div className="grid g2">
              <div className="kv"><span className="eyebrow">Owner</span><b>{detail.data.owner_name || "Not set"}</b><span className="mono hint">{detail.data.owner_phone}</span></div>
              <div className="kv"><span className="eyebrow">Status</span><span className={`badge ${detail.data.is_active ? "badge-pos" : "badge-neg"}`}>{detail.data.is_active ? "Active" : "Inactive"}</span></div>
              <div className="kv"><span className="eyebrow">Messages · 7d</span><b className="num">{detail.data.stats?.messages_7d || 0}</b></div>
              <div className="kv"><span className="eyebrow">Conversations · 7d</span><b className="num">{detail.data.stats?.convos_7d || 0}</b></div>
            </div>
            <section>
              <p className="eyebrow mb3">Connected channels</p>
              <div className="stack gap2">
                {(detail.data.channels || []).map((channelItem) => <div className="row-flex between card card-sunk card-pad" key={channelItem.id}><div><b>{channelItem.channel_display_name || channelItem.channel.replaceAll("_", " ")}</b>{channelItem.error_message && <p className="err">{channelItem.error_message}</p>}</div><span className={`badge ${channelItem.error_message ? "badge-neg" : channelItem.is_active ? "badge-pos" : "badge-outline"}`}>{channelItem.error_message ? "Error" : channelItem.is_active ? "Live" : "Off"}</span></div>)}
                {!detail.data.channels?.length && <p className="muted">No channels connected yet.</p>}
              </div>
            </section>
          </div>
        )}
      </Drawer>
    </div>
  );
}
