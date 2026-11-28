import type { Metadata, Viewport } from "next";
import { SessionProvider } from "@/lib/session";
import "./globals.css";

const DESCRIPTION =
  "See how well your resume fits a job, with the evidence: a match score, grounded rewrites and a chat that cites your documents. Live demo, no sign-in needed.";

export const metadata: Metadata = {
  metadataBase: new URL("https://jobmatch.ntrye.com"),
  title: "JobMatch AI",
  description: DESCRIPTION,
  openGraph: {
    type: "website",
    url: "/",
    siteName: "JobMatch AI",
    title: "JobMatch AI: how well does your resume fit the job?",
    description: DESCRIPTION,
    images: [{ url: "/og.png", width: 1200, height: 630, alt: "JobMatch AI match analysis with a score of 78" }],
  },
  twitter: { card: "summary_large_image", images: ["/og.png"] },
};

export const viewport: Viewport = { width: "device-width", initialScale: 1 };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="h-full antialiased">
      <body className="min-h-full flex flex-col">
        <SessionProvider>{children}</SessionProvider>
      </body>
    </html>
  );
}
