"use client";

import { useParams } from "next/navigation";
import Link from "next/link";
import { useApp } from "@/components/Providers";
import { EmptyState, ErrorState, LoadingState } from "@/components/States";
import { ist } from "@/lib/format";
import { Icon } from "@/components/Icon";
import { useAdminQuery } from "@/lib/queries/hooks";
import { queryKeys } from "@/lib/queries/keys";

type Msg = {
  id: string;
  role: string;
  content: string;
  created_at?: string;
  channel?: string;
  status?: string;
  error_message?: string;
  ai_metadata?: {
    specialist?: string;
    route_method?: string;
    route_confidence?: number;
    latency_ms?: number;
    cache_hit?: boolean;
    hitl_required?: boolean;
    handoff_chain?: string[];
    guardrails?: string[];
    memories_injected?: number;
    tokens?: number | Record<string, number>;
    estimated_cost_usd?: number;
  };
};

type Conversation = {
  id: string;
  customer_name?: string;
  channel?: string;
  summary?: string;
  external_thread_id?: string;
  started_at?: string;
  last_message_at?: string;
  message_count?: number;
  is_active?: boolean;
  is_escalated?: boolean;
};

export default function ConversationDetailPage() {
  const { id } = useParams<{ id: string }>();
  const { businessId } = useApp();
  const q = useAdminQuery<{ conversation: Conversation; messages: Msg[] }>(
    queryKeys.conversation(businessId, id),
    `/api/admin/conversations/${businessId}/${id}/messages`,
    !!businessId && !!id,
  );
  if (q.isLoading) return <LoadingState />;
  if (q.isError) return <ErrorState message={(q.error as Error).message} />;
  const messages = q.data?.messages || [];
  const conversation = q.data?.conversation;
  if (!conversation) return <EmptyState title="Conversation not found" body="This thread is unavailable for the selected business." />;
  const aiMessages = messages.filter((message) => message.role === "ai" && message.ai_metadata);
  const latestAI = aiMessages.at(-1)?.ai_metadata;
  const confidenceValues = aiMessages.map((message) => message.ai_metadata?.route_confidence).filter((value): value is number => value != null);
  const latencyValues = aiMessages.map((message) => message.ai_metadata?.latency_ms).filter((value): value is number => value != null);
  const guardrails = Array.from(new Set(aiMessages.flatMap((message) => message.ai_metadata?.guardrails || [])));
  const handoffs = Array.from(new Set(aiMessages.flatMap((message) => message.ai_metadata?.handoff_chain || [])));
  return (
    <div>
      <nav className="crumbs"><Link href="/conversations">Conversations</Link><Icon name="chev-right" className="ic ic-sm" /><b>{conversation.customer_name || conversation.id.slice(0, 8)}</b></nav>
      <header className="page-head mt3">
        <div><h1>{conversation.customer_name || "Conversation"}</h1><p className="page-sub">{conversation.summary || "Message-level conversation inspection"}</p></div>
        <div className="page-head-actions"><span className="badge badge-outline">{conversation.channel || "unknown channel"}</span><span className={`badge ${conversation.is_escalated ? "badge-accent" : conversation.is_active ? "badge-pos" : "badge-outline"}`}>{conversation.is_escalated ? "Escalated" : conversation.is_active ? "Active" : "Closed"}</span></div>
      </header>
      <div className="split-pane" style={{ gridTemplateColumns: "minmax(0, 1.6fr) minmax(320px, .8fr)" }}>
        <section className="card chatpane">
          <div className="card-head">
            <div className="row-flex gap3"><span className="avatar">{(conversation.customer_name || "??").slice(0, 2).toUpperCase()}</span><div><h3>{conversation.customer_name || "Unknown customer"}</h3><div className="row-flex gap2 mt2"><span className="badge badge-outline mono">{conversation.external_thread_id || conversation.id}</span></div></div></div>
            <span className="badge badge-brand">{messages.length} messages</span>
          </div>
          <div className="chatpane-body">
            {messages.length === 0 ? <EmptyState title="No messages" body="This conversation exists but contains no visible messages." /> : (
              <div className="thread">
                <div className="bubble-row system"><div className="bubble">{ist(conversation.started_at)} · conversation started</div></div>
                {messages.map((message) => {
                  const outgoing = message.role === "customer" || message.role === "owner" || message.role === "user";
                  const system = message.role === "system";
                  return (
                    <div key={message.id} className={`bubble-row ${outgoing ? "out" : ""} ${system ? "system" : ""}`}>
                      {!outgoing && !system && <span className="avatar avatar-sm avatar-accent">{message.role === "ai" ? "AI" : message.role.slice(0, 2).toUpperCase()}</span>}
                      <div>
                        <div className="bubble">
                          {message.content}
                          {message.role === "ai" && message.ai_metadata && (
                            <div className="bubble-meta">
                              <span className="badge badge-brand">{message.ai_metadata.specialist || "AI"}</span>
                              <span className="badge badge-outline mono">{message.ai_metadata.route_method || "unavailable"}</span>
                              {message.ai_metadata.route_confidence != null && <span className={`badge ${message.ai_metadata.route_confidence >= 0.75 ? "badge-pos" : "badge-warn"}`}>{Math.round(message.ai_metadata.route_confidence * 100)}%</span>}
                              {message.ai_metadata.latency_ms != null && <span className="badge badge-outline mono">{message.ai_metadata.latency_ms}ms</span>}
                              {message.ai_metadata.cache_hit && <span className="badge badge-info">cache hit</span>}
                              {message.ai_metadata.hitl_required && <span className="badge badge-accent">approval</span>}
                            </div>
                          )}
                        </div>
                        <div className="bubble-time">{ist(message.created_at)}{message.status ? ` · ${message.status}` : ""}</div>
                        {message.error_message && <div className="alert alert-neg mt2"><Icon name="alert" /><div><b>Delivery error</b><p>{message.error_message}</p></div></div>}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
          <div className="card-foot" style={{ justifyContent: "space-between" }}><span className="hint">Read-only viewer. Message mutation is intentionally unavailable.</span><Link className="btn btn-sm" href="/simulator">Replay in simulator</Link></div>
        </section>
        <aside className="stack gap4">
          <section className="card">
            <div className="card-head"><h3>AI metadata</h3></div>
            <div className="card-body">
              <div className="kv-grid">
                <div className="kv"><span className="eyebrow">Specialist</span><b className="mono">{latestAI?.specialist || "—"}</b></div>
                <div className="kv"><span className="eyebrow">Route method</span><b className="mono">{latestAI?.route_method || "—"}</b></div>
                <div className="kv"><span className="eyebrow">Confidence</span><b className="mono">{confidenceValues.length ? confidenceValues.map((value) => value.toFixed(2)).join(" / ") : "—"}</b></div>
                <div className="kv"><span className="eyebrow">Latency</span><b className="mono">{latencyValues.length ? latencyValues.map((value) => `${value}ms`).join(" / ") : "—"}</b></div>
                <div className="kv"><span className="eyebrow">Cache</span><b className="mono">{latestAI ? latestAI.cache_hit ? "hit" : "miss" : "—"}</b></div>
                <div className="kv"><span className="eyebrow">HITL</span><b className="mono">{latestAI ? latestAI.hitl_required ? "required" : "not required" : "—"}</b></div>
              </div>
              <hr className="thread-rule mt4" />
              <dl className="dl mt3"><div><dt>Memories injected</dt><dd>{latestAI?.memories_injected ?? "—"}</dd></div><div><dt>Guardrails</dt><dd>{guardrails.length ? guardrails.join(", ") : "none reported"}</dd></div><div><dt>Handoff chain</dt><dd>{handoffs.length ? handoffs.join(" → ") : "none"}</dd></div></dl>
            </div>
          </section>
          <section className="card">
            <div className="card-head"><h3>Routing trace</h3></div>
            <div className="card-body"><div className="trace">
              <div className="trace-node" data-kind="ok"><span className="trace-mark"><Icon name="check" className="ic ic-sm" /></span><div><div className="trace-title">Inbound received</div><div className="trace-meta">{conversation.channel || "unknown"} · {ist(conversation.started_at)}</div></div></div>
              <div className="trace-node" data-kind="brand"><span className="trace-mark"><Icon name="branch" className="ic ic-sm" /></span><div><div className="trace-title">Broker routed to {latestAI?.specialist || "unknown"} {latestAI?.route_method && <span className="badge badge-outline mono">{latestAI.route_method}</span>}</div><div className="trace-meta">{confidenceValues.length ? `confidence ${confidenceValues.at(-1)?.toFixed(2)}` : "routing confidence unavailable"}</div></div></div>
              {(latestAI?.memories_injected != null || handoffs.length > 0) && <div className="trace-node" data-kind="ok"><span className="trace-mark"><Icon name="brain" className="ic ic-sm" /></span><div><div className="trace-title">Context assembled</div><div className="trace-meta">{latestAI?.memories_injected ?? 0} memories · {handoffs.length} handoffs</div></div></div>}
              {guardrails.length > 0 && <div className="trace-node" data-kind="warn"><span className="trace-mark"><Icon name="alert" className="ic ic-sm" /></span><div><div className="trace-title">Guardrails triggered</div><div className="trace-meta">{guardrails.join(" · ")}</div></div></div>}
              <div className="trace-node" data-kind="ok"><span className="trace-mark"><Icon name="check" className="ic ic-sm" /></span><div><div className="trace-title">Latest reply processed</div><div className="trace-meta">{latencyValues.at(-1) != null ? `${latencyValues.at(-1)}ms end to end` : "No latency reported"}</div></div></div>
            </div></div>
          </section>
        </aside>
      </div>
    </div>
  );
}
