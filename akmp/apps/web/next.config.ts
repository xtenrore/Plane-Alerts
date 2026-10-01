import type { NextConfig } from "next";

const allowedPublic = new Set(["NEXT_PUBLIC_APP_VERSION", "NEXT_PUBLIC_SITE_ORIGIN"]);
for (const name of Object.keys(process.env)) {
  if (/^(NEXT_PUBLIC_|PUBLIC_|VITE_|REACT_APP_)/.test(name) && !allowedPublic.has(name)) {
    throw new Error(`Public environment variable ${name} is not in the explicit allowlist`);
  }
}

const config: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  productionBrowserSourceMaps: false,
  reactStrictMode: true,
  async headers() {
    return [{ source: "/(.*)", headers: [
      { key: "Strict-Transport-Security", value: "max-age=31536000; includeSubDomains; preload" },
      { key: "X-Content-Type-Options", value: "nosniff" },
      { key: "Referrer-Policy", value: "no-referrer" },
      { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=(), payment=(), usb=()" },
      { key: "X-Frame-Options", value: "DENY" }
    ] }];
  }
};
export default config;
