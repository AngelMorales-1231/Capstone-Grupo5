"use client";
import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { currentUser } from "@/lib/api";

export default function Home() {
  const router = useRouter();
  useEffect(() => {
    const u = currentUser();
    router.replace(u ? (u.role === "admin" ? "/admin" : "/dashboard") : "/login");
  }, [router]);
  return null;
}
