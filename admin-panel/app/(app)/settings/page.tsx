"use client";

import { useState } from "react";
import { api } from "@/lib/api";
import { useRouter } from "next/navigation";
import { useApp } from "@/components/Providers";

export default function SettingsPage() {
  const router = useRouter();
  const { admin, businessId, setAdmin } = useApp();
  const [palette, setPalette] = useState<"indigo" | "peacock" | "oxblood">("indigo");
  const [loggingOut, setLoggingOut] = useState(false);
  const [error, setError] = useState("");

  async function logout() {
    setLoggingOut(true);
    setError("");
    try {
      await api("/api/admin/auth/logout", { method: "POST" });
      sessionStorage.removeItem("reflexo_admin_token");
      setAdmin(null);
      router.replace("/login");
    } catch (cause) {
      setError((cause as Error).message);
      setLoggingOut(false);
    }
  }

  function choosePalette(value: typeof palette) {
    setPalette(value);
    document.documentElement.dataset.palette = value;
  }

  return <div>
    <header className="page-head"><div><p className="eyebrow">Administration</p><h1>Settings</h1><p className="page-sub">Session details, interface preferences and access controls.</p></div></header>
    <div className="tabs" role="tablist"><button role="tab" aria-selected="true">Platform</button><button role="tab" aria-selected="false" disabled>Staff</button><button role="tab" aria-selected="false" disabled>Roles</button><button role="tab" aria-selected="false" disabled>Audit log</button></div>
    <div className="grid g2 mt5">
      <section className="card"><div className="card-head"><div><h3>Appearance</h3><p>Handloom palette for this browser session</p></div></div><div className="card-body stack gap4"><div className="field"><span className="flabel">Palette</span><div className="segmented wrap">{(["indigo", "peacock", "oxblood"] as const).map((value) => <button key={value} aria-pressed={palette === value} onClick={() => choosePalette(value)}>{value[0].toUpperCase() + value.slice(1)}</button>)}</div><span className="hint">Colours update immediately without changing shared theme files.</span></div><hr className="thread-rule" /><dl className="dl"><div><dt>Density shortcut</dt><dd className="mono">Shift+D</dd></div><div><dt>Sidebar shortcut</dt><dd className="mono">⌘/Ctrl+B</dd></div></dl></div></section>
      <section className="card"><div className="card-head"><div><h3>Current session</h3><p>Authenticated administrator</p></div><span className={`badge ${admin?.is_active ? "badge-pos" : "badge-warn"}`}>{admin?.is_active ? "active" : "unknown"}</span></div><div className="card-body"><dl className="dl"><div><dt>Name</dt><dd>{admin?.name || "—"}</dd></div><div><dt>Email</dt><dd>{admin?.email || "—"}</dd></div><div><dt>Role</dt><dd><span className="badge badge-brand mono">{admin?.role || "—"}</span></dd></div><div><dt>Business scope</dt><dd className="mono">{businessId ? businessId.slice(0, 8) : "none"}</dd></div></dl><hr className="thread-rule mt4" /><p className="hint mt3">Authorization is enforced by the API. Hidden controls do not replace server-side role checks.</p></div></section>
    </div>
    <section className="card card-selvedge card-selvedge-neg mt4"><div className="card-head"><div><h3>Session controls</h3><p>End this administrator session on the current browser.</p></div></div><div className="card-body"><button className={`btn btn-solid-danger ${loggingOut ? "btn-loading" : ""}`} disabled={loggingOut} onClick={logout}>Log out</button>{error && <p className="err mt3">{error}</p>}</div></section>
  </div>;
}
