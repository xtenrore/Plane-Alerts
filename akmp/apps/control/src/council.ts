import type { ProviderName } from "./providers.js";

export const councilRoles = ["SCOUT", "ECONOMIST", "ENGINEER", "RISK_COMPLIANCE", "CRITIC"] as const;
export type CouncilRole = typeof councilRoles[number];
export type CouncilReview = {
  role: CouncilRole;
  recommendation: "ADVANCE" | "REJECT" | "OWNER_REVIEW";
  confidence: number;
  findings: string[];
  rewardCents?: number | null;
  estimatedEffortHours?: number | null;
  completionProbability?: number | null;
  payoutReliability?: number | null;
  expectedValueCents?: number | null;
  riskFlags: string[];
};

export type ScreenedOpportunity = {
  id: string;
  potentialRewardCents: number | null;
  requiresKyc: boolean | null;
  requiresCard: boolean | null;
  requiresUpfrontPayment: boolean | null;
  platformStatus: "APPROVED" | "RESEARCH_ONLY" | "OWNER_REVIEW_REQUIRED" | "BLOCKED";
};

export function deterministicScreen(opportunity: ScreenedOpportunity): { advance: boolean; reason: string; request?: "NEW_PLATFORM_APPROVAL" } {
  if (opportunity.requiresKyc) return { advance: false, reason: "KYC is rejected by default" };
  if (opportunity.requiresCard) return { advance: false, reason: "Credit-card requirement is rejected by default" };
  if (opportunity.requiresUpfrontPayment) return { advance: false, reason: "Upfront payment is rejected by default" };
  if (opportunity.platformStatus === "BLOCKED") return { advance: false, reason: "Platform is blocked" };
  if (opportunity.platformStatus === "OWNER_REVIEW_REQUIRED") return { advance: false, reason: "Unknown platform requires owner approval", request: "NEW_PLATFORM_APPROVAL" };
  if (!opportunity.potentialRewardCents || opportunity.potentialRewardCents <= 0) return { advance: false, reason: "Compensation is not independently identifiable" };
  return { advance: true, reason: "Eligible for quota-efficient research" };
}

export function councilPlan(rawCount: number, screenedCount: number): { cheapClassifications: number; independentReviews: number; fullCouncil: number } {
  const screened = Math.max(0, Math.min(rawCount, screenedCount));
  return { cheapClassifications: Math.min(screened, 25), independentReviews: Math.min(screened, 8), fullCouncil: Math.min(screened, 3) };
}

export function validateReview(value: CouncilReview): CouncilReview {
  if (!councilRoles.includes(value.role) || value.confidence < 0 || value.confidence > 1 || value.findings.length > 64 || value.riskFlags.length > 32) throw new Error("Invalid structured council review");
  return value;
}

export type CouncilRoute = { role: CouncilRole; provider?: ProviderName; model?: string };
