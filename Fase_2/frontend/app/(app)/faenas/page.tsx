"use client";
import { useEffect, useState } from "react";
import * as Api from "@/lib/cliente";
import type { EstadoAcceso, Faena, PlataformaFaena } from "@/lib/tipos";
import { Chip, Modal, Spinner } from "@/components/ui";

export default function FaenasPage() {
  const [faenas, setFaenas] = useState<Faena[] | null>(null);
  const [sel, setSel] = useState<Faena | null>(null);
  const [plataformas, setPlataformas] = useState<PlataformaFaena[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    // El catálogo de faenas pagina: se recorren todas las páginas para el mapa.
    Api.paginarTodo<Faena>((p, ps) => Api.faenas.listar({ page: p, page_size: ps }))
      .then(setFaenas).catch((e) => setError(Api.mensajeError(e)));
  }, []);

  const abrir = async (f: Faena) => {
    setSel(f); setError(""); setPlataformas([]);
    try {
      setPlataformas(await Api.lista(Api.faenas.plataformas(f.id, { page_size: 100 })));
    } catch (e) { setError(Api.mensajeError(e)); }
  };

  /** Upsert del acceso de la empresa a la plataforma del mandante. */
  const cambiarAcceso = async (p: PlataformaFaena, estado: EstadoAcceso) => {
    if (!sel) return;
    setError("");
    try {
      const actualizada = await Api.faenas.editarAcceso(sel.id, p.id, { estado });
      setPlataformas((prev) => prev.map((x) => (x.id === p.id ? { ...x, ...actualizada } : x)));
    } catch (e) { setError(Api.mensajeError(e)); }
  };

  if (!faenas) return error ? <p className="p-6 text-red-600">{error}</p> : <Spinner />;
  const grupos = Array.from(new Set(faenas.map((f) => f.grupo ?? "Otras faenas")));

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold text-brand">Faenas de Chile</h1>
      {error && <p className="rounded-lg bg-red-50 px-4 py-2 text-sm text-red-700">{error}</p>}
      {grupos.map((g) => (
        <section key={g}>
          <h2 className="mb-3 text-sm font-bold uppercase tracking-wide text-slate-500">{g}</h2>
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {faenas.filter((f) => (f.grupo ?? "Otras faenas") === g).map((f) => (
              <button key={f.id} onClick={() => abrir(f)}
                className="rounded-xl border border-slate-200 bg-white p-4 text-left shadow-sm transition hover:shadow-md">
                <div className="mb-1 h-1.5 w-10 rounded-full" style={{ background: f.color ?? "#2E86AB" }} />
                <div className="font-bold text-slate-800">{f.nombre}</div>
                <div className="text-xs text-slate-500">{f.mandante} · {f.region}</div>
                <div className="mt-2 flex flex-wrap gap-3 text-xs text-slate-600">
                  <span>📄 {f.stats.contratos}</span>
                  <span>👷 {f.stats.personal_acreditado}/{f.stats.personal}</span>
                  <span>🚛 {f.stats.equipos_acreditados}/{f.stats.equipos}</span>
                  {f.stats.docs_total > 0 && <span>✅ {f.stats.cumplimiento_pct}%</span>}
                </div>
              </button>
            ))}
          </div>
        </section>
      ))}

      <Modal abierto={!!sel} titulo={sel ? `${sel.nombre} — plataformas del mandante` : ""} onCerrar={() => setSel(null)}>
        <div className="space-y-2">
          {plataformas.map((p) => (
            <div key={p.id} className="rounded-lg bg-slate-50 px-3 py-2 text-sm">
              <div className="flex flex-wrap items-center gap-2">
                <b className="flex-1">{p.nombre}</b>
                <Chip estado={p.acceso.estado} />
              </div>
              {p.descripcion && <div className="text-slate-500">{p.descripcion}</div>}
              {p.acceso.solicitado_at && (
                <div className="text-xs text-slate-400">
                  solicitado {new Date(p.acceso.solicitado_at).toLocaleDateString("es-CL")}
                  {p.acceso.habilitado_at && ` · habilitado ${new Date(p.acceso.habilitado_at).toLocaleDateString("es-CL")}`}
                </div>
              )}
              <div className="mt-1 flex flex-wrap gap-2">
                {p.url && (
                  <a className="btn-ghost text-xs" href={p.url} target="_blank" rel="noopener">Abrir ↗</a>
                )}
                {p.acceso.estado === "sin_acceso" && (
                  <button className="btn-ghost text-xs" onClick={() => cambiarAcceso(p, "solicitada")}>
                    Marcar como solicitado
                  </button>
                )}
                {p.acceso.estado !== "activa" && (
                  <button className="btn-primary text-xs" onClick={() => cambiarAcceso(p, "activa")}>
                    Ya tengo acceso
                  </button>
                )}
              </div>
            </div>
          ))}
          {plataformas.length === 0 && <p className="text-sm text-slate-500">Sin plataformas registradas.</p>}
        </div>
      </Modal>
    </div>
  );
}
