"use client";
import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import * as Api from "@/lib/cliente";
import type { Cargo, Contrato, Pagina, Sujeto } from "@/lib/tipos";
import { Campo, Chip, Modal, Paginador, Spinner, Vacio } from "@/components/ui";

/** Normaliza como el backend (`checklist.resolver_cargo`) para casar el texto. */
const normalizar = (s: string) =>
  s.trim().toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "");

export default function PersonalPage() {
  const [data, setData] = useState<Pagina<Sujeto> | null>(null);
  const [page, setPage] = useState(1);
  const [contratos, setContratos] = useState<Contrato[]>([]);
  const [cargos, setCargos] = useState<Cargo[]>([]);
  const [filtro, setFiltro] = useState({ search: "", estado: "", contrato_id: "", cargo_id: "" });
  const [modal, setModal] = useState(false);
  const [error, setError] = useState("");
  const [aviso, setAviso] = useState("");
  const [f, setF] = useState({ contrato_id: "", nombre: "", rut: "", cargo: "", es_conductor: false });

  const cargar = useCallback(() => {
    Api.personal.listar({
      page, page_size: 50,
      search: filtro.search || undefined,
      estado: (filtro.estado || undefined) as Sujeto["estado"] | undefined,
      contrato_id: filtro.contrato_id || undefined,
      cargo_id: filtro.cargo_id || undefined,
    }).then(setData).catch((e) => setError(Api.mensajeError(e)));
  }, [page, filtro]);

  useEffect(() => { cargar(); }, [cargar]);
  useEffect(() => { setPage(1); }, [filtro]);

  useEffect(() => {
    // Los selectores necesitan la colección completa, no la primera página.
    Api.paginarTodo<Contrato>((p, ps) => Api.contratos.listar({ page: p, page_size: ps }))
      .then(setContratos).catch(() => {});
    Api.paginarTodo<Cargo>((p, ps) => Api.cargos.listar({ page: p, page_size: ps, activo: true }))
      .then(setCargos).catch(() => {});
  }, []);

  const items = Api.items(data);

  const crear = async (e: React.FormEvent) => {
    e.preventDefault(); setError(""); setAviso("");
    // El cargo se ofrece como selector con texto libre: si el texto coincide con
    // un cargo del catálogo se manda `cargo_id`; si no, va como texto y el
    // backend lo crea (y responde `cargo_creado`).
    const texto = f.cargo.trim();
    const conocido = cargos.find((c) => normalizar(c.nombre) === normalizar(texto));
    try {
      const s = await Api.personal.crear({
        contrato_id: f.contrato_id,
        nombre: f.nombre,
        rut: f.rut,
        es_conductor: f.es_conductor,
        ...(conocido ? { cargo_id: conocido.id } : texto ? { cargo: texto } : {}),
      });
      setModal(false);
      setF({ contrato_id: "", nombre: "", rut: "", cargo: "", es_conductor: false });
      const partes = [`${s.documentos_creados} documentos creados`];
      if (s.cargo_creado) {
        partes.push(`se creó el cargo «${s.cargo}» en el catálogo de la empresa: conviene clasificarlo`);
        Api.paginarTodo<Cargo>((p, ps) => Api.cargos.listar({ page: p, page_size: ps, activo: true }))
          .then(setCargos).catch(() => {});
      }
      if (s.expediente_emsipor_creado) partes.push("se abrió el expediente EMSIPOR");
      setAviso(`${s.nombre} agregado: ${partes.join("; ")}.`);
      setPage(1);
      cargar();
    } catch (err) { setError(Api.mensajeError(err)); }
  };

  if (!data) return error ? <p className="p-6 text-red-600">{error}</p> : <Spinner />;
  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-brand">Personal</h1>
        <button className="btn-primary" onClick={() => setModal(true)}>+ Agregar trabajador</button>
      </div>
      {error && <p className="rounded-lg bg-red-50 px-4 py-2 text-sm text-red-700">{error}</p>}
      {aviso && <p className="rounded-lg bg-sky-50 px-4 py-2 text-sm text-sky-800">ℹ️ {aviso}</p>}

      <div className="flex flex-wrap gap-3">
        <input className="input max-w-xs" placeholder="Buscar por nombre o RUT…"
          value={filtro.search} onChange={(e) => setFiltro({ ...filtro, search: e.target.value })} />
        <select className="input max-w-[180px]" value={filtro.estado} onChange={(e) => setFiltro({ ...filtro, estado: e.target.value })}>
          <option value="">Todos los estados</option>
          <option value="ok">Acreditado</option><option value="proc">En proceso</option>
          <option value="venc">Vencido</option><option value="falta">Pendiente</option>
          <option value="baja">De baja</option>
        </select>
        <select className="input max-w-[220px]" value={filtro.contrato_id} onChange={(e) => setFiltro({ ...filtro, contrato_id: e.target.value })}>
          <option value="">Todos los contratos</option>
          {contratos.map((c) => <option key={c.id} value={c.id}>{c.nombre}</option>)}
        </select>
        <select className="input max-w-[220px]" value={filtro.cargo_id} onChange={(e) => setFiltro({ ...filtro, cargo_id: e.target.value })}>
          <option value="">Todos los cargos</option>
          {cargos.map((c) => <option key={c.id} value={c.id}>{c.nombre}</option>)}
        </select>
      </div>

      {items.length === 0 ? (
        <Vacio mensaje="No hay trabajadores con ese filtro." />
      ) : (
        <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
          <table className="tabla">
            <thead><tr><th>Nombre</th><th>RUT</th><th>Cargo</th><th>Contrato</th><th>Conductor</th><th>Estado</th><th>%</th></tr></thead>
            <tbody>
              {items.map((s) => (
                <tr key={s.id}>
                  <td><Link className="font-medium text-accent hover:underline" href={`/personal/${s.id}`}>{s.nombre}</Link></td>
                  <td className="font-mono text-xs">{s.rut}</td>
                  <td>{s.cargo ?? "—"}</td>
                  <td className="text-xs">{s.contrato.nombre}</td>
                  <td>{s.es_conductor ? "🚗" : "—"}</td>
                  <td><Chip estado={s.estado} /></td>
                  <td className="font-bold">{s.stats.cumplimiento_pct}%</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <Paginador page={data.page} totalPaginas={data.total_pages} total={data.total}
        etiqueta="trabajadores" onPagina={setPage} />

      <Modal abierto={modal} titulo="Agregar trabajador" onCerrar={() => setModal(false)}>
        <form onSubmit={crear} className="space-y-4">
          {error && <p className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p>}
          <Campo etiqueta="Contrato *">
            <select className="input" value={f.contrato_id} onChange={(e) => setF({ ...f, contrato_id: e.target.value })} required>
              <option value="">Selecciona…</option>
              {contratos.map((c) => <option key={c.id} value={c.id}>{c.nombre} — {c.faena.nombre}</option>)}
            </select>
          </Campo>
          <Campo etiqueta="Nombre completo *"><input className="input" value={f.nombre} onChange={(e) => setF({ ...f, nombre: e.target.value })} required /></Campo>
          <Campo etiqueta="RUT *"><input className="input" placeholder="12.345.678-5" value={f.rut} onChange={(e) => setF({ ...f, rut: e.target.value })} required /></Campo>
          <Campo etiqueta="Cargo">
            <input className="input" list="lista-cargos" placeholder="Conductor Nacional"
              value={f.cargo} onChange={(e) => setF({ ...f, cargo: e.target.value })} />
            <datalist id="lista-cargos">
              {cargos.map((c) => (
                <option key={c.id} value={c.nombre}>
                  {c.requiere_emsipor ? "Requiere EMSIPOR" : c.categoria}
                </option>
              ))}
            </datalist>
            <span className="mt-1 block text-xs text-slate-500">
              Elige uno del catálogo o escribe uno nuevo: si no existe se creará y
              te lo avisaremos para clasificarlo.
            </span>
          </Campo>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={f.es_conductor} onChange={(e) => setF({ ...f, es_conductor: e.target.checked })} />
            Es conductor (requiere Licencia Interna de Mina — agrega 9 documentos EMSIPOR)
          </label>
          <p className="rounded-lg bg-sky-50 px-3 py-2 text-xs text-sky-800">
            ℹ️ Se instanciarán los requisitos de personal del contrato y del cargo
            {f.es_conductor ? ", más los 9 documentos EMSIPOR" : ""}. El cargo
            también puede exigir el expediente EMSIPOR por sí solo.
          </p>
          <button className="btn-primary w-full py-2.5">Agregar trabajador</button>
        </form>
      </Modal>
    </div>
  );
}
