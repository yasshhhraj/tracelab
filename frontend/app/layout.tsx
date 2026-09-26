import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "TraceLab · Investigations",
  description: "Evidence-first differential debugging dashboard",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en">
      <body>
        <header className="site-header">
          <div className="site-header-inner">
            <Link href="/investigations" className="brand" aria-label="TraceLab investigations">
              <span className="brand-mark" aria-hidden="true">T</span>
              <span>Trace<span className="brand-accent">Lab</span></span>
            </Link>
            <nav aria-label="Main navigation"><Link href="/investigations" className="nav-link">Investigations</Link></nav>
            <span className="header-label">DIFFERENTIAL DEBUGGING</span>
          </div>
        </header>
        {children}
      </body>
    </html>
  );
}
