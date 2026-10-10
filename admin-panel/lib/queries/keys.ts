export const queryKeys = {
  me: ["admin", "me"] as const,
  /** Prefix that matches every filtered business list, for invalidation. */
  businessesRoot: ["businesses"] as const,
  businesses: (filters = "") => ["businesses", filters] as const,
  business: (businessId: string) => ["business", businessId] as const,
  dashboard: (days: number) => ["dashboard", days] as const,
  conversations: (businessId: string, filters = "") =>
    ["conversations", businessId, filters] as const,
  conversation: (businessId: string, conversationId: string) =>
    ["conversation", businessId, conversationId] as const,
  knowledge: (businessId: string, filters = "") =>
    ["knowledge", businessId, filters] as const,
  gaps: (businessId: string) => ["knowledge-gaps", businessId] as const,
  entities: (businessId: string, filters = "") =>
    ["entities", businessId, filters] as const,
  workflows: (businessId: string) => ["workflows", businessId] as const,
  aiWorkflows: (businessId: string) => ["ai-workflows", businessId] as const,
  workflowExecutions: (businessId: string, ruleId: string) =>
    ["workflow-executions", businessId, ruleId] as const,
  integrations: (businessId: string) => ["integrations", businessId] as const,
  integrationHealth: ["integrations", "health"] as const,
  integrationCatalog: (businessId: string) => ["integrations", "catalog", businessId] as const,
  integrationCapabilities: (businessId: string) =>
    ["integrations", "capabilities", businessId] as const,
  setupLinks: (businessId: string) => ["integrations", "setup-links", businessId] as const,
  mcpConnections: (businessId: string) => ["mcp-connections", businessId] as const,
  mcpRegistry: (search: string) => ["mcp-registry", search] as const,
  mcpRegistryPopular: ["mcp-registry", "popular"] as const,
  agentConfig: (businessId: string) => ["agent-config", businessId] as const,
  billing: ["billing", "overview"] as const,
  usage: (businessId: string) => ["billing", "usage", businessId] as const,
  routing: (businessId: string, days: number) =>
    ["routing", businessId, days] as const,
};
