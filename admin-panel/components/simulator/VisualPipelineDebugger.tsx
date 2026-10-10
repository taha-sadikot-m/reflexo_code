"use client";

import { useCallback, useMemo, useState } from "react";
import {
  Background,
  Controls,
  MiniMap,
  ReactFlow,
  ReactFlowProvider,
  type Node,
  type Edge,
  Handle,
  Position,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { NodeInspector } from "./NodeInspector";
import { PipelineTrace, type TraceDict } from "./PipelineTrace";

export type VisualGraph = {
  nodes?: Array<Record<string, unknown>>;
  edges?: Array<Record<string, unknown>>;
  total_ms?: number;
};

function TraceNode({ data }: NodeProps) {
  const payload = data as { label?: string; status?: string; detail?: string; ms?: number | null };
  return (
    <div className={`sim-flow-node sim-flow-node-${payload.status || "ok"}`}>
      <Handle type="target" position={Position.Left} />
      <strong>{payload.label}</strong>
      <span>{typeof payload.ms === "number" ? `${payload.ms}ms` : "not timed"}</span>
      {payload.detail ? <em>{payload.detail}</em> : null}
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

const nodeTypes = { trace: TraceNode };

export function VisualPipelineDebugger({
  visual,
  trace,
}: {
  visual: VisualGraph | null;
  trace: TraceDict | null;
}) {
  const [view, setView] = useState<"flow" | "timeline" | "json">("flow");
  const [selected, setSelected] = useState<Node | null>(null);
  const nodes = (visual?.nodes ?? []) as Node[];
  const edges = (visual?.edges ?? []) as Edge[];
  const onNodeClick = useCallback((_: unknown, node: Node) => setSelected(node), []);

  const json = useMemo(() => JSON.stringify({ visual, trace }, null, 2), [visual, trace]);

  return (
    <section className="card">
      <div className="card-head">
        <div>
          <h3>Visual debugger</h3>
          <p>Real router fields only — no invented confidence scores.</p>
        </div>
        <div className="pilltabs" role="tablist">
          <button type="button" role="tab" aria-selected={view === "flow"} onClick={() => setView("flow")}>Flow</button>
          <button type="button" role="tab" aria-selected={view === "timeline"} onClick={() => setView("timeline")}>Timeline</button>
          <button type="button" role="tab" aria-selected={view === "json"} onClick={() => setView("json")}>JSON</button>
        </div>
      </div>
      <div className="card-body">
        {view === "timeline" ? <PipelineTrace trace={trace} /> : null}
        {view === "json" ? <pre className="codeblock">{json}</pre> : null}
        {view === "flow" ? (
          <div className="sim-flow-wrap">
            <div className="sim-flow-canvas">
              {nodes.length ? (
                <ReactFlowProvider>
                  <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} onNodeClick={onNodeClick} fitView>
                    <Background />
                    <Controls />
                    <MiniMap />
                  </ReactFlow>
                </ReactFlowProvider>
              ) : (
                <p className="muted">Run a message, webhook, scenario, or E2E step to render nodes.</p>
              )}
            </div>
            <aside className="sim-flow-inspect">
              <NodeInspector node={selected} />
            </aside>
          </div>
        ) : null}
      </div>
    </section>
  );
}
