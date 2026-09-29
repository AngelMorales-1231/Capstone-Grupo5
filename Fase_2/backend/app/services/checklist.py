"""Instanciación de checklists desde plantillas y cálculo de estados.

Resolución de la plantilla efectiva de un contrato (§8.3 de la especificación),
de la más específica a la más general:

1. `contrato_plantilla_overrides` — reemplazo total del ámbito para ese contrato.
2. `cargo_requisitos` — solo ámbito personal, cuando el sujeto tiene cargo.
3. `requisito_templates` de la faena (faena_id no nulo).
4. `requisito_templates` estándar (faena_id nulo).

Sobre esa base se añaden los `contrato_requisitos` de origen `custom` y
`arranque`, que la empresa administra a mano o detecta la IA.
"""
from __future__ import annotations

import unicodedata
import uuid
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import UMBRAL_ACREDITADO_PCT, UMBRAL_PORVENC_DIAS
from ..models import (
    Cargo, CargoRequisito, ContratoPlantillaOverride, ContratoRequisito,
    Documento, LicenciaInterna, RequisitoTemplate, Sujeto,
)


def normalizar(texto: str) -> str:
    """Clave de deduplicación de cargos: minúsculas y sin acentos."""
    sin_tilde = "".join(
        c for c in unicodedata.normalize("NFD", texto or "")
        if unicodedata.category(c) != "Mn"
    )
    return " ".join(sin_tilde.lower().split())


# ------------------------------------------------------------- plantillas
def plantillas(db: Session, ambito: str, faena_id: uuid.UUID | None = None
               ) -> list[RequisitoTemplate]:
    """Plantillas del ámbito; los overrides de la faena prevalecen por título."""
    base = db.scalars(select(RequisitoTemplate).where(
        RequisitoTemplate.ambito == ambito,
        RequisitoTemplate.activo,
        RequisitoTemplate.faena_id.is_(None),
    )).all()
    if faena_id:
        overrides = db.scalars(select(RequisitoTemplate).where(
            RequisitoTemplate.ambito == ambito,
            RequisitoTemplate.activo,
            RequisitoTemplate.faena_id == faena_id,
        )).all()
        por_titulo = {t.titulo: t for t in base}
        por_titulo.update({t.titulo: t for t in overrides})
        return list(por_titulo.values())
    return list(base)


def plantilla_efectiva(
    db: Session, ambito: str, *, contrato_id: uuid.UUID | None = None,
    faena_id: uuid.UUID | None = None, cargo_id: uuid.UUID | None = None,
) -> list[RequisitoTemplate]:
    """Aplica la cadena de resolución descrita en el encabezado del módulo."""
    if contrato_id:
        ov = db.scalar(select(ContratoPlantillaOverride).where(
            ContratoPlantillaOverride.contrato_id == contrato_id,
            ContratoPlantillaOverride.ambito == ambito,
        ))
        if ov and ov.requisito_template_ids:
            filas = db.scalars(select(RequisitoTemplate).where(
                RequisitoTemplate.id.in_(list(ov.requisito_template_ids)),
                RequisitoTemplate.activo,
            )).all()
            return list(filas)

    if ambito == "personal" and cargo_id:
        filas = db.scalars(
            select(RequisitoTemplate)
            .join(CargoRequisito,
                  CargoRequisito.requisito_template_id == RequisitoTemplate.id)
            .where(CargoRequisito.cargo_id == cargo_id, RequisitoTemplate.activo)
        ).all()
        if filas:
            return list(filas)

    return plantillas(db, ambito, faena_id)


def requisitos_extra(
    db: Session, contrato_id: uuid.UUID, ambito: str,
    cargo_id: uuid.UUID | None = None,
) -> list[ContratoRequisito]:
    """Requisitos custom y de carpeta de arranque aplicables a un sujeto.

    Un requisito con `cargo_id` solo aplica a los trabajadores de ese cargo; sin
    `cargo_id` aplica a todos los del ámbito.
    """
    filas = db.scalars(select(ContratoRequisito).where(
        ContratoRequisito.contrato_id == contrato_id,
        ContratoRequisito.ambito == ambito,
        ContratoRequisito.activo,
        ContratoRequisito.origen != "base",
    )).all()
    return [r for r in filas if r.cargo_id is None or r.cargo_id == cargo_id]


# ---------------------------------------------------------- instanciación
def instanciar_docs(
    db: Session, company_id: uuid.UUID, ambito: str,
    sujeto_id: uuid.UUID | None = None, contrato_id: uuid.UUID | None = None,
    faena_id: uuid.UUID | None = None, cargo_id: uuid.UUID | None = None,
    contrato_plantilla_id: uuid.UUID | None = None,
) -> int:
    """Crea los documentos del ámbito. Devuelve cuántos creó.

    `contrato_plantilla_id` es el contrato del que se resuelven overrides y
    requisitos extra: para un sujeto es su contrato, aunque el documento cuelgue
    del sujeto y no del contrato.
    """
    ref_contrato = contrato_plantilla_id or contrato_id
    n = 0
    vistos: set[str] = set()

    for t in plantilla_efectiva(db, ambito, contrato_id=ref_contrato,
                                faena_id=faena_id, cargo_id=cargo_id):
        if t.titulo.lower() in vistos:
            continue
        vistos.add(t.titulo.lower())
        db.add(Documento(
            company_id=company_id, template_id=t.id,
            sujeto_id=sujeto_id, contrato_id=contrato_id,
            titulo=t.titulo, obligatorio=t.obligatorio,
            es_emsipor=(ambito == "emsipor"),
        ))
        n += 1

    if ref_contrato:
        for r in requisitos_extra(db, ref_contrato, ambito, cargo_id):
            if r.titulo.lower() in vistos:
                continue
            vistos.add(r.titulo.lower())
            db.add(Documento(
                company_id=company_id, template_id=r.requisito_template_id,
                sujeto_id=sujeto_id, contrato_id=contrato_id,
                titulo=r.titulo, obligatorio=r.obligatorio,
                es_emsipor=(ambito == "emsipor"),
            ))
            n += 1
    return n


def crear_expediente_conductor(db: Session, sujeto: Sujeto) -> int:
    """Documentos EMSIPOR y licencia interna de un trabajador conductor."""
    existe = db.scalar(select(LicenciaInterna).where(
        LicenciaInterna.sujeto_id == sujeto.id))
    if existe:
        return 0
    n = instanciar_docs(db, sujeto.company_id, "emsipor", sujeto_id=sujeto.id,
                        contrato_plantilla_id=sujeto.contrato_id)
    db.add(LicenciaInterna(company_id=sujeto.company_id, sujeto_id=sujeto.id))
    return n


def requiere_emsipor(db: Session, sujeto: Sujeto) -> bool:
    """`cargos.requiere_emsipor` es la fuente de verdad; `es_conductor` la anula.

    El campo del sujeto se conserva como override explícito por trabajador
    (§9.2), de modo que un caso particular no obliga a crear un cargo nuevo.
    """
    if sujeto.es_conductor:
        return True
    if sujeto.cargo_id:
        cargo = db.get(Cargo, sujeto.cargo_id)
        return bool(cargo and cargo.requiere_emsipor)
    return False


def resolver_cargo(db: Session, company_id: uuid.UUID, *,
                   cargo_id: uuid.UUID | None, cargo_texto: str | None
                   ) -> tuple[Cargo | None, bool]:
    """Resuelve el cargo por id o por nombre normalizado, creándolo si no existe.

    Devuelve (cargo, creado). Con texto libre busca primero en el catálogo de la
    empresa y luego en el base de Acredittia; si no aparece, crea uno con
    categoría 'otro' para que alguien lo clasifique después.
    """
    if cargo_id:
        cargo = db.get(Cargo, cargo_id)
        if not cargo or cargo.company_id not in (None, company_id):
            return None, False
        return cargo, False

    if not cargo_texto:
        return None, False

    norm = normalizar(cargo_texto)
    cargo = db.scalar(select(Cargo).where(
        Cargo.nombre_normalizado == norm,
        Cargo.company_id == company_id,
    )) or db.scalar(select(Cargo).where(
        Cargo.nombre_normalizado == norm,
        Cargo.company_id.is_(None),
    ))
    if cargo:
        return cargo, False

    cargo = Cargo(company_id=company_id, nombre=cargo_texto.strip(),
                  nombre_normalizado=norm, categoria="otro")
    db.add(cargo)
    db.flush()
    return cargo, True


# ---------------------------------------------------------------- estados
def calc_estado_doc(doc: Documento, hoy: date | None = None) -> str:
    hoy = hoy or date.today()
    if doc.estado == "falta":
        return "falta"
    if doc.vence is None:
        return "ok"
    if doc.vence < hoy:
        return "venc"
    if doc.vence <= hoy + timedelta(days=UMBRAL_PORVENC_DIAS):
        return "porvenc"
    return "ok"


def vencimiento_por_plantilla(db: Session, doc: Documento,
                              desde: date | None = None) -> date | None:
    """Vencimiento derivado de `vigencia_meses` de la plantilla del requisito.

    Se usa cuando el usuario no informa `vence` y la IA no lo extrajo: sin fecha
    el documento quedaría `ok` para siempre y los estados `porvenc` y `venc`
    nunca se activarían.
    """
    if not doc.template_id:
        return None
    t = db.get(RequisitoTemplate, doc.template_id)
    if not t or not t.vigencia_meses:
        return None
    return (desde or date.today()) + timedelta(days=30 * t.vigencia_meses)


def stats_sujeto(docs: list[Documento]) -> dict:
    """Estadísticas sobre los documentos NO emsipor del sujeto."""
    core = [d for d in docs if not d.es_emsipor]
    oblig = [d for d in core if d.obligatorio]
    ok = sum(1 for d in oblig if d.estado_calc == "ok")
    venc = sum(1 for d in oblig if d.estado_calc == "venc")
    porvenc = sum(1 for d in core if d.estado_calc == "porvenc")
    falta = sum(1 for d in core if d.estado_calc == "falta")
    pct = round(100 * ok / len(oblig)) if oblig else 0
    return {
        "docs_total": len(core), "ok": ok, "vencidos": venc,
        "por_vencer": porvenc, "falta": falta, "cumplimiento_pct": pct,
    }


def calc_estado_sujeto(docs: list[Documento], estado_actual: str) -> str:
    if estado_actual == "baja":
        return "baja"
    s = stats_sujeto(docs)
    if s["vencidos"] > 0:
        return "venc"
    if s["cumplimiento_pct"] >= UMBRAL_ACREDITADO_PCT:
        return "ok"
    if s["ok"] == 0:
        return "falta"
    return "proc"
