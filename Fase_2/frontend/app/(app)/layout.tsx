"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Sidebar from "@/components/Sidebar";
import { currentUser } from "@/lib/api";

export default function AppLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const [listo, setListo] = useState(false);

  useEffect(() => {
    if (!currentUser()) router.replace("/login");
    else setListo(true);
  }, [router]);

  if (!listo) return null;
  return (
    <div className="flex">
      <Sidebar />
      <main className="min-h-screen flex-1 overflow-x-auto p-6">{children}</main>
    </div>
  );
}
