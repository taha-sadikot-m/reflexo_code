"use client";

import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { api, API_URL } from "@/lib/api";
import { useApp } from "@/components/Providers";
import { EmptyState } from "@/components/States";
import { PipelineTrace, type TraceDict } from "@/components/simulator/PipelineTrace";
import { VisualPipelineDebugger, type VisualGraph } from "@/components/simulator/VisualPipelineDebugger";

type Tab = "message" | "webhook" | "voice" | "workflow" | "bridge" | "scenario" | "e2e";
type Visual = VisualGraph;
type Prog = { type?: string; response_text?: string; text?: string; preview?: string; specialist?: string; tools?: string[]; workflow_matched?: boolean; status?: string; outbound?: string };
type EventLine = { id: number; type: string; text: string; specialist?: string; failed?: boolean };

const TABS: { id: Tab; label: string }[] = [
  { id: "message", label: "Message" },
  { id: "webhook", label: "Webhook" },
  { id: "voice", label: "Voice" },
  { id: "workflow", label: "Workflow" },
  { id: "bridge", label: "Bridge" },
  { id: "scenario", label: "Scenario" },
  { id: "e2e", label: "E2E" },
];

const CHANNELS: { value: string; label: string }[] = [
  { value: "whatsapp", label: "WhatsApp" },
  { value: "web", label: "Web widget" },
  { value: "facebook_messenger", label: "Messenger" },
  { value: "instagram_dm", label: "Instagram DM" },
  { value: "facebook_comment", label: "Facebook comment" },
  { value: "instagram_comment", label: "Instagram comment" },
];

const COMMENT_CHANNELS = new Set(["facebook_comment", "instagram_comment"]);

export default function SimulatorPage() {
  const { businessId } = useApp();
  const [tab, setTab] = useState<Tab>("message");
  const [error, setError] = useState("");
  const [trace, setTrace] = useState<TraceDict | null>(null);
  const [visual, setVisual] = useState<Visual | null>(null);

  return (
    <div>
      <header className="page-head">
        <div>
          <p className="eyebrow">Sandbox</p>
          <h1>Simulator</h1>
          <p className="page-sub">Seven-tab lab. Synthetic traffic always uses skip_send — nothing posts to Meta. Automations are Temporal AI workflows only.</p>
        </div>
      </header>
      <div className="tabs" role="tablist">
        {TABS.map((item) => (
          <button key={item.id} type="button" role="tab" aria-selected={tab === item.id} onClick={() => setTab(item.id)}>{item.label}</button>
        ))}
      </div>
      {error ? <p className="err mt4">{error}</p> : null}
      <div className="mt4 sim-tab-grid">
        {tab === "message" ? <MessageTab businessId={businessId} setError={setError} trace={trace} setTrace={setTrace} setVisual={setVisual} /> : null}
        {tab === "webhook" ? <WebhookTab businessId={businessId} setError={setError} /> : null}
        {tab === "voice" ? <VoiceTab businessId={businessId} setError={setError} /> : null}
        {tab === "workflow" ? <WorkflowTab businessId={businessId} setError={setError} setTrace={setTrace} setVisual={setVisual} /> : null}
        {tab === "bridge" ? <BridgeTab businessId={businessId} setError={setError} /> : null}
        {tab === "scenario" ? <ScenarioTab businessId={businessId} setError={setError} setTrace={setTrace} setVisual={setVisual} /> : null}
        {tab === "e2e" ? <E2ETab businessId={businessId} setError={setError} /> : null}
        {(tab === "message" || tab === "workflow" || tab === "scenario") ? <VisualPipelineDebugger visual={visual} trace={trace} /> : null}
      </div>
    </div>
  );
}

function MessageTab({
  businessId, setError, trace, setTrace, setVisual,
}: {
  businessId: string | null;
  setError: (v: string) => void;
  trace: TraceDict | null;
  setTrace: (v: TraceDict | null) => void;
  setVisual: (v: Visual | null) => void;
}) {
  const [sessionId, setSessionId] = useState("");
  const [events, setEvents] = useState<EventLine[]>([]);
  const [text, setText] = useState("");
  const [channel, setChannel] = useState("whatsapp");
  const [mode, setMode] = useState<"customer" | "owner">("customer");
  const [status, setStatus] = useState<"idle" | "starting" | "connected" | "error">("idle");
  const [sending, setSending] = useState(false);
  const esRef = useRef<EventSource | null>(null);
  const isComment = COMMENT_CHANNELS.has(channel);

  useEffect(() => () => esRef.current?.close(), []);

  async function start() {
    if (!businessId) return;
    setStatus("starting");
    setError("");
    try {
      const result = await api<{ session_id: string }>(`/api/admin/simulator/${businessId}/start`, { method: "POST", body: JSON.stringify({ channel, mode }) });
      setSessionId(result.session_id);
      setEvents([{ id: Date.now(), type: "session", text: `Session ${result.session_id} opened as ${mode}` }]);
      esRef.current?.close();
      const source = new EventSource(`${API_URL}/api/admin/simulator/${businessId}/events?session_id=${encodeURIComponent(result.session_id)}`, { withCredentials: true });
      source.addEventListener("ready", () => setStatus("connected"));
      source.addEventListener("progress", (event) => {
        const data = JSON.parse((event as MessageEvent<string>).data || "{}") as Prog;
        const kind = data.type ?? "event";
        const textLine = kind === "done"
          ? data.response_text || [data.status, data.outbound].filter(Boolean).join(" · ")
          : data.text ?? data.preview ?? "";
        setEvents((current) => [...current, {
          id: Date.now() + current.length,
          type: kind,
          text: textLine,
          specialist: data.specialist,
          failed: kind === "done" && !data.response_text,
        }]);
      });
      source.onerror = () => { setStatus("error"); setError("The live event stream disconnected."); };
      esRef.current = source;
    } catch (cause) {
      setStatus("error");
      setError((cause as Error).message);
    }
  }

  async function inject(burst?: number) {
    if (!businessId || !text.trim()) return;
    setSending(true);
    setError("");
    try {
      const body = await api<{ trace?: TraceDict; visual?: Visual; response?: string; specialist?: string; error?: string }>(
        "/api/admin/test-simulator/message",
        { method: "POST", body: JSON.stringify({ business_id: businessId, text: text.trim(), channel, mode, session_id: sessionId || undefined, burst }) },
      );
      if (body.trace) setTrace(body.trace);
      if (body.visual) setVisual(body.visual);
      if (Array.isArray((body as { items?: unknown[] }).items)) {
        const last = (body as { items: { trace?: TraceDict; visual?: Visual }[] }).items.at(-1);
        if (last?.trace) setTrace(last.trace);
        if (last?.visual) setVisual(last.visual);
      }
      const failed = Boolean(body.error) || !body.response;
      setEvents((current) => [
        ...current,
        { id: Date.now(), type: "you", text: text.trim() },
        {
          id: Date.now() + 1,
          type: "done",
          text: body.response || body.error || "The pipeline returned no reply. Check the trace for the failing stage.",
          specialist: body.specialist,
          failed,
        },
      ]);
      setText("");
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setSending(false);
    }
  }

  return (
    <div className="split-pane" style={{ gridTemplateColumns: "280px minmax(0,1fr) 320px" }}>
      <aside className="stack gap4">
        <section className="card">
          <div className="card-head"><h3>Inject</h3></div>
          <div className="card-body stack gap4">
            <label className="field"><span className="flabel">Acting as</span>
              <div className="segmented">
                <button type="button" aria-pressed={mode === "customer"} onClick={() => setMode("customer")}>Customer</button>
                <button type="button" aria-pressed={mode === "owner"} onClick={() => setMode("owner")}>Owner</button>
              </div>
            </label>
            <label className="field"><span className="flabel">Channel</span>
              <select className="select" value={channel} onChange={(event) => setChannel(event.target.value)}>{CHANNELS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select>
            </label>
            <div className="alert alert-warn"><div><b>skip_send</b><p>In-process inject returns a PipelineTrace. Optional live SSE still queues Redis for the worker.</p></div></div>
            <button className="btn btn-primary" onClick={start} disabled={!businessId}>{sessionId ? "New live session" : "Start live SSE"}</button>
          </div>
        </section>
      </aside>
      <section className="card chatpane">
        <div className="card-head"><div><h3>Conversation</h3><p>{channel} · {mode}</p></div><span className={`badge ${status === "connected" ? "badge-pos" : "badge-outline"}`}>{status}</span></div>
        <div className="chatpane-body">{events.length === 0 ? <EmptyState title="No turns yet" body="Send a message through the lab." /> : <div className="thread">{events.map((item) => item.type === "you" ? <div className="bubble-row out" key={item.id}><div className="bubble">{item.text}</div></div> : item.type === "done" ? <div className="bubble-row" key={item.id}><div className="bubble">{item.text}<div className="bubble-meta"><span className={`badge ${item.failed ? "badge-neg" : "badge-brand"}`}>{item.failed ? "failed" : item.specialist || "agent"}</span></div></div></div> : <div className="bubble-row system" key={item.id}><div className="bubble">{item.text}</div></div>)}</div>}</div>
        <div className="chatpane-foot">
          <form className="composer" onSubmit={(event: FormEvent) => { event.preventDefault(); void inject(); }}>
            <input className="input" value={text} onChange={(event) => setText(event.target.value)} placeholder={isComment ? "Comment text" : `Send as ${mode}`} disabled={sending} />
            <button className="btn btn-primary" disabled={sending || !text.trim()}>Send</button>
          </form>
          <button className="btn btn-sm mt3" type="button" disabled={sending || !text.trim()} onClick={() => void inject(10)}>Burst (10)</button>
        </div>
      </section>
      <aside className="card">
        <div className="card-head"><div><h3>Pipeline</h3><p>Last turn, then live SSE events</p></div></div>
        <div className="card-body stack gap4">
          <PipelineTrace trace={trace} />
          <div className="trace">
            {events.filter((item) => !["you", "session"].includes(item.type)).map((item) => (
              <div className="trace-node" data-kind={item.failed || item.type === "error" ? "neg" : "brand"} key={item.id}>
                <span className="trace-mark">·</span>
                <div>
                  <div className="trace-title">{item.type}{item.specialist ? ` · ${item.specialist}` : ""}</div>
                  {item.text ? <div className="trace-meta">{item.text}</div> : null}
                </div>
              </div>
            ))}
          </div>
        </div>
      </aside>
    </div>
  );
}

function WebhookTab({ businessId, setError }: { businessId: string | null; setError: (v: string) => void }) {
  const [preset, setPreset] = useState("whatsapp_text");
  const [raw, setRaw] = useState("{}");
  const [result, setResult] = useState("");
  const [presets, setPresets] = useState<{ id: string; name: string; payload: unknown }[]>([]);

  useEffect(() => {
    void api<{ items: { id: string; name: string; payload: unknown }[] }>("/api/admin/test-simulator/webhook/presets")
      .then((data) => {
        setPresets(data.items);
        const first = data.items[0];
        if (first) setRaw(JSON.stringify(first.payload, null, 2));
      })
      .catch((cause) => setError((cause as Error).message));
  }, [setError]);

  function applyPreset(id: string) {
    setPreset(id);
    const found = presets.find((item) => item.id === id);
    if (found) setRaw(JSON.stringify(found.payload, null, 2));
  }

  async function run(validateOnly: boolean) {
    if (!businessId) return;
    setError("");
    try {
      const payload = JSON.parse(raw) as Record<string, unknown>;
      const body = await api<Record<string, unknown>>("/api/admin/test-simulator/webhook", {
        method: "POST",
        body: JSON.stringify({ business_id: businessId, preset: preset === "custom" ? null : preset, payload, validate_only: validateOnly }),
      });
      setResult(JSON.stringify(body, null, 2));
    } catch (cause) {
      setError((cause as Error).message);
    }
  }

  return (
    <section className="card">
      <div className="card-head"><div><h3>Webhook</h3><p>Parsers run against a patched publisher that forces skip_send. Nothing is queued live unless you opt in on the API.</p></div></div>
      <div className="card-body stack gap4">
        <label className="field"><span className="flabel">Preset</span>
          <select className="select" value={preset} onChange={(event) => applyPreset(event.target.value)}>
            {presets.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
            <option value="custom">Custom JSON</option>
          </select>
        </label>
        <textarea className="input" style={{ minHeight: 220, fontFamily: "var(--font-mono)" }} value={raw} onChange={(event) => { setPreset("custom"); setRaw(event.target.value); }} />
        <div className="row gap3">
          <button className="btn btn-primary" type="button" onClick={() => void run(false)}>Process</button>
          <button className="btn" type="button" onClick={() => void run(true)}>Validate only</button>
        </div>
        {result ? <pre className="codeblock">{result}</pre> : null}
      </div>
    </section>
  );
}

function VoiceTab({ businessId, setError }: { businessId: string | null; setError: (v: string) => void }) {
  const [sessionId, setSessionId] = useState("");
  const [text, setText] = useState("");
  const [log, setLog] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);

  async function start() {
    if (!businessId) return;
    setBusy(true);
    setError("");
    try {
      const body = await api<{ session_id: string; opening: string }>("/api/admin/test-simulator/voice/start", { method: "POST", body: JSON.stringify({ business_id: businessId }) });
      setSessionId(body.session_id);
      setLog([`Agent: ${body.opening}`]);
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function turn(event?: FormEvent) {
    event?.preventDefault();
    if (!sessionId || !text.trim()) return;
    setBusy(true);
    try {
      const body = await api<{ reply: string }>("/api/admin/test-simulator/voice/turn", { method: "POST", body: JSON.stringify({ session_id: sessionId, text: text.trim() }) });
      setLog((current) => [...current, `You: ${text.trim()}`, `Agent: ${body.reply}`]);
      setText("");
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function end() {
    if (!sessionId) return;
    await api("/api/admin/test-simulator/voice/end", { method: "POST", body: JSON.stringify({ session_id: sessionId }) });
    setSessionId("");
  }

  return (
    <section className="card">
      <div className="card-head"><div><h3>Voice (text mode)</h3><p>Uses voice prompts and knowledge lookup. No LiveKit or Deepgram.</p></div></div>
      <div className="card-body stack gap4">
        <div className="row gap3">
          <button className="btn btn-primary" type="button" onClick={() => void start()} disabled={busy}>Start call</button>
          <button className="btn" type="button" onClick={() => void end()} disabled={!sessionId}>End</button>
        </div>
        <div className="thread">{log.map((line, index) => <div className="bubble" key={index}>{line}</div>)}</div>
        <form className="composer" onSubmit={(event) => void turn(event)}>
          <input className="input" value={text} onChange={(event) => setText(event.target.value)} disabled={!sessionId || busy} placeholder="Type what the caller says" />
          <button className="btn btn-primary" disabled={!sessionId || busy}>Send</button>
        </form>
      </div>
    </section>
  );
}

function WorkflowTab({
  businessId, setError, setTrace, setVisual,
}: {
  businessId: string | null;
  setError: (v: string) => void;
  setTrace: (v: TraceDict | null) => void;
  setVisual: (v: Visual | null) => void;
}) {
  const [items, setItems] = useState<{ id: string; name: string; trigger_type?: string; is_active: boolean }[]>([]);
  const [selected, setSelected] = useState("");
  const [out, setOut] = useState("");

  useEffect(() => {
    if (!businessId) return;
    void api<{ items: { id: string; name: string; trigger_type?: string; is_active: boolean }[] }>(`/api/admin/workflows/${businessId}/ai`)
      .then((data) => setItems((data.items || []).filter((row) => row.is_active)))
      .catch((cause) => setError((cause as Error).message));
  }, [businessId, setError]);

  async function fire() {
    if (!businessId) return;
    setError("");
    try {
      const body = await api<{ pipeline?: { trace?: TraceDict; visual?: Visual }; temporal_error?: string }>(
        "/api/admin/test-simulator/workflow/fire",
        { method: "POST", body: JSON.stringify({ business_id: businessId, workflow_id: selected || undefined }) },
      );
      if (body.pipeline?.trace) setTrace(body.pipeline.trace);
      if (body.pipeline?.visual) setVisual(body.pipeline.visual);
      setOut(JSON.stringify(body, null, 2));
    } catch (cause) {
      setError((cause as Error).message);
    }
  }

  return (
    <section className="card">
      <div className="card-head"><div><h3>Workflow fire</h3><p>Starts or signals Temporal. Outbound tools mock when simulate_outbound is set. If Temporal is down, the error is shown here.</p></div></div>
      <div className="card-body stack gap4">
        {items.length === 0 ? <p className="muted">No active AI workflows for this business.</p> : (
          <label className="field"><span className="flabel">Active workflow</span>
            <select className="select" value={selected} onChange={(event) => setSelected(event.target.value)}>
              <option value="">First active</option>
              {items.map((row) => <option key={row.id} value={row.id}>{row.name} · {row.trigger_type}</option>)}
            </select>
          </label>
        )}
        <button className="btn btn-primary" type="button" onClick={() => void fire()}>Fire synthetic event</button>
        {out ? <pre className="codeblock">{out}</pre> : null}
      </div>
    </section>
  );
}

function BridgeTab({ businessId, setError }: { businessId: string | null; setError: (v: string) => void }) {
  const [phone, setPhone] = useState("+919876543210");
  const [prompt, setPrompt] = useState("Please send the file on WhatsApp.");
  const [text, setText] = useState("Here is the document");
  const [out, setOut] = useState("");

  async function start() {
    if (!businessId) return;
    try {
      const body = await api("/api/admin/test-simulator/bridge/start", { method: "POST", body: JSON.stringify({ business_id: businessId, phone, prompt }) });
      setOut(JSON.stringify(body, null, 2));
    } catch (cause) {
      setError((cause as Error).message);
    }
  }

  async function receive() {
    if (!businessId) return;
    try {
      const body = await api("/api/admin/test-simulator/bridge/receive", { method: "POST", body: JSON.stringify({ business_id: businessId, phone, text }) });
      setOut(JSON.stringify(body, null, 2));
    } catch (cause) {
      setError((cause as Error).message);
    }
  }

  return (
    <section className="card">
      <div className="card-head"><div><h3>Bridge</h3><p>Registers a Redis listener only. WhatsApp Graph is never called.</p></div></div>
      <div className="card-body stack gap4">
        <label className="field"><span className="flabel">Phone</span><input className="input" value={phone} onChange={(event) => setPhone(event.target.value)} /></label>
        <label className="field"><span className="flabel">Prompt (stored, not sent)</span><input className="input" value={prompt} onChange={(event) => setPrompt(event.target.value)} /></label>
        <button className="btn btn-primary" type="button" onClick={() => void start()}>Register listener</button>
        <label className="field"><span className="flabel">Inbound WhatsApp text</span><input className="input" value={text} onChange={(event) => setText(event.target.value)} /></label>
        <button className="btn" type="button" onClick={() => void receive()}>Simulate inbound</button>
        {out ? <pre className="codeblock">{out}</pre> : null}
      </div>
    </section>
  );
}

function ScenarioTab({
  businessId, setError, setTrace, setVisual,
}: {
  businessId: string | null;
  setError: (v: string) => void;
  setTrace: (v: TraceDict | null) => void;
  setVisual: (v: Visual | null) => void;
}) {
  const [presets, setPresets] = useState<{ id: string; name: string; description?: string }[]>([]);
  const [workflows, setWorkflows] = useState<{ id: string; name: string; trigger_type?: string; is_active: boolean; is_tested?: boolean }[]>([]);
  const [workflowId, setWorkflowId] = useState("");
  const [preset, setPreset] = useState("comment_giveaway");
  const [userCount, setUserCount] = useState(3);
  const [dup, setDup] = useState(true);
  const [over, setOver] = useState(false);
  const [nomatch, setNomatch] = useState(true);
  const [out, setOut] = useState("");
  const mentionNote = useMemo(() => preset === "mention_auto_reply", [preset]);

  useEffect(() => {
    void api<{ items: { id: string; name: string; description?: string }[] }>("/api/admin/test-simulator/scenario/presets")
      .then((data) => setPresets(data.items))
      .catch((cause) => setError((cause as Error).message));
    if (businessId) {
      void api<{ items: { id: string; name: string; trigger_type?: string; is_active: boolean; is_tested?: boolean }[] }>(`/api/admin/workflows/${businessId}/ai`)
        .then((data) => setWorkflows(data.items))
        .catch((cause) => setError((cause as Error).message));
    }
  }, [businessId, setError]);

  async function run() {
    if (!businessId) return;
    try {
      const body = await api<{
        results?: { trace?: TraceDict; visual?: Visual }[];
        technical?: { per_event_traces?: { trace?: TraceDict; visual?: Visual }[] };
      }>("/api/admin/test-simulator/scenario", {
        method: "POST",
        body: JSON.stringify({
          business_id: businessId,
          workflow_id: workflowId || undefined,
          preset: workflowId ? undefined : preset,
          user_count: userCount,
          duplicate_commenter: dup,
          over_limit: over,
          non_matching: nomatch,
          process: true,
        }),
      });
      const last = workflowId
        ? body.technical?.per_event_traces?.at(-1)
        : body.results?.at(-1);
      if (last?.trace) setTrace(last.trace);
      if (last?.visual) setVisual(last.visual);
      setOut(JSON.stringify(body, null, 2));
    } catch (cause) {
      setError((cause as Error).message);
    }
  }

  return (
    <section className="card">
      <div className="card-head"><div><h3>Scenario</h3><p>Select a workflow to auto-detect the scenario, run isolated Temporal instances, snapshot state after each event, and save the activation report.</p></div></div>
      <div className="card-body stack gap4">
        <label className="field"><span className="flabel">Target Temporal workflow</span>
          <select className="select" value={workflowId} onChange={(event) => setWorkflowId(event.target.value)}>
            <option value="">Manual preset only (no activation report)</option>
            {workflows.map((item) => <option key={item.id} value={item.id}>{item.name} · {item.trigger_type} · {item.is_active ? "live" : item.is_tested ? "tested" : "draft"}</option>)}
          </select>
        </label>
        {workflowId ? <div className="alert alert-warn"><div><b>Definition-aware test</b><p>The server chooses the scenario from trigger and steps. Delay/wait time is simulated; aggregation and Temporal signals are real and isolated from live instance IDs.</p></div></div> : null}
        <label className="field"><span className="flabel">Preset</span>
          <select className="select" value={preset} onChange={(event) => setPreset(event.target.value)} disabled={Boolean(workflowId)}>
            {presets.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
          </select>
        </label>
        {mentionNote ? <div className="alert alert-warn"><div><b>Mentions</b><p>There is no instagram_mention parser. This preset injects instagram_comment events with mention-shaped text.</p></div></div> : null}
        <label className="field"><span className="flabel">Unique users</span><input className="input" type="number" min={1} max={25} value={userCount} onChange={(event) => setUserCount(Number(event.target.value))} disabled={Boolean(workflowId)} /></label>
        <label className="field"><input type="checkbox" checked={dup} onChange={(event) => setDup(event.target.checked)} disabled={Boolean(workflowId)} /> Duplicate sender</label>
        <label className="field"><input type="checkbox" checked={over} onChange={(event) => setOver(event.target.checked)} disabled={Boolean(workflowId)} /> Extra over-threshold event</label>
        <label className="field"><input type="checkbox" checked={nomatch} onChange={(event) => setNomatch(event.target.checked)} disabled={Boolean(workflowId)} /> Non-matching text</label>
        <button className="btn btn-primary" type="button" onClick={() => void run()}>{workflowId ? "Run stateful test" : "Run preset"}</button>
        {out ? <pre className="codeblock">{out}</pre> : null}
      </div>
    </section>
  );
}

function E2ETab({ businessId, setError }: { businessId: string | null; setError: (v: string) => void }) {
  const [out, setOut] = useState("");
  const [history, setHistory] = useState("");

  async function run() {
    if (!businessId) return;
    try {
      const body = await api("/api/admin/test-simulator/e2e", { method: "POST", body: JSON.stringify({ business_id: businessId }) });
      setOut(JSON.stringify(body, null, 2));
    } catch (cause) {
      setError((cause as Error).message);
    }
  }

  async function loadHistory() {
    if (!businessId) return;
    try {
      const body = await api(`/api/admin/test-simulator/e2e/history?business_id=${encodeURIComponent(businessId)}`);
      setHistory(JSON.stringify(body, null, 2));
    } catch (cause) {
      setError((cause as Error).message);
    }
  }

  return (
    <section className="card">
      <div className="card-head"><div><h3>E2E suite</h3><p>Runs T1–T10 against the selected business. Failures must not call Graph.</p></div></div>
      <div className="card-body stack gap4">
        <div className="row gap3">
          <button className="btn btn-primary" type="button" onClick={() => void run()}>Run all</button>
          <button className="btn" type="button" onClick={() => void loadHistory()}>History</button>
        </div>
        {out ? <pre className="codeblock">{out}</pre> : null}
        {history ? <pre className="codeblock">{history}</pre> : null}
      </div>
    </section>
  );
}
