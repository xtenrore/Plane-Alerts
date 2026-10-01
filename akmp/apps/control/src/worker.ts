import { createHash, randomUUID } from "node:crypto";
import Fastify from "fastify";
import pg from "pg";
import { migrate, pool, audit } from "./db.js";
import { assertEnvironmentPolicy } from "./policy.js";
import { approvedModels, providerSlots, type ProviderName } from "./providers.js";
import { safeError } from "./redact.js";

assertEnvironmentPolicy(process.env);
await migrate();
const workerId = `worker-${randomUUID()}`;
let processing = false;
let lastLoop = new Date(0);

const slotPool = (provider: ProviderName, index: number): string => `${provider.toUpperCase()}_UNVERIFIED_${index + 1}`;
const accountSlot = (index: number) => index === 0 ? "CLOUDFLARE_ACCOUNT_ID" : "CLOUDFLARE_ACCOUNT_ID_2";

async function syncProviderInventory(): Promise<void> {
  for (const [provider, slots] of Object.entries(providerSlots) as [ProviderName, readonly string[]][]) {
    for (const [index, slot] of slots.entries()) {
      const configured = Boolean(process.env[slot]);
      const account = provider === "cloudflare" ? process.env[accountSlot(index)] : undefined;
      const accountHash = account ? createHash("sha256").update(account).digest("hex") : null;
      const quotaPool = provider === "cloudflare" && accountHash ? `CLOUDFLARE_ACCOUNT_${accountHash.slice(0, 12)}` : slotPool(provider, index);
      await pool.query(
        `INSERT INTO provider_pools(id,provider,credential_slot,quota_pool,enabled,authorized,health,free_only,quota_status,model,account_identifier_hash,metadata)
         VALUES($1,$2,$3,$4,$5,$5,$6,true,'UNKNOWN',$7,$8,$9)
         ON CONFLICT(credential_slot) DO UPDATE SET enabled=excluded.enabled,authorized=excluded.authorized,
           health=CASE WHEN excluded.enabled=false THEN 'UNCONFIGURED' ELSE provider_pools.health END,
           quota_pool=excluded.quota_pool,account_identifier_hash=excluded.account_identifier_hash,metadata=excluded.metadata`,
        [`POOL-${provider}-${index + 1}`, provider, slot, quotaPool, configured, configured ? "PROBING" : "UNCONFIGURED", approvedModels[provider][0], accountHash, JSON.stringify({ quotaIndependence: accountHash ? "GROUPED_BY_ACCOUNT_ID" : "UNVERIFIED", source: "environment_presence_only" })],
      );
    }
  }
}

async function probeProvider(provider: ProviderName, slot: string, index: number): Promise<void> {
  const credential = process.env[slot];
  if (!credential || process.env.AKMP_PROVIDER_PROBES_ENABLED !== "true") return;
  let url: string;
  let headers: Record<string, string>;
  if (provider === "gemini") {
    url = "https://generativelanguage.googleapis.com/v1beta/models";
    headers = { "x-goog-api-key": credential };
  } else if (provider === "cloudflare") {
    const account = process.env[accountSlot(index)];
    if (!account || !/^[0-9a-f]{32}$/i.test(account)) {
      await pool.query("UPDATE provider_pools SET health='MISCONFIGURED',last_failure=now() WHERE credential_slot=$1", [slot]);
      return;
    }
    url = `https://api.cloudflare.com/client/v4/accounts/${encodeURIComponent(account)}/ai/models/search`;
    headers = { Authorization: `Bearer ${credential}` };
  } else {
    url = provider === "groq" ? "https://api.groq.com/openai/v1/models" : "https://api.mistral.ai/v1/models";
    headers = { Authorization: `Bearer ${credential}` };
  }
  try {
    const response = await fetch(url, { headers, signal: AbortSignal.timeout(8_000) });
    const health = response.ok ? "HEALTHY" : response.status === 429 ? "RATE_LIMITED" : [401, 403].includes(response.status) ? "AUTH_FAILED" : "DEGRADED";
    const retryAfter = response.headers.get("retry-after");
    const limitedUntil = response.status === 429 && retryAfter && /^\d+$/.test(retryAfter) ? new Date(Date.now() + Math.min(Number(retryAfter), 3600) * 1000) : null;
    await pool.query(
      "UPDATE provider_pools SET health=$2,last_success=CASE WHEN $2='HEALTHY' THEN now() ELSE last_success END,last_failure=CASE WHEN $2<>'HEALTHY' THEN now() ELSE last_failure END,rate_limited_until=$3 WHERE credential_slot=$1",
      [slot, health, limitedUntil],
    );
    if (response.ok) {
      await pool.query("UPDATE capabilities SET status='AVAILABLE',configured=true,verified=true,updated_at=now() WHERE id=$1", [`${provider}.generate`]);
      const fulfilled = await pool.query("UPDATE requests SET verified=true,status='FULFILLED',updated_at=now() WHERE secret_name=$1 AND configured=true RETURNING id", [slot]);
      for (const row of fulfilled.rows) {
        await pool.query("UPDATE jobs SET status='QUEUED',failure_reason=NULL,locked_by=NULL,locked_until=NULL WHERE status='WAITING' AND provider=$1", [provider]);
        await audit("worker", "request_fulfilled", "request", row.id, { capability: `${provider}.generate` });
      }
    }
  } catch (error) {
    await pool.query("UPDATE provider_pools SET health='DEGRADED',last_failure=now() WHERE credential_slot=$1", [slot]);
    await audit("worker", "provider_probe_failed", "provider_slot", slot, { provider, reason: safeError(error) });
  }
}

async function probeProviders(): Promise<void> {
  for (const [provider, slots] of Object.entries(providerSlots) as [ProviderName, readonly string[]][]) {
    for (const [index, slot] of slots.entries()) await probeProvider(provider, slot, index);
  }
}

type Job = { id: string; type: string; attempt: number; checkpoint: Record<string, unknown> };
async function claimJob(): Promise<Job | null> {
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    const state = await client.query<{ paused: boolean; emergency_stop: boolean }>("SELECT paused,emergency_stop FROM system_state WHERE id='global' FOR SHARE");
    if (state.rows[0]?.paused || state.rows[0]?.emergency_stop) { await client.query("ROLLBACK"); return null; }
    const result = await client.query<Job>(
      `SELECT id,type,attempt,checkpoint FROM jobs WHERE status='QUEUED' AND (locked_until IS NULL OR locked_until<now())
       ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1`,
    );
    const job = result.rows[0];
    if (!job) { await client.query("ROLLBACK"); return null; }
    const attempt = job.attempt + 1;
    await client.query("UPDATE jobs SET status='RUNNING',started_at=coalesce(started_at,now()),attempt=$2,locked_by=$3,locked_until=now()+interval '5 minutes' WHERE id=$1", [job.id, attempt, workerId]);
    await client.query("INSERT INTO job_attempts(job_id,attempt,started_at,status,checkpoint) VALUES($1,$2,now(),'RUNNING',$3)", [job.id, attempt, JSON.stringify(job.checkpoint)]);
    await client.query("COMMIT");
    await audit("worker", "job_started", "job", job.id, { type: job.type, attempt });
    return { ...job, attempt };
  } catch (error) { await client.query("ROLLBACK"); throw error; } finally { client.release(); }
}

function rewardCents(text: string): number | null {
  const matches = [...text.matchAll(/(?:\$|USD\s*)(\d{1,6})(?![\d,])/gi)].map((m) => Number(m[1]) * 100).filter((n) => n > 0);
  return matches.length === 1 ? matches[0]! : null;
}

async function discoveryCycle(job: Job): Promise<{ count: number; opportunityIds: string[] }> {
  const query = encodeURIComponent("is:issue is:open label:bounty archived:false");
  const response = await fetch(`https://api.github.com/search/issues?q=${query}&sort=updated&order=desc&per_page=10`, {
    headers: { Accept: "application/vnd.github+json", "User-Agent": "AKMP/0.1 public-opportunity-research" },
    signal: AbortSignal.timeout(12_000),
  });
  if (!response.ok) throw new Error(`GitHub public search returned ${response.status}`);
  const payload = await response.json() as { items?: Array<{ id: number; title: string; body: string | null; html_url: string; repository_url: string; updated_at: string }> };
  const ids: string[] = [];
  for (const item of (payload.items ?? []).slice(0, 10)) {
    const combined = `${item.title}\n${item.body ?? ""}`.slice(0, 20_000);
    const reward = rewardCents(combined);
    if (!reward) continue;
    const id = `AKMP-GH-${item.id}`;
    const description = (item.body ?? "").replace(/\s+/g, " ").slice(0, 600);
    const evidenceHash = createHash("sha256").update(combined).digest("hex");
    await pool.query(
      `INSERT INTO opportunities(id,title,description,source_url,source_kind,platform,state,potential_reward_cents,requires_kyc,requires_card,requires_upfront_payment)
       VALUES($1,$2,$3,$4,'REAL','GitHub','DISCOVERED',$5,false,false,false)
       ON CONFLICT(id) DO UPDATE SET title=excluded.title,description=excluded.description,potential_reward_cents=excluded.potential_reward_cents,updated_at=now()`,
      [id, item.title.slice(0, 300), description, item.html_url, reward],
    );
    await pool.query(
      `INSERT INTO opportunity_evidence(id,opportunity_id,source_url,excerpt,content_hash) VALUES($1,$2,$3,$4,$5)
       ON CONFLICT(id) DO NOTHING`,
      [`EVIDENCE-${item.id}-${evidenceHash.slice(0, 12)}`, id, item.html_url, combined.slice(0, 1000), evidenceHash],
    );
    ids.push(id);
    await audit("worker", "opportunity_discovered", "opportunity", id, { source: "GitHub public search", rewardCents: reward });
  }
  return { count: ids.length, opportunityIds: ids };
}

async function runJob(job: Job): Promise<void> {
  try {
    if (job.type !== "DISCOVERY_CYCLE") throw new Error("Unsupported job type");
    const output = await discoveryCycle(job);
    await pool.query("UPDATE jobs SET status='SUCCEEDED',finished_at=now(),output_reference=$2,checkpoint=$3,locked_by=NULL,locked_until=NULL WHERE id=$1", [job.id, `db:opportunities:${output.count}`, JSON.stringify(output)]);
    await pool.query("UPDATE job_attempts SET status='SUCCEEDED',finished_at=now(),checkpoint=$3 WHERE job_id=$1 AND attempt=$2", [job.id, job.attempt, JSON.stringify(output)]);
    await audit("worker", "job_completed", "job", job.id, output);
  } catch (error) {
    const reason = safeError(error);
    const retry = job.attempt < 3;
    await pool.query("UPDATE jobs SET status=$2,finished_at=CASE WHEN $2='FAILED' THEN now() ELSE NULL END,failure_reason=$3,locked_by=NULL,locked_until=NULL WHERE id=$1", [job.id, retry ? "QUEUED" : "FAILED", reason]);
    await pool.query("UPDATE job_attempts SET status='FAILED',finished_at=now(),failure_reason=$3 WHERE job_id=$1 AND attempt=$2", [job.id, job.attempt, reason]);
    await audit("worker", "job_failed", "job", job.id, { retry, reason });
  }
}

async function loop(): Promise<void> {
  if (processing) return;
  processing = true;
  lastLoop = new Date();
  try {
    await pool.query("UPDATE job_attempts a SET status='TIMED_OUT',finished_at=now(),failure_reason='WORKER_LEASE_EXPIRED' FROM jobs j WHERE a.job_id=j.id AND a.attempt=j.attempt AND a.status='RUNNING' AND j.status='RUNNING' AND j.locked_until<now()");
    await pool.query("UPDATE jobs SET status='QUEUED',locked_by=NULL,locked_until=NULL,failure_reason='RECOVERED_EXPIRED_LEASE' WHERE status='RUNNING' AND locked_until<now()");
    for (let count = 0; count < 10; count++) {
      const job = await claimJob();
      if (!job) break;
      await runJob(job);
    }
    await pool.query("INSERT INTO system_checkpoints(component,status,checkpoint,updated_at) VALUES('worker','HEALTHY',$1,now()) ON CONFLICT(component) DO UPDATE SET status='HEALTHY',checkpoint=excluded.checkpoint,updated_at=now()", [JSON.stringify({ workerId, lastLoop })]);
  } catch (error) {
    await pool.query("INSERT INTO system_checkpoints(component,status,checkpoint,updated_at) VALUES('worker','DEGRADED',$1,now()) ON CONFLICT(component) DO UPDATE SET status='DEGRADED',checkpoint=excluded.checkpoint,updated_at=now()", [JSON.stringify({ reason: safeError(error) })]).catch(() => undefined);
  } finally { processing = false; }
}

await syncProviderInventory();
await probeProviders();
await pool.query("INSERT INTO system_checkpoints(component,status,checkpoint) VALUES('goal_loop','HEALTHY','{\"mode\":\"event-driven\"}') ON CONFLICT(component) DO UPDATE SET status='HEALTHY',checkpoint=excluded.checkpoint,updated_at=now()");
await loop();
const timer = setInterval(loop, 5_000);
timer.unref();

const listener = new pg.Client({ connectionString: process.env.DATABASE_URL });
await listener.connect();
await listener.query("LISTEN akmp_jobs");
listener.on("notification", () => void loop());

const health = Fastify({ logger: false });
health.get("/health", async () => ({ status: "ok", service: "akmp-worker" }));
health.get("/ready", async (_request, reply) => {
  try {
    await pool.query("SELECT 1");
    const stale = Date.now() - lastLoop.getTime() > 30_000;
    return reply.code(stale ? 503 : 200).send({ status: stale ? "stale" : "ready", database: "ok", scheduler: "event-driven" });
  } catch { return reply.code(503).send({ status: "unavailable", database: "unavailable" }); }
});
await health.listen({ host: "0.0.0.0", port: Number(process.env.PORT ?? 4001) });

const shutdown = async () => {
  clearInterval(timer);
  await health.close();
  await listener.end();
  await pool.end();
  process.exit(0);
};
process.once("SIGTERM", () => void shutdown());
process.once("SIGINT", () => void shutdown());
