export type ProviderName = "gemini" | "groq" | "mistral" | "cloudflare";
export type Route = { provider: ProviderName; model: string; credentialSlot: string; quotaPool: string; enabled: boolean; authorized: boolean; healthy: boolean; freeOnly: boolean; rateLimitedUntil?: Date | null };

export const approvedModels: Readonly<Record<ProviderName, readonly string[]>> = Object.freeze({
  gemini: ["gemini-2.5-flash"],
  groq: ["llama-3.3-70b-versatile"],
  mistral: ["mistral-small-latest"],
  cloudflare: ["@cf/meta/llama-3.1-8b-instruct"],
});

export class RouteDenied extends Error {}
export class CapacityQueued extends Error {}

export function selectFreeRoute(routes: Route[], requestedProvider?: string, requestedModel?: string, now = new Date()): Route {
  if (requestedProvider && !(requestedProvider in approvedModels)) throw new RouteDenied("unknown provider denied");
  if (requestedProvider && requestedModel && !approvedModels[requestedProvider as ProviderName].includes(requestedModel)) throw new RouteDenied("model is not owner-approved");
  const eligible = routes.filter((route) => route.freeOnly && route.enabled && route.authorized && route.healthy && approvedModels[route.provider].includes(route.model) && (!requestedProvider || route.provider === requestedProvider) && (!requestedModel || route.model === requestedModel) && (!route.rateLimitedUntil || route.rateLimitedUntil <= now));
  if (!eligible.length) throw new CapacityQueued("all approved free capacity is unavailable; queue work");
  return eligible[0]!;
}

export const providerSlots: Readonly<Record<ProviderName, readonly string[]>> = Object.freeze({
  gemini: ["GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3", "GEMINI_API_KEY_4", "GEMINI_API_KEY_5"],
  groq: ["GROQ_KEY", "GROQ_KEY_2", "GROQ_KEY_3", "GROQ_KEY_4", "GROQ_KEY_5"],
  mistral: ["MISTRAL_API", "MISTRAL_API_2", "MISTRAL_API_3", "MISTRAL_API_4", "MISTRAL_API_5"],
  cloudflare: ["CLOUDFLARE_API_TOKEN", "CLOUDFLARE_API_TOKEN_2"],
});
