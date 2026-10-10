"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, canMutate } from "@/lib/api";
import { queryKeys } from "@/lib/queries/keys";
import { useApp } from "@/components/Providers";
import { EmptyState, ErrorState, LoadingState } from "@/components/States";
import { Icon } from "@/components/Icon";
import { TestJob, TestProgress, TestResults, isJobActive, useTestJobPolling } from "@/components/workflows/TestProgress";

type Rule = {
  id: string;
  name: string;
  description?: string;
  is_active: boolean;
  trigger_type?: string;
  trigger_config?: Record<string, unknown>;
  action_type?: string;
  action_config?: Record<string, unknown>;
  requires_owner_confirmation?: boolean;
  run_count?: number;
  success_count?: number;
  created_at?: string;
};

type AiWorkflow = {
  id: string;
  name: string;
  description?: string | null;
  nl_description?: string | null;
  trigger_type?: string;
  is_active: boolean;
  is_tested?: boolean;
  steps?: unknown[];
  instance_key?: string | null;
  total_runs?: number;
  successful_runs?: number;
  failed_runs?: number;
  shadow_mode?: boolean;
  shadow_allowlist?: string[];
  metadata?: {
    auto_paused_at?: string;
    test_results?: TestResults;
    test_progress?: TestJob;
  };
};

export default function WorkflowsPage() {
  const { businessId, admin } = useApp();
  const qc = useQueryClient();
  const writable = canMutate(admin?.role);
  const [tab, setTab] = useState<"rules" | "ai">("rules");
  const [selected, setSelected] = useState<Rule | null>(null);
  const [selectedAi, setSelectedAi] = useState<AiWorkflow | null>(null);
  const [shadowMode, setShadowMode] = useState(false);
  const [shadowAllowlist, setShadowAllowlist] = useState("");

  const list = useQuery({
    queryKey: queryKeys.workflows(businessId || ""),
    enabled: !!businessId,
    queryFn: () => api<{ items: Rule[] }>(`/api/admin/workflows/${businessId}/rules`),
  });
  const aiList = useQuery({
    queryKey: queryKeys.aiWorkflows(businessId || ""),
    enabled: !!businessId,
    queryFn: () => api<{ items: AiWorkflow[] }>(`/api/admin/workflows/${businessId}/ai`),
  });
  const toggle = useMutation({
    mutationFn: (rule: Rule) => api(`/api/admin/workflows/${businessId}/rules/${rule.id}`, { method: "PATCH", body: JSON.stringify({ is_active: !rule.is_active }) }),
    onSuccess: async () => qc.invalidateQueries({ queryKey: queryKeys.workflows(businessId || "") }),
  });
  const toggleAi = useMutation({
    mutationFn: (row: AiWorkflow) => api(`/api/admin/workflows/${businessId}/ai/${row.id}`, { method: "PATCH", body: JSON.stringify({ is_active: !row.is_active }) }),
    onSuccess: async () => qc.invalidateQueries({ queryKey: queryKeys.aiWorkflows(businessId || "") }),
  });
  const [testJobPath, setTestJobPath] = useState<string | null>(null);
  const { job: testJob, error: testJobError } = useTestJobPolling(testJobPath, async (finished) => {
    setSelectedAi((current) => current
      ? { ...current, is_tested: Boolean(finished.results?.all_passed), metadata: { ...current.metadata, test_results: finished.results ?? current.metadata?.test_results } }
      : current);
    await qc.invalidateQueries({ queryKey: queryKeys.aiWorkflows(businessId || "") });
  });
  const testAi = useMutation({
    mutationFn: (row: AiWorkflow) => api<TestJob>(`/api/admin/workflows/${businessId}/ai/${row.id}/test`, { method: "POST" }),
    onSuccess: (job, row) => setTestJobPath(`/api/admin/workflows/${businessId}/ai/${row.id}/test/${job.job_id}`),
  });
  const activateAi = useMutation({
    mutationFn: (row: AiWorkflow) => api<AiWorkflow>(`/api/admin/workflows/${businessId}/ai/${row.id}/activate`, {
      method: "POST",
      body: JSON.stringify({
        shadow_mode: shadowMode,
        shadow_allowlist: shadowMode
          ? shadowAllowlist.split(",").map((value) => value.trim()).filter(Boolean)
          : [],
      }),
    }),
    onSuccess: async (row) => {
      setSelectedAi(row);
      await qc.invalidateQueries({ queryKey: queryKeys.aiWorkflows(businessId || "") });
    },
  });
  const studioAi = useMutation({
    mutationFn: (row: AiWorkflow) => api<{ url: string }>(`/api/admin/workflows/${businessId}/ai/${row.id}/studio`, { method: "POST" }),
    onSuccess: (data) => window.open(data.url, "_blank", "noopener,noreferrer"),
  });

  // A test keeps running server-side, so reopening a workflow resumes its progress.
  function selectAiWorkflow(row: AiWorkflow) {
    setSelectedAi(row);
    setShadowMode(Boolean(row.shadow_mode));
    setShadowAllowlist((row.shadow_allowlist || []).join(", "));
    const running = row.metadata?.test_progress;
    setTestJobPath(running && isJobActive(running)
      ? `/api/admin/workflows/${businessId}/ai/${row.id}/test/${running.job_id}`
      : null);
  }

  if (!businessId || list.isLoading) return <LoadingState label="Loading workflow rules…" />;
  if (list.isError) return <ErrorState message={(list.error as Error).message} />;
  const items = list.data?.items ?? [];
  const aiItems = aiList.data?.items ?? [];
  const active = items.filter((rule) => rule.is_active).length;
  const runs = items.reduce((sum, rule) => sum + (rule.run_count ?? 0), 0);
  const successes = items.reduce((sum, rule) => sum + (rule.success_count ?? 0), 0);

  return <div>
    <header className="page-head"><div><p className="eyebrow">Automation</p><h1>Workflows</h1><p className="page-sub">Simple event rules plus AI-built Temporal workflows.</p></div></header>
    <div className="tabs mt4" role="tablist">
      <button role="tab" aria-selected={tab === "rules"} onClick={() => setTab("rules")}>Rules ({items.length})</button>
      <button role="tab" aria-selected={tab === "ai"} onClick={() => setTab("ai")}>AI workflows ({aiItems.length})</button>
    </div>

    {tab === "rules" && <>
      <div className="grid g3 mt4"><article className="card stat"><span className="eyebrow">Active rules</span><b className="num">{active}</b><p className="muted">{items.length - active} paused</p></article><article className="card stat"><span className="eyebrow">Recorded runs</span><b className="num">{runs}</b></article><article className="card stat"><span className="eyebrow">Success rate</span><b className="num">{runs ? `${Math.round(successes / runs * 100)}%` : "—"}</b></article></div>
      {toggle.isError && <p className="err mt4">{(toggle.error as Error).message}</p>}
      {items.length === 0 ? <div className="mt5"><EmptyState title="No rules" body="Automation rules for this business will appear here." /></div> : <>
        <div className="tablewrap mt5"><table className="tbl"><thead><tr><th>Rule</th><th>Trigger</th><th>Action</th><th className="cell-num">Runs</th><th className="cell-num">Success</th><th>Enabled</th><th></th></tr></thead><tbody>{items.map((rule) => {
          const rate = rule.run_count ? Math.round((rule.success_count ?? 0) / rule.run_count * 100) : null;
          return <tr key={rule.id}><td className="cell-strong">{rule.name}<div className="faint">{rule.description}</div></td><td><span className="badge badge-outline">{rule.trigger_type ?? "event"}</span></td><td className="muted">{rule.action_type ?? "configured action"}</td><td className="cell-num mono">{rule.run_count ?? 0}</td><td className="cell-num">{rate == null ? "—" : <span className={`badge ${rate >= 95 ? "badge-pos" : "badge-warn"}`}>{rate}%</span>}</td><td>{writable ? <label className="switch"><input type="checkbox" checked={rule.is_active} disabled={toggle.isPending} onChange={() => toggle.mutate(rule)} /><span className="track" /></label> : <span className={`badge ${rule.is_active ? "badge-pos" : "badge-outline"}`}>{rule.is_active ? "On" : "Off"}</span>}</td><td className="cell-actions"><button className="btn btn-sm" onClick={() => setSelected(rule)}>Inspect</button></td></tr>;
        })}</tbody></table></div>
        <div className="grid g2 mt5"><section className="card"><div className="card-head"><div><h3>Rule anatomy</h3><p>{selected?.name ?? items[0].name}</p></div><span className={`badge ${(selected ?? items[0]).is_active ? "badge-pos" : "badge-outline"}`}>{(selected ?? items[0]).is_active ? "Live" : "Paused"}</span></div><div className="card-body"><div className="trace"><div className="trace-node" data-kind="brand"><span className="trace-mark"><Icon name="zap" className="ic ic-sm" /></span><div><div className="trace-title">Trigger</div><div className="trace-meta">{(selected ?? items[0]).trigger_type ?? "event"} · {JSON.stringify((selected ?? items[0]).trigger_config ?? {})}</div></div></div><div className="trace-node" data-kind="ok"><span className="trace-mark"><Icon name="arrow-right" className="ic ic-sm" /></span><div><div className="trace-title">Action</div><div className="trace-meta">{(selected ?? items[0]).action_type ?? "configured action"} · {JSON.stringify((selected ?? items[0]).action_config ?? {})}</div></div></div><div className="trace-node" data-kind={(selected ?? items[0]).requires_owner_confirmation ? "warn" : "ok"}><span className="trace-mark"><Icon name={(selected ?? items[0]).requires_owner_confirmation ? "alert" : "check"} className="ic ic-sm" /></span><div><div className="trace-title">Owner confirmation</div><div className="trace-meta">{(selected ?? items[0]).requires_owner_confirmation ? "Required before execution" : "Not required"}</div></div></div></div></div></section>
        <section className="card card-pad"><span className="flabel">Operating policy</span><p className="muted mt3">Rules are evaluated inside the business boundary. Mutations remain available only to administrators with write permission.</p><hr className="thread-rule mt4" /><dl className="dl mt3"><div><dt>Rules</dt><dd>{items.length}</dd></div><div><dt>Confirmations</dt><dd>{items.filter((rule) => rule.requires_owner_confirmation).length}</dd></div><div><dt>Selected</dt><dd className="mono">{(selected ?? items[0]).id.slice(0, 8)}</dd></div></dl></section></div>
      </>}
    </>}

    {tab === "ai" && <section className="mt4">
      {aiList.isLoading ? <LoadingState label="Loading AI workflows…" /> :
        aiList.isError ? <ErrorState message={(aiList.error as Error).message} /> :
        aiItems.length === 0 ? <EmptyState title="No AI workflows" body="Ask the owner agent to describe an automation, then deploy the preview." /> : <>
          <div className="tablewrap"><table className="tbl"><thead><tr><th>Workflow</th><th>Trigger</th><th>Status</th><th className="cell-num">Runs</th><th>Health</th><th>Enabled</th><th></th></tr></thead><tbody>
            {aiItems.map((row) => {
              const total = row.total_runs ?? 0;
              const failed = row.failed_runs ?? 0;
              const rate = total ? Math.round((failed / total) * 100) : 0;
              const autoPaused = Boolean(row.metadata?.auto_paused_at);
              return <tr key={row.id}>
                <td className="cell-strong">{row.name}<div className="faint">{row.nl_description || row.description}</div></td>
                <td><span className="badge badge-outline">{row.trigger_type ?? "event"}</span></td>
                <td>{autoPaused ? <span className="badge badge-neg">Auto-paused</span> : row.is_active ? <span className="badge badge-pos">Live</span> : <span className="badge badge-outline">Paused</span>}</td>
                <td className="cell-num mono">{total}</td>
                <td>{total ? <span className={`badge ${rate > 20 ? "badge-neg" : "badge-pos"}`}>{rate}% fail</span> : "—"}</td>
                <td>{writable && row.is_active ? <label className="switch"><input type="checkbox" checked disabled={toggleAi.isPending} onChange={() => toggleAi.mutate(row)} /><span className="track" /></label> : row.is_active ? "On" : <span className="badge badge-outline">Test required</span>}</td>
                <td className="cell-actions"><button className="btn btn-sm" onClick={() => selectAiWorkflow(row)}>Inspect</button></td>
              </tr>;
            })}
          </tbody></table></div>
          {selectedAi && <section className="card mt4 card-pad">
            <h3>{selectedAi.name}</h3>
            <p className="muted mt2">{selectedAi.description}</p>
            <p className="mono faint mt3">instance_key {selectedAi.instance_key || "singleton"}</p>
            {!selectedAi.is_active && writable ? <div className="stack gap2 mt3">
              <label>
                <input type="checkbox" checked={shadowMode} onChange={(event) => setShadowMode(event.target.checked)} />{" "}
                Activate in shadow mode
              </label>
              {shadowMode ? <input className="input" value={shadowAllowlist} onChange={(event) => setShadowAllowlist(event.target.value)} placeholder="Customer or sender IDs, comma-separated" /> : null}
            </div> : null}
            <div className="row gap3 mt3">
              {writable ? <button className="btn btn-primary" disabled={testAi.isPending || isJobActive(testJob)} onClick={() => testAi.mutate(selectedAi)}>{testAi.isPending || isJobActive(testJob) ? "Testing…" : "Run stateful test"}</button> : null}
              {writable && !selectedAi.is_active ? <button className="btn" disabled={!selectedAi.metadata?.test_results?.all_passed || activateAi.isPending} onClick={() => activateAi.mutate(selectedAi)}>Activate</button> : null}
              {writable ? <button className="btn" disabled={studioAi.isPending} onClick={() => studioAi.mutate(selectedAi)}>Open owner Studio</button> : null}
            </div>
            {testAi.isError ? <p className="err mt3">{(testAi.error as Error).message}</p> : null}
            {testJobError ? <p className="err mt3">{testJobError}</p> : null}
            {activateAi.isError ? <p className="err mt3">{(activateAi.error as Error).message}</p> : null}
            {studioAi.isError ? <p className="err mt3">{(studioAi.error as Error).message}</p> : null}
            {testJob ? <TestProgress job={testJob} /> : null}
            {selectedAi.metadata?.test_results ? <div className={`alert mt3 ${selectedAi.metadata.test_results.all_passed ? "alert-pos" : "alert-warn"}`}><div><b>{selectedAi.metadata.test_results.title || (selectedAi.metadata.test_results.all_passed ? "Tests passed" : "Tests failed")}</b>{selectedAi.metadata.test_results.summary?.map((line, index) => <p key={index}>{line}</p>)}</div></div> : <div className="alert alert-warn mt3"><div><b>Activation blocked</b><p>Run the stateful Temporal scenario test first.</p></div></div>}
            <pre className="mt3" style={{ whiteSpace: "pre-wrap", fontSize: "var(--text-2xs)" }}>{JSON.stringify(selectedAi.steps ?? [], null, 2)}</pre>
          </section>}
        </>}
    </section>}
  </div>;
}
