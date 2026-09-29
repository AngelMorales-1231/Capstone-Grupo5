"use client";
import { useCallback, useEffect, useState } from "react";
import * as Api from "@/lib/cliente";
import type { Alerta, EstadoAlerta, Pagina, ResumenAlertas, Severidad } from "@/lib/tipos";
import { Chip, Kpi, Paginador, SEVERIDAD_LABEL, Spinner, Vacio } from "@/components/ui";

export default function AlertasPage() {
  const [data, setData] = useState<Pagina<Alerta> | null>(null);
  const [resumen, setResumen] = useState<ResumenAlertas | null>(null);
  const [page, setPage] = useState(1);
  const [sev, setSev] = useState("");
  const [estado, setEstado] = useState("");
  const [error, setError] = useState("");

  const cargar = useCallback(() => {
    Api.alertas.listar({
      page, page_size: 50,
      severidad: (sev || undefined) as Severidad | undefined,
      estado: (estado || undefined) as EstadoAlerta | undefined,
      // Con un estado concreto se muestran también las ya resueltas.
      solo_activas: estado === "resuelta" ? false : undefined,
    }).then(setData).catch((e) => setError(Api.mensajeError(e)));
    Api.alertas.resumen().then(setResumen).catch(() => {});
  }, [page, sev, estado]);

  useEffect(() => { cargar(); }, [cargar]);
  useEffect(() => { setPage(1); }, [sev, estado]);

  const marcar = async (id: string, cambios: { leida?: boolean; resuelta?: boolean; estado?: EstadoAlerta }) => {
    setError("");
    try { await Api.alertas.editar(id, cambios); cargar(); }
    catch (e) { setError(Api.mensajeError(e)); }
  };

  const todasLeidas = async () => {
    setError("");
    try { await Api.alertas.marcarLeidas(); cargar(); }
    catch (e) { setError(Api.mensajeError(e)); }
  };

  if (!data || !resumen) return error ? <p className="p-6 text-red-600">{error}</p> : <Spinner />;
  const items = Api.items(data);
  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-brand">Alertas</h1>
        <button className="btn-ghost" onClick={todasLeidas}>Marcar todas como leídas</button>
      </div>
      {error && <p className="rounded-lg bg-red-50 px-4 py-2 text-sm text-red-700">{error}</p>}
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-5">
        <Kpi titulo="Críticas" valor={resumen.criticas} color="text-red-600" />
        <Kpi titulo="Advertencias" valor={resumen.advertencias} color="text-amber-600" />
        <Kpi titulo="Activas" valor={resumen.activas} />
        <Kpi titulo="No leídas" valor={resumen.no_leidas} />
        <Kpi titulo="Resueltas (30d)" valor={resumen.resueltas_30d} color="text-emerald-600" />
      </div>
      <div className="flex flex-wrap gap-3">
        <select className="input max-w-[200px]" value={sev} onChange={(e) => setSev(e.target.value)}>
          <option value="">Todas las severidades</option>
          <option value="critica">Crítica</option><option value="alta">Alta</option>
          <option value="media">Media</option><option value="baja">Baja</option>
          <option value="advertencia">Advertencia</option><option value="informativa">Informativa</option>
        </select>
        <select className="input max-w-[200px]" value={estado} onChange={(e) => setEstado(e.target.value)}>
          <option value="">Todos los estados</option>
          <option value="nueva">Nueva</option>
          <option value="en_progreso">En progreso</option>
          <option value="bloqueante">Bloqueante</option>
          <option value="informativa">Informativa</option>
          <option value="resuelta">Resuelta</option>
        </select>
      </div>

      {items.length === 0 ? (
        <Vacio mensaje="Sin alertas con ese filtro. Todo en orden. ✅" />
      ) : (
        <div className="space-y-2">
          {items.map((a) => (
            <div key={a.id}
              className={`flex flex-wrap items-center gap-3 rounded-xl border bg-white px-4 py-3 shadow-sm ${a.leida ? "border-slate-200" : "border-accent/60"}`}>
              <Chip estado={a.severidad} texto={SEVERIDAD_LABEL[a.severidad]} />
              <Chip estado={a.estado} />
              <div className="min-w-0 flex-1 text-sm">
                <b>{a.titulo}</b>
                <div className="text-slate-500">{a.descripcion}</div>
                <div className="text-xs text-slate-400">
                  {new Date(a.created_at).toLocaleString("es-CL")} · origen: {a.origen}
                  {a.plataforma && ` · ${a.plataforma}`}
                  {a.resuelta_at && ` · resuelta ${new Date(a.resuelta_at).toLocaleDateString("es-CL")}`}
                </div>
              </div>
              {!a.leida && <button className="btn-ghost text-xs" onClick={() => marcar(a.id, { leida: true })}>Marcar leída</button>}
              {a.estado !== "en_progreso" && !a.resuelta && (
                <button className="btn-ghost text-xs" onClick={() => marcar(a.id, { estado: "en_progreso" })}>En progreso</button>
              )}
              {a.resuelta
                ? <button className="btn-ghost text-xs" onClick={() => marcar(a.id, { resuelta: false })}>Reabrir</button>
                : <button className="btn-primary text-xs" onClick={() => marcar(a.id, { resuelta: true })}>Resolver</button>}
            </div>
          ))}
        </div>
      )}
      <Paginador page={data.page} totalPaginas={data.total_pages} total={data.total}
        etiqueta="alertas" onPagina={setPage} />
    </div>
  );
}
