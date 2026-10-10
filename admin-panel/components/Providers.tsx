"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createContext, useContext, useEffect, useMemo, useState } from "react";
import type { Admin } from "@/lib/api";
import { ToastProvider } from "@/components/ui";

type Ctx = {
  admin: Admin | null;
  setAdmin: (a: Admin | null) => void;
  businessId: string;
  setBusinessId: (id: string) => void;
};

const AppCtx = createContext<Ctx | null>(null);

export function useApp() {
  const ctx = useContext(AppCtx);
  if (!ctx) throw new Error("useApp outside provider");
  return ctx;
}

export function Providers({ children }: { children: React.ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 30_000,
            retry: 1,
            refetchOnWindowFocus: false,
          },
          mutations: { retry: 0 },
        },
      })
  );
  const [admin, setAdmin] = useState<Admin | null>(null);
  const [businessId, setBusinessId] = useState("");
  useEffect(() => {
    const stored = window.localStorage.getItem("reflexo.admin.business");
    if (stored) setBusinessId(stored);
  }, []);
  useEffect(() => {
    if (businessId) window.localStorage.setItem("reflexo.admin.business", businessId);
  }, [businessId]);
  const value = useMemo(
    () => ({ admin, setAdmin, businessId, setBusinessId }),
    [admin, businessId]
  );
  return (
    <QueryClientProvider client={client}>
      <AppCtx.Provider value={value}>
        <ToastProvider>{children}</ToastProvider>
      </AppCtx.Provider>
    </QueryClientProvider>
  );
}
