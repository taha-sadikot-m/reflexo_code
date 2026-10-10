"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, canBill } from "@/lib/api";
import { useApp } from "@/components/Providers";
import { EmptyState, ErrorState, LoadingState } from "@/components/States";
import { inr, num } from "@/lib/format";
import { Button, Dialog, Field, useToast } from "@/components/ui";

type Plan = { plan: string; businesses: number; messages_used: number; message_limit: number };
type AtLimit = { id: string; name: string; plan: string; messages_used: number; message_limit: number };
type Usage = { business: { id: string; name: string; plan: string; plan_status?: string; messages_used: number; message_limit: number; billing_cycle_start?: string }; events: { event_type: string; channel?: string; quantity: number; cost_inr: number }[] };

export default function BillingPage() {
  const { admin, businessId } = useApp();
  const qc = useQueryClient();
  const [dialog, setDialog] = useState<"reset" | "plan" | null>(null);
  const [plan, setPlan] = useState("pro");
  const { toast } = useToast();
  const overview = useQuery({ queryKey: ["billing"], queryFn: () => api<{ spend_inr_mtd: number; plans: Plan[]; at_limit: AtLimit[] }>("/api/admin/billing/overview") });
  const usage = useQuery({ queryKey: ["usage", businessId], enabled: !!businessId, queryFn: () => api<Usage>(`/api/admin/billing/usage/${businessId}`) });
  const change = useMutation({ mutationFn: (nextPlan: string) => api(`/api/admin/billing/change-plan/${businessId}`, { method: "POST", body: JSON.stringify({ plan: nextPlan }) }), onSuccess: async () => { setDialog(null); toast({ tone: "pos", title: "Plan changed", body: `${business?.name || "Business"} is now on ${plan}.` }); await Promise.all([qc.invalidateQueries({ queryKey: ["usage", businessId] }), qc.invalidateQueries({ queryKey: ["billing"] })]); } });
  const reset = useMutation({ mutationFn: () => api(`/api/admin/billing/reset-usage/${businessId}`, { method: "POST" }), onSuccess: async () => { setDialog(null); toast({ tone: "warn", title: "Usage cycle reset", body: "The audited billing counter was set to zero." }); await qc.invalidateQueries({ queryKey: ["usage", businessId] }); } });
  if (overview.isLoading || (businessId && usage.isLoading)) return <LoadingState label="Calculating plan usage…" />;
  if (overview.isError || usage.isError) return <ErrorState message={((overview.error || usage.error) as Error).message} />;
  const business = usage.data?.business;
  const percent = business?.message_limit ? Math.min(100, Math.round(business.messages_used / business.message_limit * 100)) : 0;
  const totalBusinesses = (overview.data?.plans ?? []).reduce((sum, item) => sum + item.businesses, 0);

  return <div>
    <header className="page-head"><div><p className="eyebrow">Plans & usage</p><h1>Billing</h1><p className="page-sub">Subscription distribution, AI spend and message allowances.</p></div></header>
    <div className="grid g3"><article className="card stat"><span className="eyebrow">AI spend MTD</span><b className="num">{inr(overview.data?.spend_inr_mtd)}</b></article><article className="card stat"><span className="eyebrow">Active businesses</span><b className="num">{num(totalBusinesses)}</b></article><article className="card stat"><span className="eyebrow">Near limit</span><b className="num">{overview.data?.at_limit.length ?? 0}</b><p className="muted">at or above 90%</p></article></div>
    <div className="grid g3 mt4"><section className="card span2"><div className="card-head"><div><h3>Plan distribution</h3><p>Usage across active businesses</p></div></div>{(overview.data?.plans.length ?? 0) === 0 ? <EmptyState title="No plan data" body="Active subscriptions will appear here." /> : <div className="tablewrap" style={{ border: 0, boxShadow: "none", borderRadius: 0 }}><table className="tbl"><thead><tr><th>Plan</th><th className="cell-num">Businesses</th><th className="cell-num">Messages</th><th>Allowance used</th></tr></thead><tbody>{overview.data?.plans.map((item) => { const used = item.message_limit ? Math.min(100, Math.round(item.messages_used / item.message_limit * 100)) : 0; return <tr key={item.plan}><td className="cell-strong">{item.plan}</td><td className="cell-num mono">{num(item.businesses)}</td><td className="cell-num mono">{num(item.messages_used)}</td><td style={{ minWidth: 150 }}><div className={`bar ${used >= 90 ? "bar-neg" : used >= 70 ? "bar-accent" : ""}`}><i style={{ width: `${used}%` }} /></div><span className="hint">{used}%</span></td></tr>; })}</tbody></table></div>}</section>
      {business ? <section className="card card-selvedge"><div className="card-head"><div><h3>{business.name}</h3><p>{business.plan} · {business.plan_status ?? "active"}</p></div><span className="badge badge-pos">{business.plan_status ?? "active"}</span></div><div className="card-body stack gap4"><div className="plan-usage"><div className="pu-top"><span>{num(business.messages_used)} of {num(business.message_limit)} messages</span><b>{percent}%</b></div><div className={`bar ${percent >= 90 ? "bar-neg" : "bar-accent"}`}><i style={{ width: `${percent}%` }} /></div></div><hr className="thread-rule" /><dl className="dl"><div><dt>Cycle began</dt><dd>{business.billing_cycle_start ? new Date(business.billing_cycle_start).toLocaleDateString() : "—"}</dd></div><div><dt>Recorded event types</dt><dd>{usage.data?.events.length ?? 0}</dd></div></dl>{canBill(admin?.role) && <div className="row-flex gap2 wrap"><button className="btn btn-sm grow" onClick={() => setDialog("plan")}>Change plan</button><button className="btn btn-sm btn-danger grow" onClick={() => setDialog("reset")}>Reset cycle</button></div>}</div></section> : <section className="card"><EmptyState title="Select a business" body="Business usage appears after a business is selected." /></section>}
    </div>
    {(overview.data?.at_limit.length ?? 0) > 0 && <section className="card mt4"><div className="card-head"><div><h3>Allowance watchlist</h3><p>Businesses at or above 90% usage</p></div></div><div className="tablewrap" style={{ border: 0, boxShadow: "none", borderRadius: 0 }}><table className="tbl"><thead><tr><th>Business</th><th>Plan</th><th className="cell-num">Used</th><th className="cell-num">Limit</th><th>Status</th></tr></thead><tbody>{overview.data?.at_limit.map((item) => <tr key={item.id}><td className="cell-strong">{item.name}</td><td><span className="badge badge-outline">{item.plan}</span></td><td className="cell-num mono">{num(item.messages_used)}</td><td className="cell-num mono">{num(item.message_limit)}</td><td><span className="badge badge-warn">{Math.round(item.messages_used / item.message_limit * 100)}%</span></td></tr>)}</tbody></table></div></section>}
    <Dialog
      open={dialog !== null}
      title={dialog === "reset" ? "Reset usage cycle?" : "Change subscription plan"}
      description={business?.name}
      onClose={() => setDialog(null)}
      footer={<><Button onClick={() => setDialog(null)}>Cancel</Button><Button variant={dialog === "reset" ? "solid-danger" : "primary"} loading={change.isPending || reset.isPending} onClick={() => dialog === "reset" ? reset.mutate() : change.mutate(plan)}>{dialog === "reset" ? "Reset usage" : "Change plan"}</Button></>}
    >
      {dialog === "reset" ? <p>This sets message usage to zero and starts a new billing cycle. The action is audited.</p> : <Field label="New plan"><select className="select" value={plan} onChange={(event) => setPlan(event.target.value)}><option value="free">Free</option><option value="starter">Starter</option><option value="growth">Growth</option><option value="pro">Pro</option><option value="premium">Premium</option></select></Field>}
      {(change.isError || reset.isError) && <p className="err mt3">{((change.error || reset.error) as Error).message}</p>}
    </Dialog>
  </div>;
}
