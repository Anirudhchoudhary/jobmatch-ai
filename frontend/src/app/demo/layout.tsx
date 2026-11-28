import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "JobMatch AI · Live demo",
  openGraph: {
    url: "/demo/",
    title: "JobMatch AI live demo: a real run, no sign-in",
    description:
      "A recorded run of a sample resume: match score with evidence, grounded rewrites, and a chat that cites its sources.",
    images: [{ url: "/og.png", width: 1200, height: 630, alt: "JobMatch AI match analysis" }],
  },
};

export default function DemoLayout({ children }: { children: React.ReactNode }) {
  return children;
}
