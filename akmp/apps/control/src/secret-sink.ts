import { providerSlots } from "./providers.js";

const allowedSecretNames = new Set([
  ...Object.values(providerSlots).flat(),
  "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_ACCOUNT_ID_2",
]);

export function isAllowedSecretName(name: string): boolean {
  return allowedSecretNames.has(name);
}

export async function setWorkerSecret(name: string, value: string, fetcher: typeof fetch = fetch): Promise<void> {
  if (!isAllowedSecretName(name)) throw new Error("Secret name is not approved");
  if (!value || value.length > 8192 || /[\r\n\0]/.test(value)) throw new Error("Invalid secret value");
  const token = process.env.AKMP_RAILWAY_PROJECT_TOKEN;
  const projectId = process.env.AKMP_RAILWAY_PROJECT_ID;
  const environmentId = process.env.AKMP_RAILWAY_ENVIRONMENT_ID;
  const serviceId = process.env.AKMP_RAILWAY_WORKER_SERVICE_ID;
  if (!token || !projectId || !environmentId || !serviceId) throw new Error("A.K.M.P secret sink is not configured");
  const query = `mutation VariableCollectionUpsert($input: VariableCollectionUpsertInput!) { variableCollectionUpsert(input: $input) }`;
  const response = await fetcher("https://backboard.railway.app/graphql/v2", {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify({ query, variables: { input: { projectId, environmentId, serviceId, variables: { [name]: value } } }),
    signal: AbortSignal.timeout(10_000),
  });
  const payload = await response.json() as { errors?: unknown[] };
  if (!response.ok || payload.errors?.length) throw new Error("Railway rejected the A.K.M.P-scoped variable update");
}
