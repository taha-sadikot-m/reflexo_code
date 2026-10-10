"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, canMutate, API_URL } from "@/lib/api";
import { useApp } from "@/components/Providers";
import { EmptyState, ErrorState, LoadingState } from "@/components/States";
import { Button, Dialog, useToast } from "@/components/ui";
import { Icon } from "@/components/Icon";
import { queryKeys } from "@/lib/queries/keys";

type Health = { channel: string; healthy: number; erroring: number; total: number };
type Channel = {
  id: string;
  channel: string;
  channel_display_name?: string;
  channel_account_id?: string;
  is_active: boolean;
  error_message?: string;
  last_synced_at?: string;
  token_expires_at?: string;
  meta_page_data?: Record<string, unknown>;
};
type ConnectedAccount = {
  integration_id: string;
  connected_account_id: string;
  account_identifier?: string;
  account_display_name?: string;
  is_active?: boolean;
  connection_error?: string;
  updated_at?: string;
};
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
type Catalog = { business_name: string; widget_embed: string; items: CatalogItem[] };
type FeatureStatus = {
  id: string;
  label: string;
  live: boolean;
  blocked: boolean;
  missing_scopes: string[];
  missing_token_scopes: string[];
  unapproved_app_permissions: string[];
  missing_page_fields: string[];
  missing_app_fields: string[];
  reason: string;
};
type CapabilityItem = {
  channel: string;
  account?: string;
  account_id?: string;
  is_active: boolean;
  error?: string | null;
  integration?: string | null;
  granted_scopes: string[];
  token_valid: boolean;
  page_id?: string | null;
  page_fields: string[];
  features: FeatureStatus[];
};
type Capabilities = {
  business_id: string;
  app_subscriptions: Record<string, string[]>;
  app_permissions: Record<string, string>;
  app_permissions_checked: boolean;
  instagram_comments_subscribed: boolean;
  instagram_messages_subscribed: boolean;
  items: CapabilityItem[];
};
type SetupLink = {
  id: string;
  expires_at?: string;
  revoked_at?: string | null;
  created_at?: string;
  active?: boolean;
};
type Minted = { id: string; token: string; url: string; expires_at?: string };
type MetaOAuthMode = "normal" | "rerequest" | "fresh";

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

function metaAccount(channel: Channel): Record<string, unknown> {
  const metadata = channel.meta_page_data || {};
  const nested = channel.channel.startsWith("instagram")
    ? metadata.instagram
    : metadata;
  return nested && typeof nested === "object"
    ? nested as Record<string, unknown>
    : metadata;
}

function storageKey(businessId: string) {
  return `reflexo.setupLink.${businessId}`;
}

function readMinted(businessId: string): Minted | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = sessionStorage.getItem(storageKey(businessId));
    return raw ? (JSON.parse(raw) as Minted) : null;
  } catch {
    return null;
  }
}

async function copyText(value: string) {
  await navigator.clipboard.writeText(value);
}

export default function IntegrationsPage() {
  const { businessId, admin } = useApp();
  const mutate = canMutate(admin?.role);
  const isSuperAdmin = admin?.role === "super_admin";
  const qc = useQueryClient();
  const { toast } = useToast();
  const [shareOpen, setShareOpen] = useState(false);
  const [minted, setMinted] = useState<Minted | null>(() =>
    businessId ? readMinted(businessId) : null,
  );
  const [keysFor, setKeysFor] = useState<CatalogItem | null>(null);
  const [apiKey, setApiKey] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [revokePrevious, setRevokePrevious] = useState(true);

  useEffect(() => {
    setMinted(businessId ? readMinted(businessId) : null);
  }, [businessId]);

  useEffect(() => {
    const refreshAfterOAuth = () => {
      if (document.visibilityState === "visible") {
        void qc.invalidateQueries({
          queryKey: queryKeys.integrationCapabilities(businessId),
        });
        void qc.invalidateQueries({
          queryKey: queryKeys.integrationCatalog(businessId),
        });
      }
    };
    window.addEventListener("focus", refreshAfterOAuth);
    return () => window.removeEventListener("focus", refreshAfterOAuth);
  }, [businessId, qc]);

  const health = useQuery({
    queryKey: queryKeys.integrationHealth,
    queryFn: () => api<{ channels: Health[]; connected_accounts: number }>("/api/admin/integrations/health"),
  });
  const channels = useQuery({
    queryKey: ["channels", businessId],
    enabled: !!businessId,
    queryFn: () => api<{ items: Channel[] }>(`/api/admin/integrations/channels/${businessId}`),
  });
  const accounts = useQuery({
    queryKey: ["connected-accounts", businessId],
    enabled: !!businessId,
    queryFn: () => api<{ items: ConnectedAccount[] }>(`/api/admin/integrations/accounts/${businessId}`),
  });
  const catalog = useQuery({
    queryKey: queryKeys.integrationCatalog(businessId),
    enabled: !!businessId,
    queryFn: () => api<Catalog>(`/api/admin/integrations/${businessId}/catalog`),
  });
  const links = useQuery({
    queryKey: queryKeys.setupLinks(businessId),
    enabled: !!businessId,
    queryFn: () => api<{ items: SetupLink[] }>(`/api/admin/integrations/${businessId}/setup-links`),
  });
  const capabilities = useQuery({
    queryKey: queryKeys.integrationCapabilities(businessId),
    enabled: !!businessId,
    queryFn: () => api<Capabilities>(`/api/admin/integrations/${businessId}/capabilities`),
  });

  const test = useMutation({
    mutationFn: () =>
      api<{ ok: boolean; active_channels: number }>(`/api/admin/integrations/channels/${businessId}/test`, {
        method: "POST",
      }),
  });

  function invalidateAll() {
    return Promise.all([
      qc.invalidateQueries({ queryKey: queryKeys.integrationCatalog(businessId) }),
      qc.invalidateQueries({ queryKey: queryKeys.integrationCapabilities(businessId) }),
      qc.invalidateQueries({ queryKey: queryKeys.setupLinks(businessId) }),
      qc.invalidateQueries({ queryKey: ["channels", businessId] }),
      qc.invalidateQueries({ queryKey: ["connected-accounts", businessId] }),
      qc.invalidateQueries({ queryKey: queryKeys.integrationHealth }),
    ]);
  }

  const mint = useMutation({
    mutationFn: () =>
      api<Minted>(`/api/admin/integrations/${businessId}/setup-links`, {
        method: "POST",
        body: JSON.stringify({ ttl_hours: 168, revoke_previous: revokePrevious }),
      }),
    onSuccess: (data) => {
      setMinted(data);
      sessionStorage.setItem(storageKey(businessId), JSON.stringify(data));
      toast({ tone: "pos", title: "Setup page ready", body: "Copy the link and send it to the owner. It is shown only once." });
      void invalidateAll();
    },
    onError: (err) => toast({ tone: "neg", title: "Could not create link", body: err.message }),
  });

  const revoke = useMutation({
    mutationFn: (id: string) =>
      api(`/api/admin/integrations/${businessId}/setup-links/${id}/revoke`, { method: "POST" }),
    onSuccess: () => {
      setMinted(null);
      sessionStorage.removeItem(storageKey(businessId));
      toast({ tone: "warn", title: "Setup link revoked" });
      void invalidateAll();
    },
  });

  const seedWa = useMutation({
    mutationFn: () => api(`/api/admin/integrations/${businessId}/seed-whatsapp`, { method: "POST" }),
    onSuccess: () => {
      toast({ tone: "pos", title: "WhatsApp seeded", body: "The shared platform number is mapped to this business." });
      void invalidateAll();
    },
    onError: (err) => toast({ tone: "neg", title: "WhatsApp seed failed", body: err.message }),
  });

  const ensureWidget = useMutation({
    mutationFn: () => api(`/api/admin/integrations/${businessId}/ensure-widget`, { method: "POST" }),
    onSuccess: () => {
      toast({ tone: "pos", title: "Widget channel ready" });
      void invalidateAll();
    },
  });

  const oauth = useMutation({
    mutationFn: ({ integration_id, mode }: { integration_id: string; mode: MetaOAuthMode }) =>
      api<{ oauth_url: string }>(`/api/admin/integrations/${businessId}/oauth-url`, {
        method: "POST",
        body: JSON.stringify({ integration_id, mode }),
      }),
    onSuccess: (data, variables) => {
      const popup = window.open(data.oauth_url, "_blank");
      if (!popup) {
        toast({ tone: "warn", title: "OAuth popup blocked", body: "Allow popups and try again." });
        return;
      }
      try {
        popup.opener = null;
      } catch {
        // Cross-origin popup isolation is best-effort.
      }
      const timer = window.setInterval(() => {
        if (!popup.closed) return;
        window.clearInterval(timer);
        void refreshAfterOAuth(variables.integration_id, variables.mode);
      }, 750);
    },
    onError: (err) => toast({ tone: "neg", title: "Could not start OAuth", body: err.message }),
  });

  const subscribeIgComments = useMutation({
    mutationFn: () =>
      api(`/api/admin/integrations/${businessId}/subscribe-instagram-comments`, { method: "POST" }),
    onSuccess: () => {
      toast({ tone: "pos", title: "Instagram comments subscribed", body: "App-level comments webhooks are enabled." });
      void invalidateAll();
    },
    onError: (err) => toast({ tone: "neg", title: "Could not subscribe Instagram comments", body: err.message }),
  });

  const subscribePageFeed = useMutation({
    mutationFn: () =>
      api(`/api/admin/integrations/${businessId}/subscribe-page-fields`, {
        method: "POST",
        body: JSON.stringify({ channel: "facebook_messenger", fields: ["feed", "messages"] }),
      }),
    onSuccess: () => {
      toast({ tone: "pos", title: "Page webhooks subscribed", body: "Facebook comment (feed) webhooks are enabled." });
      void invalidateAll();
    },
    onError: (err) => toast({ tone: "neg", title: "Could not subscribe Page fields", body: err.message }),
  });

  const subscribeIgWebhooks = useMutation({
    mutationFn: () =>
      api(`/api/admin/integrations/${businessId}/subscribe-instagram-webhooks`, {
        method: "POST",
        body: JSON.stringify({ fields: ["messages", "comments"] }),
      }),
    onSuccess: () => {
      toast({ tone: "pos", title: "Instagram webhooks subscribed", body: "App-level messages and comments are enabled." });
      void invalidateAll();
    },
    onError: (err) => toast({ tone: "neg", title: "Could not subscribe Instagram webhooks", body: err.message }),
  });

  const saveKeys = useMutation({
    mutationFn: () =>
      api(`/api/admin/integrations/${businessId}/api-keys`, {
        method: "POST",
        body: JSON.stringify({
          integration_id: keysFor?.id,
          api_key: apiKey,
          api_secret: apiSecret || undefined,
        }),
      }),
    onSuccess: () => {
      toast({ tone: "pos", title: `${keysFor?.label} connected` });
      setKeysFor(null);
      setApiKey("");
      setApiSecret("");
      void invalidateAll();
    },
    onError: (err) => toast({ tone: "neg", title: "Keys were not saved", body: err.message }),
  });

  const disconnect = useMutation({
    mutationFn: (integration_id: string) =>
      api(`/api/admin/integrations/${businessId}/accounts/${integration_id}/disconnect`, { method: "POST" }),
    onSuccess: () => {
      toast({ tone: "warn", title: "Disconnected" });
      void invalidateAll();
    },
    onError: (err) => toast({ tone: "neg", title: "Disconnect failed", body: err.message }),
  });

  async function copySetup(connect?: string) {
    let current = minted || readMinted(businessId);
    if (!current?.url) {
      const created = await mint.mutateAsync();
      current = created;
    }
    const url = connect ? `${current.url}?connect=${encodeURIComponent(connect)}` : current.url;
    await copyText(url);
    toast({ tone: "pos", title: "Copied", body: connect ? `Deep link for ${connect}` : "Setup page URL" });
  }

  async function refreshAfterOAuth(integrationId: string, mode: MetaOAuthMode) {
    await invalidateAll();
    if (integrationId !== "instagram" && integrationId !== "facebook") {
      toast({ tone: "pos", title: "Connection flow completed", body: "Integration status refreshed." });
      return;
    }
    const refreshed = await capabilities.refetch();
    const blocked = Array.from(
      new Set(
        (refreshed.data?.items ?? []).flatMap((item) =>
          item.features.flatMap((feature) => feature.unapproved_app_permissions),
        ),
      ),
    );
    toast({
      tone: blocked.length ? "warn" : "pos",
      title: mode === "fresh" ? "Fresh reconnect completed" : "Permission check completed",
      body: blocked.length
        ? `Account refreshed. Meta App Review still blocks ${blocked.length} permission${blocked.length === 1 ? "" : "s"}.`
        : "Account and available permissions refreshed.",
    });
  }

  if (!businessId || health.isLoading || channels.isLoading || catalog.isLoading) {
    return <LoadingState label="Checking connector health…" />;
  }
  if (channels.isError || health.isError || catalog.isError) {
    return <ErrorState message={((channels.error || health.error || catalog.error) as Error).message} />;
  }

  const items = channels.data?.items ?? [];
  const catalogItems = catalog.data?.items ?? [];
  const healthy = items.filter((channel) => channel.is_active && !channel.error_message).length;
  const warning = items.filter((channel) => !!channel.error_message).length;
  const activeLinks = (links.data?.items || []).filter((link) => link.active);
  const unapprovedAppPermissions = Array.from(
    new Set(
      (capabilities.data?.items ?? []).flatMap((item) =>
        item.features.flatMap((feature) => feature.unapproved_app_permissions),
      ),
    ),
  ).sort();
  const pageFeedSubscribed = (capabilities.data?.items ?? []).some(
    (item) => item.channel === "facebook_messenger" && item.page_fields.includes("feed"),
  );

  return (
    <div>
      <header className="page-head">
        <div>
          <p className="eyebrow">Channels & services</p>
          <h1>Integrations</h1>
          <p className="page-sub">Connect channels and tools, or share a setup page with the owner.</p>
        </div>
        <div className="row-flex gap2">
          {mutate && (
            <Button onClick={() => setShareOpen(true)}>
              <Icon name="link" /> Share setup page
            </Button>
          )}
          {mutate && (
            <Button disabled={test.isPending} loading={test.isPending} onClick={() => test.mutate()}>
              Run health check
            </Button>
          )}
        </div>
      </header>

      <div className="grid g3">
        <article className="card stat">
          <span className="eyebrow">Healthy</span>
          <b className="num">{healthy}</b>
          <p className="muted">of {items.length} channels</p>
        </article>
        <article className="card stat">
          <span className="eyebrow">Needs attention</span>
          <b className="num">{warning}</b>
        </article>
        <article className="card stat">
          <span className="eyebrow">OAuth connections</span>
          <b className="num">{health.data?.connected_accounts ?? 0}</b>
        </article>
      </div>

      {items.some((channel) => Object.keys(channel.meta_page_data || {}).length > 0) && (
        <section className="mt5">
          <div className="card-head" style={{ paddingInline: 0 }}>
            <div><h3>Meta account profiles</h3><p>Business details pulled from Graph during connection.</p></div>
          </div>
          <div className="grid g3 mt3">
            {items.filter((channel) => Object.keys(channel.meta_page_data || {}).length > 0).map((channel) => {
              const profile = metaAccount(channel);
              const followers = profile.followers_count ?? profile.fan_count;
              return <article className="card card-pad" key={`meta-${channel.id}`}>
                <div className="row-flex between gap2"><b>{String(profile.username || profile.name || channel.channel_display_name || channel.channel)}</b><span className="badge badge-brand">{channel.channel}</span></div>
                {profile.category ? <p className="faint mt2">{String(profile.category)}</p> : null}
                {profile.biography || profile.about ? <p className="muted mt2">{String(profile.biography || profile.about)}</p> : null}
                {followers !== undefined ? <p className="mt2"><b>{String(followers)}</b> followers</p> : null}
              </article>;
            })}
          </div>
        </section>
      )}

      {test.isSuccess && (
        <div className={`alert ${test.data.ok ? "alert-pos" : "alert-warn"} mt4`}>
          <div>
            <b>{test.data.ok ? "Channel test passed" : "No active channel passed"}</b>
            <p>
              {test.data.active_channels} active connection{test.data.active_channels === 1 ? "" : "s"} detected.
            </p>
          </div>
        </div>
      )}
      {test.isError && <p className="err mt4">{(test.error as Error).message}</p>}

      <section className="mt5">
        <div className="card-head" style={{ paddingInline: 0 }}>
          <div>
            <h3>Catalog</h3>
            <p>Share the combined page, or copy a single-integration deep link.</p>
          </div>
        </div>
        {catalogItems.length === 0 ? (
          <EmptyState title="No integrations" body="Catalog could not be loaded." />
        ) : (
          <div className="grid g3 mt4">
            {catalogItems.map((item) => {
              const state = item.status === "connected" ? "healthy" : item.status === "error" ? "error" : "off";
              return (
                <article key={item.id} className={`card integration-card ${item.status === "error" ? "card-selvedge card-selvedge-neg" : ""}`}>
                  <div className="icn-head">
                    <span className="icn-logo">
                      <Icon name={ICON_FOR[item.id] || "plug"} />
                    </span>
                    <div className="grow">
                      <b>{item.label}</b>
                      <div className="faint" style={{ fontSize: "var(--text-2xs)" }}>
                        {item.account || item.note}
                      </div>
                    </div>
                    <span className={`badge ${state === "healthy" ? "badge-pos" : state === "error" ? "badge-neg" : "badge-outline"}`}>
                      {item.status}
                    </span>
                  </div>
                  <hr className="thread-rule" />
                  {item.error && <p className="err">{item.error}</p>}
                  <div className="row-flex gap2 wrap mt3">
                    {mutate && (
                      <Button size="sm" onClick={() => void copySetup(item.id)}>
                        <Icon name="copy" /> Copy link
                      </Button>
                    )}
                    {mutate && item.connect === "oauth_meta" && (
                      <>
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() =>
                            oauth.mutate({
                              integration_id: item.id,
                              mode: item.connected ? "rerequest" : "normal",
                            })
                          }
                        >
                          <Icon name="external" />{" "}
                          {item.connected ? "Repair permissions" : "Open connect"}
                        </Button>
                        {item.connected && (
                          <Button
                            size="sm"
                            variant="ghost"
                            onClick={() =>
                              oauth.mutate({
                                integration_id: item.id,
                                mode: "fresh",
                              })
                            }
                          >
                            <Icon name="external" /> Fresh reconnect
                          </Button>
                        )}
                      </>
                    )}
                    {mutate && item.connect === "oauth_channel" && (
                      <Button size="sm" variant="ghost" onClick={() => oauth.mutate({ integration_id: item.id, mode: "normal" })}>
                        <Icon name="external" /> Open connect
                      </Button>
                    )}
                    {mutate && item.connect === "api_key" && (
                      <Button size="sm" variant="ghost" onClick={() => setKeysFor(item)}>
                        <Icon name="key" /> Enter keys
                      </Button>
                    )}
                    {mutate && item.connect === "seed" && !item.connected && (
                      <Button size="sm" variant="primary" loading={seedWa.isPending} onClick={() => seedWa.mutate()}>
                        Seed WhatsApp
                      </Button>
                    )}
                    {mutate && item.connect === "embed" && (
                      <Button size="sm" variant="ghost" onClick={() => ensureWidget.mutate()}>
                        Enable channel
                      </Button>
                    )}
                    {mutate && item.connect === "embed" && catalog.data?.widget_embed && (
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => {
                          void copyText(catalog.data!.widget_embed);
                          toast({ tone: "pos", title: "Embed snippet copied" });
                        }}
                      >
                        <Icon name="clip" /> Copy embed
                      </Button>
                    )}
                    {mutate && item.kind === "connected_account" && item.connected && (
                      <Button size="sm" variant="danger" onClick={() => disconnect.mutate(item.id)}>
                        Disconnect
                      </Button>
                    )}
                  </div>
                  {item.connect === "oauth_meta" && item.connected && (
                    <p className="faint mt2">
                      Repair re-asks for declined permissions. Fresh reconnect
                      forces credential confirmation; to switch Facebook users,
                      log out of Facebook or open the link in a private window.
                    </p>
                  )}
                </article>
              );
            })}
          </div>
        )}
      </section>

      <section className="card mt5">
        <div className="card-head">
          <div>
            <h3>Meta permissions</h3>
            <p>Granted scopes, live vs blocked features, and comment/DM webhook status.</p>
          </div>
          {isSuperAdmin && (
            <div className="row-flex gap1 wrap">
              <Button
                size="sm"
                variant="ghost"
                loading={subscribePageFeed.isPending}
                onClick={() => subscribePageFeed.mutate()}
              >
                Subscribe Page feed
              </Button>
              <Button
                size="sm"
                variant="ghost"
                loading={subscribeIgWebhooks.isPending}
                onClick={() => subscribeIgWebhooks.mutate()}
              >
                Subscribe Instagram webhooks
              </Button>
              <Button
                size="sm"
                variant="ghost"
                loading={subscribeIgComments.isPending}
                onClick={() => subscribeIgComments.mutate()}
              >
                Subscribe Instagram comments
              </Button>
            </div>
          )}
        </div>
        {capabilities.isLoading ? (
          <LoadingState label="Checking Meta permissions…" />
        ) : capabilities.isError ? (
          <div className="card-body">
            <p className="muted">{(capabilities.error as Error).message}</p>
          </div>
        ) : (
          <div className="card-body stack gap3">
            <div className="row-flex gap2 wrap">
              <span className={`badge ${capabilities.data?.items.every((item) => item.token_valid) ? "badge-pos" : "badge-neg"}`}>
                Account tokens: {capabilities.data?.items.filter((item) => item.token_valid).length ?? 0}/{capabilities.data?.items.length ?? 0} valid
              </span>
              <span className={`badge ${pageFeedSubscribed ? "badge-pos" : "badge-warn"}`}>
                Page feed webhook: {pageFeedSubscribed ? "live" : "missing"}
              </span>
              <span className={`badge ${capabilities.data?.instagram_messages_subscribed ? "badge-pos" : "badge-warn"}`}>
                Instagram messages webhook: {capabilities.data?.instagram_messages_subscribed ? "live" : "missing"}
              </span>
              <span className={`badge ${capabilities.data?.instagram_comments_subscribed ? "badge-pos" : "badge-warn"}`}>
                Instagram comments webhook: {capabilities.data?.instagram_comments_subscribed ? "live" : "missing"}
              </span>
              <span className={`badge ${unapprovedAppPermissions.length ? "badge-warn" : "badge-pos"}`}>
                Meta App Review: {unapprovedAppPermissions.length ? `${unapprovedAppPermissions.length} blocked` : "approved"}
              </span>
            </div>
            {unapprovedAppPermissions.length > 0 && (
              <div className="alert alert-warn">
                <div>
                  <b>Meta app approval required</b>
                  <p>
                    The account connection is healthy, but reconnecting cannot
                    fix these app-level blocks. In Meta App Dashboard, open App
                    Review → Permissions and Features, request Advanced Access,
                    then publish the app.
                  </p>
                  <div className="row-flex gap1 wrap mt2">
                    {unapprovedAppPermissions.map((permission) => (
                      <span key={permission} className="badge badge-warn">{permission}</span>
                    ))}
                  </div>
                </div>
              </div>
            )}
            {(capabilities.data?.items.length ?? 0) === 0 ? (
              <p className="muted">No Instagram or Facebook channels connected.</p>
            ) : (
              capabilities.data!.items.map((item) => (
                <article key={`${item.channel}-${item.account_id}`} className="stack gap2">
                  <div className="row-flex between gap2 wrap">
                    <b>
                      {item.channel} · {item.account || item.account_id}
                    </b>
                    <span className={`badge ${item.token_valid ? "badge-pos" : "badge-neg"}`}>
                      {item.token_valid ? "token valid" : "token invalid"}
                    </span>
                  </div>
                  {item.error && <p className="err">{item.error}</p>}
                  <p className="faint">
                    Scopes: {item.granted_scopes.length ? item.granted_scopes.join(", ") : "none reported"}
                  </p>
                  <p className="faint">
                    Page fields: {item.page_fields.length ? item.page_fields.join(", ") : "none"}
                  </p>
                  <div className="stack gap1">
                    {item.features.map((feature) => (
                      <div key={feature.id} className="row-flex between gap2 wrap">
                        <span>{feature.label}</span>
                        <span className={`badge ${feature.live ? "badge-pos" : "badge-neg"}`}>
                          {feature.live
                            ? "live"
                            : feature.unapproved_app_permissions.length
                              ? "app review required"
                              : feature.reason || "blocked"}
                        </span>
                      </div>
                    ))}
                  </div>
                </article>
              ))
            )}
          </div>
        )}
      </section>

      {catalog.data?.widget_embed && (
        <section className="card mt5">
          <div className="card-head">
            <div>
              <h3>Web widget embed</h3>
              <p>Paste before <code>&lt;/body&gt;</code> on the owner site. API is {API_URL}.</p>
            </div>
          </div>
          <div className="card-body">
            <pre className="mono setup-embed">{catalog.data.widget_embed}</pre>
          </div>
        </section>
      )}

      <section className="card mt5">
        <div className="card-head">
          <div>
            <h3>Connected accounts</h3>
            <p>Channel Framework OAuth and API-key services</p>
          </div>
          <span className="badge badge-outline">{accounts.data?.items.length ?? 0}</span>
        </div>
        {accounts.isLoading ? (
          <LoadingState />
        ) : accounts.isError ? (
          <ErrorState message={(accounts.error as Error).message} />
        ) : (accounts.data?.items.length ?? 0) === 0 ? (
          <div className="card-body">
            <p className="muted">No connected accounts for this business.</p>
          </div>
        ) : (
          <div className="tablewrap" style={{ border: 0, boxShadow: "none", borderRadius: 0 }}>
            <table className="tbl">
              <thead>
                <tr>
                  <th>Provider</th>
                  <th>Account</th>
                  <th>Status</th>
                  <th>Updated</th>
                </tr>
              </thead>
              <tbody>
                {accounts.data?.items.map((connection) => (
                  <tr key={`${connection.integration_id}-${connection.connected_account_id}`}>
                    <td className="cell-strong">{connection.integration_id}</td>
                    <td className="mono">{connection.account_display_name || connection.account_identifier || connection.connected_account_id}</td>
                    <td>
                      <span className={`badge ${connection.connection_error ? "badge-neg" : "badge-pos"}`}>
                        {connection.connection_error || (connection.is_active === false ? "inactive" : "connected")}
                      </span>
                    </td>
                    <td>{connection.updated_at ? new Date(connection.updated_at).toLocaleDateString() : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <Dialog
        open={shareOpen}
        onClose={() => setShareOpen(false)}
        title="Share setup page"
        description="The owner opens this page without an admin login and finishes Instagram, Gmail, payments, and the widget."
        footer={
          <>
            <Button variant="ghost" onClick={() => setShareOpen(false)}>
              Close
            </Button>
            {mutate && (
              <Button loading={mint.isPending} onClick={() => mint.mutate()}>
                Create link
              </Button>
            )}
          </>
        }
      >
        <label className="row-flex gap2">
          <input type="checkbox" checked={revokePrevious} onChange={(e) => setRevokePrevious(e.target.checked)} />
          <span className="muted">Revoke previous active links</span>
        </label>
        {minted && (
          <div className="stack gap2 mt4">
            <p className="mono setup-embed">{minted.url}</p>
            <Button
              onClick={() => {
                void copyText(minted.url);
                toast({ tone: "pos", title: "Copied setup URL" });
              }}
            >
              <Icon name="copy" /> Copy URL
            </Button>
            <p className="faint">Expires {minted.expires_at ? new Date(minted.expires_at).toLocaleString() : "in 7 days"}.</p>
          </div>
        )}
        {activeLinks.length > 0 && (
          <div className="stack gap2 mt4">
            <p className="muted">{activeLinks.length} active link{activeLinks.length === 1 ? "" : "s"} on file.</p>
            {activeLinks.map((link) => (
              <div key={link.id} className="row-flex between gap2">
                <span className="faint mono">{link.id.slice(0, 8)}… expires {link.expires_at ? new Date(link.expires_at).toLocaleDateString() : "—"}</span>
                {mutate && (
                  <Button size="sm" variant="danger" onClick={() => revoke.mutate(link.id)}>
                    Revoke
                  </Button>
                )}
              </div>
            ))}
          </div>
        )}
      </Dialog>

      <Dialog
        open={!!keysFor}
        onClose={() => setKeysFor(null)}
        title={keysFor ? `Connect ${keysFor.label}` : "Connect"}
        description={keysFor?.note}
        footer={
          <>
            <Button variant="ghost" onClick={() => setKeysFor(null)}>
              Cancel
            </Button>
            <Button loading={saveKeys.isPending} onClick={() => saveKeys.mutate()} disabled={!apiKey.trim()}>
              Save keys
            </Button>
          </>
        }
      >
        <label className="field">
          <span>{keysFor?.id === "razorpay" ? "Key ID" : "API key"}</span>
          <input className="input mono" value={apiKey} onChange={(e) => setApiKey(e.target.value)} autoComplete="off" />
        </label>
        {keysFor?.id === "razorpay" && (
          <label className="field mt3">
            <span>Key secret</span>
            <input className="input mono" type="password" value={apiSecret} onChange={(e) => setApiSecret(e.target.value)} autoComplete="off" />
          </label>
        )}
      </Dialog>
    </div>
  );
}
