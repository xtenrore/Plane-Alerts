import { readFile } from "node:fs/promises";
import pg from "pg";

const { Pool } = pg;
export const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
  max: Number(process.env.AKMP_DB_POOL_SIZE ?? 10),
  idleTimeoutMillis: 30_000,
  connectionTimeoutMillis: 5_000,
  ssl: process.env.AKMP_DB_SSL === "true" ? { rejectUnauthorized: true } : undefined,
});

export async function migrate(): Promise<void> {
  const client = await pool.connect();
  try {
    await client.query("SELECT pg_advisory_lock(680761)");
    const sql = await readFile(new URL("../migrations/001_init.sql", import.meta.url), "utf8");
    await client.query(sql);
  } finally {
    await client.query("SELECT pg_advisory_unlock(680761)").catch(() => undefined);
    client.release();
  }
}

export async function audit(actor: string, eventType: string, entityType: string, entityId: string | null, details: Record<string, unknown> = {}): Promise<void> {
  await pool.query("INSERT INTO audit_events(actor,event_type,entity_type,entity_id,details) VALUES($1,$2,$3,$4,$5)", [actor, eventType, entityType, entityId, JSON.stringify(details)]);
}
