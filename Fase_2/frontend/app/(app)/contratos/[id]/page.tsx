"use client";
import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import * as Api from "@/lib/cliente";
import type {
  ActividadFila, Alerta, Contrato, Documento, Matriz, Pagina, Revision, Sujeto,
} from "@/lib/tipos";
import { Barra, Chip, Kpi, Modal, Paginador, SEVERIDAD_LABEL, Spinner } from "@/components/ui";

const TABS = ["Resumen", "Docs. empresa", "Personal", "Equipos", "Matriz",
  "Alertas", "Historial"] as const;
type Tab = (typeof TABS)[number];

export default function ContratoDetalle() {
  const { id } = useParams<{ id: string }>();
  const [c, setC] = useState<Contrato | null>(null);
  const [tab, setTab] = useState<Tab>("Resumen");
  const [page, setPage] = useState(1);
  const [docs, setDocs] = useState<Pagina<Documento> | null>(null);
  const [personal, setPersonal] = useState<Pagina<Sujeto> | null>(null);
  const [equipos, setEquipos] = useState<Pagina<Sujeto> | null>(null);
  const [alertas, setAlertas] = useState<Pagina<Alerta> | null>(null);
  const [hist, setHist] = useState<Pagina<ActividadFila> | null>(null);
  const [matriz, setMatriz] = useState<Matriz | null>(null);
  const [tipoMatriz, setTipoMatriz] = useState<"personal" | "equipo">("personal");
  const [review, setReview] = useState<Revision | null>(null);
  const [subiendo, setSubiendo] = useState<{ docId: string; pct: number; texto: string } | null>(null);
  const [analizandoId, setAnalizandoId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [aviso, setAviso] = useState("");

  const cargar = useCallback(() => {
    Api.contratos.detalle(id).then(setC).catch((e) => setError(Api.mensajeError(e)));
  }, [id]);
  useEffect(cargar, [cargar]);

  const cargarDocs = useCallback(() => {
    Api.contratos.documentos(id, { page, page_size: 50 })
      .then(setDocs).catch((e) => setError(Api.mensajeError(e)));
  }, [id, page]);

  // Cada pestaña consume su propio endpoint del contrato (§4): ya no hace falta
  // pedir /alertas o /dashboard/actividad y filtrar en el navegador.
  useEffect(() => {
    setError("");
    const fallo = (e: unknown) => setError(Api.mensajeError(e));
    if (tab === "Docs. empresa") cargarDocs();
    if (tab === "Personal") Api.contratos.personal(id, { page, page_size: 50 }).then(setPersonal).catch(fallo);
    if (tab === "Equipos") Api.contratos.equipos(id, { page, page_size: 50 }).then(setEquipos).catch(fallo);
    if (tab === "Alertas") Api.contratos.alertas(id, { page, page_size: 50 }).then(setAlertas).catch(fallo);
    if (tab === "Historial") Api.contratos.historial(id, { page, page_size: 50 }).then(setHist).catch(fallo);
    if (tab === "Matriz") {
      Api.contratos.matriz(id, { tipo: tipoMatriz, page, page_size: 25 })
        .then(setMatriz).catch(fallo);
    }
  }, [tab, id, page, tipoMatriz, cargarDocs]);

  const cambiarTab = (t: Tab) => { setTab(t); setPage(1); };

  /** Subida por SAS + espera del veredicto de la IA (el doc queda `falta`). */
  const subir = async (docId: string, file: File) => {
    setError(""); setAviso("");
    setSubiendo({ docId, pct: 0, texto: "Preparando la subida…" });
    let jobId = "";
    try {
      const res = await Api.subirDocumento(docId, file, (pct, etapa) => {
        const texto = etapa === "subiendo" ? "Subiendo el archivo…"
          : etapa === "confirmando" ? "Registrando el archivo…"
          : etapa === "firmando" ? "Preparando la subida…" : "Listo";
        setSubiendo({ docId, pct, texto });
      });
      jobId = res.job_id;
      cargarDocs();
    } catch (e) {
      setError(Api.mensajeError(e));
      return;
    } finally { setSubiendo(null); }

    setAnalizandoId(docId);
    try {
      setReview(await Api.esperarRevision(jobId));
      cargarDocs();
      cargar();
    } catch (e) {
      if (Api.esApiError(e) && e.code === "REVISION_TIMEOUT") setAviso(e.message);
      else setError(Api.mensajeError(e));
    } finally { setAnalizandoId(null); }
  };

  const descargar = async (docId: string, archivoId: string) => {
    setError("");
    try { await Api.descargarArchivo(docId, archivoId); }
    catch (e) { setError(Api.mensajeError(e)); }
  };

  if (!c) return error ? <p className="p-6 text-red-600">{error}</p> : <Spinner />;
  return (
    <div className="space-y-5">
      <div className="text-sm text-slate-500">
        <Link href="/contratos" className="text-accent hover:underline">Contratos</Link> / {c.nombre}
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-bold text-brand">{c.nombre}</h1>
        <Chip estado={c.estado} />
        <span className="text-sm text-slate-500">
          {c.faena.nombre} · {c.faena.mandante}
          {c.faena.region ? ` · ${c.faena.region}` : ""} · {c.codigo ?? "sin código"}
        </span>
      </div>
      {error && <p className="rounded-lg bg-red-50 px-4 py-2 text-sm text-red-700">{error}</p>}
      {aviso && <p className="rounded-lg bg-sky-50 px-4 py-2 text-sm text-sky-800">ℹ️ {aviso}</p>}

      <div className="flex gap-1 overflow-x-auto rounded-lg bg-slate-200/70 p-1 text-sm font-semibold">
        {TABS.map((t) => (
          <button key={t} onClick={() => cambiarTab(t)}
            className={`whitespace-nowrap rounded-md px-3 py-1.5 ${tab === t ? "bg-white text-brand shadow" : "text-slate-500 hover:text-slate-700"}`}>
            {t}
          </button>
        ))}
      </div>

      {tab === "Resumen" && (
        <div className="grid grid-cols-2 gap-4 lg:grid-cols-5">
          <Kpi titulo="Cumplimiento" valor={`${c.stats.cumplimiento_pct}%`} />
          <Kpi titulo="Personal" valor={`${c.stats.personal.acreditados}/${c.stats.personal.total}`} sub="acreditados" />
          <Kpi titulo="Equipos" valor={`${c.stats.equipos.acreditados}/${c.stats.equipos.total}`} sub="acreditados" />
          <Kpi titulo="Docs. empresa" valor={`${c.stats.docs_empresa.ok}/${c.stats.docs_empresa.total}`} sub="al día" />
          <Kpi titulo="Alertas activas" valor={c.stats.alertas_activas} color={c.stats.alertas_activas > 0 ? "text-red-600" : "text-emerald-600"} />
          <div className="col-span-2 rounded-xl border border-slate-200 bg-white p-4 text-sm shadow-sm lg:col-span-5">
            <b>Período:</b> {c.fecha_inicio ?? "—"} → {c.fecha_termino ?? "—"} ·{" "}
            <b>Renovación automática:</b> {c.renovacion_automatica ? "Sí" : "No"}
            {c.origen_ia_review_id && (
              <> · <b>Origen:</b> alta asistida por IA</>
            )}
          </div>
        </div>
      )}

      {tab === "Docs. empresa" && (
        <>
          <section className="rounded-xl border border-slate-200 bg-white shadow-sm">
            {Api.items(docs).map((d) => (
              <div key={d.id} className="flex flex-wrap items-center gap-2 border-b border-slate-100 px-4 py-3 last:border-0">
                <div className="min-w-0 flex-1">
                  <span className="text-sm font-medium">{d.titulo}</span>
                  {!d.obligatorio && <span className="ml-1 text-xs text-slate-400">(opcional)</span>}
                  <div className="text-xs text-slate-500">
                    {d.vence && `vence ${d.vence} · `}
                    {(d.archivos ?? []).map((a) => (
                      <button key={a.id} className="mr-1 rounded bg-slate-100 px-1.5 hover:bg-slate-200"
                        onClick={() => descargar(d.id, a.id)}>📎 {a.filename}</button>
                    ))}
                  </div>
                  {subiendo?.docId === d.id && (
                    <div className="mt-1.5 max-w-xs"><Barra pct={subiendo.pct} texto={subiendo.texto} /></div>
                  )}
                  {analizandoId === d.id && (
                    <div className="mt-1 text-xs font-medium text-sky-700">🤖 Analizando el documento con IA…</div>
                  )}
                </div>
                <Chip estado={d.estado_calc} />
                <label className={`btn-primary cursor-pointer text-xs ${subiendo || analizandoId === d.id ? "pointer-events-none opacity-60" : ""}`}>
                  {subiendo?.docId === d.id ? `Subiendo ${subiendo.pct}%`
                    : analizandoId === d.id ? "Analizando…" : "📎 Subir"}
                  <input type="file" accept=".pdf,.jpg,.jpeg,.png" className="hidden"
                    onChange={(e) => { const fl = e.target.files?.[0]; if (fl) subir(d.id, fl); e.target.value = ""; }} />
                </label>
              </div>
            ))}
            {docs && docs.total === 0 && <p className="p-4 text-sm text-slate-500">Este contrato no tiene documentos de empresa.</p>}
          </section>
          {docs && <Paginador page={docs.page} totalPaginas={docs.total_pages} total={docs.total} etiqueta="documentos" onPagina={setPage} />}
        </>
      )}

      {tab === "Personal" && (
        <>
          <ListaSujetos items={Api.items(personal)} tipo="personal" />
          {personal && <Paginador page={personal.page} totalPaginas={personal.total_pages} total={personal.total} etiqueta="trabajadores" onPagina={setPage} />}
        </>
      )}
      {tab === "Equipos" && (
        <>
          <ListaSujetos items={Api.items(equipos)} tipo="equipos" />
          {equipos && <Paginador page={equipos.page} totalPaginas={equipos.total_pages} total={equipos.total} etiqueta="equipos" onPagina={setPage} />}
        </>
      )}

      {tab === "Matriz" && (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-3">
            <select className="input max-w-[200px]" value={tipoMatriz}
              onChange={(e) => { setTipoMatriz(e.target.value as "personal" | "equipo"); setPage(1); }}>
              <option value="personal">Personal</option>
              <option value="equipo">Equipos</option>
            </select>
            <span className="text-xs text-slate-500">
              «—» significa que el requisito no aplica a ese sujeto; no es un incumplimiento.
            </span>
          </div>
          {matriz && <TablaMatriz matriz={matriz} onPagina={setPage} />}
        </div>
      )}

      {tab === "Alertas" && (
        <section className="space-y-2">
          {Api.items(alertas).length === 0 && <p className="text-sm text-slate-500">Sin alertas para este contrato.</p>}
          {Api.items(alertas).map((a) => (
            <div key={a.id} className="flex items-center gap-3 rounded-xl border border-slate-200 bg-white px-4 py-3 shadow-sm">
              <Chip estado={a.severidad} texto={SEVERIDAD_LABEL[a.severidad]} />
              <div className="text-sm"><b>{a.titulo}</b><div className="text-slate-500">{a.descripcion}</div></div>
              <Chip estado={a.estado} />
            </div>
          ))}
          {alertas && <Paginador page={alertas.page} totalPaginas={alertas.total_pages} total={alertas.total} etiqueta="alertas" onPagina={setPage} />}
        </section>
      )}

      {tab === "Historial" && (
        <>
          <ul className="space-y-1.5 rounded-xl border border-slate-200 bg-white p-4 text-sm text-slate-600 shadow-sm">
            {Api.items(hist).map((h) => (
              <li key={h.id}>
                <span className="text-slate-400">{new Date(h.created_at).toLocaleString("es-CL")} · </span>
                {h.descripcion}
                {h.usuario?.nombre && <span className="text-slate-400"> · {h.usuario.nombre}</span>}
              </li>
            ))}
            {hist && hist.total === 0 && <li className="text-slate-500">Sin actividad registrada para este contrato.</li>}
          </ul>
          {hist && <Paginador page={hist.page} totalPaginas={hist.total_pages} total={hist.total} etiqueta="movimientos" onPagina={setPage} />}
        </>
      )}

      <Modal abierto={!!review} titulo="Revisión IA" onCerrar={() => setReview(null)} ancho="max-w-xl">
        {review && (
          <div className="space-y-3">
            <div className="flex flex-wrap items-center gap-3">
              <Chip estado={review.status === "failed" ? "failed"
                : review.resultado === "validado" ? "ok"
                : review.resultado === "con_errores" ? "venc" : "porvenc"}
                texto={review.resultado ?? review.status} />
              {review.confianza != null && (
                <span className="text-sm text-slate-500">Confianza: {(review.confianza * 100).toFixed(0)}%</span>
              )}
            </div>
            {review.accion_aplicada && (
              <p className="rounded-lg bg-slate-50 px-3 py-2 text-sm text-slate-600">
                Acción aplicada: {review.accion_aplicada}
              </p>
            )}
            <ul className="space-y-2 text-sm">
              {review.hallazgos.map((h, i) => (
                <li key={i} className="rounded-lg bg-slate-50 px-3 py-2">
                  {h.tipo === "error" ? "⛔" : h.tipo === "warning" ? "⚠️" : "✅"} {h.mensaje}
                </li>
              ))}
            </ul>
            <button className="btn-primary w-full" onClick={() => setReview(null)}>Entendido</button>
          </div>
        )}
      </Modal>
    </div>
  );
}

function ListaSujetos({ items, tipo }: { items: Sujeto[]; tipo: "personal" | "equipos" }) {
  return (
    <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
      <table className="tabla">
        <thead>
          <tr>{tipo === "personal" ? <><th>Nombre</th><th>RUT</th><th>Cargo</th></> : <><th>Patente</th><th>Tipo</th><th>Marca</th></>}<th>Estado</th><th>%</th></tr>
        </thead>
        <tbody>
          {items.map((s) => (
            <tr key={s.id}>
              {tipo === "personal" ? (
                <>
                  <td><Link className="font-medium text-accent hover:underline" href={`/personal/${s.id}`}>{s.nombre}</Link></td>
                  <td className="font-mono text-xs">{s.rut}</td><td>{s.cargo ?? "—"}</td>
                </>
              ) : (
                <>
                  <td><Link className="font-mono font-medium text-accent hover:underline" href={`/equipos/${s.id}`}>{s.patente}</Link></td>
                  <td>{s.tipo_equipo}</td><td>{s.marca ?? "—"} {s.modelo ?? ""}</td>
                </>
              )}
              <td><Chip estado={s.estado} /></td>
              <td className="font-bold">{s.stats.cumplimiento_pct}%</td>
            </tr>
          ))}
        </tbody>
      </table>
      {items.length === 0 && <p className="p-4 text-sm text-slate-500">Sin registros en este contrato.</p>}
    </div>
  );
}

/**
 * Matriz de cumplimiento sujeto × requisito.
 *
 * Es **dispersa**: solo llegan las celdas de los requisitos que aplican a cada
 * sujeto, indexadas por `col`. El hueco se pinta «—», que no es lo mismo que
 * `falta`. Las columnas son las de los sujetos de la página, así que pueden
 * cambiar al paginar.
 */
function TablaMatriz({ matriz, onPagina }: { matriz: Matriz; onPagina: (p: number) => void }) {
  return (
    <div className="space-y-3">
      <div className="overflow-x-auto rounded-xl border border-slate-200 bg-white shadow-sm">
        <table className="tabla">
          <thead>
            <tr>
              <th className="sticky left-0 bg-slate-50">Sujeto</th>
              <th>%</th>
              {matriz.columnas.map((col, i) => (
                <th key={i} className="whitespace-nowrap text-xs">
                  {col.titulo}{col.obligatorio ? "" : " (opc.)"}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {matriz.filas.map((f) => {
              const porCol = new Map(f.celdas.map((c) => [c.col, c]));
              return (
                <tr key={f.sujeto_id}>
                  <td className="sticky left-0 bg-white">
                    <div className="font-medium">{f.nombre}</div>
                    <div className="text-xs text-slate-500">{f.rut ?? ""} {f.cargo ?? ""}</div>
                  </td>
                  <td className="font-bold">{f.cumplimiento_pct}%</td>
                  {matriz.columnas.map((_, i) => {
                    const celda = porCol.get(i);
                    if (!celda) return <td key={i} className="text-center text-slate-300">—</td>;
                    return (
                      <td key={i} className="text-center">
                        <Chip estado={celda.estado_calc} texto={celda.vence ?? undefined} />
                      </td>
                    );
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
        {matriz.filas.length === 0 && <p className="p-4 text-sm text-slate-500">Sin sujetos para esta matriz.</p>}
      </div>
      <Paginador page={matriz.page} totalPaginas={matriz.total_pages} total={matriz.total_filas}
        etiqueta="filas" onPagina={onPagina} />
    </div>
  );
}
