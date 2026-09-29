"use client";
import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import * as Api from "@/lib/cliente";
import type { AdminStats, Empresa } from "@/lib/tipos";
import { Chip, Kpi, Spinner } from "@/components/ui";

export default function AdminPage() {
  const router = useRouter();
  const [companies, setCompanies] = useState<Empresa[] | null>(null);
  const [stats, setStats] = useState<AdminStats | null>(null);
  const [error, setError] = useState("");
  const [aviso, setAviso] = useState("");

  const cargar = useCallback(() => {
    // El listado pagina: se recorren todas las páginas para el back-office.
    Api.paginarTodo<Empresa>((p, ps) => Api.admin.empresas({ page: p, page_size: ps }))
      .then(setCompanies).catch((e) => setError(Api.mensajeError(e)));
    Api.admin.stats().then(setStats).catch((e) => setError(Api.mensajeError(e)));
  }, []);

  useEffect(() => {
    if (Api.currentUser()?.role !== "admin") { router.replace("/dashboard"); return; }
    sessionStorage.clear();
    cargar();
  }, [cargar, router]);

  const conAviso = async (accion: () => Promise<string>) => {
    setError(""); setAviso("");
    try { setAviso(await accion()); cargar(); }
    catch (e) { setError(Api.mensajeError(e)); }
  };

  const aprobar = (c: Empresa) => conAviso(async () => {
    await Api.admin.aprobar(c.id);
    return `${c.nombre} aprobada.`;
  });

  const rechazar = (c: Empresa) => {
    const reason = prompt("Motivo del rechazo:");
    if (!reason) return;
    conAviso(async () => {
      await Api.admin.rechazar(c.id, reason);
      return `${c.nombre} rechazada.`;
    });
  };

  const resetDemo = (c: Empresa) => {
    if (!confirm(`¿Reiniciar los datos de demo de "${c.nombre}"? Es irreversible.`)) return;
    conAviso(async () => {
      const r = await Api.admin.resetDemo(c.id);
      return r.nota;
    });
  };

  const impersonar = (c: Empresa) => {
    sessionStorage.setItem("impersonar", c.id);
    sessionStorage.setItem("impersonar_nombre", c.nombre);
    router.push("/dashboard");
  };

  if (!companies || !stats) return error ? <p className="p-6 text-red-600">{error}</p> : <Spinner />;
  const pendientes = companies.filter((c) => c.status === "pending");
  const resto = companies.filter((c) => c.status !== "pending");

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold text-brand">Panel de administración</h1>
      {error && <p className="rounded-lg bg-red-50 px-4 py-2 text-sm text-red-700">{error}</p>}
      {aviso && <p className="rounded-lg bg-sky-50 px-4 py-2 text-sm text-sky-800">ℹ️ {aviso}</p>}
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Kpi titulo="Empresas" valor={stats.empresas_total} sub={`${stats.rechazadas} rechazadas`} />
        <Kpi titulo="Pendientes" valor={stats.pendientes} color="text-amber-600" />
        <Kpi titulo="Aprobadas" valor={stats.aprobadas} color="text-emerald-600" />
        <Kpi titulo="Sujetos registrados" valor={stats.sujetos_total}
          sub={`${stats.personal.total} personas · ${stats.equipos.total} equipos`} />
      </div>
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Kpi titulo="Contratos" valor={stats.contratos_total} />
        <Kpi titulo="Faenas activas" valor={`${stats.faenas_activas}/${stats.faenas_total}`} />
        {/* Dos métricas distintas a propósito: media simple por empresa y media
            ponderada por documentos. */}
        <Kpi titulo="Cumplimiento promedio" valor={`${stats.cumplimiento_promedio}%`}
          sub={`media simple de ${stats.empresas_medidas} empresas`} />
        <Kpi titulo="Cumplimiento global" valor={`${stats.cumplimiento_global_pct}%`}
          sub={`${stats.documentos_ok}/${stats.documentos_total} documentos`} />
      </div>

      {pendientes.length > 0 && (
        <section className="rounded-xl border-2 border-amber-300 bg-amber-50/50 shadow-sm">
          <h2 className="border-b border-amber-200 px-4 py-3 text-sm font-bold uppercase text-amber-800">
            Solicitudes pendientes ({pendientes.length})
          </h2>
          {pendientes.map((c) => (
            <div key={c.id} className="flex flex-wrap items-center gap-3 border-b border-amber-100 px-4 py-3 last:border-0">
              <div className="flex-1 text-sm">
                <b>{c.nombre}</b> <span className="font-mono text-xs">{c.rut}</span>
                <div className="text-slate-500">{c.email} · {new Date(c.created_at).toLocaleDateString("es-CL")}</div>
              </div>
              <button className="btn-primary text-xs" onClick={() => aprobar(c)}>✓ Aprobar</button>
              <button className="rounded-lg bg-red-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-red-700"
                onClick={() => rechazar(c)}>✕ Rechazar</button>
            </div>
          ))}
        </section>
      )}

      <section className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
        <h2 className="border-b border-slate-200 px-4 py-3 text-sm font-bold uppercase text-slate-600">Empresas</h2>
        <table className="tabla">
          <thead><tr><th>Empresa</th><th>RUT</th><th>Email</th><th>Estado</th><th>Contratos</th><th>Cumplimiento</th><th></th></tr></thead>
          <tbody>
            {resto.map((c) => (
              <tr key={c.id}>
                <td className="font-medium">
                  {c.nombre}
                  {c.es_demo && <span className="ml-2 rounded bg-indigo-50 px-1.5 text-xs text-indigo-700">demo</span>}
                </td>
                <td className="font-mono text-xs">{c.rut}</td>
                <td>{c.email}</td>
                <td><Chip estado={c.status} /></td>
                <td>{c.contratos}</td>
                <td>{c.docs_total > 0 ? `${c.cumplimiento_pct}% (${c.docs_ok}/${c.docs_total})` : "—"}</td>
                <td className="whitespace-nowrap">
                  {c.status === "approved" && (
                    <button className="btn-ghost text-xs" onClick={() => impersonar(c)}>Ver como empresa →</button>
                  )}
                  {c.es_demo && (
                    <button className="btn-ghost text-xs" onClick={() => resetDemo(c)}>Reiniciar demo</button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </div>
  );
}
