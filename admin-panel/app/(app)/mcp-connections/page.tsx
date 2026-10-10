"use client";

import { FormEvent, useEffect, useMemo, useState } from "react";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { API_URL, api, canMutate } from "@/lib/api";
import { queryKeys } from "@/lib/queries/keys";
import { useApp } from "@/components/Providers";
import { EmptyState, ErrorState, LoadingState } from "@/components/States";
import { Icon } from "@/components/Icon";
import { Button, Dialog, Field, useToast } from "@/components/ui";

type InputSpec = {
  description: string;
  required: boolean;
  secret: boolean;
  default?: string;
  choices: string[];
};
type RegistryRemote = {
  index: number;
  url: string;
  variables: Record<string, InputSpec>;
  headers: Array<InputSpec & { name: string }>;
};
type RegistryItem = {
  name: string;
  version: string;
  title: string;
  description: string;
  icon?: string | null;
  repository_url?: string | null;
  remotes: RegistryRemote[];
};
type RegistryPage = { items: RegistryItem[]; next_cursor?: string | null };
type Conn = {
  id: string;
  name: string;
  description?: string | null;
  server_url: string;
  registry_name?: string | null;
  registry_version?: string | null;
  auth_type?: string;
  auth_status?: string;
  is_active: boolean;
  last_health_status?: string | null;
  discovered_tools?: string[];
  has_auth?: boolean;
};
type ConnectBody = {
  registry_name: string;
  registry_version: string;
  remote_index: number;
  variables: Record<string, string>;
  headers: Record<string, string>;
};

function RegistryCard({
  item,
  connections,
  writable,
  onConnect,
}: {
  item: RegistryItem;
  connections: Conn[];
  writable: boolean;
  onConnect: (item: RegistryItem) => void;
}) {
  const connectedRow = connections.find((row) => row.registry_name === item.name);
  const needsAuth = connectedRow?.auth_status === "reauth_required";
  const selvedge = needsAuth ? "card-selvedge card-selvedge-neg" : connectedRow ? "card-selvedge card-selvedge-pos" : "";
  return (
    <article className={`card mcp-card ${selvedge}`}>
      <div className="icn-head">
        <span className="icn-logo" style={item.icon ? { backgroundImage: `url("${item.icon}")` } : undefined}>
          {!item.icon && <Icon name="spark" />}
        </span>
        <div className="grow" style={{ minWidth: 0 }}>
          <h3 title={item.title}>{item.title}</h3>
          <div className="mcp-name" title={item.name}>{item.name}</div>
        </div>
        {needsAuth
          ? <span className="badge badge-neg">Needs authorization</span>
          : connectedRow && <span className="badge badge-pos">Connected</span>}
      </div>
      <hr className="thread-rule" />
      <p className="mcp-desc">{item.description || "Remote MCP server"}</p>
      <div className="row-flex between gap2 mcp-foot">
        <span className="badge badge-outline">v{item.version}</span>
        {(!connectedRow || needsAuth) && writable && (
          <Button size="sm" variant="primary" onClick={() => onConnect(item)}>
            {needsAuth ? "Authorize" : "Connect"}
          </Button>
        )}
      </div>
    </article>
  );
}

export default function McpConnectionsPage() {
  const { businessId, admin } = useApp();
  const qc = useQueryClient();
  const { toast } = useToast();
  const writable = canMutate(admin?.role);
  const [view, setView] = useState<"browse" | "connected">("browse");
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<RegistryItem | null>(null);
  const [remoteIndex, setRemoteIndex] = useState(0);
  const [showCustom, setShowCustom] = useState(false);

  const list = useQuery({
    queryKey: queryKeys.mcpConnections(businessId || ""),
    enabled: !!businessId,
    queryFn: () => api<{ items: Conn[] }>(`/api/admin/mcp-connections/${businessId}`),
  });
  const registry = useInfiniteQuery({
    queryKey: queryKeys.mcpRegistry(search),
    initialPageParam: "",
    queryFn: ({ pageParam }) =>
      api<RegistryPage>(
        `/api/admin/mcp-connections/registry?limit=24&search=${encodeURIComponent(search)}&cursor=${encodeURIComponent(String(pageParam || ""))}`,
      ),
    getNextPageParam: (last) => last.next_cursor || undefined,
  });
  const popular = useQuery({
    queryKey: queryKeys.mcpRegistryPopular,
    queryFn: () => api<{ items: RegistryItem[] }>("/api/admin/mcp-connections/registry/popular"),
    enabled: !search,
  });
  const registryItems = useMemo(
    () => registry.data?.pages.flatMap((page) => page.items) ?? [],
    [registry.data],
  );
  const popularItems = useMemo(() => popular.data?.items ?? [], [popular.data]);
  const popularNames = useMemo(() => new Set(popularItems.map((item) => item.name)), [popularItems]);
  const allItems = useMemo(
    () => (search ? registryItems : registryItems.filter((item) => !popularNames.has(item.name))),
    [search, registryItems, popularNames],
  );

  const refreshConnections = async () => {
    await qc.invalidateQueries({ queryKey: queryKeys.mcpConnections(businessId || "") });
  };
  const oauth = useMutation({
    mutationFn: (body: ConnectBody) =>
      api<{ oauth_url: string }>(`/api/admin/mcp-connections/registry/oauth/${businessId}/start`, {
        method: "POST",
        body: JSON.stringify(body),
      }),
    onSuccess: (data) => {
      const popup = window.open(data.oauth_url, "reflexo-mcp-oauth", "popup,width=620,height=760");
      if (!popup) toast({ tone: "neg", title: "Popup blocked", body: "Allow popups and try again." });
      else toast({ tone: "pos", title: "Authorization opened", body: "Complete authorization in the new window." });
    },
  });
  const connect = useMutation({
    mutationFn: (body: ConnectBody) =>
      api<{ connected: boolean; requires_oauth: boolean; error?: string; tools?: string[] }>(
        `/api/admin/mcp-connections/registry/connect/${businessId}`,
        { method: "POST", body: JSON.stringify(body) },
      ).then((result) => ({ result, body })),
    onSuccess: async ({ result, body }) => {
      if (result.connected) {
        setSelected(null);
        setView("connected");
        toast({ tone: "pos", title: "MCP server connected", body: `${result.tools?.length || 0} tool(s) discovered.` });
        await refreshConnections();
      } else if (result.requires_oauth) {
        oauth.mutate(body);
      } else {
        toast({ tone: "neg", title: "Connection failed", body: result.error || "The remote server could not be reached." });
      }
    },
  });

  useEffect(() => {
    const onOAuth = (event: MessageEvent) => {
      let expected: URL;
      let actual: URL;
      try {
        expected = new URL(API_URL);
        actual = new URL(event.origin);
      } catch {
        return;
      }
      const loopbackAlias = ["localhost", "127.0.0.1"].includes(expected.hostname)
        && ["localhost", "127.0.0.1"].includes(actual.hostname)
        && expected.port === actual.port;
      if (event.origin !== expected.origin && !loopbackAlias) return;
      if (event.data?.type !== "reflexo-mcp-oauth") return;
      void qc.invalidateQueries({ queryKey: queryKeys.mcpConnections(businessId || "") });
      setSelected(null);
      setView("connected");
      toast({
        tone: event.data.ok ? "pos" : "neg",
        title: event.data.ok ? "MCP server connected" : "Authorization completed with an error",
      });
    };
    const onFocus = () => {
      if (document.visibilityState === "visible") {
        void qc.invalidateQueries({ queryKey: queryKeys.mcpConnections(businessId || "") });
      }
    };
    window.addEventListener("message", onOAuth);
    window.addEventListener("focus", onFocus);
    return () => {
      window.removeEventListener("message", onOAuth);
      window.removeEventListener("focus", onFocus);
    };
  }, [businessId, qc, toast]);

  const create = useMutation({
    mutationFn: (body: { name: string; server_url: string; description: string; auth_headers?: Record<string, string> }) =>
      api(`/api/admin/mcp-connections/${businessId}`, { method: "POST", body: JSON.stringify(body) }),
    onSuccess: async () => {
      setShowCustom(false);
      setView("connected");
      toast({ tone: "pos", title: "Custom MCP connection added", body: "Use Test to discover tools." });
      await refreshConnections();
    },
  });
  const toggle = useMutation({
    mutationFn: (row: Conn) => api(`/api/admin/mcp-connections/${businessId}/${row.id}`, {
      method: "PATCH", body: JSON.stringify({ is_active: !row.is_active }),
    }),
    onSuccess: refreshConnections,
  });
  const test = useMutation({
    mutationFn: (row: Conn) => api<{ ok: boolean; tools: string[]; error?: string }>(
      `/api/admin/mcp-connections/${businessId}/${row.id}/test`, { method: "POST" },
    ),
    onSuccess: async (result) => {
      toast({
        tone: result.ok ? "pos" : "neg",
        title: result.ok ? "Connection healthy" : "Connection failed",
        body: result.ok ? `${result.tools.length} tool(s) discovered` : result.error,
      });
      await refreshConnections();
    },
  });
  const remove = useMutation({
    mutationFn: (row: Conn) => api(`/api/admin/mcp-connections/${businessId}/${row.id}`, { method: "DELETE" }),
    onSuccess: async () => {
      toast({ tone: "pos", title: "Connection removed" });
      await refreshConnections();
    },
  });

  function submitRegistry(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selected) return;
    const data = new FormData(event.currentTarget);
    const remote = selected.remotes.find((item) => item.index === remoteIndex) || selected.remotes[0];
    const variables = Object.fromEntries(
      Object.keys(remote.variables).map((name) => [name, String(data.get(`var:${name}`) || "")]),
    );
    const headers = Object.fromEntries(
      remote.headers.map((header) => [header.name, String(data.get(`header:${header.name}`) || "")]),
    );
    const body = {
      registry_name: selected.name,
      registry_version: selected.version,
      remote_index: remote.index,
      variables,
      headers,
    };
    const existing = connections.find((row) => row.registry_name === selected.name);
    if (existing?.auth_status === "reauth_required") oauth.mutate(body);
    else connect.mutate(body);
  }

  function submitCustom(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const bearer = String(data.get("bearer") || "").trim();
    create.mutate({
      name: String(data.get("name") || "").trim(),
      server_url: String(data.get("server_url") || "").trim(),
      description: String(data.get("description") || "").trim(),
      auth_headers: bearer ? { Authorization: bearer.startsWith("Bearer ") ? bearer : `Bearer ${bearer}` } : undefined,
    });
  }

  if (!businessId || list.isLoading) return <LoadingState label="Loading MCP connections…" />;
  if (list.isError) return <ErrorState message={(list.error as Error).message} />;
  const connections = list.data?.items ?? [];
  const currentRemote = selected?.remotes.find((item) => item.index === remoteIndex) || selected?.remotes[0];

  return <div>
    <header className="page-head">
      <div>
        <p className="eyebrow">Universal tools</p>
        <h1>MCP servers</h1>
        <p className="page-sub">Connect remote tools from the official MCP Registry. Tools remain owner-only.</p>
      </div>
      {writable && <Button onClick={() => setShowCustom(true)}><Icon name="plus" className="ic ic-sm" />Custom server</Button>}
    </header>

    <div className="tabs mt4" role="tablist">
      <button role="tab" aria-selected={view === "browse"} onClick={() => setView("browse")}>Browse servers</button>
      <button role="tab" aria-selected={view === "connected"} onClick={() => setView("connected")}>Connected ({connections.length})</button>
    </div>

    {view === "browse" && <section className="mt4">
      <form className="mcp-search" onSubmit={(event) => { event.preventDefault(); setSearch(searchInput.trim()); }}>
        <input className="input" value={searchInput} onChange={(event) => setSearchInput(event.target.value)} placeholder="Search the official MCP Registry…" />
        <Button type="submit">Search</Button>
      </form>
      {!search && popularItems.length > 0 && (
        <div className="mcp-section">
          <h2>Popular</h2>
          <div className="grid g3 mt3">
            {popularItems.map((item) => (
              <RegistryCard
                key={`${item.name}:${item.version}`}
                item={item}
                connections={connections}
                writable={writable}
                onConnect={(server) => { setSelected(server); setRemoteIndex(server.remotes[0].index); }}
              />
            ))}
          </div>
        </div>
      )}
      {registry.isLoading ? <LoadingState label="Loading Registry servers…" /> :
        registry.isError ? <ErrorState message={(registry.error as Error).message} /> :
        allItems.length === 0 && (search || popularItems.length === 0) ? <EmptyState title="No remote servers found" body="Try another search. Local stdio-only packages are not shown." /> :
        allItems.length > 0 ? (
          <div className={search ? "mt4" : "mcp-section"}>
            {!search && <h2>All servers</h2>}
            <div className={`grid g3 ${search ? "" : "mt3"}`}>
              {allItems.map((item) => (
                <RegistryCard
                  key={`${item.name}:${item.version}`}
                  item={item}
                  connections={connections}
                  writable={writable}
                  onConnect={(server) => { setSelected(server); setRemoteIndex(server.remotes[0].index); }}
                />
              ))}
            </div>
          </div>
        ) : null}
      {registry.hasNextPage && <div className="mt4"><Button loading={registry.isFetchingNextPage} onClick={() => registry.fetchNextPage()}>Load more</Button></div>}
    </section>}

    {view === "connected" && <section className="mt4">
      {connections.length === 0 ? <EmptyState title="No MCP servers connected" body="Browse the Registry or add a custom Streamable HTTP endpoint." /> :
        <div className="tablewrap"><table className="tbl"><thead><tr><th>Connection</th><th>URL</th><th>Health</th><th>Tools</th><th>Enabled</th><th></th></tr></thead><tbody>
          {connections.map((row) => <tr key={row.id}>
            <td className="cell-strong">{row.name}<div className="faint">{row.registry_name || row.description || "Custom server"}</div></td>
            <td className="mono muted">{row.server_url}</td>
            <td>
              <span className={`badge ${row.auth_status === "reauth_required" ? "badge-neg" : row.last_health_status === "healthy" ? "badge-pos" : "badge-outline"}`}>
                {row.auth_status === "reauth_required" ? "Needs authorization" : row.last_health_status || "untested"}
              </span>
            </td>
            <td className="muted" title={(row.discovered_tools || []).join(", ")}>
              {(row.discovered_tools || []).length > 0
                ? <span className="badge badge-outline">{(row.discovered_tools || []).length} {(row.discovered_tools || []).length === 1 ? "tool" : "tools"}</span>
                : "—"}
            </td>
            <td>{writable ? <label className="switch"><input type="checkbox" checked={row.is_active} disabled={toggle.isPending} onChange={() => toggle.mutate(row)} /><span className="track" /></label> : row.is_active ? "On" : "Off"}</td>
            <td className="cell-actions">
              {writable && <button className="btn btn-sm" disabled={test.isPending} onClick={() => test.mutate(row)}>Test</button>}
              {writable && <button className="btn btn-sm" disabled={remove.isPending} onClick={() => remove.mutate(row)}>Remove</button>}
            </td>
          </tr>)}
        </tbody></table></div>}
    </section>}

    <Dialog
      open={!!selected}
      title={`Connect ${selected?.title || "MCP server"}`}
      description="Reflexo will validate the Registry record and discover its tools before saving."
      onClose={() => setSelected(null)}
      footer={<><Button onClick={() => setSelected(null)}>Cancel</Button><Button variant="primary" loading={connect.isPending || oauth.isPending} type="submit" form="registry-connect-form">Connect</Button></>}
    >
      {selected && currentRemote && <form id="registry-connect-form" className="form-grid" onSubmit={submitRegistry}>
        {selected.remotes.length > 1 && <div className="full"><Field label="Remote endpoint"><select className="input" value={remoteIndex} onChange={(event) => setRemoteIndex(Number(event.target.value))}>
          {selected.remotes.map((remote) => <option value={remote.index} key={remote.index}>{remote.url}</option>)}
        </select></Field></div>}
        <p className="mono muted full">{currentRemote.url}</p>
        {Object.entries(currentRemote.variables).map(([name, spec]) =>
          <Field key={name} label={name} required={spec.required}>
            {spec.choices.length ? <select className="input" name={`var:${name}`} defaultValue={String(spec.default || "")}>
              {!spec.required && <option value="">Default</option>}
              {spec.choices.map((choice) => <option key={choice}>{choice}</option>)}
            </select> : <input className="input" type={spec.secret ? "password" : "text"} name={`var:${name}`} defaultValue={String(spec.default || "")} required={spec.required} placeholder={spec.description} />}
          </Field>)}
        {currentRemote.headers.map((header) =>
          <Field key={header.name} label={header.name} required={header.required}>
            <input className="input" type={header.secret ? "password" : "text"} name={`header:${header.name}`} defaultValue={String(header.default || "")} required={header.required} placeholder={header.description} />
          </Field>)}
        {(connect.isError || oauth.isError) && <p className="err full">{((connect.error || oauth.error) as Error).message}</p>}
      </form>}
    </Dialog>

    <Dialog
      open={showCustom && writable}
      title="Add a custom MCP server"
      description="Use a Streamable HTTP endpoint. Tool names are prefixed with the connection name."
      onClose={() => setShowCustom(false)}
      footer={<><Button onClick={() => setShowCustom(false)}>Cancel</Button><Button variant="primary" loading={create.isPending} type="submit" form="custom-mcp-form">Save</Button></>}
    >
      <form id="custom-mcp-form" className="form-grid" onSubmit={submitCustom}>
        <Field label="Name" required><input className="input" name="name" placeholder="my_crm" /></Field>
        <Field label="Server URL" required><input className="input" name="server_url" placeholder="https://example.com/mcp" /></Field>
        <div className="full"><Field label="Description"><input className="input" name="description" placeholder="Internal CRM" /></Field></div>
        <div className="full"><Field label="Authorization (optional)"><input className="input" type="password" name="bearer" placeholder="Bearer token" /></Field></div>
        {create.isError && <p className="err full">{(create.error as Error).message}</p>}
      </form>
    </Dialog>
  </div>;
}
