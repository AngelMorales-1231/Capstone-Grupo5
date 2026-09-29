"use client";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import * as Api from "@/lib/cliente";

const ITEMS = [
  { href: "/dashboard", icono: "🏠", nombre: "Inicio" },
  { href: "/contratos", icono: "📄", nombre: "Contratos" },
  { href: "/faenas", icono: "⛰️", nombre: "Faenas" },
  { href: "/personal", icono: "👷", nombre: "Personal" },
  { href: "/equipos", icono: "🚛", nombre: "Equipos" },
  { href: "/alertas", icono: "🔔", nombre: "Alertas" },
];

export default function Sidebar() {
  const path = usePathname();
  const router = useRouter();
  const [noLeidas, setNoLeidas] = useState(0);
  const user = typeof window !== "undefined" ? Api.currentUser() : null;
  const esAdmin = user?.role === "admin";
  const impersonando = typeof window !== "undefined" ? sessionStorage.getItem("impersonar_nombre") : null;

  useEffect(() => {
    if (esAdmin && !impersonando) return;
    Api.alertas.resumen().then((r) => setNoLeidas(r.no_leidas)).catch(() => {});
  }, [path, esAdmin, impersonando]);

  const salir = async () => {
    // Se revoca el refresh token en el servidor antes de limpiar la sesión: si
    // solo se borrara del navegador el token seguiría siendo válido hasta caducar.
    const refresh = Api.refreshToken();
    if (refresh) await Api.auth.logout(refresh).catch(() => {});
    Api.clearSession();
    sessionStorage.clear();
    router.push("/login");
  };

  // Para un contract_admin el rótulo útil es su contrato, no la empresa.
  const subtitulo = esAdmin ? "Administración"
    : user?.contrato?.nombre ?? user?.company?.nombre ?? "";

  return (
    <aside className="flex h-screen w-56 flex-col border-r border-slate-200 bg-brand text-white max-md:hidden">
      <div className="px-5 py-5">
        <div className="text-xl font-extrabold tracking-tight">ACREDITTIA</div>
        <div className="text-xs text-white/60">{subtitulo}</div>
      </div>
      {impersonando && (
        <div className="mx-3 mb-2 rounded-lg bg-amber-500/90 px-3 py-2 text-xs font-semibold text-white">
          Viendo como {impersonando}
          <button className="ml-2 underline" onClick={() => { sessionStorage.clear(); router.push("/admin"); }}>salir</button>
        </div>
      )}
      <nav className="flex-1 space-y-1 px-3">
        {esAdmin && (
          <Link href="/admin" className={`flex items-center gap-3 rounded-lg px-3 py-2 text-sm ${path === "/admin" ? "bg-white/15 font-semibold" : "hover:bg-white/10"}`}>
            <span>🛡️</span> Panel admin
          </Link>
        )}
        {(!esAdmin || impersonando) && ITEMS.map((i) => (
          <Link key={i.href} href={i.href}
            className={`flex items-center gap-3 rounded-lg px-3 py-2 text-sm ${path.startsWith(i.href) ? "bg-white/15 font-semibold" : "hover:bg-white/10"}`}>
            <span>{i.icono}</span> {i.nombre}
            {i.href === "/alertas" && noLeidas > 0 && (
              <span className="ml-auto rounded-full bg-red-500 px-2 py-0.5 text-xs font-bold">{noLeidas}</span>
            )}
          </Link>
        ))}
      </nav>
      <button onClick={salir} className="m-3 rounded-lg px-3 py-2 text-left text-sm text-white/70 hover:bg-white/10">
        ⎋ Cerrar sesión
      </button>
    </aside>
  );
}
