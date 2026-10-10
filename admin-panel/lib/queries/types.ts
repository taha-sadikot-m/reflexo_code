export type Page<T> = {
  items: T[];
  total: number;
  limit?: number;
  offset?: number;
};

export type BusinessSummary = {
  id: string;
  name: string;
  slug?: string | null;
  owner_name?: string | null;
  owner_phone: string;
  owner_email?: string | null;
  vertical: string;
  plan: string;
  plan_status?: string;
  message_limit: number;
  messages_used: number;
  is_active: boolean;
  onboarding_completed: boolean;
  created_at: string;
  updated_at: string;
};

export type RoutingMeta = {
  specialist?: string | null;
  route_method?: string | null;
  route_confidence?: number | null;
  handoff_chain: string[];
  guardrails: string[];
  memories_injected?: number | null;
  cache_hit?: boolean | null;
  latency_ms?: number | null;
  tokens?: number | null;
  estimated_cost_usd?: number | null;
  hitl_required?: boolean | null;
  raw?: Record<string, unknown>;
};

export type ConversationSummary = {
  id: string;
  channel: string;
  is_active: boolean;
  is_escalated: boolean;
  summary?: string | null;
  message_count: number;
  last_message_at?: string | null;
  started_at: string;
  customer_name?: string | null;
  customer_id?: string | null;
  last_message?: string | null;
  ai_metadata: RoutingMeta;
};

export type ConversationMessage = {
  id: string;
  role: "customer" | "owner" | "ai" | "system";
  content: string;
  content_type: string;
  media_url?: string | null;
  channel: string;
  ai_metadata: RoutingMeta;
  status?: string | null;
  error_message?: string | null;
  created_at: string;
};

export type KnowledgeEntry = {
  id: string;
  question?: string | null;
  answer: string;
  category: string;
  source?: string | null;
  confidence?: number | null;
  is_active?: boolean;
  usage_count?: number;
  last_used_at?: string | null;
  created_at?: string;
  updated_at?: string;
  rrf_score?: number;
};

export type Entity = {
  id: string;
  entity_type: string;
  name: string;
  description?: string | null;
  data: Record<string, unknown>;
  sort_order?: number;
  created_at?: string;
  updated_at?: string;
};

export type Dashboard = {
  messages_today: number;
  messages_yesterday: number;
  messages_delta_pct: number | null;
  median_reply_ms: number | null;
  active_businesses: number;
  onboarding_businesses: number;
  spend_inr_today: number;
  volume: { day: string; count: number }[];
  specialists: { name: string; count: number; share: number }[];
};
