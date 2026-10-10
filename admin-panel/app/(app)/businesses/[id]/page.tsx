"use client";

import { useParams } from "next/navigation";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { ErrorState, LoadingState } from "@/components/States";
import { num } from "@/lib/format";
import { Icon } from "@/components/Icon";

type Channel = {
  id: string;
  channel: string;
  channel_display_name?: string;
  channel_account_id?: string;
  is_active: boolean;
  error_message?: string;
  last_synced_at?: string;
};

type BusinessDetail = {
  id: string;
  name: string;
  slug?: string;
  plan?: string;
  plan_status?: string;
  owner_name?: string;
  owner_phone?: string;
  owner_email?: string;
  vertical?: string;
  is_active?: boolean;
  onboarding_completed?: boolean;
  message_limit?: number;
  messages_used?: number;
  channels?: Channel[];
  stats?: { messages_7d?: number; convos_7d?: number };
};

export default function BusinessDetailPage() {
  const { id } = useParams<{ id: string }>();
  const q = useQuery({
    queryKey: ["business", id],
    queryFn: () => api<BusinessDetail>(`/api/admin/businesses/${id}`),
  });
  if (q.isLoading) return <LoadingState />;
  if (q.isError) return <ErrorState message={(q.error as Error).message} />;
  const b = q.data!;
  const stats = b.stats || {};
  const quotaPercent = b.message_limit ? Math.min(100, Math.round(((b.messages_used || 0) / b.message_limit) * 100)) : 0;
  return (
    <div>
      <nav className="crumbs"><Link href="/businesses">Businesses</Link><Icon name="chev-right" className="ic ic-sm" /><b>{b.name}</b></nav>
      <header className="page-head mt3">
        <div className="row-flex gap3">
          <span className="avatar avatar-lg">{b.name.slice(0, 2).toUpperCase()}</span>
          <div><h1>{b.name}</h1><p className="page-sub">{b.slug || b.id}</p></div>
        </div>
        <div className="page-head-actions">
          <span className={`badge ${b.is_active ? "badge-pos" : "badge-neg"}`}><i className={`dot ${b.is_active ? "dot-live" : ""}`} />{b.is_active ? "Active" : "Inactive"}</span>
          <span className="badge badge-outline">{b.plan || "free"}</span>
        </div>
      </header>
      <div className="grid g4">
        <article className="card stat card-selvedge"><div className="eyebrow">Messages · 7 days</div><b>{num(stats.messages_7d)}</b><span className="stat-delta flat">Across all channels</span></article>
        <article className="card stat"><div className="eyebrow">Conversations · 7 days</div><b>{num(stats.convos_7d)}</b><span className="stat-delta flat">Distinct threads</span></article>
        <article className="card stat"><div className="eyebrow">Quota used</div><b>{b.message_limit ? `${quotaPercent}%` : "—"}</b><span className="stat-delta flat">{num(b.messages_used)} of {b.message_limit ? num(b.message_limit) : "unlimited"}</span></article>
        <article className="card stat"><div className="eyebrow">Channels</div><b>{b.channels?.length || 0}</b><span className="stat-delta flat">{b.channels?.filter((channel) => channel.is_active).length || 0} connected</span></article>
      </div>
      <div className="grid g3 mt4">
        <section className="card span2">
          <div className="card-head"><div><h3>Channel connections</h3><p>Delivery status and account identity</p></div></div>
          <div className="card-body stack gap3">
            {(b.channels || []).map((channel) => (
              <div className={`card card-sunk card-pad ${channel.error_message ? "card-selvedge card-selvedge-neg" : ""}`} key={channel.id}>
                <div className="row-flex between gap3">
                  <div className="row-flex gap3"><span className={`avatar avatar-sm chan-${channel.channel === "whatsapp" ? "wa" : channel.channel}`}>{channel.channel.slice(0, 2).toUpperCase()}</span><div><b>{channel.channel_display_name || channel.channel}</b><div className="faint mono" style={{ fontSize: "var(--text-2xs)" }}>{channel.channel_account_id || "No external account ID"}</div></div></div>
                  <span className={`badge ${channel.is_active && !channel.error_message ? "badge-pos" : "badge-neg"}`}>{channel.error_message ? "Error" : channel.is_active ? "Connected" : "Disabled"}</span>
                </div>
                {channel.error_message && <div className="alert alert-neg mt3"><Icon name="alert" /><div><b>Connection error</b><p>{channel.error_message}</p></div></div>}
              </div>
            ))}
            {!b.channels?.length && <p className="muted">No channels have been connected for this business.</p>}
          </div>
        </section>
        <aside className="stack gap4">
          <section className="card">
            <div className="card-head"><h3>Owner &amp; tenant</h3></div>
            <div className="card-body"><dl className="dl">
              <div><dt>Owner</dt><dd>{b.owner_name || "—"}</dd></div>
              <div><dt>Phone</dt><dd className="mono">{b.owner_phone || "—"}</dd></div>
              <div><dt>Email</dt><dd>{b.owner_email || "—"}</dd></div>
              <div><dt>Vertical</dt><dd>{b.vertical || "general"}</dd></div>
              <div><dt>Onboarding</dt><dd>{b.onboarding_completed ? "Complete" : "In progress"}</dd></div>
            </dl></div>
          </section>
          <section className="card card-selvedge">
            <div className="card-head"><h3>Plan usage</h3></div>
            <div className="card-body plan-usage">
              <div className="pu-top"><span>{b.plan || "free"}</span><b>{b.message_limit ? `${quotaPercent}%` : "Unlimited"}</b></div>
              <div className={`bar ${quotaPercent > 85 ? "bar-neg" : quotaPercent > 70 ? "bar-accent" : ""}`}><i style={{ width: `${quotaPercent}%` }} /></div>
              <span className="hint">{num(b.messages_used)} messages counted in the current billing period.</span>
            </div>
          </section>
        </aside>
      </div>
    </div>
  );
}
