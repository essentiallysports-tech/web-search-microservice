import type { Metadata } from "next";

import "./globals.css";

export const metadata: Metadata = {
  title: "Search Service — API Tokens",
  description: "Issue and revoke API tokens for the web search microservice.",
  // An internal admin panel has no business in search results.
  robots: { index: false, follow: false },
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
