export const opportunityStates = ["DISCOVERED", "SCREENING", "RESEARCHING", "COUNCIL_REVIEW", "APPROVED", "REJECTED", "QUEUED", "EXECUTING", "VALIDATING", "WAITING_FOR_OWNER", "READY_FOR_SUBMISSION", "SUBMITTED", "AWAITING_RESULT", "ACCEPTED", "PAYMENT_PENDING", "PAID", "FAILED", "CANCELLED"] as const;
export const jobStates = ["QUEUED", "RUNNING", "WAITING", "SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED"] as const;
export const requestStates = ["OPEN", "VIEWED", "WAITING_FOR_OWNER", "IN_PROGRESS", "FULFILLED", "VERIFICATION_FAILED", "REJECTED", "NO_LONGER_NEEDED", "EXPIRED"] as const;
export const revenueStates = ["PROJECTED", "OFFERED", "SUBMITTED", "ACCEPTED", "PAYMENT_PENDING", "PAYMENT_DETECTED", "VERIFIED_PAID"] as const;
export const quotaSources = ["PROVIDER_API", "RESPONSE_HEADER", "DOCUMENTED_STATIC_LIMIT", "LOCALLY_TRACKED", "OBSERVED", "UNKNOWN"] as const;
export type OpportunityState = typeof opportunityStates[number];
export type JobState = typeof jobStates[number];
export type RequestState = typeof requestStates[number];
export type RevenueState = typeof revenueStates[number];
export type QuotaSource = typeof quotaSources[number];
export const publicEnvironmentAllowlist = ["NEXT_PUBLIC_APP_VERSION", "NEXT_PUBLIC_SITE_ORIGIN"] as const;
export function isSeedPhraseLike(value: string): boolean { const words = value.trim().toLowerCase().split(/\s+/).filter(Boolean); return [12,15,18,21,24].includes(words.length) && words.every((word) => /^[a-z]{2,12}$/.test(word)); }
export function isWalletSecretLike(value: string): boolean { const compact = value.trim(); return isSeedPhraseLike(compact) || /-----BEGIN (?:EC |RSA )?PRIVATE KEY-----/.test(compact) || /^(?:0x)?[0-9a-f]{64}$/i.test(compact) || /^(?:xprv|tprv)[1-9A-HJ-NP-Za-km-z]{80,120}$/.test(compact) || /^[5KL][1-9A-HJ-NP-Za-km-z]{50,51}$/.test(compact); }
export function verifiedRevenueCents(events: Array<{ state: RevenueState; amountCents: number }>): number { return events.filter((event) => event.state === "VERIFIED_PAID").reduce((sum, event) => sum + event.amountCents, 0); }
