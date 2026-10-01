export const absolutePolicy = Object.freeze({
  paidUsageAllowed: false,
  maximumSpendUsd: 0,
  autoUpgrade: false,
  paidFallback: false,
  onFreeCapacityExhausted: "QUEUE" as const,
  allowUnknownProvider: false,
  allowDynamicProviderDiscovery: false,
  allowAgentProviderRegistration: false,
  allowAgentModelRegistration: false,
  allowUnapprovedProviderFallback: false,
});

export type ProposedAction =
  | "AI_FREE_INFERENCE" | "AI_PAID_INFERENCE" | "MONEY_RECEIVE" | "MONEY_SPEND"
  | "BORROW" | "LEVERAGE" | "CRYPTO_RECEIVE" | "CRYPTO_SEND" | "CRYPTO_TRADE"
  | "READ_RAW_SECRET" | "EXPOSE_ENVIRONMENT" | "MODIFY_WATCHDOG" | "USE_PLANE_ALERTS_RAILWAY_TOKEN"
  | "GITHUB_PUBLIC_READ" | "GITHUB_SUBMIT" | "UNKNOWN_PROVIDER";

export function watchdog(action: ProposedAction): { allowed: boolean; approvalRequired: boolean; reason: string } {
  const denied = new Set<ProposedAction>([
    "AI_PAID_INFERENCE", "BORROW", "LEVERAGE", "CRYPTO_TRADE", "READ_RAW_SECRET",
    "EXPOSE_ENVIRONMENT", "MODIFY_WATCHDOG", "USE_PLANE_ALERTS_RAILWAY_TOKEN", "UNKNOWN_PROVIDER",
  ]);
  if (denied.has(action)) return { allowed: false, approvalRequired: false, reason: `Policy denies ${action}` };
  if (["MONEY_SPEND", "CRYPTO_SEND", "GITHUB_SUBMIT"].includes(action)) {
    return { allowed: false, approvalRequired: true, reason: "Explicit owner approval required" };
  }
  return { allowed: true, approvalRequired: false, reason: "Allowed by deterministic policy" };
}

export function assertEnvironmentPolicy(env: NodeJS.ProcessEnv): void {
  const required: Record<string, string> = {
    AI_PAID_USAGE_ALLOWED: "false",
    AI_MAXIMUM_SPEND_USD: "0",
    AI_AUTO_UPGRADE: "false",
    AI_PAID_FALLBACK: "false",
    AI_ON_FREE_CAPACITY_EXHAUSTED: "QUEUE",
  };
  for (const [name, expected] of Object.entries(required)) {
    if ((env[name] ?? expected).toUpperCase() !== expected.toUpperCase()) {
      throw new Error(`${name} violates the immutable zero-paid-AI policy`);
    }
  }
}
