"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useQueryClient } from "@tanstack/react-query";
import { api, canBill } from "@/lib/api";
import { useApp } from "@/components/Providers";
import { Forbidden } from "@/components/States";
import Link from "next/link";
import { Icon } from "@/components/Icon";
import { useToast } from "@/components/ui";
import { queryKeys } from "@/lib/queries/keys";
import { businessSchema, validationErrors, type FieldErrors } from "@/lib/schemas";

export default function NewBusinessPage() {
  const { admin } = useApp();
  const router = useRouter();
  const qc = useQueryClient();
  const [error, setError] = useState("");
  const [formErrors, setFormErrors] = useState<FieldErrors>({});
  const [submitting, setSubmitting] = useState(false);
  const { toast } = useToast();
  if (!canBill(admin?.role)) return <Forbidden />;

  async function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const fd = new FormData(e.currentTarget);
    const parsed = businessSchema.safeParse({
      name: fd.get("name"),
      owner_phone: fd.get("owner_phone"),
      owner_name: fd.get("owner_name"),
      owner_email: fd.get("owner_email"),
      vertical: fd.get("vertical") || "general",
      plan: fd.get("plan") || "free",
    });
    if (!parsed.success) {
      setFormErrors(validationErrors(parsed.error));
      return;
    }
    setSubmitting(true);
    setError("");
    setFormErrors({});
    try {
      const row = await api<{ id: string }>("/api/admin/businesses", {
        method: "POST",
        body: JSON.stringify({
          ...parsed.data,
          owner_email: parsed.data.owner_email || null,
          slug: fd.get("slug") || null,
          persona_name: fd.get("persona_name") || null,
          greeting_message: fd.get("greeting_message") || null,
        }),
      });
      await qc.invalidateQueries({ queryKey: queryKeys.businessesRoot });
      toast({ tone: "pos", title: "Business created", body: `${parsed.data.name} is ready for channel setup.` });
      router.push(`/businesses/${row.id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not create");
      setSubmitting(false);
    }
  }

  return (
    <div>
      <nav className="crumbs"><Link href="/businesses">Businesses</Link><Icon name="chev-right" className="ic ic-sm" /><b>New business</b></nav>
      <header className="page-head mt3"><div><h1>New business</h1><p className="page-sub">Create the tenant, owner identity and first agent persona.</p></div></header>
      <form className="grid g3" onSubmit={onSubmit}>
        <div className="card span2">
          <div className="card-head"><div><h3>Business identity</h3><p>Core details shown throughout the admin panel</p></div></div>
          <div className="card-body form-grid">
            <label className="field" data-invalid={formErrors.name ? "" : undefined}><span>Business name <i className="req">*</i></span><input className="input" name="name" required autoFocus placeholder="e.g. Green Bowl Kitchen" />{formErrors.name && <span className="err">{formErrors.name}</span>}</label>
            <label className="field"><span>Slug</span><input className="input mono" name="slug" placeholder="green-bowl-kitchen" pattern="[a-z0-9-]+" /><span className="hint">Lowercase letters, numbers and hyphens.</span></label>
            <label className="field"><span>Vertical</span><select className="select" name="vertical" defaultValue="general"><option value="general">General</option><option value="food">Food &amp; hospitality</option><option value="retail">Retail</option><option value="healthcare">Healthcare</option><option value="services">Professional services</option><option value="education">Education</option></select></label>
            <label className="field"><span>Plan</span><select className="select" name="plan" defaultValue="free"><option value="free">Free</option><option value="starter">Starter</option><option value="growth">Growth</option><option value="premium">Premium</option></select></label>
          </div>
          <div className="card-head"><div><h3>Owner contact</h3><p>Primary operational contact for approvals and setup</p></div></div>
          <div className="card-body form-grid">
            <label className="field"><span>Owner name</span><input className="input" name="owner_name" autoComplete="name" /></label>
            <label className="field" data-invalid={formErrors.owner_phone ? "" : undefined}><span>Owner phone <i className="req">*</i></span><input className="input mono" name="owner_phone" type="tel" required placeholder="+91…" autoComplete="tel" />{formErrors.owner_phone && <span className="err">{formErrors.owner_phone}</span>}</label>
            <label className="field full" data-invalid={formErrors.owner_email ? "" : undefined}><span>Owner email</span><input className="input" name="owner_email" type="email" autoComplete="email" placeholder="owner@example.com" />{formErrors.owner_email && <span className="err">{formErrors.owner_email}</span>}</label>
          </div>
          <div className="card-head"><div><h3>Agent welcome</h3><p>Optional starter identity; editable after creation</p></div></div>
          <div className="card-body form-grid">
            <label className="field"><span>Persona name</span><input className="input" name="persona_name" placeholder="Maya" /></label>
            <label className="field full"><span>Greeting message</span><textarea className="textarea" name="greeting_message" placeholder="Namaste! How can I help today?" /></label>
          </div>
          {error && <div className="alert alert-neg"><Icon name="alert" /><div><b>Business was not created</b><p>{error}</p></div></div>}
          <div className="card-foot"><Link className="btn btn-ghost" href="/businesses">Cancel</Link><button className={`btn btn-primary ${submitting ? "btn-loading" : ""}`} type="submit" disabled={submitting}>Create business</button></div>
        </div>
        <aside className="stack gap4">
          <div className="card card-selvedge card-pad"><span className="badge badge-accent">Before you create</span><h3 className="mt3">A clean first warp</h3><p className="muted mt2">Creating a business does not connect live channels. Add credentials and complete onboarding from the business detail after creation.</p></div>
          <div className="card card-pad"><span className="flabel">Access control</span><div className="dl mt3"><div><dt>Create tenants</dt><dd>Super admin</dd></div><div><dt>Edit tenants</dt><dd>Admin+</dd></div><div><dt>View tenant</dt><dd>Scoped roles</dd></div></div></div>
        </aside>
      </form>
    </div>
  );
}
