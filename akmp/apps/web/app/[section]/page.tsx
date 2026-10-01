import { notFound } from "next/navigation";
import Dashboard from "../components/dashboard";

export const dynamic = "force-dynamic";
const sections = ["overview", "opportunities", "work", "requests", "treasury", "providers", "ledger", "activity", "policies", "settings"] as const;
export type Section = typeof sections[number];

export default async function DashboardPage({ params }: { params: Promise<{ section: string }> }) {
  const { section } = await params;
  if (!sections.includes(section as Section)) notFound();
  return <Dashboard section={section as Section}/>;
}
