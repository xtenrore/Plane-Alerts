import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";
const allowedMethods = new Set(["GET", "POST", "PUT", "PATCH", "DELETE"]);

async function proxy(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  if (!allowedMethods.has(request.method)) return NextResponse.json({ error: "Method not allowed" }, { status: 405 });
  const { path } = await context.params;
  const base = process.env.AKMP_CONTROL_API_URL;
  if (!base) return NextResponse.json({ error: "Control service unavailable" }, { status: 503 });
  const url = new URL(path.join("/"), `${base.replace(/\/$/, "")}/`);
  url.search = request.nextUrl.search;
  const headers = new Headers();
  for (const name of ["content-type", "cookie", "x-akmp-csrf", "origin", "user-agent"]) { const value = request.headers.get(name); if (value) headers.set(name, value); }
  headers.set("x-forwarded-for", request.headers.get("x-forwarded-for") ?? "web-proxy");
  const body = ["GET", "HEAD"].includes(request.method) ? undefined : await request.arrayBuffer();
  try {
    const init: RequestInit = { method: request.method, headers, cache: "no-store", signal: AbortSignal.timeout(15_000) };
    if (body) init.body = body;
    const upstream = await fetch(url, init);
    const response = new NextResponse(upstream.body, { status: upstream.status });
    for (const name of ["content-type", "cache-control", "www-authenticate"]) { const value = upstream.headers.get(name); if (value) response.headers.set(name, value); }
    const setCookies = typeof upstream.headers.getSetCookie === "function" ? upstream.headers.getSetCookie() : [];
    for (const value of setCookies) response.headers.append("set-cookie", value);
    return response;
  } catch { return NextResponse.json({ error: "Control service unavailable" }, { status: 503 }); }
}

export { proxy as GET, proxy as POST, proxy as PUT, proxy as PATCH, proxy as DELETE };
