"use client";
import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import * as Api from "@/lib/cliente";
import type { Contrato, Faena, Pagina } from "@/lib/tipos";
import { Campo, Chip, Modal, Paginador, Spinner, Vacio } from "@/components/ui";

export default function ContratosPage() {
  const [data, setData] = useState<Pagina<Contrato> | null>(null);
  const [page, setPage] = useState(1);
  const [faenas, setFaenas] = useState<Faena[]>([]);
  const [modal, setModal] = useState(false);
  const [error, setError] = useState("");
  const [aviso, setAviso] = useState("");
  const [f, setF] = useState({ nombre: "", codigo: "", faena_id: "", fecha_inicio: "", fecha_termino: "" });

  const cargar = useCallback(() => {
    Api.contratos.listar({ page, page_size: 25, sort: "nombre" })
      .then(setData).catch((e) => setError(Api.mensajeError(e)));
  }, [page]);

  useEffect(() => { cargar(); }, [cargar]);

  useEffect(() => {
    // El selector necesita TODAS las faenas: sin recorrer las páginas el
    // desplegable se quedaría en las 25 primeras.
    Api.paginarTodo<Faena>((p, ps) => Api.faenas.listar({ page: p, page_size: ps, activa: true }))
      .then(setFaenas).catch(() => {});
  }, []);

  const items = Api.items(data);
  const grupos = Array.from(new Set(faenas.map((x) => x.grupo ?? "Otras faenas")));
  const templatePreview = f.faena_id
    ? "Se crearán automáticamente los documentos de empresa del estándar de la faena." : "";

  const crear = async (e: React.FormEvent) => {
    e.preventDefault(); setError("");
    try {
      const c = await Api.contratos.crear({
        nombre: f.nombre,
        faena_id: f.faena_id,
        codigo: f.codigo || null,
        fecha_inicio: f.fecha_inicio || null,
        fecha_termino: f.fecha_termino || null,
      });
      setModal(false);
      setF({ nombre: "", codigo: "", faena_id: "", fecha_inicio: "", fecha_termino: "" });
      setAviso(`Contrato creado con ${c.documentos_creados} documentos de empresa.`);
      setPage(1);
      cargar();
    } catch (err) { setError(Api.mensajeError(err)); }
  };

  const eliminar = async (c: Contrato) => {
    if (!confirm(`¿Eliminar "${c.nombre}"? Se eliminará todo su personal, equipos y documentos.`)) return;
    setError("");
    try {
      const r = await Api.contratos.eliminar(c.id);
      setAviso(`Contrato eliminado: ${r.sujetos_eliminados} sujetos y ${r.archivos_eliminados} archivos purgados.`);
      cargar();
    } catch (err) { setError(Api.mensajeError(err)); }
  };

  if (!data) return error ? <p className="p-6 text-red-600">{error}</p> : <Spinner />;
  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-brand">Contratos</h1>
        <button className="btn-primary" onClick={() => setModal(true)}>+ Nuevo contrato</button>
      </div>
      {error && <p className="rounded-lg bg-red-50 px-4 py-2 text-sm text-red-700">{error}</p>}
      {aviso && <p className="rounded-lg bg-sky-50 px-4 py-2 text-sm text-sky-800">{aviso}</p>}
      {items.length === 0 ? (
        <Vacio mensaje="Aún no tienes contratos. Crea el primero para comenzar a acreditar." cta="+ Nuevo contrato" onCta={() => setModal(true)} />
      ) : (
        <>
          <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
            <table className="tabla">
              <thead><tr><th>Contrato</th><th>Código</th><th>Faena</th><th>Estado</th><th>%</th><th>Personal</th><th>Equipos</th><th></th></tr></thead>
              <tbody>
                {items.map((c) => (
                  <tr key={c.id}>
                    <td><Link href={`/contratos/${c.id}`} className="font-medium text-accent hover:underline">{c.nombre}</Link></td>
                    <td className="font-mono text-xs">{c.codigo ?? "—"}</td>
                    <td>{c.faena.nombre}</td>
                    <td><Chip estado={c.estado} texto={c.estado === "vigente" ? "Vigente" : c.estado} /></td>
                    <td className="font-bold">{c.stats.cumplimiento_pct}%</td>
                    <td>{c.stats.personal.acreditados}/{c.stats.personal.total}</td>
                    <td>{c.stats.equipos.acreditados}/{c.stats.equipos.total}</td>
                    <td><button className="text-xs text-red-500 hover:underline" onClick={() => eliminar(c)}>Eliminar</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Paginador page={data.page} totalPaginas={data.total_pages} total={data.total}
            etiqueta="contratos" onPagina={setPage} />
        </>
      )}

      <Modal abierto={modal} titulo="Nuevo contrato" onCerrar={() => setModal(false)}>
        <form onSubmit={crear} className="space-y-4">
          {error && <p className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p>}
          <Campo etiqueta="Nombre del contrato *">
            <input className="input" value={f.nombre} onChange={(e) => setF({ ...f, nombre: e.target.value })} required />
          </Campo>
          <Campo etiqueta="Código / N° contrato">
            <input className="input" placeholder="TIEX-MLP-OP-2026" value={f.codigo} onChange={(e) => setF({ ...f, codigo: e.target.value })} />
          </Campo>
          <Campo etiqueta="Faena *">
            <select className="input" value={f.faena_id} onChange={(e) => setF({ ...f, faena_id: e.target.value })} required>
              <option value="">Selecciona una faena…</option>
              {grupos.map((g) => (
                <optgroup key={g} label={g}>
                  {faenas.filter((x) => (x.grupo ?? "Otras faenas") === g).map((x) => (
                    <option key={x.id} value={x.id}>{x.nombre} — {x.mandante}</option>
                  ))}
                </optgroup>
              ))}
            </select>
          </Campo>
          <div className="grid grid-cols-2 gap-3">
            <Campo etiqueta="Fecha inicio"><input className="input" type="date" value={f.fecha_inicio} onChange={(e) => setF({ ...f, fecha_inicio: e.target.value })} /></Campo>
            <Campo etiqueta="Fecha término"><input className="input" type="date" value={f.fecha_termino} onChange={(e) => setF({ ...f, fecha_termino: e.target.value })} /></Campo>
          </div>
          {templatePreview && <p className="rounded-lg bg-sky-50 px-3 py-2 text-xs text-sky-800">ℹ️ {templatePreview}</p>}
          <button className="btn-primary w-full py-2.5">Crear contrato</button>
        </form>
      </Modal>
    </div>
  );
}
