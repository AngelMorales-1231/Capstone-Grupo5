"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import * as Api from "@/lib/cliente";
import type {
  AcreditacionesEstado, ActividadFila, CumplimientoContrato, Kpis,
  ProximoVencimiento, Tendencia,
} from "@/lib/tipos";
import { Chip, Donut, Kpi, Spinner } from "@/components/ui";

export default function Dashboard() {
  const [kpis, setKpis] = useState<Kpis | null>(null);
  const [cumpl, setCumpl] = useState<CumplimientoContrato[]>([]);
  const [acred, setAcred] = useState<AcreditacionesEstado | null>(null);
  const [feed, setFeed] = useState<ActividadFila[]>([]);
  const [venc, setVenc] = useState<ProximoVencimiento[]>([]);
  const [tend, setTend] = useState<Tendencia | null>(null);

  useEffect(() => {
    Api.dashboard.kpis().then(setKpis).catch(() => {});
    // Los listados ahora paginan: sin page_size solo llegarían 25 filas.
    Api.lista(Api.dashboard.cumplimientoContratos({ page_size: 100 }))
      .then(setCumpl).catch(() => {});
    Api.dashboard.acreditacionesEstado().then(setAcred).catch(() => {});
    // `limit` desapareció: la actividad se pide con page / page_size.
    Api.lista(Api.dashboard.actividad({ page_size: 8 })).then(setFeed).catch(() => {});
    Api.lista(Api.dashboard.proximosVencimientos({ page_size: 10 }))
      .then(setVenc).catch(() => {});
    Api.dashboard.tendencia({ periodo: "mes" }).then(setTend).catch(() => {});
  }, []);

  if (!kpis) return <Spinner />;

  // §8: `delta_pct` puede NO venir (hace falta un segundo periodo con
  // snapshots). Se comprueba la presencia de la clave, no se asume 0.
  const delta = tend?.delta_pct;
  const subDonut = delta === undefined
    ? tend?.nota ?? "Sin histórico comparable"
    : `${delta >= 0 ? "▲" : "▼"} ${Math.abs(delta)} pts vs. mes anterior`;

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold text-brand">Inicio</h1>
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-5">
        <Kpi titulo="Contratos activos" valor={kpis.contratos_activos} />
        <Kpi titulo="Faenas activas" valor={kpis.faenas_activas} />
        <Kpi titulo="Personal acreditado" valor={`${kpis.personal.acreditados}/${kpis.personal.total}`} />
        <Kpi titulo="Equipos acreditados" valor={`${kpis.equipos.acreditados}/${kpis.equipos.total}`} />
        <div className="flex flex-col items-center justify-center rounded-xl border border-slate-200 bg-white p-2 shadow-sm">
          <Donut pct={kpis.cumplimiento_general_pct} size={96} />
          <div className="text-center text-[11px] leading-tight text-slate-500">{subDonut}</div>
        </div>
      </div>
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-5">
        <Kpi titulo="Documentos al día" valor={`${kpis.documentos.ok}/${kpis.documentos.total}`} />
        <Kpi titulo="Alertas críticas" valor={kpis.alertas.criticas}
          color={kpis.alertas.criticas > 0 ? "text-red-600" : "text-emerald-600"} />
        <Kpi titulo="Advertencias" valor={kpis.alertas.advertencias} color="text-amber-600" />
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        <section className="rounded-xl border border-slate-200 bg-white shadow-sm lg:col-span-2">
          <h2 className="border-b border-slate-200 px-4 py-3 text-sm font-bold uppercase text-slate-600">Cumplimiento por contrato</h2>
          <table className="tabla">
            <thead><tr><th>Contrato</th><th>Faena</th><th>%</th><th>Personal</th><th>Equipos</th><th>Alertas</th></tr></thead>
            <tbody>
              {cumpl.map((c) => (
                <tr key={c.id}>
                  <td><Link className="font-medium text-accent hover:underline" href={`/contratos/${c.id}`}>{c.nombre}</Link></td>
                  <td>{c.faena}</td>
                  <td className="font-bold">{c.cumplimiento_pct}%</td>
                  <td>{c.personal.acreditados}/{c.personal.total}</td>
                  <td>{c.equipos.acreditados}/{c.equipos.total}</td>
                  <td>{c.alertas_activas > 0 ? <span className="font-bold text-red-600">{c.alertas_activas}</span> : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {cumpl.length === 0 && <p className="p-4 text-sm text-slate-500">Aún no tienes contratos. <Link href="/contratos" className="text-accent underline">Crea el primero</Link>.</p>}
        </section>

        <div className="space-y-6">
          {acred && (
            <section className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
              <h2 className="mb-2 text-sm font-bold uppercase text-slate-600">Acreditaciones</h2>
              <div className="space-y-1 text-sm">
                <div className="flex justify-between"><span>🟢 Acreditados</span><b>{acred.acreditados}</b></div>
                <div className="flex justify-between"><span>🟡 Pendientes</span><b>{acred.pendientes}</b></div>
                <div className="flex justify-between"><span>🔴 Vencidos</span><b>{acred.vencidos}</b></div>
                <div className="flex justify-between border-t border-slate-200 pt-1 text-slate-500"><span>Total</span><b>{acred.total}</b></div>
              </div>
            </section>
          )}
          <section className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
            <h2 className="mb-2 text-sm font-bold uppercase text-slate-600">Próximos vencimientos</h2>
            {venc.length === 0 && <p className="text-sm text-slate-400">Sin vencimientos en 30 días.</p>}
            <ul className="space-y-2 text-sm">
              {venc.slice(0, 6).map((v) => (
                <li key={v.documento_id} className="flex items-center justify-between gap-2">
                  <span className="truncate">{v.titulo}{v.sujeto ? ` — ${v.sujeto}` : ""}</span>
                  <Chip estado={v.estado_calc} texto={`${v.dias}d`} />
                </li>
              ))}
            </ul>
          </section>
        </div>
      </div>

      <section className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
        <h2 className="mb-2 text-sm font-bold uppercase text-slate-600">Actividad reciente</h2>
        <ul className="space-y-1.5 text-sm text-slate-600">
          {feed.map((a) => (
            <li key={a.id}>
              <span className="text-slate-400">{new Date(a.created_at).toLocaleString("es-CL")} · </span>
              {a.descripcion}
              {a.usuario?.nombre && <span className="text-slate-400"> · {a.usuario.nombre}</span>}
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
