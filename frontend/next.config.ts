import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  experimental: { useTypeScriptCli: false },
  async rewrites() {
    // The production load balancer sends /api/* directly to FastAPI.
    if (process.env.DISABLE_API_REWRITE === "1") return [];
    const apiOrigin = (process.env.API_ORIGIN || "http://127.0.0.1:8000").replace(/\/$/, "");
    return [{ source: "/api/:path*", destination: `${apiOrigin}/api/:path*` }];
  },
};

export default nextConfig;
