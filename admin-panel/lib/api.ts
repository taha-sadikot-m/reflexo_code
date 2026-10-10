export const API_URL =
  process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "") || "http://localhost:8030";

export type Admin = {
  id: string;
  email: string;
  name: string;
  role: "support" | "admin" | "super_admin";
  is_active: boolean;
};

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const token = typeof window !== "undefined" ? sessionStorage.getItem("reflexo_admin_token") : null;
  if (token && !headers.has("Authorization")) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  const res = await fetch(`${API_URL}${path}`, {
    ...init,
    headers,
    credentials: "include",
  });
  if (res.status === 401 && typeof window !== "undefined" && !path.includes("/auth/login")) {
    window.location.replace(new URL("/login", window.location.origin).toString());
    throw new ApiError(401, "Not authenticated");
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || JSON.stringify(body);
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, typeof detail === "string" ? detail : "Request failed");
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export function canMutate(role?: string) {
  return role === "admin" || role === "super_admin";
}

export function canBill(role?: string) {
  return role === "super_admin";
}
