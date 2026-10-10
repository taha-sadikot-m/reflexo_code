"use client";

import { Suspense, useEffect, useRef, useState } from "react";
import { useParams, useSearchParams } from "next/navigation";
import { API_URL } from "@/lib/api";
import { Brandmark } from "@/components/admin/Brandmark";
import { Button, Input } from "@/components/ui";
import { ErrorState, LoadingState } from "@/components/States";

type Platform = {
  label: string;
  auth_type: "oauth2" | "basic" | "api_key";
  capabilities: string[];
};
type Account = {
  platform: string;
  account_display_name?: string;
  account_identifier?: string;
  is_active: boolean;
  connection_error?: string;
};
type HubState = {
  business_id: string;
  platforms: Record<string, Platform>;
  accounts: Account[];
};

function ConnectInner() {
  const { business_id: businessId } = useParams<{ business_id: string }>();
  const search = useSearchParams();
  const token = search.get("token") || "";
  const requestedConnection = search.get("connect") || "";
  const deepLinkStarted = useRef(false);
  const [state, setState] = useState<HubState | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [keysFor, setKeysFor] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [apiSecret, setApiSecret] = useState("");

  async function request<T>(suffix = "", init?: RequestInit): Promise<T> {
    const separator = suffix.includes("?") ? "&" : "?";
    const response = await fetch(
      `${API_URL}/api/connect/${encodeURIComponent(businessId)}${suffix}${separator}token=${encodeURIComponent(token)}`,
      {
        ...init,
        credentials: "omit",
        headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
      },
    );
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || response.statusText);
    }
    return response.json() as Promise<T>;
  }

  async function reload() {
    setState(await request<HubState>());
  }

  useEffect(() => {
    if (!token) {
      setError("This connection link is missing or invalid.");
      return;
    }
    reload().catch((reason) => setError(reason.message));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [businessId, token]);

  async function connectOAuth(platform: string) {
    setBusy(platform);
    try {
      const result = await request<{ url: string }>(`/auth/${platform}`);
      window.location.assign(result.url);
    } catch (reason) {
      setError((reason as Error).message);
      setBusy("");
    }
  }

  async function saveKey() {
    setBusy(keysFor);
    try {
      await request("/api-key", {
        method: "POST",
        body: JSON.stringify({ platform: keysFor, api_key: apiKey, api_secret: apiSecret || null }),
      });
      setKeysFor("");
      setApiKey("");
      setApiSecret("");
      await reload();
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setBusy("");
    }
  }

  async function disconnect(platform: string) {
    setBusy(platform);
    try {
      await request(`/${platform}`, { method: "DELETE" });
      await reload();
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setBusy("");
    }
  }

  useEffect(() => {
    if (!state || !requestedConnection || deepLinkStarted.current) return;
    const platform = state.platforms[requestedConnection];
    if (!platform || state.accounts.some((item) => item.platform === requestedConnection && item.is_active)) return;
    deepLinkStarted.current = true;
    if (platform.auth_type === "oauth2") {
      void connectOAuth(requestedConnection);
    } else {
      setKeysFor(requestedConnection);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state, requestedConnection]);

  if (error && !state) return <ErrorState message={error} />;
  if (!state) return <LoadingState label="Loading Connection Hub…" />;
  return <main className="setup-page"><section className="setup-sheet">
    <header className="row-flex between gap3"><Brandmark /><div><h1>Connect your tools</h1><p className="muted">Securely connect communication and business accounts.</p></div></header>
    {search.get("connected") ? <div className="alert alert-pos mt4">Connection completed successfully.</div> : null}
    {error ? <p className="err mt4">{error}</p> : null}
    <div className="grid g3 mt5">
      {Object.entries(state.platforms).map(([id, platform]) => {
        const account = state.accounts.find((item) => item.platform === id && item.is_active);
        return <article className="card integration-card" key={id}>
          <div className="icn-head"><div className="grow"><b>{platform.label}</b><p className="faint">{account?.account_display_name || account?.account_identifier || platform.capabilities.join(" · ")}</p></div><span className={`badge ${account ? "badge-pos" : "badge-outline"}`}>{account ? "connected" : "not connected"}</span></div>
          <div className="row-flex gap2 mt3">
            {account ? <Button variant="ghost" loading={busy === id} onClick={() => disconnect(id)}>Disconnect</Button> : platform.auth_type === "oauth2" ? <Button loading={busy === id} onClick={() => connectOAuth(id)}>Connect</Button> : <Button onClick={() => setKeysFor(id)}>Add API key</Button>}
          </div>
        </article>;
      })}
    </div>
    {keysFor ? <section className="card card-pad mt5"><h3>Connect {state.platforms[keysFor].label}</h3><div className="stack gap3 mt3"><Input value={apiKey} onChange={(event) => setApiKey(event.target.value)} placeholder="API key" />{state.platforms[keysFor].auth_type === "basic" ? <Input type="password" value={apiSecret} onChange={(event) => setApiSecret(event.target.value)} placeholder="API secret" /> : null}<div className="row-flex gap2"><Button loading={busy === keysFor} onClick={saveKey}>Validate and connect</Button><Button variant="ghost" onClick={() => setKeysFor("")}>Cancel</Button></div></div></section> : null}
  </section></main>;
}

export default function ConnectionHubPage() {
  return <Suspense fallback={<LoadingState label="Loading Connection Hub…" />}><ConnectInner /></Suspense>;
}
