"""Dashboard: KPI, cumplimiento por contrato, feed y tendencia histórica (§14).

Todo lo de aquí es de solo lectura y se calcula en vivo, EXCEPTO la tendencia:
`GET /dashboard/tendencia` lee `cumplimiento_snapshots`, la serie diaria que
escribe el job nocturno. No se recalcula el pasado a partir del estado actual
porque sería falso: un documento que hoy está vencido estaba vigente el mes
pasado, y reconstruir la historia desde el presente daría una curva plana.

Un contract_admin ve el dashboard de SU contrato: todos los endpoints aplican
`contrato_scope` y los agregados quedan acotados a ese contrato.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import (Page, contrato_scope, err, get_company_id, get_current_user,
                    paginacion, require_contrato, sobre)
from ..models import (Actividad, Alerta, Contrato, CumplimientoSnapshot,
                      Documento, Sujeto, User)

logger = logging.getLogger("acredittia.dashboard")

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

PERIODOS: tuple[str, ...] = ("semana", "mes", "trimestre")

# Ventana por defecto de la tendencia, en días, según la granularidad pedida.
# Se eligen múltiplos del periodo para que el primer y el último punto sean
# comparables (12 semanas, 12 meses, 8 trimestres).
VENTANA_DEFECTO = {"semana": 84, "mes": 365, "trimestre": 730}


# ============================================================================
# Helpers
# ============================================================================
def _scope_sujeto(q, scope: uuid.UUID | None):
    return q.where(Sujeto.contrato_id == scope) if scope else q


def _scope_documento(q, scope: uuid.UUID | None):
    """Acota documentos al contrato: cuelgan del contrato o de un sujeto suyo."""
    if not scope:
        return q
    return q.where(or_(
        Documento.contrato_id == scope,
        Documento.sujeto_id.in_(select(Sujeto.id).where(
            Sujeto.contrato_id == scope))))


@router.get("/kpis")
def kpis(db: Session = Depends(get_db),
         cid: uuid.UUID = Depends(get_company_id),
         user: User = Depends(get_current_user)):
    """Tarjetas de cabecera: contratos, dotación, cumplimiento y alertas."""
    scope = contrato_scope(user)

    def n(q):
        return db.scalar(q) or 0

    q_contratos = select(func.count()).select_from(Contrato).where(
        Contrato.company_id == cid, Contrato.estado == "vigente")
    q_faenas = select(func.count(func.distinct(Contrato.faena_id))).where(
        Contrato.company_id == cid)
    if scope:
        q_contratos = q_contratos.where(Contrato.id == scope)
        q_faenas = q_faenas.where(Contrato.id == scope)

    base_s = select(func.count()).select_from(Sujeto).where(
        Sujeto.company_id == cid, Sujeto.estado != "baja")
    base_s = _scope_sujeto(base_s, scope)
    p_tot = n(base_s.where(Sujeto.tipo == "trabajador"))
    p_ok = n(base_s.where(Sujeto.tipo == "trabajador", Sujeto.estado == "ok"))
    e_tot = n(base_s.where(Sujeto.tipo == "equipo"))
    e_ok = n(base_s.where(Sujeto.tipo == "equipo", Sujeto.estado == "ok"))

    oblig = select(func.count()).select_from(Documento).where(
        Documento.company_id == cid, Documento.obligatorio.is_(True))
    oblig = _scope_documento(oblig, scope)
    d_tot = n(oblig)
    d_ok = n(oblig.where(Documento.estado_calc == "ok"))

    base_a = select(func.count()).select_from(Alerta).where(
        Alerta.company_id == cid, Alerta.resuelta_at.is_(None))
    if scope:
        base_a = base_a.where(Alerta.contrato_id == scope)
    return {
        "contratos_activos": n(q_contratos),
        "faenas_activas": n(q_faenas),
        "personal": {"acreditados": p_ok, "total": p_tot},
        "equipos": {"acreditados": e_ok, "total": e_tot},
        "documentos": {"ok": d_ok, "total": d_tot},
        "cumplimiento_general_pct": round(100 * d_ok / d_tot) if d_tot else 0,
        "alertas": {
            "criticas": n(base_a.where(Alerta.severidad == "critica")),
            "advertencias": n(base_a.where(
                Alerta.severidad.in_(("advertencia", "alta")))),
        },
    }


@router.get("/cumplimiento-contratos")
def cumplimiento(p: Page = Depends(paginacion), db: Session = Depends(get_db),
                 cid: uuid.UUID = Depends(get_company_id),
                 user: User = Depends(get_current_user)):
    """Cumplimiento contrato a contrato, con el mismo cálculo que su ficha."""
    from .contratos import _stats

    scope = contrato_scope(user)
    q = select(Contrato).where(Contrato.company_id == cid)
    if scope:
        q = q.where(Contrato.id == scope)
    if p.search:
        q = q.where(Contrato.nombre.ilike(f"%{p.search}%"))
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    filas = list(db.scalars(q.order_by(Contrato.nombre)
                            .offset(p.offset).limit(p.page_size)))
    items = [{
        "id": str(c.id), "nombre": c.nombre, "codigo": c.codigo,
        "faena": c.faena.nombre, "faena_id": str(c.faena_id),
        "estado": c.estado, **_stats(db, c),
    } for c in filas]
    return sobre(items, total, p)


@router.get("/acreditaciones-estado")
def acreditaciones(db: Session = Depends(get_db),
                   cid: uuid.UUID = Depends(get_company_id),
                   user: User = Depends(get_current_user)):
    """Reparto de sujetos por estado de acreditación (dona del dashboard)."""
    q = select(Sujeto.estado, func.count()).where(
        Sujeto.company_id == cid, Sujeto.estado != "baja")
    q = _scope_sujeto(q, contrato_scope(user)).group_by(Sujeto.estado)
    conteo = {estado: int(c) for estado, c in db.execute(q).all()}
    return {
        "acreditados": conteo.get("ok", 0),
        "pendientes": conteo.get("proc", 0) + conteo.get("falta", 0),
        "vencidos": conteo.get("venc", 0),
        "total": sum(conteo.values()),
    }


@router.get("/actividad")
def feed(p: Page = Depends(paginacion), db: Session = Depends(get_db),
         cid: uuid.UUID = Depends(get_company_id),
         user: User = Depends(get_current_user)):
    """Últimas acciones de la empresa. El historial completo está en /actividad."""
    from .actividad import _filtro_contrato

    q = select(Actividad).where(Actividad.company_id == cid)
    scope = contrato_scope(user)
    if scope:
        q = _filtro_contrato(q, cid, scope)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    filas = list(db.scalars(q.order_by(Actividad.id.desc())
                            .offset(p.offset).limit(p.page_size)))
    items = [{
        "id": a.id, "tipo": a.tipo, "modulo": a.modulo,
        "descripcion": a.descripcion, "entidad_tipo": a.entidad_tipo,
        "entidad_id": str(a.entidad_id) if a.entidad_id else None,
        "created_at": a.created_at.isoformat(),
    } for a in filas]
    return sobre(items, total, p)


@router.get("/proximos-vencimientos")
def vencimientos(dias: int = Query(30, ge=1, le=365),
                 p: Page = Depends(paginacion), db: Session = Depends(get_db),
                 cid: uuid.UUID = Depends(get_company_id),
                 user: User = Depends(get_current_user)):
    """Documentos vigentes que vencen dentro de la ventana pedida."""
    hasta = date.today() + timedelta(days=dias)
    q = select(Documento).where(
        Documento.company_id == cid,
        Documento.vence.is_not(None),
        Documento.vence <= hasta,
        Documento.estado == "ok")
    q = _scope_documento(q, contrato_scope(user))
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    filas = list(db.scalars(q.order_by(Documento.vence)
                            .offset(p.offset).limit(p.page_size)))
    items = [{
        "documento_id": str(d.id), "titulo": d.titulo,
        "vence": d.vence.isoformat(),
        "dias": (d.vence - date.today()).days,
        "sujeto_id": str(d.sujeto_id) if d.sujeto_id else None,
        "sujeto": d.sujeto.nombre if d.sujeto else None,
        "estado_calc": d.estado_calc,
    } for d in filas]
    return sobre(items, total, p)


# ============================================================================
# §14.2 — Tendencia histórica de cumplimiento
# ============================================================================
def _bucket(f: date, periodo: str) -> date:
    """Inicio del periodo al que pertenece la fecha (lunes, día 1, trimestre)."""
    if periodo == "semana":
        return f - timedelta(days=f.weekday())
    if periodo == "mes":
        return f.replace(day=1)
    return f.replace(month=3 * ((f.month - 1) // 3) + 1, day=1)


def _serie_snapshots(db: Session, cid: uuid.UUID, *,
                     contrato_id: uuid.UUID | None,
                     contratos_faena: list[uuid.UUID] | None,
                     desde: date, hasta: date) -> list[dict]:
    """Puntos diarios de `cumplimiento_snapshots` según el alcance pedido.

    Tres alcances distintos sobre la misma tabla:

    * empresa → la fila agregada, `contrato_id IS NULL`, que ya escribió el job.
    * un contrato → sus propias filas.
    * una faena → se AGREGAN las filas de los contratos de esa faena, porque el
      job no guarda una fila por faena. El porcentaje se recalcula desde
      `docs_ok / docs_total` y no como media de porcentajes: un contrato con tres
      documentos no debe pesar igual que uno con trescientos.
    """
    q = (select(CumplimientoSnapshot.fecha,
                func.sum(CumplimientoSnapshot.docs_ok).label("ok"),
                func.sum(CumplimientoSnapshot.docs_total).label("total"),
                func.avg(CumplimientoSnapshot.cumplimiento_pct).label("pct"),
                func.sum(CumplimientoSnapshot.personal_acreditados).label("p_ok"),
                func.sum(CumplimientoSnapshot.personal_total).label("p_tot"),
                func.sum(CumplimientoSnapshot.equipos_acreditados).label("e_ok"),
                func.sum(CumplimientoSnapshot.equipos_total).label("e_tot"),
                func.sum(CumplimientoSnapshot.alertas_criticas).label("alertas"))
         .where(CumplimientoSnapshot.company_id == cid,
                CumplimientoSnapshot.fecha >= desde,
                CumplimientoSnapshot.fecha <= hasta)
         .group_by(CumplimientoSnapshot.fecha)
         .order_by(CumplimientoSnapshot.fecha))
    if contrato_id:
        q = q.where(CumplimientoSnapshot.contrato_id == contrato_id)
    elif contratos_faena is not None:
        if not contratos_faena:
            return []
        q = q.where(CumplimientoSnapshot.contrato_id.in_(contratos_faena))
    else:
        q = q.where(CumplimientoSnapshot.contrato_id.is_(None))

    puntos = []
    for f in db.execute(q).all():
        total, ok = int(f.total or 0), int(f.ok or 0)
        pct = (round(100 * ok / total) if total
               else int(round(float(f.pct or 0))))
        puntos.append({
            "fecha": f.fecha, "cumplimiento_pct": pct,
            "docs_ok": ok, "docs_total": total,
            "personal_acreditados": int(f.p_ok or 0),
            "personal_total": int(f.p_tot or 0),
            "equipos_acreditados": int(f.e_ok or 0),
            "equipos_total": int(f.e_tot or 0),
            "alertas_criticas": int(f.alertas or 0),
        })
    return puntos


@router.get("/tendencia")
def tendencia(periodo: str = Query("mes"),
              desde: date | None = Query(None),
              hasta: date | None = Query(None),
              contrato_id: uuid.UUID | None = Query(None),
              faena_id: uuid.UUID | None = Query(None),
              db: Session = Depends(get_db),
              cid: uuid.UUID = Depends(get_company_id),
              user: User = Depends(get_current_user)):
    """Evolución del cumplimiento a partir de `cumplimiento_snapshots`.

    `periodo` define la granularidad de la serie: de todos los snapshots del
    periodo se toma el ÚLTIMO, porque el snapshot es una medición puntual y no un
    acumulado —promediar los días de un mes suavizaría justo el pico que
    interesa ver.

    **Si no hay al menos dos periodos con datos se omiten `anterior` y
    `delta_pct`**, en lugar de compararse contra cero o contra sí mismo: una
    empresa que empezó ayer no ha «mejorado un 100 %», simplemente no tiene
    histórico. La respuesta lo dice en `nota`.
    """
    if periodo not in PERIODOS:
        raise err(400, "PERIODO_INVALIDO",
                  f"Periodo debe ser uno de: {', '.join(PERIODOS)}")
    if desde and hasta and hasta < desde:
        raise err(400, "RANGO_INVALIDO", "'hasta' no puede ser anterior a 'desde'")
    if contrato_id and faena_id:
        raise err(400, "FILTRO_AMBIGUO",
                  "Indique 'contrato_id' o 'faena_id', no ambos")

    scope = contrato_scope(user)
    if contrato_id:
        require_contrato(contrato_id, user)
    # Un contract_admin no puede ver la serie agregada de la empresa: se le
    # fuerza la de su contrato.
    efectivo = contrato_id or scope

    contratos_faena: list[uuid.UUID] | None = None
    if faena_id and not efectivo:
        contratos_faena = list(db.scalars(select(Contrato.id).where(
            Contrato.company_id == cid, Contrato.faena_id == faena_id)))

    hasta = hasta or date.today()
    desde = desde or (hasta - timedelta(days=VENTANA_DEFECTO[periodo]))

    puntos = _serie_snapshots(db, cid, contrato_id=efectivo,
                              contratos_faena=contratos_faena,
                              desde=desde, hasta=hasta)

    # Un punto por periodo: el último de cada bucket. Los puntos vienen en orden
    # ascendente, así que la última asignación gana.
    por_bucket: dict[date, dict] = {}
    for punto in puntos:
        por_bucket[_bucket(punto["fecha"], periodo)] = punto
    serie = [{
        "periodo_inicio": b.isoformat(),
        "fecha": p["fecha"].isoformat(),
        "cumplimiento_pct": p["cumplimiento_pct"],
        "docs_ok": p["docs_ok"], "docs_total": p["docs_total"],
        "personal_acreditados": p["personal_acreditados"],
        "personal_total": p["personal_total"],
        "equipos_acreditados": p["equipos_acreditados"],
        "equipos_total": p["equipos_total"],
        "alertas_criticas": p["alertas_criticas"],
    } for b, p in sorted(por_bucket.items())]

    salida: dict = {
        "periodo": periodo,
        "desde": desde.isoformat(), "hasta": hasta.isoformat(),
        "contrato_id": str(efectivo) if efectivo else None,
        "faena_id": str(faena_id) if faena_id else None,
        "serie": serie,
        "snapshots_leidos": len(puntos),
    }
    if not serie:
        salida["nota"] = ("No hay snapshots de cumplimiento en el rango pedido. "
                          "La serie la escribe el job nocturno; una empresa "
                          "recién creada no tiene histórico todavía.")
        return salida

    ultimo = serie[-1]
    salida["actual"] = {"fecha": ultimo["fecha"],
                        "periodo_inicio": ultimo["periodo_inicio"],
                        "cumplimiento_pct": ultimo["cumplimiento_pct"],
                        "docs_ok": ultimo["docs_ok"],
                        "docs_total": ultimo["docs_total"]}
    if len(serie) >= 2:
        previo = serie[-2]
        salida["anterior"] = {"fecha": previo["fecha"],
                              "periodo_inicio": previo["periodo_inicio"],
                              "cumplimiento_pct": previo["cumplimiento_pct"],
                              "docs_ok": previo["docs_ok"],
                              "docs_total": previo["docs_total"]}
        salida["delta_pct"] = (ultimo["cumplimiento_pct"]
                               - previo["cumplimiento_pct"])
    else:
        # Se omiten 'anterior' y 'delta_pct' a propósito: con un solo periodo
        # cualquier delta sería inventado.
        salida["nota"] = (f"Solo hay datos de un {periodo}: no se puede calcular "
                          "la variación, por eso no se devuelven 'anterior' ni "
                          "'delta_pct'.")
    return salida
