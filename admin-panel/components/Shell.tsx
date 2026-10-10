"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, canMutate, ApiError, API_URL, type Admin } from "@/lib/api";
import { queryKeys } from "@/lib/queries/keys";
import { Icon } from "./Icon";
import { useApp } from "./Providers";
import { ErrorState, LoadingState } from "./States";
import { Brandmark } from "./admin/Brandmark";

const NAV = [
  { group: "Overview", items: [
    { href: "/dashboard", label: "Dashboard", icon: "grid" },
    { href: "/businesses", label: "Businesses", icon: "building" },
    { href: "/conversations", label: "Conversations", icon: "chat" },
  ] },
  { group: "Intelligence", items: [
    { href: "/knowledge", label: "Knowledge", icon: "book" },
    { href: "/entities", label: "Entities", icon: "box" },
    { href: "/workflows", label: "Workflows", icon: "flow" },
    { href: "/agent", label: "Agent", icon: "spark" },
    { href: "/simulator", label: "Simulator", icon: "play" },
  ] },
  { group: "Operations", items: [
    { href: "/integrations", label: "Integrations", icon: "plug" },
    { href: "/mcp-connections", label: "MCP", icon: "spark" },
    { href: "/routing", label: "Routing", icon: "branch" },
    { href: "/billing", label: "Billing", icon: "receipt" },
    { href: "/settings", label: "Settings", icon: "gear" },
  ] },
];
const NAV_ITEMS = NAV.flatMap((group) => group.items);
const PALETTES = [
  { id: "indigo", label: "Indigo & Marigold" },
  { id: "peacock", label: "Peacock & Brass" },
  { id: "oxblood", label: "Oxblood & Gold leaf" },
] as const;

const BUSINESS_PICKER_QUERY = "limit=100";

type BusinessOption = { id: string; name: string; slug?: string | null };
type BizList = { items: BusinessOption[] };
type Palette = (typeof PALETTES)[number]["id"];
type Density = "comfortable" | "compact";

function businessLabel(business?: BusinessOption) {
  if (!business) return "Choose business";
  const identity = business.slug || business.id.slice(0, 8);
  return `${business.name} · ${identity}`;
}

export function Shell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const { admin, setAdmin, businessId, setBusinessId } = useApp();
  const [collapsed, setCollapsed] = useState(false);
  const [mobileNav, setMobileNav] = useState(false);
  const [cmdOpen, setCmdOpen] = useState(false);
  const [commandQuery, setCommandQuery] = useState("");
  const [density, setDensity] = useState<Density>("comfortable");
  const [palette, setPalette] = useState<Palette>("indigo");

  const me = useQuery({
    queryKey: queryKeys.me,
    queryFn: () => api<Admin>("/api/admin/auth/me"),
    retry: (failureCount, error) =>
      !(error instanceof ApiError && error.status === 401) && failureCount < 1,
  });

  useEffect(() => {
    if (me.data) setAdmin(me.data);
  }, [me.data, setAdmin]);

  const unauthenticated = me.error instanceof ApiError && me.error.status === 401;

  useEffect(() => {
    if (unauthenticated) router.replace("/login");
  }, [unauthenticated, router]);

  const businesses = useQuery({
    queryKey: queryKeys.businesses(BUSINESS_PICKER_QUERY),
    queryFn: () => api<BizList>(`/api/admin/businesses?${BUSINESS_PICKER_QUERY}`),
    enabled: !!me.data,
  });

  useEffect(() => {
    const items = businesses.data?.items || [];
    if (!businessId && items[0]) setBusinessId(items[0].id);
  }, [businesses.data, businessId, setBusinessId]);

  useEffect(() => {
    document.documentElement.dataset.density = density;
    document.documentElement.dataset.palette = palette;
    window.localStorage.setItem("reflexo-density", density);
    window.localStorage.setItem("reflexo-palette", palette);
  }, [density, palette]);

  useEffect(() => {
    const savedDensity = window.localStorage.getItem("reflexo-density");
    const savedPalette = window.localStorage.getItem("reflexo-palette");
    if (savedDensity === "compact" || savedDensity === "comfortable") setDensity(savedDensity);
    if (PALETTES.some((item) => item.id === savedPalette)) setPalette(savedPalette as Palette);
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setCmdOpen((v) => !v);
      }
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "b") {
        e.preventDefault();
        setCollapsed((v) => !v);
      }
      if (e.shiftKey && e.key.toLowerCase() === "d" && !(e.metaKey || e.ctrlKey)) {
        const t = e.target as HTMLElement;
        if (t.tagName === "INPUT" || t.tagName === "TEXTAREA") return;
        setDensity((d) => (d === "compact" ? "comfortable" : "compact"));
      }
      if (e.key === "Escape") {
        setCmdOpen(false);
        setMobileNav(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const currentBiz = useMemo(
    () => (businesses.data?.items || []).find((b) => b.id === businessId),
    [businesses.data, businessId]
  );
  const currentItem = NAV_ITEMS.find((item) => pathname.startsWith(item.href));
  const visibleCommands = NAV_ITEMS.filter((item) =>
    item.label.toLowerCase().includes(commandQuery.toLowerCase()),
  );
  const paletteName = PALETTES.find((item) => item.id === palette)?.label;

  function cyclePalette() {
    const current = PALETTES.findIndex((item) => item.id === palette);
    setPalette(PALETTES[(current + 1) % PALETTES.length].id);
  }

  if (me.isPending) {
    return (
      <div className="app-boot">
        <LoadingState label="Checking your staff session…" />
      </div>
    );
  }

  if (me.isError) {
    if (unauthenticated) return null;
    return (
      <div className="app-boot">
        <ErrorState
          message={`Could not reach the admin API at ${API_URL} (${(me.error as Error).message}). Start it with "python -m api.server", then retry.`}
          onRetry={() => void me.refetch()}
        />
      </div>
    );
  }

  return (
    <div
      className="app"
      data-collapsed={collapsed ? "true" : "false"}
      data-mobile-nav={mobileNav ? "true" : "false"}
    >
      {mobileNav && <button className="mobile-nav-scrim" onClick={() => setMobileNav(false)} aria-label="Close navigation" />}
      <aside className="sidebar" id="sidebar">
        <div className="sidebar-top">
          <Brandmark />
          <button className="sidebar-collapse" onClick={() => setCollapsed((value) => !value)} aria-label="Collapse sidebar">
            <Icon name="chev-left" className="ic ic-sm" />
          </button>
        </div>
        <div className="sidebar-scroll">
          <nav aria-label="Primary navigation">
            {NAV.map((group) => (
              <div key={group.group}>
                <div className="nav-group-label">{group.group}</div>
                {group.items.map((item) => (
                  <Link
                    key={item.href}
                    href={item.href}
                    className="nav-item"
                    aria-current={pathname.startsWith(item.href) ? "page" : undefined}
                    onClick={() => setMobileNav(false)}
                  >
                    <Icon name={item.icon} />
                    <span>{item.label}</span>
                  </Link>
                ))}
              </div>
            ))}
          </nav>
        </div>
        <div className="sidebar-foot">
          <button className="sidebar-user">
            <span className="avatar avatar-sm">{initials(admin?.name)}</span>
            <div>
              <b>{admin?.name || "REFLEXO staff"}</b>
              <span>{formatRole(admin?.role)}</span>
            </div>
            <Icon name="chev-up" className="ic ic-sm sidebar-user-chev" />
          </button>
        </div>
      </aside>
      <div className="main">
        <header className="topbar">
          <div className="row-flex gap3 grow topbar-leading">
            <button className="icon-btn mobile-nav-btn" onClick={() => setMobileNav(true)} aria-label="Open navigation">
              <Icon name="menu" />
            </button>
            <nav className="crumbs" aria-label="Breadcrumb">
              <Link href="/dashboard">Control Room</Link>
              <Icon name="chev-right" className="ic ic-sm" />
              <b>{currentItem?.label || "Overview"}</b>
            </nav>
          </div>
          <div className="topbar-tools">
            <button className="cmdk-trigger" onClick={() => setCmdOpen(true)}>
              <Icon name="search" className="ic ic-sm" />
              <span>Search or jump to</span>
              <span className="kbd-inline"><kbd className="kbd">Ctrl</kbd><kbd className="kbd">K</kbd></span>
            </button>
            <label className="biz-switch">
              <span className="avatar avatar-sm">{initials(currentBiz?.name)}</span>
              <span className="bs-text">{businessLabel(currentBiz)}</span>
              <Icon name="chev-down" className="ic ic-sm biz-chevron" />
              <select
                className="biz-select"
                aria-label="Current business"
                value={businessId}
                onChange={(event) => setBusinessId(event.target.value)}
              >
                {(businesses.data?.items || []).map((business) => (
                  <option key={business.id} value={business.id}>{businessLabel(business)}</option>
                ))}
              </select>
            </label>
            <span className="tip-host">
              <button className="icon-btn" onClick={cyclePalette} aria-label="Change palette"><Icon name="palette" /></button>
              <span className="tip">Palette: <b>{paletteName}</b></span>
            </span>
            <span className="tip-host">
              <button
                className="icon-btn"
                onClick={() => setDensity((value) => value === "compact" ? "comfortable" : "compact")}
                aria-label="Change density"
              >
                <Icon name="sliders" />
              </button>
              <span className="tip">Density: <b>{density}</b></span>
            </span>
            <button className="icon-btn" aria-label="Notifications">
              <Icon name="bell" /><span className="pip" />
            </button>
            {!canMutate(admin?.role) && <span className="badge badge-outline">Read only</span>}
          </div>
        </header>
        <main className="canvas">{children}</main>
      </div>
      {cmdOpen && (
        <div className="cmdk-scrim" onMouseDown={(event) => event.target === event.currentTarget && setCmdOpen(false)}>
          <section className="cmdk" role="dialog" aria-modal="true" aria-label="Command palette">
            <div className="cmdk-input">
              <Icon name="search" />
              <input
                autoFocus
                value={commandQuery}
                onChange={(event) => setCommandQuery(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && visibleCommands[0]) {
                    event.preventDefault();
                    setCmdOpen(false);
                    setCommandQuery("");
                    router.push(visibleCommands[0].href);
                  }
                }}
                placeholder="Search pages and actions…"
              />
              <kbd className="kbd">Esc</kbd>
            </div>
            <div className="cmdk-list">
              <div className="menu-label">Navigate</div>
              {visibleCommands.map((item, index) => (
                  <button
                    key={item.href}
                    className="menu-item cmdk-item"
                    data-active={index === 0 ? "true" : undefined}
                    onClick={() => {
                      setCmdOpen(false);
                      setCommandQuery("");
                      router.push(item.href);
                    }}
                  >
                    <Icon name={item.icon} />
                    <span>{item.label}</span>
                  </button>
              ))}
              {!visibleCommands.length && <p className="cmdk-empty">No matching destination</p>}
            </div>
            <footer className="cmdk-foot">
              <span><kbd className="kbd">↑</kbd> <kbd className="kbd">↓</kbd> to navigate</span>
              <span><kbd className="kbd">Enter</kbd> to open</span>
            </footer>
          </section>
        </div>
      )}
    </div>
  );
}

function initials(value?: string) {
  return (value || "RF").split(/\s+/).map((part) => part[0]).join("").slice(0, 2).toUpperCase();
}

function formatRole(role?: string) {
  if (!role) return "Staff";
  return role.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}
