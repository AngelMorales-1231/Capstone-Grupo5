"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import * as Api from "@/lib/cliente";
import type {
  Archivo, Documento, Ejemplo, EstadoJob, Revision, SujetoDetalle,
} from "@/lib/tipos";
import { Barra, Chip, Modal, Spinner } from "./ui";

/**
 * Expediente de un trabajador o de un equipo.
 *
 * Concentra el flujo nuevo de archivos (§6 de RUPTURAS): la subida son tres
 * pasos con SAS y la revisión IA es asíncrona, así que tras subir el documento
 * sigue en `falta` y hay que sondear el job antes de saber el veredicto. La
 * pantalla lo refleja con dos estados distintos: «Subiendo» y «Analizando».
 */
const ICONO_HALLAZGO: Record<string, string> = {
  error: "⛔", warning: "⚠️", info: "✅",
};

type Subiendo = { docId: string; pct: number; texto: string };
type Analizando = { docId: string; titulo: string; status: EstadoJob };

function FilaDoc({ doc, subiendo, analizando, onSubir, onToggle, onEjemplo, onDescargar }: {
  doc: Documento;
  subiendo: Subiendo | null;
  analizando: boolean;
  onSubir: (d: Documento, f: File) => void;
  onToggle: (d: Documento) => void;
  onEjemplo: (clave: string) => void;
  onDescargar: (d: Documento, a: Archivo) => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const ocupado = !!subiendo || analizando;
  return (
    <div className="flex flex-wrap items-center gap-2 border-b border-slate-100 px-4 py-3 last:border-0">
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="truncate text-sm font-medium text-slate-800">{doc.titulo}</span>
          {!doc.obligatorio && <span className="text-xs text-slate-400">(opcional)</span>}
          {doc.plataforma && <span className="rounded bg-indigo-50 px-1.5 text-xs text-indigo-700">{doc.plataforma}</span>}
        </div>
        <div className="mt-0.5 flex flex-wrap items-center gap-2 text-xs text-slate-500">
          {doc.vence && <span>vence {doc.vence}{doc.dias_para_vencer != null && doc.dias_para_vencer >= 0 ? ` (${doc.dias_para_vencer} días)` : ""}</span>}
          {(doc.archivos ?? []).map((a) => (
            <button key={a.id} onClick={() => onDescargar(doc, a)}
              className="rounded bg-slate-100 px-1.5 py-0.5 text-slate-600 hover:bg-slate-200">📎 {a.filename}</button>
          ))}
        </div>
        {subiendo && <div className="mt-1.5 max-w-xs"><Barra pct={subiendo.pct} texto={subiendo.texto} /></div>}
        {analizando && (
          <div className="mt-1 text-xs font-medium text-sky-700">
            🤖 Analizando el documento con IA…
          </div>
        )}
      </div>
      <Chip estado={doc.estado_calc} />
      {doc.ejemplo_clave && (
        <button className="btn-ghost text-xs" onClick={() => onEjemplo(doc.ejemplo_clave!)}>Ver ejemplo</button>
      )}
      <button className="btn-ghost text-xs" onClick={() => onToggle(doc)}>
        {doc.estado === "ok" ? "Marcar pendiente" : "Marcar al día"}
      </button>
      <input ref={inputRef} type="file" accept=".pdf,.jpg,.jpeg,.png" className="hidden"
        onChange={(e) => { const f = e.target.files?.[0]; if (f) onSubir(doc, f); e.target.value = ""; }} />
      <button className="btn-primary text-xs" disabled={ocupado} onClick={() => inputRef.current?.click()}>
        {subiendo ? `Subiendo ${subiendo.pct}%` : analizando ? "Analizando…" : "📎 Subir"}
      </button>
    </div>
  );
}

export default function SubjectDetail({ tipo, id }: { tipo: "personal" | "equipos"; id: string }) {
  const [data, setData] = useState<SujetoDetalle | null>(null);
  const [subiendo, setSubiendo] = useState<Subiendo | null>(null);
  const [analizando, setAnalizando] = useState<Analizando | null>(null);
  const [review, setReview] = useState<{ doc: string; r: Revision } | null>(null);
  const [ejemplo, setEjemplo] = useState<Ejemplo | null>(null);
  const [error, setError] = useState("");
  const [aviso, setAviso] = useState("");

  const cargar = useCallback(() => {
    const pedir = tipo === "personal" ? Api.personal.detalle : Api.equipos.detalle;
    pedir(id).then(setData).catch((e) => setError(Api.mensajeError(e)));
  }, [tipo, id]);
  useEffect(cargar, [cargar]);

  /**
   * Sube el archivo y espera el veredicto de la IA.
   *
   * El documento sigue en `falta` en la respuesta de la subida: se recarga el
   * expediente dos veces, una al confirmar el archivo y otra cuando el job
   * termina y ya se aplicó la acción (marcar ok, autocompletar el vencimiento).
   */
  const subir = async (doc: Documento, file: File) => {
    setError(""); setAviso("");
    setSubiendo({ docId: doc.id, pct: 0, texto: "Preparando la subida…" });
    let jobId = "";
    try {
      const res = await Api.subirDocumento(doc.id, file, (pct, etapa) => {
        const texto = etapa === "firmando" ? "Preparando la subida…"
          : etapa === "subiendo" ? "Subiendo el archivo…"
          : etapa === "confirmando" ? "Registrando el archivo…" : "Listo";
        setSubiendo({ docId: doc.id, pct, texto });
      });
      jobId = res.job_id;
      cargar();
    } catch (e) {
      setError(Api.mensajeError(e));
      return;
    } finally {
      setSubiendo(null);
    }

    setAnalizando({ docId: doc.id, titulo: doc.titulo, status: "queued" });
    try {
      const r = await Api.esperarRevision(jobId, {
        onEstado: (parcial) =>
          setAnalizando({ docId: doc.id, titulo: doc.titulo, status: parcial.status }),
      });
      setReview({ doc: doc.titulo, r });
      cargar();
    } catch (e) {
      // El job sigue vivo en el servidor: no es un fallo de la subida.
      if (Api.esApiError(e) && e.code === "REVISION_TIMEOUT") setAviso(e.message);
      else setError(Api.mensajeError(e));
    } finally {
      setAnalizando(null);
    }
  };

  const toggle = async (doc: Documento) => {
    setError("");
    try {
      const actualizado = await Api.documentos.editar(doc.id,
        { estado: doc.estado === "ok" ? "falta" : "ok" });
      if (actualizado.vence_derivado) {
        setAviso(actualizado.nota
          ?? `El vencimiento se derivó de la plantilla: ${actualizado.vence}`);
      }
      cargar();
    } catch (e) { setError(Api.mensajeError(e)); }
  };

  const verEjemplo = async (clave: string) => {
    try { setEjemplo(await Api.catalogo.ejemplo(clave)); }
    catch (e) { setError(Api.mensajeError(e)); }
  };

  const descargar = async (doc: Documento, a: Archivo) => {
    setError("");
    try { await Api.descargarArchivo(doc.id, a.id); }
    catch (e) { setError(Api.mensajeError(e)); }
  };

  if (error && !data) return <p className="p-6 text-red-600">{error}</p>;
  if (!data) return <Spinner />;

  const esTrabajador = tipo === "personal";
  const fila = (d: Documento) => (
    <FilaDoc key={d.id} doc={d}
      subiendo={subiendo?.docId === d.id ? subiendo : null}
      analizando={analizando?.docId === d.id}
      onSubir={subir} onToggle={toggle} onEjemplo={verEjemplo}
      onDescargar={descargar} />
  );

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-bold text-brand">{data.nombre}</h1>
        <Chip estado={data.estado} />
        <span className="text-sm text-slate-500">
          {esTrabajador ? `${data.rut} · ${data.cargo ?? "—"}` : `${data.patente} · ${data.tipo_equipo}`}
          {" · "}{data.contrato.nombre} ({data.contrato.faena})
        </span>
        <span className="ml-auto text-sm font-semibold text-slate-600">
          Cumplimiento: {data.stats.cumplimiento_pct}%
        </span>
      </div>
      {error && <p className="rounded-lg bg-red-50 px-4 py-2 text-sm text-red-700">{error}</p>}
      {aviso && (
        <p className="flex items-start gap-2 rounded-lg bg-sky-50 px-4 py-2 text-sm text-sky-800">
          <span>ℹ️</span><span className="flex-1">{aviso}</span>
          <button className="text-sky-600 underline" onClick={() => setAviso("")}>cerrar</button>
        </p>
      )}

      <section className="rounded-xl border border-slate-200 bg-white shadow-sm">
        <h2 className="border-b border-slate-200 px-4 py-3 text-sm font-bold uppercase tracking-wide text-slate-600">
          Documentos requeridos ({data.documentos.length})
        </h2>
        {data.documentos.map(fila)}
      </section>

      {esTrabajador && data.licencia_interna && (
        <section className="rounded-xl border border-slate-200 bg-white shadow-sm">
          <div className="flex flex-wrap items-center gap-3 border-b border-slate-200 px-4 py-3">
            <h2 className="text-sm font-bold uppercase tracking-wide text-slate-600">
              Licencia interna de mina (EMSIPOR)
            </h2>
            <Chip estado={data.licencia_interna.emsipor_estado} />
            {data.licencia_interna.numero && (
              <span className="text-xs text-slate-500">
                {data.licencia_interna.numero} · vence {data.licencia_interna.vence ?? "—"}
              </span>
            )}
          </div>
          {data.documentos_emsipor.map(fila)}
        </section>
      )}

      <Modal abierto={!!review} titulo={`Revisión IA — ${review?.doc}`} onCerrar={() => setReview(null)}>
        {review && (
          <div className="space-y-3">
            <div className="flex flex-wrap items-center gap-3">
              {review.r.status === "failed" ? (
                <Chip estado="failed" texto="La revisión falló" />
              ) : (
                <Chip estado={review.r.resultado === "validado" ? "ok" : review.r.resultado === "con_errores" ? "venc" : "porvenc"}
                  texto={review.r.resultado === "validado" ? "Validado" : review.r.resultado === "con_errores" ? "Con errores" : "Con observaciones"} />
              )}
              {review.r.confianza != null && (
                <span className="text-sm text-slate-500">Confianza: {(review.r.confianza * 100).toFixed(0)}%</span>
              )}
            </div>
            {review.r.accion_aplicada && (
              <p className="rounded-lg bg-slate-50 px-3 py-2 text-sm text-slate-600">
                Acción aplicada: {review.r.accion_aplicada}
              </p>
            )}
            {review.r.error && (
              <p className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">{review.r.error}</p>
            )}
            <ul className="space-y-2">
              {review.r.hallazgos.map((h, i) => (
                <li key={i} className="flex items-start gap-2 rounded-lg bg-slate-50 px-3 py-2 text-sm">
                  <span>{ICONO_HALLAZGO[h.tipo]}</span>
                  <div>
                    <div className="font-mono text-xs text-slate-400">{h.codigo}</div>
                    {h.mensaje}
                  </div>
                </li>
              ))}
            </ul>
            <button className="btn-primary w-full" onClick={() => setReview(null)}>Entendido</button>
          </div>
        )}
      </Modal>

      <Modal abierto={!!ejemplo} titulo={ejemplo?.nombre ?? ""} onCerrar={() => setEjemplo(null)}>
        {ejemplo && (
          <div className="space-y-3 text-sm">
            {ejemplo.referencia && <p className="text-slate-500">Referencia: {ejemplo.referencia}</p>}
            {ejemplo.campos_clave && ejemplo.campos_clave.length > 0 && (
              <div className="rounded-lg bg-slate-50 p-3">
                <div className="mb-2 font-semibold text-slate-700">Campos clave que valida la IA:</div>
                {ejemplo.campos_clave.map(([k, v], i) => (
                  <div key={i} className="flex justify-between border-b border-slate-200 py-1 last:border-0">
                    <span className="font-medium">{k}</span><span className="text-slate-500">{v}</span>
                  </div>
                ))}
              </div>
            )}
            {ejemplo.notas && ejemplo.notas.length > 0 && (
              <ul className="list-inside list-disc text-slate-600">
                {ejemplo.notas.map((n, i) => <li key={i}>{n}</li>)}
              </ul>
            )}
            {ejemplo.pdf_url && (
              <a className="btn-ghost inline-block" href={ejemplo.pdf_url} target="_blank" rel="noopener">
                Ver documento de ejemplo ↗
              </a>
            )}
            {ejemplo.tip && <p className="rounded-lg bg-amber-50 px-3 py-2 text-amber-800">💡 {ejemplo.tip}</p>}
          </div>
        )}
      </Modal>
    </div>
  );
}
