const sensitiveKey = /authorization|api[-_]?key|token|cookie|password|secret|private[-_]?key|seed[-_]?phrase|mnemonic/i;
const bearer = /Bearer\s+[A-Za-z0-9._~+/=-]+/gi;
const likelyKey = /\b(?:AIza[\w-]{20,}|gsk_[\w-]{16,}|cf_[\w-]{16,}|sk-[\w-]{16,})\b/g;

export function redact(value: unknown, depth = 0): unknown {
  if (depth > 8) return "[TRUNCATED]";
  if (typeof value === "string") return value.replace(bearer, "Bearer [REDACTED]").replace(likelyKey, "[REDACTED]");
  if (Array.isArray(value)) return value.map((item) => redact(item, depth + 1));
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, sensitiveKey.test(key) ? "[REDACTED]" : redact(item, depth + 1)]));
  return value;
}

export function safeError(error: unknown): string { return error instanceof Error ? String(redact(error.message)).slice(0, 500) : "Unexpected error"; }
