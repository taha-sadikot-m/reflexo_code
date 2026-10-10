"use client";

export type TraceStep = {
  name: string;
  status: string;
  detail?: string;
  ms?: number | null;
  data?: Record<string, unknown>;
};

export type TraceDict = { steps: TraceStep[]; total_ms?: number };

export function PipelineTrace({ trace }: { trace: TraceDict | null }) {
  if (!trace?.steps?.length) {
    return <p className="muted">No pipeline steps yet.</p>;
  }
  return (
    <div className="trace">
      {trace.steps.map((step, index) => (
        <div
          className="trace-node"
          data-kind={step.status === "error" ? "neg" : step.status === "skip" ? "brand" : "ok"}
          key={`${step.name}-${index}`}
        >
          <span className="trace-mark">·</span>
          <div>
            <div className="trace-title">
              {step.name}
              {typeof step.ms === "number" ? ` · ${step.ms}ms` : ""}
            </div>
            <div className="trace-meta">{step.detail || JSON.stringify(step.data || {})}</div>
          </div>
        </div>
      ))}
      {typeof trace.total_ms === "number" ? <p className="hint mt3">Total {trace.total_ms}ms</p> : null}
    </div>
  );
}
