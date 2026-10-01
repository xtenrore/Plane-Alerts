"use client";
import { useState, type FormEvent } from "react";
import { ArrowRight, KeyRound, LockKeyhole } from "lucide-react";

export default function LoginPage() {
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setLoading(true); setError("");
    const data = new FormData(event.currentTarget);
    const response = await fetch("/api/control/auth/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password: data.get("password") }) });
    const payload = await response.json().catch(() => ({})) as { error?: string };
    if (!response.ok) { setError(payload.error ?? "Unable to sign in"); setLoading(false); return; }
    window.location.assign("/overview");
  }
  return <main className="login-shell"><section className="login-panel" aria-labelledby="login-title"><div className="brand-mark"><span>AK</span></div><p className="eyebrow">Owner control plane</p><h1 id="login-title">A.K.M.P</h1><p className="login-subtitle">API Key Money Project</p><form onSubmit={submit} className="login-form"><label htmlFor="password">Admin password</label><div className="input-with-icon"><LockKeyhole size={17} aria-hidden="true"/><input id="password" name="password" type="password" autoComplete="current-password" required autoFocus /></div>{error && <div className="form-error" role="alert">{error}</div>}<button className="primary-button" disabled={loading}>{loading ? "Signing in…" : "Sign in securely"}<ArrowRight size={16}/></button></form><div className="security-note"><KeyRound size={16}/><span>Credentials are verified by the server and are never stored in browser storage.</span></div></section></main>;
}
