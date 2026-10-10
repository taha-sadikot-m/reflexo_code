"use client";

import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import Link from "next/link";
import { Icon } from "@/components/Icon";
import { inr, num } from "@/lib/format";
import { EmptyState, ErrorState, LoadingState } from "@/components/States";
import { useAdminQuery } from "@/lib/queries/hooks";
import { queryKeys } from "@/lib/queries/keys";

type Dash = {
  messages_today: number;
  messages_yesterday: number;
  messages_delta_pct: number | null;
  median_reply_ms: number | null;
  active_businesses: number;
  onboarding_businesses: number;
  spend_inr_today: number;
  volume: { day: string; count: number }[];
  specialists: { name: string; count: number; share: number }[];
};

export default function DashboardPage() {
  const q = useAdminQuery<Dash>(queryKeys.dashboard(7), "/api/admin/dashboard");
  if (q.isLoading) return <LoadingState label="Gathering today’s warp…" />;
  if (q.isError) return <ErrorState message={(q.error as Error).message} />;
  const d = q.data!;
  if (!d) return <EmptyState title="No telemetry yet" body="Messages will appear here once businesses start chatting." />;
  const totalSpecialistMessages = d.specialists.reduce((sum, specialist) => sum + specialist.count, 0);
  const donut = d.specialists.length
    ? `conic-gradient(${d.specialists
        .reduce<{ stops: string[]; cursor: number }>(
          (acc, specialist, index) => {
            const colors = ["var(--brand)", "var(--accent)", "var(--pos)", "var(--info)", "var(--warn)"];
            const next = acc.cursor + specialist.share;
            acc.stops.push(`${colors[index % colors.length]} ${acc.cursor}% ${next}%`);
            acc.cursor = next;
            return acc;
          },
          { stops: [], cursor: 0 }
        )
        .stops.join(",")})`
    : "var(--paper-sunk)";
  return (
    <div>
      <header className="page-head">
        <div>
          <h1>Dashboard</h1>
          <p className="page-sub">Live health across every REFLEXO business.</p>
        </div>
        <div className="page-head-actions">
          <span className="badge badge-outline"><Icon name="calendar" className="ic ic-sm" />Last 7 days</span>
        </div>
      </header>
      <div className="grid g4">
        <article className="card stat card-selvedge">
          <div className="eyebrow">Messages today</div>
          <b className="num">{num(d.messages_today)}</b>
          <span className={`stat-delta ${d.messages_delta_pct != null && d.messages_delta_pct >= 0 ? "up" : "down"}`}>
            {d.messages_delta_pct == null ? "No prior-day baseline" : <><Icon name={d.messages_delta_pct >= 0 ? "arrow-up" : "arrow-down"} className="ic ic-sm" />{Math.abs(d.messages_delta_pct)}% vs yesterday</>}
          </span>
        </article>
        <article className="card stat">
          <div className="eyebrow">Median reply</div>
          <b className="num">{d.median_reply_ms == null ? "—" : `${(d.median_reply_ms / 1000).toFixed(1)}s`}</b>
          <span className="stat-delta flat">AI replies · 7 day median</span>
        </article>
        <article className="card stat">
          <div className="eyebrow">Active businesses</div>
          <b className="num">{num(d.active_businesses)}</b>
          <span className="stat-delta flat">{num(d.onboarding_businesses)} onboarding</span>
        </article>
        <article className="card stat">
          <div className="eyebrow">AI spend today</div>
          <b className="num">{inr(d.spend_inr_today)}</b>
          <span className="stat-delta flat">
            {d.messages_today ? `${inr(d.spend_inr_today / d.messages_today)} per message` : "No messages today"}
          </span>
        </article>
      </div>
      <div className="grid g3 mt4">
        <section className="card span2">
          <div className="card-head">
            <div><h3>Message volume</h3><p>Daily messages in the selected window</p></div>
            <span className="badge badge-brand">{num(d.volume.reduce((sum, day) => sum + day.count, 0))} total</span>
          </div>
          <div className="card-body" style={{ height: 270 }}>
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={d.volume} className="chart">
                <defs>
                  <linearGradient id="messageVolume" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="var(--brand)" stopOpacity={0.22} />
                    <stop offset="100%" stopColor="var(--brand)" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid stroke="var(--line)" strokeDasharray="4 5" vertical={false} />
                <XAxis dataKey="day" tick={{ fontFamily: "var(--font-mono)", fontSize: 10 }} tickLine={false} axisLine={false} />
                <YAxis tick={{ fontFamily: "var(--font-mono)", fontSize: 10 }} tickLine={false} axisLine={false} width={36} />
                <Tooltip contentStyle={{ background: "var(--surface)", border: "1px solid var(--line)", borderRadius: "var(--r-sm)" }} />
                <Area type="monotone" dataKey="count" stroke="var(--brand)" strokeWidth={2} fill="url(#messageVolume)" />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </section>
        <section className="card">
          <div className="card-head"><div><h3>Specialists</h3><p>Share of routed messages</p></div></div>
          <div className="card-body stack gap4" style={{ placeItems: "center" }}>
            <div aria-label={`${num(totalSpecialistMessages)} specialist messages`} style={{ width: 150, height: 150, borderRadius: "50%", background: donut, display: "grid", placeItems: "center" }}>
              <div className="card" style={{ width: 94, height: 94, borderRadius: "50%", display: "grid", placeItems: "center", textAlign: "center" }}>
                <div><b className="mono">{num(totalSpecialistMessages)}</b><div className="eyebrow">Messages</div></div>
              </div>
            </div>
            <div className="legend" style={{ width: "100%" }}>
              {d.specialists.map((specialist, index) => (
                <div key={specialist.name}>
                  <i className="swatch" style={{ background: ["var(--brand)", "var(--accent)", "var(--pos)", "var(--info)", "var(--warn)"][index % 5] }} />
                  <span className="mono">{specialist.name}</span><span className="lg-val">{specialist.share}%</span>
                </div>
              ))}
              {!d.specialists.length && <p className="muted">No AI turns in this window.</p>}
            </div>
          </div>
        </section>
      </div>
      <div className="grid g2 mt4">
        <section className="card card-selvedge">
          <div className="card-head"><div><h3>Needs attention</h3><p>Operational signals from available telemetry</p></div></div>
          <div className="card-body stack gap3">
            {d.onboarding_businesses > 0 ? (
              <div className="alert alert-accent"><Icon name="building" /><div><b>{num(d.onboarding_businesses)} businesses still onboarding</b><p>Complete setup before channel traffic can be handled end to end.</p></div></div>
            ) : (
              <div className="alert alert-pos"><Icon name="check" /><div><b>Onboarding queue is clear</b><p>All active businesses have completed onboarding.</p></div></div>
            )}
            <p className="hint">Integration incidents and approval queues are not exposed by the dashboard API.</p>
          </div>
        </section>
        <section className="card">
          <div className="card-head"><div><h3>Activity composition</h3><p>Current routing mix</p></div><Link href="/conversations" className="btn btn-sm">Open viewer</Link></div>
          <div className="card-body stack gap3">
            {d.specialists.slice(0, 4).map((specialist) => (
              <div className="plan-usage" key={specialist.name}>
                <div className="pu-top"><span className="mono">{specialist.name}</span><b>{num(specialist.count)}</b></div>
                <div className="bar"><i style={{ width: `${specialist.share}%` }} /></div>
              </div>
            ))}
            {!d.specialists.length && <p className="muted">Activity appears after the first AI-routed turn.</p>}
          </div>
        </section>
      </div>
    </div>
  );
}
