import type { NextConfig } from "next";

const api = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8030";

const nextConfig: NextConfig = {
  async rewrites() {
    return [{ source: "/backend/:path*", destination: `${api}/:path*` }];
  },
};

export default nextConfig;
