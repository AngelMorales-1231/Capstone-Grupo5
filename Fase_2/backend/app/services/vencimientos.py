"""Recalculo de estados de vencimiento, snapshots y expiración de credenciales.

Es el contenido del cron diario de las 00:30 America/Santiago (§10.1 de la
especificación). Se ejecuta con `worker_session(is_admin=True)` porque barre
todas las empresas: RLS se relaja a propósito solo en este proceso.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..config import UMBRAL_ACREDITADO_PCT
from ..models import (Alerta, Contrato, CumplimientoSnapshot, Documento,
                      PlataformaCredencial, Sujeto)
from .checklist import calc_estado_doc, calc_estado_sujeto

log = logging.getLogger("acredittia.vencimientos")


def _alerta_existente(db: Session, documento_id: uuid.UUID, severidad: str) -> bool:
    return db.scalar(select(Alerta.id).where(
        Alerta.documento_id == documento_id,
        Alerta.severidad == severidad,
        Alerta.origen == "vencimiento",
        Alerta.resuelta_at.is_(None),
    )) is not None


def _emitir_alerta_vencimiento(db: Session, doc: Documento) -> None:
    quien = ""
    if doc.sujeto_id and doc.sujeto:
        quien = f" — {doc.sujeto.nombre}"
    if doc.estado_calc == "venc":
        sev, estado = "critica", "bloqueante"
        titulo = f"{doc.titulo} vencido"
        desc = f"{doc.titulo}{quien} venció el {doc.vence:%d/%m/%Y}."
    else:
        sev, estado = "advertencia", "nueva"
        dias = (doc.vence - date.today()).days
        titulo = f"{doc.titulo} por vencer"
        desc = f"{doc.titulo}{quien} vence en {dias} días ({doc.vence:%d/%m/%Y})."
    if not _alerta_existente(db, doc.id, sev):
        db.add(Alerta(
            company_id=doc.company_id, severidad=sev, estado=estado,
            origen="vencimiento", titulo=titulo, descripcion=desc,
            documento_id=doc.id, sujeto_id=doc.sujeto_id, contrato_id=doc.contrato_id,
        ))


def recalcular_documentos(db: Session, company_id: uuid.UUID | None = None) -> int:
    """Recalcula `estado_calc` y emite alertas. Devuelve documentos cambiados."""
    q = select(Documento)
    if company_id:
        q = q.where(Documento.company_id == company_id)
    cambios = 0
    sujetos_afectados: set[uuid.UUID] = set()
    for doc in db.scalars(q):
        nuevo = calc_estado_doc(doc)
        if nuevo != doc.estado_calc:
            doc.estado_calc = nuevo
            cambios += 1
            if doc.sujeto_id:
                sujetos_afectados.add(doc.sujeto_id)
        if doc.estado_calc in ("venc", "porvenc"):
            _emitir_alerta_vencimiento(db, doc)
    for sid in sujetos_afectados:
        recalcular_sujeto(db, sid)
    db.commit()
    return cambios


def recalcular_sujeto(db: Session, sujeto_id: uuid.UUID) -> None:
    sujeto = db.get(Sujeto, sujeto_id)
    if not sujeto:
        return
    docs = db.scalars(select(Documento).where(Documento.sujeto_id == sujeto_id)).all()
    for d in docs:
        d.estado_calc = calc_estado_doc(d)
    sujeto.estado = calc_estado_sujeto(list(docs), sujeto.estado)


# ------------------------------------------------------------- snapshots
def _agregados(db: Session, company_id: uuid.UUID,
               contrato_id: uuid.UUID | None) -> dict:
    """Cumplimiento y conteos de una empresa o de uno de sus contratos."""
    docs = select(Documento).where(Documento.company_id == company_id)
    sujetos = select(Sujeto).where(Sujeto.company_id == company_id,
                                  Sujeto.estado != "baja")
    if contrato_id:
        # Documentos del contrato: los de empresa (contrato_id) más los de sus sujetos.
        ids_sujetos = select(Sujeto.id).where(Sujeto.contrato_id == contrato_id)
        docs = docs.where((Documento.contrato_id == contrato_id)
                          | Documento.sujeto_id.in_(ids_sujetos))
        sujetos = sujetos.where(Sujeto.contrato_id == contrato_id)

    filas = db.scalars(docs).all()
    oblig = [d for d in filas if d.obligatorio and not d.es_emsipor]
    ok = sum(1 for d in oblig if d.estado_calc == "ok")
    total = len(oblig)
    pct = round(100 * ok / total) if total else 0

    por_sujeto: dict[uuid.UUID, list[Documento]] = {}
    for d in filas:
        if d.sujeto_id:
            por_sujeto.setdefault(d.sujeto_id, []).append(d)

    p_total = p_ok = e_total = e_ok = 0
    for s in db.scalars(sujetos):
        sd = por_sujeto.get(s.id, [])
        so = [d for d in sd if d.obligatorio and not d.es_emsipor]
        spct = round(100 * sum(1 for d in so if d.estado_calc == "ok") / len(so)) if so else 0
        acreditado = spct >= UMBRAL_ACREDITADO_PCT
        if s.tipo == "trabajador":
            p_total += 1
            p_ok += int(acreditado)
        else:
            e_total += 1
            e_ok += int(acreditado)

    criticas = db.scalar(
        select(func.count()).select_from(Alerta).where(
            Alerta.company_id == company_id,
            Alerta.severidad == "critica",
            Alerta.resuelta_at.is_(None),
            *([Alerta.contrato_id == contrato_id] if contrato_id else []),
        )
    ) or 0

    return {
        "cumplimiento_pct": pct, "docs_ok": ok, "docs_total": total,
        "personal_acreditados": p_ok, "personal_total": p_total,
        "equipos_acreditados": e_ok, "equipos_total": e_total,
        "alertas_criticas": int(criticas),
    }


def escribir_snapshots(db: Session, fecha: date | None = None) -> int:
    """Una fila por empresa (contrato_id NULL) y una por contrato, cada día.

    Los snapshots son inmutables: si el día ya está escrito no se reescribe, se
    omite. Un recálculo posterior no debe alterar el histórico, porque la
    comparativa del dashboard pierde sentido si el pasado cambia.
    """
    hoy = fecha or date.today()
    creados = 0

    empresas = db.scalars(select(Contrato.company_id).distinct()).all()
    for cid in set(empresas):
        objetivos: list[uuid.UUID | None] = [None]
        objetivos += list(db.scalars(select(Contrato.id).where(
            Contrato.company_id == cid, Contrato.estado != "terminado")).all())

        for contrato_id in objetivos:
            existe = db.scalar(select(CumplimientoSnapshot.id).where(
                CumplimientoSnapshot.company_id == cid,
                CumplimientoSnapshot.fecha == hoy,
                (CumplimientoSnapshot.contrato_id == contrato_id
                 if contrato_id else CumplimientoSnapshot.contrato_id.is_(None)),
            ))
            if existe:
                continue
            db.add(CumplimientoSnapshot(
                company_id=cid, contrato_id=contrato_id, fecha=hoy,
                **_agregados(db, cid, contrato_id)))
            creados += 1

    db.commit()
    return creados


# --------------------------------------------------- credenciales expiradas
def expirar_credenciales(db: Session, dias_aviso: int = 30) -> int:
    """Marca expiradas las credenciales caducadas y avisa de las próximas.

    El JWE lleva su propio claim `exp`, así que una credencial vencida ya no
    descifra; esto mantiene la columna `estado` coherente para que la interfaz
    lo muestre y el chequeo 18 de `05_verificacion.sql` no dispare.
    """
    ahora = datetime.now(timezone.utc)

    vencidas = db.scalars(select(PlataformaCredencial).where(
        PlataformaCredencial.estado == "activa",
        PlataformaCredencial.expira_at <= ahora,
    )).all()
    for c in vencidas:
        db.execute(update(PlataformaCredencial)
                   .where(PlataformaCredencial.id == c.id)
                   .values(estado="expirada"))
        db.add(Alerta(
            company_id=c.company_id, severidad="critica", estado="bloqueante",
            origen="integracion", titulo=f"Credencial expirada: {c.usuario}",
            descripcion=("La credencial de acceso a la plataforma del mandante "
                         f"expiró el {c.expira_at:%d/%m/%Y}. Debe rotarse para "
                         "reanudar la sincronización."),
        ))

    limite = ahora + timedelta(days=dias_aviso)
    proximas = db.scalars(select(PlataformaCredencial).where(
        PlataformaCredencial.estado == "activa",
        PlataformaCredencial.expira_at > ahora,
        PlataformaCredencial.expira_at <= limite,
    )).all()
    for c in proximas:
        ya = db.scalar(select(Alerta.id).where(
            Alerta.company_id == c.company_id,
            Alerta.origen == "integracion",
            Alerta.titulo == f"Credencial por expirar: {c.usuario}",
            Alerta.resuelta_at.is_(None),
        ))
        if ya:
            continue
        dias = (c.expira_at - ahora).days
        db.add(Alerta(
            company_id=c.company_id, severidad="advertencia", estado="nueva",
            origen="integracion", titulo=f"Credencial por expirar: {c.usuario}",
            descripcion=(f"La credencial vence en {dias} días "
                         f"({c.expira_at:%d/%m/%Y}). Código CREDENCIAL_EXPIRADA."),
        ))

    db.commit()
    return len(vencidas)
