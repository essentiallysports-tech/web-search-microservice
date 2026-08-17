/** @type {import('next').NextConfig} */
const nextConfig = {
  // Nothing here is a public page, so no need for image optimisation or telemetry.
  poweredByHeader: false,
  async headers() {
    return [
      {
        source: "/(.*)",
        headers: [
          // An admin panel should never be framed, sniffed, or leak its URL onward.
          { key: "X-Frame-Options", value: "DENY" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "no-referrer" },
          { key: "X-Robots-Tag", value: "noindex, nofollow" },
        ],
      },
    ];
  },
};

export default nextConfig;
