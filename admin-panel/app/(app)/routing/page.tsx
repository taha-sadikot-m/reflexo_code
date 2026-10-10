"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useApp } from "@/components/Providers";
import { EmptyState, ErrorState, LoadingState } from "@/components/States";
import { num } from "@/lib/format";

type RoutingDatum = { count: number; pct: number; avg_latency_ms: number };
type Stats = { period_days: number; total_messages: number; customer_messages: number; owner_messages: number; routing: Record<string, RoutingDatum>; handoff_rate: number; router_methods: Record<string, { count: number; pct: number }>; cost_estimate_usd: number };
type Handoff = { id: string; conversation_id: string; response_text?: string; chain?: unknown[]; created_at?: string };

export default function RoutingPage() {
  const { businessId } = useApp();
  const [days, setDays] = useState(7);
  const stats = useQuery({ queryKey: ["routing", businessId, days], enabled: !!businessId, queryFn: () => api<Stats>(`/api/admin/routing/${businessId}/stats?days=${days}`) });
  const handoffs = useQuery({ queryKey: ["handoffs", businessId, days], enabled: !!businessId, queryFn: () => api<{ handoffs: Handoff[] }>(`/api/admin/routing/${businessId}/handoffs?days=${days}`) });
  if (!businessId || stats.isLoading) return <LoadingState label="Tracing routing decisions…" />;
  if (stats.isError) return <ErrorState message={(stats.error as Error).message} />;
  const data = stats.data!;
  const specialists = Object.entries(data.routing);
  const methods = Object.entries(data.router_methods);

  return <div>
    <header className="page-head"><div><p className="eyebrow">Broker telemetry</p><h1>Routing analytics</h1><p className="page-sub">Specialist selection, route method, latency and handoffs.</p></div><div className="pilltabs" role="tablist" aria-label="Time period">{[7, 30, 90].map((value) => <button key={value} role="tab" aria-selected={days === value} onClick={() => setDays(value)}>{value} days</button>)}</div></header>
    <div className="grid g4"><article className="card stat"><span className="eyebrow">Messages</span><b className="num">{num(data.total_messages)}</b><p className="muted">{num(data.customer_messages)} customer</p></article><article className="card stat"><span className="eyebrow">Specialists</span><b className="num">{specialists.length}</b></article><article className="card stat"><span className="eyebrow">Handoff rate</span><b className="num">{(data.handoff_rate * 100).toFixed(1)}%</b></article><article className="card stat"><span className="eyebrow">Router cost</span><b className="num">${data.cost_estimate_usd.toFixed(4)}</b></article></div>
    <div className="grid g3 mt4"><section className="card"><div className="card-head"><div><h3>Route method</h3><p>How a specialist was chosen</p></div></div><div className="card-body stack gap4">{methods.length === 0 ? <p className="muted">No route decisions in this period.</p> : methods.map(([name, item]) => <div key={name}><div className="row-flex between"><span className="cell-strong">{name.replaceAll("_", " ")}</span><b className="mono">{item.pct}%</b></div><div className="bar mt2"><i style={{ width: `${item.pct}%` }} /></div><span className="hint">{num(item.count)} decisions</span></div>)}</div></section>
      <section className="card span2"><div className="card-head"><div><h3>Specialist performance</h3><p>Share and average response latency</p></div></div>{specialists.length === 0 ? <EmptyState title="No routed messages" body="Specialist telemetry appears after AI replies." /> : <div className="tablewrap" style={{ border: 0, boxShadow: "none", borderRadius: 0 }}><table className="tbl"><thead><tr><th>Specialist</th><th className="cell-num">Messages</th><th className="cell-num">Share</th><th className="cell-num">Avg latency</th><th>Load</th></tr></thead><tbody>{specialists.map(([name, item]) => <tr key={name}><td className="cell-strong mono">{name}</td><td className="cell-num mono">{num(item.count)}</td><td className="cell-num">{item.pct}%</td><td className="cell-num mono">{num(item.avg_latency_ms)} ms</td><td style={{ minWidth: 130 }}><div className="bar"><i style={{ width: `${Math.min(100, item.pct * 2)}%` }} /></div></td></tr>)}</tbody></table></div>}</section>
    </div>
    <section className="card mt4"><div className="card-head"><div><h3>Recent handoffs</h3><p>Cross-specialist transfer traces</p></div><span className="badge badge-outline">{handoffs.data?.handoffs.length ?? 0}</span></div>{handoffs.isLoading ? <LoadingState /> : handoffs.isError ? <ErrorState message={(handoffs.error as Error).message} /> : (handoffs.data?.handoffs.length ?? 0) === 0 ? <div className="card-body"><p className="muted">No handoffs in the selected period.</p></div> : <div className="tablewrap" style={{ border: 0, boxShadow: "none", borderRadius: 0 }}><table className="tbl"><thead><tr><th>Conversation</th><th>Chain</th><th>Response</th><th>Time</th></tr></thead><tbody>{handoffs.data?.handoffs.map((handoff) => <tr key={handoff.id}><td className="mono">{handoff.conversation_id.slice(0, 8)}</td><td><span className="badge badge-accent">{Array.isArray(handoff.chain) ? handoff.chain.length : 0} hops</span></td><td className="muted">{handoff.response_text?.slice(0, 90) || "—"}</td><td className="faint">{handoff.created_at ? new Date(handoff.created_at).toLocaleString() : "—"}</td></tr>)}</tbody></table></div>}</section>
  </div>;
}
