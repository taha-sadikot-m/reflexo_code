"use client";

import { Suspense, useEffect, useState } from "react";
import { useParams, useSearchParams } from "next/navigation";
import { API_URL } from "@/lib/api";
import { Brandmark } from "@/components/admin/Brandmark";
import { Badge, Button, Field, Input } from "@/components/ui";
import { Icon } from "@/components/Icon";
import { ErrorState, LoadingState } from "@/components/States";

type CatalogItem = {
  id: string;
  label: string;
  kind: string;
  connect: string;
  note: string;
  connected: boolean;
  status: string;
  account?: string;
  error?: string;
};

type Catalog = {
  business_name: string;
  widget_embed: string;
  expires_at?: string;
  items: CatalogItem[];
};

const ICON_FOR: Record<string, string> = {
  whatsapp: "wa",
  instagram: "ig",
  facebook: "fb",
  web_widget: "globe",
  gmail: "send",
  outlook: "send",
  slack: "message",
  razorpay: "receipt",
  fast2sms: "zap",
};

async function setupFetch<T>(token: string, suffix = "", init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const res = await fetch(`${API_URL}/api/setup/${encodeURIComponent(token)}${suffix}`, {
    ...init,
    headers,
    credentials: "omit",
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || JSON.stringify(body);
    } catch {
      /* ignore */
    }
    throw new Error(typeof detail === "string" ? detail : "Request failed");
  }
  return (await res.json()) as T;
}

function SetupInner() {
  const params = useParams<{ token: string }>();
  const search = useSearchParams();
  const token = params.token;
  const connect = search.get("connect");
  const connected = search.get("connected");
  const errorParam = search.get("error");
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [loadError, setLoadError] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [keysFor, setKeysFor] = useState<CatalogItem | null>(null);
  const [apiKey, setApiKey] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [notice, setNotice] = useState("");

  async function reload() {
    const data = await setupFetch<Catalog>(token);
    setCatalog(data);
    return data;
  }

  useEffect(() => {
    let cancelled = false;
    setupFetch<Catalog>(token)
      .then((data) => {
        if (!cancelled) setCatalog(data);
      })
      .catch((err: Error) => {
        if (!cancelled) setLoadError(err.message || "Setup link not found");
      });
    return () => {
      cancelled = true;
    };
  }, [token]);

  useEffect(() => {
    if (connected && connected !== "failed") {
      setNotice(`${connected} connected.`);
      void reload().catch(() => undefined);
    }
    if (errorParam) setNotice(errorParam);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- one-shot after OAuth return
  }, [connected, errorParam]);

  useEffect(() => {
    if (!catalog || !connect) return;
    const item = catalog.items.find((row) => row.id === connect);
    if (!item || item.connected) return;
    if (item.connect === "oauth_meta" || item.connect === "oauth_channel") {
      void startOauth(item.id);
    }
    if (item.connect === "api_key") setKeysFor(item);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [catalog, connect]);

  async function startOauth(integrationId: string) {
    setBusy(integrationId);
    try {
      const res = await setupFetch<{ oauth_url: string }>(token, "/oauth-url", {
        method: "POST",
        body: JSON.stringify({ integration_id: integrationId }),
      });
      window.location.assign(res.oauth_url);
    } catch (err) {
      setNotice(err instanceof Error ? err.message : "Could not start connect");
      setBusy(null);
    }
  }

  async function saveKeys() {
    if (!keysFor) return;
    setBusy(keysFor.id);
    try {
      await setupFetch(token, "/api-keys", {
        method: "POST",
        body: JSON.stringify({
          integration_id: keysFor.id,
          api_key: apiKey,
          api_secret: apiSecret || undefined,
        }),
      });
      setKeysFor(null);
      setApiKey("");
      setApiSecret("");
      setNotice(`${keysFor.label} connected.`);
      await reload();
    } catch (err) {
      setNotice(err instanceof Error ? err.message : "Could not save keys");
    } finally {
      setBusy(null);
    }
  }

  if (loadError) {
    return (
      <section className="setup-page">
        <ErrorState message={loadError} />
      </section>
    );
  }
  if (!catalog) {
    return (
      <section className="setup-page">
        <LoadingState label="Opening your setup page…" />
      </section>
    );
  }

  const done = catalog.items.filter((item) => item.connected).length;

  return (
    <section className="setup-page">
      <div className="setup-sheet">
        <Brandmark />
        <Badge tone="accent">Owner setup</Badge>
        <h1>{catalog.business_name}</h1>
        <p className="muted">
          Connect the channels and tools REFLEXO should use. {done} of {catalog.items.length} complete.
          {catalog.expires_at && <> This link expires {new Date(catalog.expires_at).toLocaleString()}.</>}
        </p>
        {notice && (
          <div className={`alert ${notice.includes("connected") ? "alert-pos" : "alert-warn"} mt4`}>
            <div>
              <p>{notice}</p>
            </div>
          </div>
        )}
        <div className="stack gap3 mt5">
          {catalog.items.map((item) => (
            <article key={item.id} className="card card-pad">
              <div className="icn-head">
                <span className="icn-logo">
                  <Icon name={ICON_FOR[item.id] || "plug"} />
                </span>
                <div className="grow">
                  <b>{item.label}</b>
                  <p className="muted">{item.note}</p>
                </div>
                <span className={`badge ${item.connected ? "badge-pos" : "badge-outline"}`}>
                  {item.connected ? "Connected" : "Not connected"}
                </span>
              </div>
              {!item.connected && item.connect === "seed" && (
                <p className="faint mt3">WhatsApp is provisioned by REFLEXO staff on the shared number.</p>
              )}
              {!item.connected && (item.connect === "oauth_meta" || item.connect === "oauth_channel") && (
                <Button className="mt3" loading={busy === item.id} onClick={() => void startOauth(item.id)}>
                  Connect {item.label}
                </Button>
              )}
              {!item.connected && item.connect === "api_key" && keysFor?.id !== item.id && (
                <Button className="mt3" variant="ghost" onClick={() => setKeysFor(item)}>
                  Enter API keys
                </Button>
              )}
              {keysFor?.id === item.id && (
                <div className="stack gap3 mt4">
                  <Field label={item.id === "razorpay" ? "Key ID" : "API key"} required>
                    <Input className="mono" value={apiKey} onChange={(e) => setApiKey(e.target.value)} />
                  </Field>
                  {item.id === "razorpay" && (
                    <Field label="Key secret" required>
                      <Input type="password" className="mono" value={apiSecret} onChange={(e) => setApiSecret(e.target.value)} />
                    </Field>
                  )}
                  <div className="row-flex gap2">
                    <Button loading={busy === item.id} onClick={() => void saveKeys()} disabled={!apiKey.trim()}>
                      Save
                    </Button>
                    <Button variant="ghost" onClick={() => setKeysFor(null)}>
                      Cancel
                    </Button>
                  </div>
                </div>
              )}
              {item.connect === "embed" && (
                <div className="mt3">
                  <pre className="mono setup-embed">{catalog.widget_embed}</pre>
                  <Button
                    className="mt2"
                    size="sm"
                    onClick={() => void navigator.clipboard.writeText(catalog.widget_embed)}
                  >
                    <Icon name="copy" /> Copy embed
                  </Button>
                </div>
              )}
            </article>
          ))}
        </div>
      </div>
    </section>
  );
}

export default function SetupPage() {
  return (
    <Suspense
      fallback={
        <section className="setup-page">
          <LoadingState label="Opening your setup page…" />
        </section>
      }
    >
      <SetupInner />
    </Suspense>
  );
}
