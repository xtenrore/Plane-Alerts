import { createHash, randomBytes } from "node:crypto";
import argon2 from "argon2";
import type { FastifyReply, FastifyRequest } from "fastify";
import { pool, audit } from "./db.js";

const SESSION_COOKIE = "akmp_session";
const CSRF_COOKIE = "akmp_csrf";
const ttlSeconds = 60 * 60 * 8;
const hash = (value: string) => createHash("sha256").update(value).digest("hex");

export async function verifyAdminPassword(password: string): Promise<boolean> {
  const passwordHash = process.env.AKMP_ADMIN_PASSWORD_HASH?.trim();
  if (!passwordHash?.startsWith("$argon2id$")) return false;
  try { return await argon2.verify(passwordHash, password); } catch { return false; }
}

export async function loginAllowed(ip: string): Promise<boolean> {
  const ipHash = hash(ip + (process.env.AKMP_SESSION_SIGNING_SECRET ?? "missing"));
  const result = await pool.query<{ attempts: string }>("SELECT count(*)::text attempts FROM login_attempts WHERE ip_hash=$1 AND succeeded=false AND created_at > now()-interval '15 minutes'", [ipHash]);
  return Number(result.rows[0]?.attempts ?? 0) < 5;
}

export async function recordLogin(ip: string, succeeded: boolean): Promise<void> {
  const ipHash = hash(ip + (process.env.AKMP_SESSION_SIGNING_SECRET ?? "missing"));
  await pool.query("INSERT INTO login_attempts(ip_hash,succeeded) VALUES($1,$2)", [ipHash, succeeded]);
}

export async function createSession(reply: FastifyReply, actor = "owner"): Promise<string> {
  const token = randomBytes(32).toString("base64url");
  const csrf = randomBytes(24).toString("base64url");
  await pool.query("INSERT INTO sessions(id,user_id,token_hash,csrf_hash,expires_at) VALUES($1,'owner',$2,$3,now()+$4::interval)", [crypto.randomUUID(), hash(token), hash(csrf), `${ttlSeconds} seconds`]);
  const common = { secure: true, sameSite: "strict" as const, path: "/", maxAge: ttlSeconds };
  reply.setCookie(SESSION_COOKIE, token, { ...common, httpOnly: true });
  reply.setCookie(CSRF_COOKIE, csrf, { ...common, httpOnly: false });
  await audit(actor, "session_created", "session", null);
  return csrf;
}

export async function requireSession(request: FastifyRequest, reply: FastifyReply): Promise<void> {
  const token = request.cookies[SESSION_COOKIE];
  if (!token) return reply.code(401).send({ error: "Authentication required" });
  const result = await pool.query<{ id: string; csrf_hash: string }>("SELECT id,csrf_hash FROM sessions WHERE token_hash=$1 AND revoked_at IS NULL AND expires_at>now()", [hash(token)]);
  const session = result.rows[0];
  if (!session) return reply.code(401).send({ error: "Session expired" });
  (request as FastifyRequest & { sessionId?: string; csrfHash?: string }).sessionId = session.id;
  (request as FastifyRequest & { sessionId?: string; csrfHash?: string }).csrfHash = session.csrf_hash;
  await pool.query("UPDATE sessions SET last_seen_at=now() WHERE id=$1", [session.id]);
}

export async function requireCsrf(request: FastifyRequest, reply: FastifyReply): Promise<void> {
  const stored = (request as FastifyRequest & { csrfHash?: string }).csrfHash;
  const header = String(request.headers["x-akmp-csrf"] ?? "");
  const cookie = request.cookies[CSRF_COOKIE] ?? "";
  if (!stored || !header || header !== cookie || hash(header) !== stored) return reply.code(403).send({ error: "CSRF validation failed" });
}

export async function revokeSession(request: FastifyRequest, reply: FastifyReply): Promise<void> {
  const id = (request as FastifyRequest & { sessionId?: string }).sessionId;
  if (id) await pool.query("UPDATE sessions SET revoked_at=now() WHERE id=$1", [id]);
  reply.clearCookie(SESSION_COOKIE, { path: "/" });
  reply.clearCookie(CSRF_COOKIE, { path: "/" });
  await audit("owner", "session_revoked", "session", id ?? null);
}
