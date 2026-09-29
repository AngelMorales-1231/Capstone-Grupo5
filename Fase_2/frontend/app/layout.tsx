import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Acredittia",
  description: "Acreditación de contratistas para faenas mineras y energéticas",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="es">
      <body className="bg-slate-100 text-slate-900 antialiased">{children}</body>
    </html>
  );
}
