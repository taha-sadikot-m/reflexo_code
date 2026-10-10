"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import {
  Background,
  Controls,
  ReactFlow,
  ReactFlowProvider,
  type Edge,
  type Node,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { API_URL, api } from "@/lib/api";
import { TestJob, TestProgress, TestResults, isJobActive, useTestJobPolling } from "@/components/workflows/TestProgress";

type StudioState = {
  id: string;
  workflow_id: string;
  workflow_name: string;
  description?: string;
  nl_description?: string;
  trigger_type: string;
  status: string;
  expires_at: string;
  is_active: boolean;
  shadow_mode?: boolean;
  shadow_allowlist?: string[];
  visual_data?: { nodes?: Node[]; edges?: Edge[] };
  test_results?: TestResults;
};

type ChatLine = { role: "owner" | "assistant"; text: string };

export default function OwnerWorkflowStudioPage() {
  const params = useParams<{ token: string }>();
  const token = params.token;
  const [state, setState] = useState<StudioState | null>(null);
  const [results, setResults] = useState<TestResults | null>(null);
  const [busy, setBusy] = useState<"test" | "activate" | "edit" | "">("");
  const [error, setError] = useState("");
  const [editText, setEditText] = useState("");
  const [chat, setChat] = useState<ChatLine[]>([]);
  const [shadowMode, setShadowMode] = useState(false);
  const [shadowAllowlist, setShadowAllowlist] = useState("");
  const [testJobPath, setTestJobPath] = useState<string | null>(null);
  const { job: testJob, error: testJobError } = useTestJobPolling(testJobPath, (finished) => {
    if (finished.results) setResults(finished.results);
    setState((current) => current
      ? { ...current, status: finished.results?.all_passed ? "tested" : "draft", test_results: finished.results ?? current.test_results }
      : current);
  });

  useEffect(() => {
    if (!token) return;
    void api<StudioState>(`/api/wf/${encodeURIComponent(token)}`)
      .then((data) => {
        setState(data);
        setResults(data.test_results || null);
        setShadowMode(Boolean(data.shadow_mode));
        setShadowAllowlist((data.shadow_allowlist || []).join(", "));
      })
      .catch((cause) => setError((cause as Error).message));
  }, [token]);

  useEffect(() => {
    if (!token) return;
    const socket = new WebSocket(
      `${API_URL.replace(/^http/, "ws")}/ws/wf/${encodeURIComponent(token)}`,
    );
    socket.onmessage = (event) => {
      try {
        const update = JSON.parse(event.data) as {
          type?: string;
          session?: StudioState;
        };
        if (update.type === "studio_state" && update.session) {
          setState(update.session);
          setResults(update.session.test_results || null);
        }
      } catch {
        // Ignore malformed progress frames; the regular API state stays usable.
      }
    };
    return () => socket.close();
  }, [token]);

  async function runTest() {
    setBusy("test");
    setError("");
    try {
      const job = await api<TestJob>(`/api/wf/${encodeURIComponent(token)}/test`, {
        method: "POST",
      });
      setTestJobPath(`/api/wf/${encodeURIComponent(token)}/test/${job.job_id}`);
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setBusy("");
    }
  }

  async function activate() {
    setBusy("activate");
    setError("");
    try {
      const allowlist = shadowAllowlist
        .split(",")
        .map((value) => value.trim())
        .filter(Boolean);
      await api(`/api/wf/${encodeURIComponent(token)}/activate`, {
        method: "POST",
        body: JSON.stringify({
          shadow_mode: shadowMode,
          shadow_allowlist: shadowMode ? allowlist : [],
        }),
      });
      setState((current) => current ? {
        ...current,
        status: "active",
        is_active: true,
        shadow_mode: shadowMode,
        shadow_allowlist: allowlist,
      } : current);
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setBusy("");
    }
  }

  async function submitEdit() {
    const message = editText.trim();
    if (!message) return;
    setBusy("edit");
    setError("");
    setChat((current) => [...current, { role: "owner", text: message }]);
    try {
      const result = await api<{
        updated: boolean;
        session: StudioState;
        warnings?: string[];
        bridge_warning?: string | null;
      }>(`/api/wf/${encodeURIComponent(token)}/chat`, {
        method: "POST",
        body: JSON.stringify({ message }),
      });
      setState(result.session);
      setResults(null);
      setEditText("");
      setChat((current) => [
        ...current,
        {
          role: "assistant",
          text: result.warnings?.length
            ? `Draft updated. Review: ${result.warnings.join(" ")}`
            : result.bridge_warning ||
              "Draft updated. Review the flow, then run the stateful test again.",
        },
      ]);
    } catch (cause) {
      const messageText = (cause as Error).message;
      setError(messageText);
      setChat((current) => [
        ...current,
        { role: "assistant", text: `Edit rejected: ${messageText}` },
      ]);
    } finally {
      setBusy("");
    }
  }

  if (error && !state) {
    return <main className="auth-page"><section className="card card-pad"><h1>Workflow Studio</h1><p className="err mt3">{error}</p></section></main>;
  }
  if (!state) {
    return <main className="auth-page"><section className="card card-pad"><p>Loading workflow…</p></section></main>;
  }

  const nodes = state.visual_data?.nodes || [];
  const edges = state.visual_data?.edges || [];
  return (
    <main style={{ maxWidth: 1180, margin: "0 auto", padding: "32px 20px" }}>
      <header className="page-head">
        <div>
          <p className="eyebrow">Owner Workflow Studio</p>
          <h1>{state.workflow_name}</h1>
          <p className="page-sub">{state.nl_description || state.description}</p>
        </div>
        <span className={`badge ${state.status === "active" ? "badge-pos" : results?.all_passed ? "badge-brand" : "badge-outline"}`}>{state.status}</span>
      </header>

      {error ? <p className="err mt4">{error}</p> : null}
      <div className="grid g2 mt4">
        <section className="card">
          <div className="card-head"><div><h3>Workflow flow</h3><p>Trigger: {state.trigger_type}</p></div></div>
          <div className="card-body">
            <div className="sim-flow-canvas" style={{ height: 440 }}>
              <ReactFlowProvider>
                <ReactFlow nodes={nodes} edges={edges} fitView>
                  <Background />
                  <Controls />
                </ReactFlow>
              </ReactFlowProvider>
            </div>
          </div>
        </section>

        <section className="card">
          <div className="card-head"><div><h3>Test and activate</h3><p>Outbound delivery is always simulated during testing.</p></div></div>
          <div className="card-body stack gap4">
            <button className="btn btn-primary" type="button" disabled={Boolean(busy) || isJobActive(testJob) || state.is_active} onClick={() => void runTest()}>
              {busy === "test" || isJobActive(testJob) ? "Running stateful test…" : "Run stateful test"}
            </button>
            {testJobError ? <p className="err">{testJobError}</p> : null}
            {testJob ? <TestProgress job={testJob} /> : null}
            {results ? (
              <div className={`alert ${results.all_passed ? "alert-pos" : "alert-warn"}`}>
                <div>
                  <b>{results.title}</b>
                  {results.summary?.map((line, index) => <p key={index}>{line}</p>)}
                </div>
              </div>
            ) : <p className="muted">Activation is blocked until the stateful Temporal scenario passes.</p>}
            {!testJob && results?.state_checks?.length ? (
              <div className="trace">
                {results.state_checks.map((check) => (
                  <div className="trace-node" data-kind={check.outcome === "error" ? "neg" : check.matched ? "ok" : "brand"} key={check.event}>
                    <span className="trace-mark">·</span>
                    <div>
                      <div className="trace-title">Event {check.event} · {check.outcome}</div>
                      <div className="trace-meta">{check.events_received ?? 0} events in instance · {(check.completed_steps || []).join(", ") || "waiting"}</div>
                    </div>
                  </div>
                ))}
              </div>
            ) : null}
            <label className="stack gap2">
              <span>
                <input
                  type="checkbox"
                  checked={shadowMode}
                  onChange={(event) => setShadowMode(event.target.checked)}
                  disabled={state.is_active || Boolean(busy)}
                />{" "}
                Activate in shadow mode
              </span>
              <span className="hint">
                Only allowlisted customer IDs or channel sender IDs execute the
                workflow. Other matches are logged and continue to the agent.
              </span>
            </label>
            {shadowMode ? (
              <input
                className="input"
                value={shadowAllowlist}
                onChange={(event) => setShadowAllowlist(event.target.value)}
                placeholder="Allowlisted IDs, comma-separated"
                disabled={state.is_active || Boolean(busy)}
              />
            ) : null}
            <button className="btn" type="button" disabled={!results?.all_passed || Boolean(busy) || isJobActive(testJob) || state.is_active} onClick={() => void activate()}>
              {state.is_active
                ? state.shadow_mode ? "Active in shadow mode" : "Active"
                : busy === "activate" ? "Activating…" : "Activate workflow"}
            </button>
            <p className="hint">Link expires {new Date(state.expires_at).toLocaleString()}.</p>
          </div>
        </section>
      </div>

      <section className="card mt4">
        <div className="card-head">
          <div>
            <h3>Edit with chat</h3>
            <p>This conversation is logged in the same owner WhatsApp thread.</p>
          </div>
          <span className="badge badge-outline">Live updates</span>
        </div>
        <div className="card-body stack gap4">
          {chat.length ? (
            <div className="thread">
              {chat.map((line, index) => (
                <div
                  className={`bubble-row ${line.role === "owner" ? "out" : ""}`}
                  key={`${line.role}-${index}`}
                >
                  <div className="bubble">{line.text}</div>
                </div>
              ))}
            </div>
          ) : (
            <p className="muted">
              Example: “Wait two days instead of one, then send the reminder.”
            </p>
          )}
          <form
            className="composer"
            onSubmit={(event) => {
              event.preventDefault();
              void submitEdit();
            }}
          >
            <input
              className="input"
              value={editText}
              onChange={(event) => setEditText(event.target.value)}
              placeholder="Describe the workflow change"
              disabled={Boolean(busy) || isJobActive(testJob) || state.is_active}
            />
            <button
              className="btn btn-primary"
              disabled={Boolean(busy) || isJobActive(testJob) || !editText.trim() || state.is_active}
            >
              {busy === "edit" ? "Updating…" : "Update draft"}
            </button>
          </form>
          <p className="hint">
            Any edit clears previous test results and deactivates the workflow
            until the new definition passes.
          </p>
        </div>
      </section>
    </main>
  );
}
