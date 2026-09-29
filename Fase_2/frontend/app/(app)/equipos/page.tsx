"use client";
import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import * as Api from "@/lib/cliente";
import type { Contrato, Pagina, Sujeto } from "@/lib/tipos";
import { Campo, Chip, Modal, Paginador, Spinner, Vacio } from "@/components/ui";

export default function EquiposPage() {
  const [data, setData] = useState<Pagina<Sujeto> | null>(null);
  const [page, setPage] = useState(1);
  const [contratos, setContratos] = useState<Contrato[]>([]);
  const [tipos, setTipos] = useState<string[]>([]);
  const [filtro, setFiltro] = useState({ search: "", estado: "", contrato_id: "", tipo_equipo: "" });
  const [modal, setModal] = useState(false);
  const [error, setError] = useState("");
  const [aviso, setAviso] = useState("");
  const [f, setF] = useState({ contrato_id: "", patente: "", tipo_equipo: "", marca: "", modelo: "", anio: "" });

  const cargar = useCallback(() => {
    Api.equipos.listar({
      page, page_size: 50,
      search: filtro.search || undefined,
      estado: (filtro.estado || undefined) as Sujeto["estado"] | undefined,
      contrato_id: filtro.contrato_id || undefined,
      tipo_equipo: filtro.tipo_equipo || undefined,
    }).then(setData).catch((e) => setError(Api.mensajeError(e)));
  }, [page, filtro]);

  useEffect(() => { cargar(); }, [cargar]);
  useEffect(() => { setPage(1); }, [filtro]);

  useEffect(() => {
    Api.paginarTodo<Contrato>((p, ps) => Api.contratos.listar({ page: p, page_size: ps }))
      .then(setContratos).catch(() => {});
    // Conjunto cerrado: `{items, total}`, no pagina.
    Api.catalogo.tiposEquipo().then((r) => setTipos(Api.items(r))).catch(() => {});
  }, []);

  const items = Api.items(data);

  const crear = async (e: React.FormEvent) => {
    e.preventDefault(); setError(""); setAviso("");
    try {
      const s = await Api.equipos.crear({
        contrato_id: f.contrato_id,
        patente: f.patente,
        tipo_equipo: f.tipo_equipo,
        marca: f.marca || null,
        modelo: f.modelo || null,
        anio: f.anio ? Number(f.anio) : null,
      });
      setModal(false);
      setF({ contrato_id: "", patente: "", tipo_equipo: "", marca: "", modelo: "", anio: "" });
      setAviso(`${s.patente} agregado con ${s.documentos_creados} documentos requeridos.`);
      setPage(1);
      cargar();
    } catch (err) { setError(Api.mensajeError(err)); }
  };

  if (!data) return error ? <p className="p-6 text-red-600">{error}</p> : <Spinner />;
  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-brand">Equipos y vehículos</h1>
        <button className="btn-primary" onClick={() => setModal(true)}>+ Agregar equipo</button>
      </div>
      {error && <p className="rounded-lg bg-red-50 px-4 py-2 text-sm text-red-700">{error}</p>}
      {aviso && <p className="rounded-lg bg-sky-50 px-4 py-2 text-sm text-sky-800">ℹ️ {aviso}</p>}

      <div className="flex flex-wrap gap-3">
        <input className="input max-w-xs" placeholder="Buscar patente o marca…"
          value={filtro.search} onChange={(e) => setFiltro({ ...filtro, search: e.target.value })} />
        <select className="input max-w-[180px]" value={filtro.estado} onChange={(e) => setFiltro({ ...filtro, estado: e.target.value })}>
          <option value="">Todos los estados</option>
          <option value="ok">Acreditado</option><option value="proc">En proceso</option>
          <option value="venc">Vencido</option><option value="falta">Pendiente</option>
        </select>
        <select className="input max-w-[220px]" value={filtro.contrato_id} onChange={(e) => setFiltro({ ...filtro, contrato_id: e.target.value })}>
          <option value="">Todos los contratos</option>
          {contratos.map((c) => <option key={c.id} value={c.id}>{c.nombre}</option>)}
        </select>
        <select className="input max-w-[200px]" value={filtro.tipo_equipo} onChange={(e) => setFiltro({ ...filtro, tipo_equipo: e.target.value })}>
          <option value="">Todos los tipos</option>
          {tipos.map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
      </div>

      {items.length === 0 ? (
        <Vacio mensaje="No hay equipos con ese filtro." />
      ) : (
        <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
          <table className="tabla">
            <thead><tr><th>Patente</th><th>Tipo</th><th>Marca / modelo</th><th>Año</th><th>Contrato</th><th>Estado</th><th>%</th></tr></thead>
            <tbody>
              {items.map((s) => (
                <tr key={s.id}>
                  <td><Link className="font-mono font-medium text-accent hover:underline" href={`/equipos/${s.id}`}>{s.patente}</Link></td>
                  <td>{s.tipo_equipo}</td>
                  <td>{s.marca ?? "—"} {s.modelo ?? ""}</td>
                  <td>{s.anio ?? "—"}</td>
                  <td className="text-xs">{s.contrato.nombre}</td>
                  <td><Chip estado={s.estado} /></td>
                  <td className="font-bold">{s.stats.cumplimiento_pct}%</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <Paginador page={data.page} totalPaginas={data.total_pages} total={data.total}
        etiqueta="equipos" onPagina={setPage} />

      <Modal abierto={modal} titulo="Agregar equipo" onCerrar={() => setModal(false)}>
        <form onSubmit={crear} className="space-y-4">
          {error && <p className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p>}
          <Campo etiqueta="Contrato *">
            <select className="input" value={f.contrato_id} onChange={(e) => setF({ ...f, contrato_id: e.target.value })} required>
              <option value="">Selecciona…</option>
              {contratos.map((c) => <option key={c.id} value={c.id}>{c.nombre} — {c.faena.nombre}</option>)}
            </select>
          </Campo>
          <div className="grid grid-cols-2 gap-3">
            <Campo etiqueta="Patente *">
              <input className="input uppercase" placeholder="ABCD-12" value={f.patente}
                onChange={(e) => setF({ ...f, patente: e.target.value.toUpperCase() })} required />
            </Campo>
            <Campo etiqueta="Tipo *">
              <select className="input" value={f.tipo_equipo} onChange={(e) => setF({ ...f, tipo_equipo: e.target.value })} required>
                <option value="">Selecciona…</option>
                {tipos.map((t) => <option key={t} value={t}>{t}</option>)}
              </select>
            </Campo>
          </div>
          <div className="grid grid-cols-3 gap-3">
            <Campo etiqueta="Marca"><input className="input" value={f.marca} onChange={(e) => setF({ ...f, marca: e.target.value })} /></Campo>
            <Campo etiqueta="Modelo"><input className="input" value={f.modelo} onChange={(e) => setF({ ...f, modelo: e.target.value })} /></Campo>
            <Campo etiqueta="Año"><input className="input" type="number" min={1990} max={2030} value={f.anio} onChange={(e) => setF({ ...f, anio: e.target.value })} /></Campo>
          </div>
          <p className="rounded-lg bg-sky-50 px-3 py-2 text-xs text-sky-800">
            ℹ️ Se instanciarán los requisitos de equipo del contrato y de la faena.
          </p>
          <button className="btn-primary w-full py-2.5">Agregar equipo</button>
        </form>
      </Modal>
    </div>
  );
}
