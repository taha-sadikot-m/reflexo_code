"use client";

type NodePayload = {
  label?: string;
  status?: string;
  detail?: string;
  ms?: number;
  payload?: Record<string, unknown>;
};

export function NodeInspector({ node }: { node: { id: string; data?: NodePayload } | null }) {
  if (!node) {
    return <p className="muted">Select a node to inspect real trace fields. Comment channels use a rule override to social_agent — there is no score table.</p>;
  }
  const data = node.data || {};
  return (
    <div className="stack gap3">
      <div>
        <span className="flabel">Node</span>
        <p className="mono mt2">{node.id} · {data.label || "step"}</p>
      </div>
      <div>
        <span className="flabel">Status</span>
        <p className="mt2">{data.status || "ok"}{typeof data.ms === "number" ? ` · ${data.ms}ms` : ""}</p>
      </div>
      {data.detail ? <p className="hint">{data.detail}</p> : null}
      <pre className="codeblock">{JSON.stringify(data.payload ?? {}, null, 2)}</pre>
    </div>
  );
}
