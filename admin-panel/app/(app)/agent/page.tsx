"use client";

import { FormEvent, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, canMutate } from "@/lib/api";
import { useApp } from "@/components/Providers";
import { ErrorState, LoadingState } from "@/components/States";
import { useToast } from "@/components/ui";
import { personaSchema, validationErrors, type FieldErrors } from "@/lib/schemas";

type AgentConfig = {
  persona_name?: string;
  persona_tone?: string;
  persona_language?: string[];
  greeting_message?: string;
  custom_instructions?: string;
  persona?: Record<string, unknown>;
  smart_number_mode?: boolean;
  smart_number_default?: "ask_owner" | "personal" | "business";
  knowledge_entries: number;
  entities: number;
  business: { id: string; name: string; vertical?: string };
};

export default function AgentPage() {
  const { businessId, admin } = useApp();
  const qc = useQueryClient();
  const [tab, setTab] = useState<"persona" | "instructions">("persona");
  const [formErrors, setFormErrors] = useState<FieldErrors>({});
  const { toast } = useToast();
  const query = useQuery({ queryKey: ["agent", businessId], enabled: !!businessId, queryFn: () => api<AgentConfig>(`/api/admin/agent-config/${businessId}`) });
  const save = useMutation({
    mutationFn: (body: Partial<AgentConfig>) => api(`/api/admin/agent-config/${businessId}/persona`, { method: "PUT", body: JSON.stringify(body) }),
    onSuccess: async () => {
      setFormErrors({});
      toast({ tone: "pos", title: "Agent configuration saved", body: "The next agent turn uses the updated persona." });
      await qc.invalidateQueries({ queryKey: ["agent", businessId] });
    },
  });
  if (!businessId || query.isLoading) return <LoadingState label="Compiling agent configuration…" />;
  if (query.isError) return <ErrorState message={(query.error as Error).message} />;
  const config = query.data!;
  const writable = canMutate(admin?.role);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!writable) return;
    const data = new FormData(event.currentTarget);
    const parsed = personaSchema.safeParse({
      persona_name: String(data.get("persona_name") ?? "").trim(),
      persona_tone: String(data.get("persona_tone") ?? "").trim(),
      persona_language: String(data.get("persona_language") ?? "").split(",").map((value) => value.trim()).filter(Boolean),
      greeting_message: String(data.get("greeting_message") ?? "").trim(),
      custom_instructions: String(data.get("custom_instructions") ?? "").trim(),
    });
    if (!parsed.success) {
      setFormErrors(validationErrors(parsed.error));
      return;
    }
    let persona: Record<string, unknown> = {};
    try {
      persona = JSON.parse(String(data.get("persona") || "{}")) as Record<string, unknown>;
    } catch {
      setFormErrors({ persona: "Persona JSON must be valid JSON." });
      return;
    }
    setFormErrors({});
    save.mutate({
      ...parsed.data,
      persona,
      smart_number_mode: data.get("smart_number_mode") === "on",
      smart_number_default: String(data.get("smart_number_default") || "ask_owner") as AgentConfig["smart_number_default"],
    });
  }

  return <div>
    <header className="page-head"><div><p className="eyebrow">Behaviour</p><h1>Agent config</h1><p className="page-sub">Identity, language and instructions used to compile the business prompt.</p></div></header>
    <div className="tabs" role="tablist"><button role="tab" aria-selected={tab === "persona"} onClick={() => setTab("persona")}>Persona</button><button role="tab" aria-selected={tab === "instructions"} onClick={() => setTab("instructions")}>Instructions</button><button role="tab" aria-selected={false} disabled>Guardrails</button><button role="tab" aria-selected={false} disabled>Limits</button></div>
    <form onSubmit={submit} className="split-pane mt5" style={{ gridTemplateColumns: "minmax(0,1fr) 372px" }}>
      <div className="stack gap4">
        {tab === "persona" ? <section className="card"><div className="card-head"><div><h3>Identity</h3><p>How the agent introduces itself on every channel.</p></div></div><div className="card-body form-grid"><label className="field"><span className="flabel">Agent name</span><input className="input" name="persona_name" defaultValue={config.persona_name ?? ""} readOnly={!writable} /></label><label className="field"><span className="flabel">Tone</span><input className="input" name="persona_tone" defaultValue={config.persona_tone ?? ""} readOnly={!writable} placeholder="Warm and professional" /></label><label className="field full"><span className="flabel">Languages</span><input className="input" name="persona_language" defaultValue={(config.persona_language ?? []).join(", ")} readOnly={!writable} placeholder="English, Hindi" /><span className="hint">Comma-separated language names.</span></label><label className="field full"><span className="flabel">Greeting</span><textarea className="textarea" name="greeting_message" defaultValue={config.greeting_message ?? ""} readOnly={!writable} /></label><label className="field full"><span className="flabel">Structured persona JSON</span><textarea className="textarea mono" style={{ minHeight: 220 }} name="persona" defaultValue={JSON.stringify(config.persona || {}, null, 2)} readOnly={!writable} /></label><label className="field"><span><input type="checkbox" name="smart_number_mode" defaultChecked={config.smart_number_mode} disabled={!writable} /> Smart Number mode</span></label><label className="field"><span className="flabel">Unknown contact default</span><select className="input" name="smart_number_default" defaultValue={config.smart_number_default || "ask_owner"} disabled={!writable}><option value="ask_owner">Ask owner</option><option value="business">Business</option><option value="personal">Personal</option></select></label><input type="hidden" name="custom_instructions" value={config.custom_instructions ?? ""} /></div></section> : <section className="card"><div className="card-head"><div><h3>Custom instructions</h3><p>Business-specific context and response policy.</p></div></div><div className="card-body"><label className="field"><span className="flabel">Instructions</span><textarea className="textarea" style={{ minHeight: 280 }} name="custom_instructions" defaultValue={config.custom_instructions ?? ""} readOnly={!writable} /></label><input type="hidden" name="persona_name" value={config.persona_name ?? ""} /><input type="hidden" name="persona_tone" value={config.persona_tone ?? ""} /><input type="hidden" name="persona_language" value={(config.persona_language ?? []).join(", ")} /><input type="hidden" name="greeting_message" value={config.greeting_message ?? ""} /><input type="hidden" name="persona" value={JSON.stringify(config.persona || {})} /><input type="hidden" name="smart_number_default" value={config.smart_number_default || "ask_owner"} />{config.smart_number_mode ? <input type="hidden" name="smart_number_mode" value="on" /> : null}</div></section>}
        {formErrors.persona_language && <p className="err">{formErrors.persona_language}</p>}{save.isError && <p className="err">{(save.error as Error).message}</p>}{writable && <button className={`btn btn-primary ${save.isPending ? "btn-loading" : ""}`} disabled={save.isPending}>Save changes</button>}
      </div>
      <aside className="stack gap4"><section className="card card-selvedge"><div className="card-head"><div><h3>Live preview</h3><p>{config.business.name}</p></div></div><div className="card-body"><div className="thread"><div className="bubble-row"><span className="avatar avatar-sm avatar-accent">{(config.persona_name ?? "A").slice(0, 1)}</span><div><div className="bubble">{config.greeting_message || "Add a greeting to preview the opening message."}</div><div className="bubble-meta"><span className="badge badge-brand">{config.persona_name || "Agent"}</span><span className="badge badge-outline">{config.persona_tone || "default tone"}</span></div></div></div></div></div></section><section className="card card-pad"><span className="flabel">Prompt context</span><dl className="dl mt3"><div><dt>Business</dt><dd>{config.business.name}</dd></div><div><dt>Vertical</dt><dd>{config.business.vertical || "—"}</dd></div><div><dt>Knowledge</dt><dd>{config.knowledge_entries} entries</dd></div><div><dt>Entities</dt><dd>{config.entities}</dd></div></dl><hr className="thread-rule mt4" /><p className="hint mt3">Guardrails and limits are enforced elsewhere and are read-only on this API.</p></section></aside>
    </form>
  </div>;
}
