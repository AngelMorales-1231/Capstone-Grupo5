"""Identidad multi-registro: personas por RUT y flota por patente (§9.1).

Un `Sujeto` no es una persona: es el registro de una persona EN UN CONTRATO. La
misma persona (mismo RUT) puede estar acreditada a la vez en varios contratos, y
sus documentos son por par (sujeto, contrato) porque cada mandante exige su
propia copia vigente.

Estos endpoints son de solo lectura y agrupan por identidad para responder la
pregunta operativa "¿puede este trabajador entrar a faena?", que no se contesta
mirando un único registro. El cumplimiento agregado se calcula sobre la UNIÓN de
documentos por título —un requisito satisfecho en un contrato cuenta como
cubierto para la persona— y no promediando porcentajes, que ocultaría a quien
tiene todo listo en un contrato y nada en otro recién abierto.

La paginación agrupa en SQL (`GROUP BY rut|patente`) para contar y recortar la
página; solo después se cargan los registros y documentos de esa página.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from ..deps import (Page, contrato_scope, err, get_company_id, get_current_user,
                    get_db, paginacion, sobre)
from ..models import Contrato, Documento, LicenciaInterna, Sujeto, User

logger = logging.getLogger("acredittia.personas")

router = APIRouter(prefix="/personas", tags=["personas"])
router_flota = APIRouter(prefix="/flota", tags=["flota"])


# ------------------------------------------------------------------ consultas
def _consulta_grupos(cid: uuid.UUID, tipo: str, clave,
                     *, scope: uuid.UUID | None = None,
                     cargo_id: uuid.UUID | None = None,
                     estado: str | None = None,
                     faena_id: uuid.UUID | None = None,
                     tipo_equipo: str | None = None,
                     sin_asignacion: bool = False,
                     search: str | None = None):
    """Agrupación por identidad. Los filtros se evalúan sobre los registros: la
    identidad entra en el listado si CUALQUIERA de sus registros los cumple."""
    q = (select(clave.label("clave"),
                func.count().label("registros"),
                func.min(Sujeto.nombre).label("nombre"),
                func.max(Sujeto.created_at).label("ultimo"))
         .select_from(Sujeto)
         .join(Contrato, Contrato.id == Sujeto.contrato_id)
         .where(Sujeto.company_id == cid, Sujeto.tipo == tipo,
                clave.is_not(None), clave != "")
         .group_by(clave))
    if scope:
        q = q.where(Sujeto.contrato_id == scope)
    if cargo_id:
        q = q.where(Sujeto.cargo_id == cargo_id)
    if estado:
        q = q.where(Sujeto.estado == estado)
    if faena_id:
        q = q.where(Contrato.faena_id == faena_id)
    if tipo_equipo:
        q = q.where(Sujeto.tipo_equipo == tipo_equipo)
    if sin_asignacion:
        # Sin faena vigente: el contrato terminó o el registro está de baja.
        q = q.where(or_(Contrato.estado == "terminado", Sujeto.estado == "baja"))
    if search:
        like = f"%{search}%"
        q = q.where(or_(Sujeto.nombre.ilike(like), clave.ilike(like)))
    return q


def _ordenar_grupos(q, clave, sort: str | None):
    """Orden sobre las columnas agregadas; por defecto el registro más reciente."""
    expr_por_campo = {
        "nombre": func.min(Sujeto.nombre),
        "clave": clave,
        "registros": func.count(),
        "created_at": func.max(Sujeto.created_at),
    }
    campo = (sort or "-created_at")
    desc = campo.startswith("-")
    campo = campo.lstrip("-")
    expr = expr_por_campo.get(campo, expr_por_campo["created_at"])
    return q.order_by(expr.desc() if desc else expr.asc())


def _registros(db: Session, cid: uuid.UUID, tipo: str, clave,
               valores: list[str], scope: uuid.UUID | None) -> list[Sujeto]:
    """Todos los registros de esas identidades, aunque no casen con los filtros.

    La ficha de una persona pierde sentido si se ocultan sus otros contratos: el
    filtro sirve para encontrarla, no para recortar su historial. El único
    recorte que se mantiene es el del contract_admin, que es de seguridad.
    """
    if not valores:
        return []
    q = (select(Sujeto)
         .options(joinedload(Sujeto.contrato).joinedload(Contrato.faena))
         .where(Sujeto.company_id == cid, Sujeto.tipo == tipo,
                clave.in_(valores)))
    if scope:
        q = q.where(Sujeto.contrato_id == scope)
    return list(db.scalars(q.order_by(Sujeto.created_at.desc())).unique())


def _docs_por_sujeto(db: Session, cid: uuid.UUID,
                     sujeto_ids: list[uuid.UUID]) -> dict[uuid.UUID, list]:
    if not sujeto_ids:
        return {}
    filas = db.scalars(select(Documento).where(
        Documento.company_id == cid,
        Documento.sujeto_id.in_(sujeto_ids)).order_by(Documento.titulo)).all()
    out: dict[uuid.UUID, list] = {}
    for d in filas:
        out.setdefault(d.sujeto_id, []).append(d)
    return out


# -------------------------------------------------------------- agregaciones
def _union_documentos(docs: list[Documento]) -> dict:
    """Cumplimiento sobre la UNIÓN de documentos obligatorios, por título.

    Un mismo requisito existe repetido en cada contrato; para la persona basta
    tenerlo vigente en uno para considerarlo cubierto.
    """
    por_titulo: dict[str, bool] = {}
    for d in docs:
        if d.es_emsipor or not d.obligatorio:
            continue
        k = d.titulo.strip().lower()
        por_titulo[k] = por_titulo.get(k, False) or d.estado_calc == "ok"
    total = len(por_titulo)
    ok = sum(1 for v in por_titulo.values() if v)
    return {"docs_total": total, "docs_ok": ok,
            "cumplimiento_pct": round(100 * ok / total) if total else 0}


def _stats_registro(docs: list[Documento]) -> dict:
    oblig = [d for d in docs if d.obligatorio and not d.es_emsipor]
    ok = sum(1 for d in oblig if d.estado_calc == "ok")
    return {"ok": ok, "total": len(oblig),
            "cumplimiento_pct": round(100 * ok / len(oblig)) if oblig else 0}


def _proximo_vencimiento(docs: list[Documento]) -> dict | None:
    hoy = date.today()
    cand = [d for d in docs
            if d.vence and d.estado == "ok" and d.vence >= hoy]
    if not cand:
        return None
    d = min(cand, key=lambda x: x.vence)
    return {"titulo": d.titulo, "vence": d.vence.isoformat(),
            "dias": (d.vence - hoy).days}


def _certificaciones(docs: list[Documento], n: int = 2) -> list[dict]:
    """Los documentos obligatorios vigentes más recientes, sin repetir título."""
    vigentes = [d for d in docs
                if d.obligatorio and d.estado_calc == "ok"]
    vigentes.sort(key=lambda d: d.updated_at, reverse=True)
    salida, vistos = [], set()
    for d in vigentes:
        k = d.titulo.strip().lower()
        if k in vistos:
            continue
        vistos.add(k)
        salida.append({"titulo": d.titulo,
                       "vence": d.vence.isoformat() if d.vence else None})
        if len(salida) == n:
            break
    return salida


def _activos(regs: list[Sujeto]) -> list[Sujeto]:
    return [s for s in regs if s.estado != "baja"]


def _identidad_out(tipo: str, valor: str, regs: list[Sujeto],
                   docs_por_sujeto: dict[uuid.UUID, list]) -> dict:
    """Fila de identidad: datos del registro más reciente y agregados de todos."""
    regs = sorted(regs, key=lambda s: s.created_at, reverse=True)
    reciente = regs[0]
    activos = _activos(regs)
    docs_activos = [d for s in activos for d in docs_por_sujeto.get(s.id, [])]
    agregado = _union_documentos(docs_activos)

    contratos, faenas = [], []
    for s in regs:
        st = _stats_registro(docs_por_sujeto.get(s.id, []))
        c = s.contrato
        contratos.append({
            "id": str(c.id), "nombre": c.nombre, "faena": c.faena.nombre,
            "estado": s.estado,                 # acreditación en ese contrato
            "contrato_estado": c.estado,
            "ok": st["ok"], "total": st["total"],
        })
        if c.faena.nombre not in faenas:
            faenas.append(c.faena.nombre)

    d = {
        "nombre": reciente.nombre, "registros": len(regs),
        "activos": len(activos), "faenas": faenas, "contratos": contratos,
        "cumplimiento_pct": agregado["cumplimiento_pct"],
        "docs_ok": agregado["docs_ok"], "docs_total": agregado["docs_total"],
        "proximo_vencimiento": _proximo_vencimiento(docs_activos),
        "certificaciones_principales": _certificaciones(docs_activos),
    }
    if tipo == "trabajador":
        d = {"rut": valor, **d, "cargo": reciente.cargo,
             "cargo_id": str(reciente.cargo_id) if reciente.cargo_id else None,
             "es_conductor": any(s.es_conductor for s in regs)}
    else:
        d = {"patente": valor, **d, "tipo_equipo": reciente.tipo_equipo,
             "marca": reciente.marca, "modelo": reciente.modelo,
             "anio": reciente.anio}
    return d


def _listar(db: Session, cid: uuid.UUID, tipo: str, clave, p: Page,
            scope: uuid.UUID | None, **filtros) -> dict:
    q = _consulta_grupos(cid, tipo, clave, scope=scope, search=p.search, **filtros)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    filas = db.execute(_ordenar_grupos(q, clave, p.sort)
                       .offset(p.offset).limit(p.page_size)).all()
    valores = [f.clave for f in filas]
    regs = _registros(db, cid, tipo, clave, valores, scope)
    docs = _docs_por_sujeto(db, cid, [s.id for s in regs])
    por_clave: dict[str, list[Sujeto]] = {}
    for s in regs:
        por_clave.setdefault(s.rut if tipo == "trabajador" else s.patente,
                             []).append(s)
    items = [_identidad_out(tipo, v, por_clave[v], docs)
             for v in valores if por_clave.get(v)]
    return sobre(items, total, p)


def _ficha(db: Session, cid: uuid.UUID, tipo: str, clave, valor: str,
           scope: uuid.UUID | None) -> dict:
    regs = _registros(db, cid, tipo, clave, [valor], scope)
    if not regs:
        raise err(404, "NO_ENCONTRADO", "No existe esa identidad en la empresa")
    docs = _docs_por_sujeto(db, cid, [s.id for s in regs])
    ficha = _identidad_out(tipo, valor, regs, docs)

    ficha["registros_detalle"] = [{
        "sujeto_id": str(s.id), "estado": s.estado,
        "contrato": {"id": str(s.contrato.id), "nombre": s.contrato.nombre,
                     "estado": s.contrato.estado,
                     "faena": s.contrato.faena.nombre},
        "cargo": s.cargo,
        **_stats_registro(docs.get(s.id, [])),
        "creado_at": s.created_at.isoformat(),
    } for s in sorted(regs, key=lambda s: s.created_at, reverse=True)]

    # La licencia interna es del par (sujeto, contrato); para la ficha basta la
    # más exigente de las que tenga: si está vigente en algún contrato, la
    # persona la tiene emitida.
    lims = list(db.scalars(select(LicenciaInterna).where(
        LicenciaInterna.company_id == cid,
        LicenciaInterna.sujeto_id.in_([s.id for s in regs]))))
    orden = {"vigente": 0, "por_vencer": 1, "pendiente": 2}
    lim = min(lims, key=lambda l: orden.get(l.estado, 9)) if lims else None
    ficha["licencia_interna"] = {
        "sujeto_id": str(lim.sujeto_id), "numero": lim.numero,
        "estado": lim.estado,
        "vence": lim.vence.isoformat() if lim.vence else None,
        "emsipor_estado": lim.emsipor_estado,
    } if lim else None
    return ficha


# ------------------------------------------------------------------ personas
@router.get("")
def listar_personas(cargo_id: uuid.UUID | None = Query(None),
                    estado: str | None = Query(None),
                    faena_id: uuid.UUID | None = Query(None),
                    sin_asignacion: bool = Query(False),
                    p: Page = Depends(paginacion),
                    db: Session = Depends(get_db),
                    cid: uuid.UUID = Depends(get_company_id),
                    user: User = Depends(get_current_user)):
    """Personas de la empresa agrupadas por RUT, con su cumplimiento agregado."""
    return _listar(db, cid, "trabajador", Sujeto.rut, p, contrato_scope(user),
                   cargo_id=cargo_id, estado=estado, faena_id=faena_id,
                   sin_asignacion=sin_asignacion)


@router.get("/{rut}")
def ficha_persona(rut: str, db: Session = Depends(get_db),
                  cid: uuid.UUID = Depends(get_company_id),
                  user: User = Depends(get_current_user)):
    """Ficha consolidada de la persona: todos sus registros y su licencia."""
    return _ficha(db, cid, "trabajador", Sujeto.rut, rut.strip(),
                  contrato_scope(user))


# --------------------------------------------------------------------- flota
@router_flota.get("")
def listar_flota(tipo_equipo: str | None = Query(None),
                 estado: str | None = Query(None),
                 faena_id: uuid.UUID | None = Query(None),
                 p: Page = Depends(paginacion),
                 db: Session = Depends(get_db),
                 cid: uuid.UUID = Depends(get_company_id),
                 user: User = Depends(get_current_user)):
    """Equipos de la empresa agrupados por patente."""
    return _listar(db, cid, "equipo", Sujeto.patente, p, contrato_scope(user),
                   tipo_equipo=tipo_equipo, estado=estado, faena_id=faena_id)


@router_flota.get("/{patente}")
def ficha_equipo(patente: str, db: Session = Depends(get_db),
                 cid: uuid.UUID = Depends(get_company_id),
                 user: User = Depends(get_current_user)):
    """Ficha consolidada del equipo con todos los contratos en que opera."""
    return _ficha(db, cid, "equipo", Sujeto.patente,
                  patente.strip().upper(), contrato_scope(user))
