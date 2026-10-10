"use client";

import Link from "next/link";
import { useState } from "react";
import { useApp } from "@/components/Providers";
import { EmptyState, ErrorState, LoadingState } from "@/components/States";
import { ist } from "@/lib/format";
import { Icon } from "@/components/Icon";
import { useAdminQuery } from "@/lib/queries/hooks";
import { queryKeys } from "@/lib/queries/keys";

type Item = {
  id: string;
  channel: string;
  customer_name?: string;
  last_message?: string;
  last_message_at?: string;
  message_count?: number;
  is_active?: boolean;
  is_escalated?: boolean;
  summary?: string;
  ai_metadata?: {
    specialist?: string;
    route_method?: string;
    route_confidence?: number;
    latency_ms?: number;
    cache_hit?: boolean;
    hitl_required?: boolean;
  };
};

function ChannelBadge({ channel }: { channel: string }) {
  const code = channel === "whatsapp" ? "wa" : channel.startsWith("instagram") ? "ig" : channel.startsWith("facebook") ? "fb" : "web";
  const icon = code === "web" ? "globe" : code;
  return <span className={`chan chan-${code}`}><Icon name={icon} className="ic ic-sm" />{channel.replaceAll("_", " ")}</span>;
}

export default function ConversationsPage() {
  const { businessId } = useApp();
  const [search, setSearch] = useState("");
  const [channel, setChannel] = useState("");
  const [selectedId, setSelectedId] = useState("");
  const params = new URLSearchParams();
  if (channel) params.set("channel", channel);
  const suffix = params.size ? `?${params}` : "";
  const q = useAdminQuery<{ items: Item[]; total: number }>(
    queryKeys.conversations(businessId, params.toString()),
    `/api/admin/conversations/${businessId}${suffix}`,
    !!businessId,
  );
  if (!businessId || q.isLoading) return <LoadingState />;
  if (q.isError) return <ErrorState message={(q.error as Error).message} />;
  const allItems = q.data?.items || [];
  const needle = search.trim().toLocaleLowerCase();
  const items = needle
    ? allItems.filter((item) => [item.customer_name, item.last_message, item.summary, item.id].some((value) => value?.toLocaleLowerCase().includes(needle)))
    : allItems;
  const selected = items.find((item) => item.id === selectedId) || items[0];
  return (
    <div>
      <header className="page-head"><div><h1>Conversations</h1><p className="page-sub">Inspect customer threads and the broker decisions behind every AI reply.</p></div></header>
      <div className="tbl-toolbar">
        <span className="input-affix search-field"><Icon name="search" className="ic ic-sm" /><input className="input" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search customer, message or ID" /></span>
        <select className="select" value={channel} onChange={(event) => setChannel(event.target.value)} aria-label="Filter by channel" style={{ width: 170 }}>
          <option value="">All channels</option><option value="whatsapp">WhatsApp</option><option value="web_widget">Web widget</option><option value="instagram_dm">Instagram DM</option><option value="facebook_messenger">Facebook Messenger</option>
        </select>
        {(search || channel) && <button className="btn btn-ghost" onClick={() => { setSearch(""); setChannel(""); }}>Clear filters</button>}
        <span className="badge badge-outline" style={{ marginLeft: "auto" }}>{q.data?.total || 0} threads</span>
      </div>
      {items.length === 0 ? (
        <EmptyState title="No threads yet" body="Widget and channel messages will list here, scoped to this business." />
      ) : (
        <div className="split-pane">
          <section className="list-panel">
            <div className="row-flex between" style={{ padding: "9px var(--s4)", borderBottom: "1px solid var(--line)", background: "var(--surface-2)" }}><span className="eyebrow">{items.length} conversations</span><span className="hint">Newest first</span></div>
            {items.map((conversation) => (
              <button type="button" className="list-row" aria-current={selected?.id === conversation.id ? "true" : undefined} onClick={() => setSelectedId(conversation.id)} key={conversation.id}>
                <div className="lr-top"><ChannelBadge channel={conversation.channel} /><b>{conversation.customer_name || "Unknown customer"}</b>{conversation.is_escalated && <i className="dot" style={{ color: "var(--accent)" }} />}<span className="lr-time">{ist(conversation.last_message_at)}</span></div>
                <div className="lr-preview">{conversation.last_message || conversation.summary || "No message preview"}</div>
                <div className="lr-meta"><span className="badge badge-outline mono">{conversation.ai_metadata?.specialist || "unrouted"}</span>{conversation.ai_metadata?.route_confidence != null && <span className={`badge ${conversation.ai_metadata.route_confidence >= 0.75 ? "badge-pos" : "badge-warn"}`}>{Math.round(conversation.ai_metadata.route_confidence * 100)}%</span>}{conversation.ai_metadata?.hitl_required && <span className="badge badge-accent">approval</span>}</div>
              </button>
            ))}
          </section>
          {selected && (
            <div className="stack gap4">
              <section className="card card-selvedge">
                <div className="card-head">
                  <div className="row-flex gap3"><span className="avatar">{(selected.customer_name || "??").slice(0, 2).toUpperCase()}</span><div><h3>{selected.customer_name || "Unknown customer"}</h3><div className="row-flex gap2 mt2"><ChannelBadge channel={selected.channel} /><span className="badge badge-outline mono">{selected.id.slice(0, 12)}</span></div></div></div>
                  <Link href={`/conversations/${selected.id}`} className="btn btn-sm btn-primary">Open thread</Link>
                </div>
                <div className="card-body">
                  <p className="eyebrow">Latest message</p><p className="mt3">{selected.last_message || selected.summary || "No message preview is available."}</p>
                  <hr className="thread-rule mt4" />
                  <div className="kv-grid mt4">
                    <div className="kv"><span className="eyebrow">Specialist</span><b className="mono">{selected.ai_metadata?.specialist || "—"}</b></div>
                    <div className="kv"><span className="eyebrow">Route method</span><b className="mono">{selected.ai_metadata?.route_method || "—"}</b></div>
                    <div className="kv"><span className="eyebrow">Confidence</span><b className="mono">{selected.ai_metadata?.route_confidence != null ? selected.ai_metadata.route_confidence.toFixed(2) : "—"}</b></div>
                    <div className="kv"><span className="eyebrow">Messages</span><b className="mono">{selected.message_count || 0}</b></div>
                  </div>
                </div>
                <div className="card-foot"><span className="hint">Select a conversation to inspect the complete chat and routing trace.</span></div>
              </section>
              <section className="card">
                <div className="card-head"><h3>Routing snapshot</h3></div>
                <div className="card-body"><div className="trace">
                  <div className="trace-node" data-kind="ok"><span className="trace-mark"><Icon name="check" className="ic ic-sm" /></span><div><div className="trace-title">Inbound received <ChannelBadge channel={selected.channel} /></div><div className="trace-meta">{ist(selected.last_message_at)}</div></div></div>
                  <div className="trace-node" data-kind="brand"><span className="trace-mark"><Icon name="branch" className="ic ic-sm" /></span><div><div className="trace-title">Routed to {selected.ai_metadata?.specialist || "unknown"} <span className="badge badge-outline mono">{selected.ai_metadata?.route_method || "unavailable"}</span></div><div className="trace-meta">{selected.ai_metadata?.latency_ms != null ? `${selected.ai_metadata.latency_ms}ms end-to-end` : "Latency unavailable in list response"}</div></div></div>
                  <div className="trace-node" data-kind={selected.is_escalated ? "warn" : "ok"}><span className="trace-mark"><Icon name={selected.is_escalated ? "alert" : "check"} className="ic ic-sm" /></span><div><div className="trace-title">{selected.is_escalated ? "Escalated for review" : "Thread remains active"}</div><div className="trace-meta">Open the thread for message-level evidence.</div></div></div>
                </div></div>
              </section>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
