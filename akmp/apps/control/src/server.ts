import { randomUUID, createHash } from "node:crypto";
import Fastify from "fastify";
import cookie from "@fastify/cookie";
import helmet from "@fastify/helmet";
import rateLimit from "@fastify/rate-limit";
import { z } from "zod";
import { isWalletSecretLike } from "@akmp/shared";
import { migrate, pool, audit } from "./db.js";
import { assertEnvironmentPolicy, absolutePolicy } from "./policy.js";
import { createSession, loginAllowed, recordLogin, requireCsrf, requireSession, revokeSession, verifyAdminPassword } from "./auth.js";
import { isAllowedSecretName, setWorkerSecret } from "./secret-sink.js";
import { providerSlots } from "./providers.js";
import { safeError } from "./redact.js";

assertEnvironmentPolicy(process.env);
await migrate();

const app = Fastify({ logger: false, trustProxy: true, bodyLimit: 32 * 1024 });
await app.register(cookie);
await app.register(rateLimit, { max: 120, timeWindow: "1 minute" });
await app.register(helmet, {
  global: true,
  contentSecurityPolicy: {
    directives: {
      defaultSrc: ["'none'"],
      frameAncestors: ["'none'"],
      baseUri: ["'none'"],
      formAction: ["'self'"],
    },
  },
  strictTransportSecurity: { maxAge: 31_536_000, includeSubDomains: true, preload: true },
  referrerPolicy: { policy: "no-referrer" },
});

const allowedOrigins = new Set((process.env.AKMP_ALLOWED_ORIGINS ?? "").split(",").map((v) => v.trim()).filter(Boolean));
app.addHook("onRequest", async (request, reply) => {
  const origin = request.headers.origin;
  if (!["GET", "HEAD", "OPTIONS"].includes(request.method) && origin && !allowedOrigins.has(origin)) {
    return reply.code(403).send({ error: "Origin denied" });
  }
});
app.addHook("onSend", async (_request, reply, payload) => {
  reply.header("Cache-Control", "no-store");
  reply.header("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()");
  return payload;
});

app.get("/health", async () => ({ status: "ok", service: "akmp-control", version: process.env.AKMP_APP_VERSION ?? "0.1.0" }));
app.get("/ready", async (_request, reply) => {
  try {
    await pool.query("SELECT 1");
    const migration = await pool.query("SELECT max(version) version FROM schema_migrations");
    return { status: "ready", database: "ok", migrations: Number(migration.rows[0]?.version ?? 0), providerRouter: "initialized" };
  } catch { return reply.code(503).send({ status: "unavailable", database: "unavailable" }); }
});

app.post("/auth/login", { config: { rateLimit: { max: 8, timeWindow: "15 minutes" } } }, async (request, reply) => {
  const body = z.object({ password: z.string().min(1).max(1024) }).safeParse(request.body);
  if (!body.success) return reply.code(400).send({ error: "Invalid login request" });
  if (!process.env.AKMP_ADMIN_PASSWORD_HASH?.startsWith("$argon2id$")) {
    return reply.code(503).send({ error: "Admin setup required", code: "ADMIN_SETUP_REQUIRED" });
  }
  if (!await loginAllowed(request.ip)) {
    await audit("anonymous", "login_rate_limited", "authentication", null);
    return reply.code(429).send({ error: "Too many failed attempts. Try again later." });
  }
  const valid = await verifyAdminPassword(body.data.password);
  await recordLogin(request.ip, valid);
  if (!valid) {
    await audit("anonymous", "login_failed", "authentication", null);
    return reply.code(401).send({ error: "Invalid credentials" });
  }
  const csrf = await createSession(reply);
  return { authenticated: true, csrf };
});

app.get("/auth/me", { preHandler: requireSession }, async () => ({ authenticated: true, role: "OWNER" }));
app.post("/auth/logout", { preHandler: [requireSession, requireCsrf] }, async (request, reply) => {
  await revokeSession(request, reply);
  return { loggedOut: true };
});

app.get("/api/overview", { preHandler: requireSession }, async () => {
  const [revenue, expenses, jobs, requests, opportunities, state, checkpoints] = await Promise.all([
    pool.query("SELECT coalesce(sum(amount_cents) FILTER (WHERE state='VERIFIED_PAID'),0)::text verified, coalesce(sum(amount_cents) FILTER (WHERE state<>'VERIFIED_PAID'),0)::text pending FROM revenue_events"),
    pool.query("SELECT coalesce(sum(amount_cents),0)::text total FROM expenses"),
    pool.query("SELECT status,count(*)::text count FROM jobs GROUP BY status"),
    pool.query("SELECT count(*)::text count FROM requests WHERE status IN ('OPEN','WAITING_FOR_OWNER','VERIFICATION_FAILED')"),
    pool.query("SELECT count(*)::text count FROM opportunities WHERE state NOT IN ('REJECTED','PAID','FAILED','CANCELLED')"),
    pool.query("SELECT paused,emergency_stop FROM system_state WHERE id='global'"),
    pool.query("SELECT component,status,updated_at FROM system_checkpoints ORDER BY component"),
  ]);
  const counts = Object.fromEntries(jobs.rows.map((row) => [row.status, Number(row.count)]));
  const verified = Number(revenue.rows[0]?.verified ?? 0);
  const pending = Number(revenue.rows[0]?.pending ?? 0);
  const spent = Number(expenses.rows[0]?.total ?? 0);
  return {
    money: { verifiedRevenueCents: verified, pendingRevenueCents: pending, expensesCents: spent, netProfitCents: verified - spent, aiSpendCents: 0 },
    goal: { title: "Earn first independently verified $1", currentCents: verified, targetCents: 100 },
    work: { running: counts.RUNNING ?? 0, queued: counts.QUEUED ?? 0, waiting: counts.WAITING ?? 0 },
    activeOpportunities: Number(opportunities.rows[0]?.count ?? 0),
    ownerRequests: Number(requests.rows[0]?.count ?? 0),
    system: { ...(state.rows[0] ?? { paused: false, emergency_stop: false }), checkpoints: checkpoints.rows },
  };
});

app.get("/api/opportunities", { preHandler: requireSession }, async () => (await pool.query("SELECT * FROM opportunities ORDER BY created_at DESC LIMIT 200")).rows);
app.get("/api/jobs", { preHandler: requireSession }, async () => (await pool.query("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 200")).rows);
app.get("/api/requests", { preHandler: requireSession }, async () => (await pool.query("SELECT * FROM requests ORDER BY CASE WHEN status IN ('OPEN','WAITING_FOR_OWNER','VERIFICATION_FAILED') THEN 0 ELSE 1 END, created_at DESC LIMIT 200")).rows);
app.get("/api/activity", { preHandler: requireSession }, async () => (await pool.query("SELECT id,actor,event_type,entity_type,entity_id,details,created_at FROM audit_events ORDER BY id DESC LIMIT 250")).rows);
app.get("/api/policies", { preHandler: requireSession }, async () => ({ immutable: absolutePolicy, documents: (await pool.query("SELECT id,version,document,created_at FROM policies WHERE active=true ORDER BY created_at DESC")).rows }));

app.get("/api/providers", { preHandler: requireSession }, async () => {
  const [pools, quotas, blocked] = await Promise.all([
    pool.query("SELECT id,provider,credential_slot,quota_pool,enabled,authorized,health,free_only,last_success,last_failure,rate_limited_until,quota_status,model,metadata FROM provider_pools ORDER BY provider,credential_slot"),
    pool.query("SELECT provider,pool,constraint_name,quota_limit,remaining,reset_at,source,observed_at FROM provider_quota ORDER BY provider,pool,constraint_name"),
    pool.query("SELECT provider,quota_pool,count(*)::text blocked_jobs FROM jobs WHERE status='WAITING' AND failure_reason='FREE_CAPACITY_EXHAUSTED' GROUP BY provider,quota_pool"),
  ]);
  const inventory = Object.entries(providerSlots).map(([provider, slots]) => ({ provider, expectedSlots: slots.length, configuredSlots: pools.rows.filter((row) => row.provider === provider).length }));
  return { inventory, pools: pools.rows, quotas: quotas.rows, blocked: blocked.rows, aiSpendCents: 0, googleAiPro: { status: "UNCLASSIFIED", enabled: false } };
});

app.get("/api/treasury", { preHandler: requireSession }, async () => {
  const [revenue, expenses, ledger, wallets] = await Promise.all([
    pool.query("SELECT state,coalesce(sum(amount_cents),0)::text amount_cents FROM revenue_events GROUP BY state"),
    pool.query("SELECT coalesce(sum(amount_cents),0)::text amount_cents FROM expenses"),
    pool.query("SELECT * FROM ledger_entries ORDER BY created_at DESC LIMIT 200"),
    pool.query("SELECT id,network,public_address,status,created_at FROM wallet_accounts ORDER BY created_at DESC"),
  ]);
  return { revenue: revenue.rows, expensesCents: Number(expenses.rows[0]?.amount_cents ?? 0), ledger: ledger.rows, wallets: wallets.rows };
});

app.post("/api/requests/:id/message", { preHandler: [requireSession, requireCsrf] }, async (request, reply) => {
  const params = z.object({ id: z.string().max(128) }).parse(request.params);
  const body = z.object({ message: z.string().min(1).max(4000) }).safeParse(request.body);
  if (!body.success) return reply.code(400).send({ error: "Invalid message" });
  if (isWalletSecretLike(body.data.message) || /(?:Bearer\s+\S{12,}|(?:api[_ -]?key|password|secret|token)\s*[:=]\s*\S{8,})/i.test(body.data.message)) {
    await audit("owner", "conversation_secret_rejected", "request", params.id);
    return reply.code(400).send({ error: "Secret-like content cannot be stored in conversations. Use the secure credential form." });
  }
  await pool.query("INSERT INTO request_messages(id,request_id,author,kind,body) VALUES($1,$2,'OWNER','MESSAGE',$3)", [randomUUID(), params.id, body.data.message]);
  await audit("owner", "request_message_added", "request", params.id);
  return { saved: true };
});

app.post("/api/requests/:id/reject", { preHandler: [requireSession, requireCsrf] }, async (request, reply) => {
  const { id } = z.object({ id: z.string().max(128) }).parse(request.params);
  const updated = await pool.query("UPDATE requests SET status='REJECTED',updated_at=now() WHERE id=$1 RETURNING id", [id]);
  if (!updated.rowCount) return reply.code(404).send({ error: "Request not found" });
  await audit("owner", "request_rejected", "request", id);
  return { rejected: true };
});

app.post("/api/requests/:id/secret", { preHandler: [requireSession, requireCsrf] }, async (request, reply) => {
  const { id } = z.object({ id: z.string().max(128) }).parse(request.params);
  const parsed = z.object({ secretName: z.string().max(128), value: z.string().min(1).max(8192) }).safeParse(request.body);
  if (!parsed.success || !isAllowedSecretName(parsed.data?.secretName ?? "")) return reply.code(400).send({ error: "Unapproved secret name" });
  if (isWalletSecretLike(parsed.data.value) || /(?:seed|recovery|mnemonic|private key)/i.test(parsed.data.secretName)) {
    await audit("owner", "wallet_secret_rejected", "request", id);
    return reply.code(400).send({ error: "Never submit a seed phrase, recovery phrase, mnemonic, or private key." });
  }
  try {
    await setWorkerSecret(parsed.data.secretName, parsed.data.value);
    await pool.query("UPDATE requests SET configured=true,configured_at=now(),status='IN_PROGRESS',updated_at=now(),secret_name=$2 WHERE id=$1", [id, parsed.data.secretName]);
    await audit("owner", "secret_configured", "request", id, { secretName: parsed.data.secretName });
    return { configured: true, verification: "queued" };
  } catch (error) {
    await audit("owner", "secret_configuration_failed", "request", id, { reason: safeError(error) });
    return reply.code(503).send({ error: "Secure A.K.M.P secret sink unavailable. Fulfill the scoped Railway access Request first." });
  }
});

app.post("/api/wallets", { preHandler: [requireSession, requireCsrf] }, async (request, reply) => {
  const body = z.object({ network: z.string().min(1).max(64), publicAddress: z.string().min(12).max(256) }).safeParse(request.body);
  if (!body.success || isWalletSecretLike(String((request.body as Record<string, unknown>)?.publicAddress ?? ""))) return reply.code(400).send({ error: "Only a public receive address is accepted. Never enter a seed phrase or private key." });
  const id = `WALLET-${randomUUID()}`;
  await pool.query("INSERT INTO wallet_accounts(id,network,public_address,status) VALUES($1,$2,$3,'RECEIVE_ONLY')", [id, body.data.network, body.data.publicAddress]);
  await audit("owner", "wallet_receive_address_added", "wallet", id, { network: body.data.network, addressHash: createHash("sha256").update(body.data.publicAddress).digest("hex") });
  return { id, status: "RECEIVE_ONLY" };
});

app.post("/api/system/:action", { preHandler: [requireSession, requireCsrf] }, async (request, reply) => {
  const { action } = z.object({ action: z.enum(["pause", "resume", "stop"]) }).parse(request.params);
  const values = action === "resume" ? [false, false] : action === "pause" ? [true, false] : [true, true];
  await pool.query("UPDATE system_state SET paused=$1,emergency_stop=$2,updated_at=now() WHERE id='global'", values);
  await audit("owner", action === "stop" ? "emergency_stop" : `system_${action}`, "system", "global");
  return { action, paused: values[0], emergencyStop: values[1] };
});

app.setErrorHandler(async (error, request, reply) => {
  await audit("system", "api_error", "request", null, { path: request.url, reason: safeError(error) }).catch(() => undefined);
  const candidate = error as { statusCode?: number; message?: string };
  const status = candidate.statusCode && candidate.statusCode < 500 ? candidate.statusCode : 500;
  return reply.code(status).send({ error: status < 500 ? candidate.message ?? "Request rejected" : "Internal error" });
});

const port = Number(process.env.PORT ?? 4000);
await app.listen({ host: "0.0.0.0", port });
