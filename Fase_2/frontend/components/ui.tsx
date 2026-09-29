"use client";
import { ReactNode } from "react";

export const ESTADO_LABEL: Record<string, string> = {
  ok: "Vigente", falta: "Pendiente", venc: "Vencido", porvenc: "Por vencer",
  proc: "En proceso", baja: "De baja",
  pending: "Pendiente", approved: "Aprobada", rejected: "Rechazada",
  vigente: "Vigente", pendiente: "Pendiente", parcial: "Parcial",
  aprobado: "Aprobado", por_vencer: "Por vencer",
  critica: "Crítica", alta: "Alta", media: "Media",
  advertencia: "Advertencia", informativa: "Informativa",
  // Estados de alerta (§7) y de acceso a plataformas (§3)
  nueva: "Nueva", en_progreso: "En progreso", bloqueante: "Bloqueante",
  resuelta: "Resuelta",
  activa: "Con acceso", solicitada: "Acceso solicitado", sin_acceso: "Sin acceso",
  // Estados de un job de IA (§6.1): la revisión es asíncrona
  queued: "En cola", processing: "Analizando", done: "Listo", failed: "Falló",
};

/**
 * Rótulo de la severidad de una alerta.
 *
 * Vive aparte de `ESTADO_LABEL` porque `baja` colisiona: para un sujeto
 * significa «de baja» y para una alerta, «severidad baja».
 */
export const SEVERIDAD_LABEL: Record<string, string> = {
  critica: "Crítica", alta: "Alta", media: "Media", baja: "Baja",
  advertencia: "Advertencia", informativa: "Informativa",
};

const ESTADO_CLASE: Record<string, string> = {
  ok: "bg-emerald-100 text-emerald-800", vigente: "bg-emerald-100 text-emerald-800",
  aprobado: "bg-emerald-100 text-emerald-800", approved: "bg-emerald-100 text-emerald-800",
  falta: "bg-slate-200 text-slate-700", pendiente: "bg-slate-200 text-slate-700",
  pending: "bg-amber-100 text-amber-800",
  venc: "bg-red-100 text-red-800", rejected: "bg-red-100 text-red-800",
  critica: "bg-red-100 text-red-800",
  porvenc: "bg-amber-100 text-amber-800", por_vencer: "bg-amber-100 text-amber-800",
  advertencia: "bg-amber-100 text-amber-800", alta: "bg-orange-100 text-orange-800",
  proc: "bg-sky-100 text-sky-800", parcial: "bg-sky-100 text-sky-800",
  informativa: "bg-sky-100 text-sky-800",
  baja: "bg-slate-100 text-slate-500", media: "bg-amber-100 text-amber-800",
  nueva: "bg-sky-100 text-sky-800", en_progreso: "bg-indigo-100 text-indigo-800",
  bloqueante: "bg-red-100 text-red-800", resuelta: "bg-emerald-100 text-emerald-800",
  activa: "bg-emerald-100 text-emerald-800",
  solicitada: "bg-amber-100 text-amber-800",
  sin_acceso: "bg-slate-200 text-slate-700",
  queued: "bg-slate-200 text-slate-700", processing: "bg-sky-100 text-sky-800",
  done: "bg-emerald-100 text-emerald-800", failed: "bg-red-100 text-red-800",
};

export function Chip({ estado, texto }: { estado: string; texto?: string }) {
  return (
    <span className={`inline-block rounded-full px-2.5 py-0.5 text-xs font-semibold ${ESTADO_CLASE[estado] ?? "bg-slate-100 text-slate-600"}`}>
      {texto ?? ESTADO_LABEL[estado] ?? estado}
    </span>
  );
}

export function Kpi({ titulo, valor, sub, color }: { titulo: string; valor: ReactNode; sub?: string; color?: string }) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
      <div className="text-xs font-medium uppercase tracking-wide text-slate-500">{titulo}</div>
      <div className={`mt-1 text-2xl font-bold ${color ?? "text-brand"}`}>{valor}</div>
      {sub && <div className="text-xs text-slate-500">{sub}</div>}
    </div>
  );
}

export function Modal({ abierto, titulo, onCerrar, children, ancho = "max-w-lg" }: {
  abierto: boolean; titulo: string; onCerrar: () => void; children: ReactNode; ancho?: string;
}) {
  if (!abierto) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={onCerrar}>
      <div className={`w-full ${ancho} max-h-[85vh] overflow-y-auto rounded-xl bg-white p-5 shadow-xl`} onClick={(e) => e.stopPropagation()}>
        <div className="mb-4 flex items-center justify-between">
          <h3 className="text-lg font-bold text-brand">{titulo}</h3>
          <button onClick={onCerrar} className="text-2xl leading-none text-slate-400 hover:text-slate-700">×</button>
        </div>
        {children}
      </div>
    </div>
  );
}

export function Spinner({ texto = "Cargando..." }: { texto?: string }) {
  return (
    <div className="flex items-center gap-2 p-6 text-slate-500">
      <span className="h-4 w-4 animate-spin rounded-full border-2 border-slate-300 border-t-brand" />
      {texto}
    </div>
  );
}

export function Donut({ pct, size = 110 }: { pct: number; size?: number }) {
  const r = 42, c = 2 * Math.PI * r;
  const color = pct >= 70 ? "#059669" : pct >= 40 ? "#d97706" : "#dc2626";
  return (
    <svg width={size} height={size} viewBox="0 0 100 100">
      <circle cx="50" cy="50" r={r} fill="none" stroke="#e2e8f0" strokeWidth="10" />
      <circle cx="50" cy="50" r={r} fill="none" stroke={color} strokeWidth="10"
        strokeDasharray={`${(pct / 100) * c} ${c}`} strokeLinecap="round"
        transform="rotate(-90 50 50)" />
      <text x="50" y="55" textAnchor="middle" fontSize="20" fontWeight="700" fill={color}>{pct}%</text>
    </svg>
  );
}

export function Vacio({ mensaje, cta, onCta }: { mensaje: string; cta?: string; onCta?: () => void }) {
  return (
    <div className="rounded-xl border border-dashed border-slate-300 bg-slate-50 p-10 text-center">
      <p className="text-slate-500">{mensaje}</p>
      {cta && <button onClick={onCta} className="btn-primary mt-4">{cta}</button>}
    </div>
  );
}

export function Campo({ etiqueta, children }: { etiqueta: string; children: ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1 block text-sm font-medium text-slate-700">{etiqueta}</span>
      {children}
    </label>
  );
}

/**
 * Paginador de un listado envuelto (`Pagina<T>`).
 *
 * Existe porque desde la v1.1 **todos** los listados paginan: sin controles el
 * usuario solo vería la primera página y creería que no hay más registros.
 */
export function Paginador({ page, totalPaginas, total, etiqueta, onPagina }: {
  page: number; totalPaginas: number; total: number; etiqueta: string;
  onPagina: (p: number) => void;
}) {
  return (
    <div className="flex flex-wrap items-center gap-3 text-xs text-slate-500">
      <span>{total} {etiqueta}</span>
      {totalPaginas > 1 && (
        <>
          <button className="btn-ghost text-xs" disabled={page <= 1}
            onClick={() => onPagina(page - 1)}>← Anterior</button>
          <span>Página {page} de {totalPaginas}</span>
          <button className="btn-ghost text-xs" disabled={page >= totalPaginas}
            onClick={() => onPagina(page + 1)}>Siguiente →</button>
        </>
      )}
    </div>
  );
}

/** Barra de progreso de la subida por SAS (el PUT va directo al storage). */
export function Barra({ pct, texto }: { pct: number; texto?: string }) {
  return (
    <div className="w-full">
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-slate-200">
        <div className="h-full rounded-full bg-accent transition-all"
          style={{ width: `${Math.min(100, Math.max(0, pct))}%` }} />
      </div>
      {texto && <div className="mt-1 text-xs text-slate-500">{texto}</div>}
    </div>
  );
}
