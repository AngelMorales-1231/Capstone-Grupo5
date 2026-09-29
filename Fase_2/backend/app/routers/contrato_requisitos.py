"""Requisitos documentales por contrato y vínculos (§8.3).

Todo requisito se ancla a un **vínculo**: una plataforma del contrato
(`vinculo_tipo='plataforma'` + `vinculo_ref`), la Carpeta de Arranque
(`'arranque'`) u `'otro'`. Así el usuario ve el checklist agrupado por dónde hay
que presentar cada documento, que es como se trabaja en faena.

Dos orígenes convivien en el mismo listado:

* `origen='base'` — **no existe en la tabla**. Se proyecta al vuelo desde
  `requisito_templates` con `checklist.plantilla_efectiva`, respetando overrides
  de contrato, cargo y faena. Es de solo lectura para la empresa: se administra
  en las plantillas maestras (de ahí el 403 al intentar editarlo o borrarlo).
* `origen='custom'|'arranque'` — filas reales de `contrato_requisitos`, que la
  empresa añade a mano o confirma tras la extracción de la Carpeta de Arranque.

Crear un requisito afecta por defecto solo a los sujetos futuros: los
expedientes ya instanciados no se reescriben salvo que se pida
`aplicar_retroactivo=true`.
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import ALLOWED_EXTENSIONS_ARRANQUE
from ..deps import (Page, err, get_company_id, get_current_user, get_db,
                    paginacion, sobre)
from ..models import (Cargo, Contrato, ContratoPlantillaOverride,
                      ContratoRequisito, Documento, IaReview,
                      RequisitoTemplate, Sujeto, User)
from ..services import actividad
from ..services.checklist import plantilla_efectiva, requiere_emsipor
from ..services.jobs import enqueue
from .plataformas import (contrato_de_empresa, plataformas_efectivas,
                          resolver_plataforma, slug_plataforma)

logger = logging.getLogger("acredittia.requisitos")

router = APIRouter(prefix="/contratos/{contrato_id}", tags=["requisitos"])

Ambito = Literal["empresa", "personal", "equipo", "emsipor"]
VinculoTipo = Literal["plataforma", "arranque", "otro"]
AMBITOS: tuple[str, ...] = ("empresa", "personal", "equipo", "emsipor")

# Vínculos que no cuelgan de una plataforma (vinculo_ref siempre nulo).
VINCULOS_FIJOS = (
    ("arranque", "Carpeta de Arranque"),
    ("otro", "Otros requisitos"),
)


# ------------------------------------------------------------------- entradas
class RequisitoIn(BaseModel):
    vinculo_tipo: VinculoTipo = "otro"
    vinculo_ref: uuid.UUID | None = None
    ambito: Ambito
    titulo: str
    obligatorio: bool = True
    cargo_id: uuid.UUID | None = None
    vigencia_meses: int | None = None
    ejemplo_clave: str | None = None
    # 'arranque' marca los requisitos confirmados tras leer la carpeta.
    origen: Literal["custom", "arranque"] = "custom"


class RequisitoPatch(BaseModel):
    titulo: str | None = None
    obligatorio: bool | None = None
    cargo_id: uuid.UUID | None = None
    vigencia_meses: int | None = None
    activo: bool | None = None


class PlantillaIn(BaseModel):
    requisito_template_ids: list[uuid.UUID]


class CarpetaArranqueIn(BaseModel):
    blob_path: str
    filename: str


# ------------------------------------------------------------------- helpers
def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if v else None


def _out(r: ContratoRequisito) -> dict:
    """Serializador de un requisito real del contrato (editable)."""
    return {
        "id": str(r.id),
        "vinculo_tipo": r.vinculo_tipo,
        "vinculo_ref": str(r.vinculo_ref) if r.vinculo_ref else None,
        "ambito": r.ambito, "titulo": r.titulo, "obligatorio": r.obligatorio,
        "cargo_id": str(r.cargo_id) if r.cargo_id else None,
        "origen": r.origen,
        "template_id": (str(r.requisito_template_id)
                        if r.requisito_template_id else None),
        "ejemplo_clave": r.ejemplo_clave, "vigencia_meses": r.vigencia_meses,
        "activo": r.activo,
        "editable": r.origen != "base",
        "created_at": _iso(r.created_at),
    }


def _out_base(t: RequisitoTemplate, ambito: str, vinculo_tipo: str,
              vinculo_ref: str | None, cargo_id: uuid.UUID | None = None) -> dict:
    """Requisito de origen `base`: proyección de una plantilla, sin fila propia."""
    return {
        "id": None,                     # no existe en contrato_requisitos
        "vinculo_tipo": vinculo_tipo, "vinculo_ref": vinculo_ref,
        "ambito": ambito, "titulo": t.titulo, "obligatorio": t.obligatorio,
        "cargo_id": str(cargo_id) if cargo_id else None,
        "origen": "base", "template_id": str(t.id),
        "ejemplo_clave": t.ejemplo_clave, "vigencia_meses": t.vigencia_meses,
        "activo": t.activo,
        "editable": False,              # se administra en las plantillas maestras
        "created_at": None,
    }


def _refs_por_plataforma(efectivas: list[dict]) -> dict[str, str]:
    """slug del nombre -> ref utilizable como `vinculo_ref`.

    Para una plataforma heredada sin materializar la ref es el id de la
    `faena_plataforma`: al crear un requisito contra ella se materializa la
    herencia y la ref real pasa a ser la de `contrato_plataformas`.
    """
    return {slug_plataforma(e["nombre"]): (e["id"] or e["faena_plataforma_id"])
            for e in efectivas}


def _proyectar_base(db: Session, contrato: Contrato, efectivas: list[dict], *,
                    ambito: str | None = None,
                    cargo_id: uuid.UUID | None = None) -> list[dict]:
    """Requisitos base del contrato, proyectados desde `requisito_templates`.

    El vínculo se deduce de `requisito_templates.plataforma`: si el texto de la
    plantilla coincide con una plataforma efectiva del contrato, el requisito
    aparece colgado de ella; si no, cae en 'otro'.
    """
    por_slug = _refs_por_plataforma(efectivas)
    ambitos = [ambito] if ambito else list(AMBITOS)
    salida: list[dict] = []
    vistos: set[tuple[str, str]] = set()
    for a in ambitos:
        plantillas = plantilla_efectiva(
            db, a, contrato_id=contrato.id, faena_id=contrato.faena_id,
            cargo_id=cargo_id if a == "personal" else None)
        for t in sorted(plantillas, key=lambda x: x.titulo):
            clave = (a, t.titulo.strip().lower())
            if clave in vistos:
                continue
            vistos.add(clave)
            ref = por_slug.get(slug_plataforma(t.plataforma)) if t.plataforma else None
            salida.append(_out_base(
                t, a, "plataforma" if ref else "otro", ref,
                cargo_id if a == "personal" else None))
    return salida


def _cargos_del_contrato(db: Session, cid: uuid.UUID,
                         contrato_id: uuid.UUID) -> list[dict]:
    """Cargos realmente presentes en el personal activo del contrato."""
    filas = db.execute(
        select(Cargo.id, Cargo.nombre, func.count(Sujeto.id))
        .join(Sujeto, Sujeto.cargo_id == Cargo.id)
        .where(Sujeto.company_id == cid, Sujeto.contrato_id == contrato_id,
               Sujeto.tipo == "trabajador", Sujeto.estado != "baja")
        .group_by(Cargo.id, Cargo.nombre)
        .order_by(Cargo.nombre)).all()
    return [{"id": str(f[0]), "nombre": f[1], "trabajadores": int(f[2])}
            for f in filas]


def _cargo_visible(db: Session, cid: uuid.UUID, cargo_id: uuid.UUID) -> Cargo:
    """Cargo de la empresa o del catálogo base de Acredittia."""
    c = db.get(Cargo, cargo_id)
    if not c or c.company_id not in (None, cid):
        raise err(404, "NO_ENCONTRADO", "Cargo no existe")
    return c


def _reales(db: Session, cid: uuid.UUID, contrato_id: uuid.UUID
            ) -> list[ContratoRequisito]:
    return list(db.scalars(
        select(ContratoRequisito)
        .where(ContratoRequisito.company_id == cid,
               ContratoRequisito.contrato_id == contrato_id)
        .order_by(ContratoRequisito.ambito, ContratoRequisito.titulo)))


def _clave_vinculo(tipo: str, ref) -> str:
    return f"{tipo}:{ref}" if ref else tipo


def _duplicado(db: Session, cid: uuid.UUID, contrato_id: uuid.UUID, *,
               vinculo_tipo: str, vinculo_ref: uuid.UUID | None, ambito: str,
               titulo: str, excluir: uuid.UUID | None = None) -> bool:
    """Choque con el índice único (contrato, vínculo, ámbito, lower(título))."""
    q = select(func.count(ContratoRequisito.id)).where(
        ContratoRequisito.contrato_id == contrato_id,
        ContratoRequisito.vinculo_tipo == vinculo_tipo,
        ContratoRequisito.ambito == ambito,
        func.lower(ContratoRequisito.titulo) == titulo.strip().lower())
    q = q.where(ContratoRequisito.vinculo_ref == vinculo_ref
                if vinculo_ref else ContratoRequisito.vinculo_ref.is_(None))
    if excluir:
        q = q.where(ContratoRequisito.id != excluir)
    return bool(db.scalar(q))


def _sujetos_del_ambito(db: Session, cid: uuid.UUID, contrato_id: uuid.UUID,
                        ambito: str, cargo_id: uuid.UUID | None) -> list[Sujeto]:
    """Sujetos activos a los que aplica un requisito del ámbito indicado."""
    if ambito == "empresa":
        return []                       # el dueño del documento es el contrato
    tipo = "equipo" if ambito == "equipo" else "trabajador"
    q = select(Sujeto).where(
        Sujeto.company_id == cid, Sujeto.contrato_id == contrato_id,
        Sujeto.tipo == tipo, Sujeto.estado != "baja")
    if cargo_id:
        q = q.where(Sujeto.cargo_id == cargo_id)
    sujetos = list(db.scalars(q))
    if ambito == "emsipor":
        sujetos = [s for s in sujetos if requiere_emsipor(db, s)]
    return sujetos


def _instanciar_retroactivo(db: Session, cid: uuid.UUID, contrato: Contrato,
                            requisitos: list[ContratoRequisito]) -> int:
    """Crea el documento en estado `falta` en los sujetos activos que aplican.

    Se compara por título en minúsculas para no duplicar un documento que el
    sujeto ya tenía por otra vía (plantilla base, otro requisito custom…).
    """
    creados = 0
    for r in requisitos:
        if r.ambito == "empresa":
            titulos = {t.strip().lower() for t in db.scalars(
                select(Documento.titulo).where(
                    Documento.company_id == cid,
                    Documento.contrato_id == contrato.id))}
            if r.titulo.strip().lower() in titulos:
                continue
            db.add(Documento(
                company_id=cid, template_id=r.requisito_template_id,
                contrato_id=contrato.id, titulo=r.titulo,
                obligatorio=r.obligatorio, estado="falta", estado_calc="falta",
                es_emsipor=False))
            creados += 1
            continue

        for s in _sujetos_del_ambito(db, cid, contrato.id, r.ambito, r.cargo_id):
            titulos = {t.strip().lower() for t in db.scalars(
                select(Documento.titulo).where(Documento.sujeto_id == s.id))}
            if r.titulo.strip().lower() in titulos:
                continue
            db.add(Documento(
                company_id=cid, template_id=r.requisito_template_id,
                sujeto_id=s.id, titulo=r.titulo, obligatorio=r.obligatorio,
                estado="falta", estado_calc="falta",
                es_emsipor=(r.ambito == "emsipor")))
            creados += 1
    if creados:
        db.flush()
    return creados


def _resolver_entrada(db: Session, cid: uuid.UUID, contrato: Contrato,
                      item: RequisitoIn) -> tuple[uuid.UUID | None, int]:
    """Valida el vínculo y el cargo de una entrada. Devuelve (ref real, materializadas).

    Los tres CHECK de `contrato_requisitos` se comprueban antes de tocar la BD
    para devolver 400 con un mensaje entendible en vez de un 23514 opaco.
    """
    if (item.vinculo_tipo == "plataforma") != (item.vinculo_ref is not None):
        raise err(400, "VINCULO_INVALIDO",
                  "vinculo_ref es obligatorio con vinculo_tipo='plataforma' y "
                  "debe omitirse en los demás casos")
    if item.cargo_id and item.ambito != "personal":
        raise err(400, "CARGO_FUERA_DE_AMBITO",
                  "cargo_id solo aplica a requisitos de ámbito personal")
    if not (item.titulo or "").strip():
        raise err(400, "TITULO_REQUERIDO", "El título del requisito es obligatorio")
    if item.vigencia_meses is not None and not 1 <= item.vigencia_meses <= 120:
        raise err(400, "VIGENCIA_INVALIDA",
                  "La vigencia debe estar entre 1 y 120 meses")
    if item.cargo_id:
        _cargo_visible(db, cid, item.cargo_id)

    if item.vinculo_ref is None:
        return None, 0
    # Acepta la ref de una plataforma heredada aún sin materializar.
    plat, materializadas = resolver_plataforma(
        db, cid, contrato, item.vinculo_ref, materializar=True)
    return plat.id, materializadas


# =============================================================================
# Vínculos disponibles
# =============================================================================
@router.get("/vinculos")
def listar_vinculos(contrato_id: uuid.UUID, db: Session = Depends(get_db),
                    cid: uuid.UUID = Depends(get_company_id),
                    user: User = Depends(get_current_user)):
    """Vínculos a los que se puede anclar un requisito, con su carga actual.

    Es el selector de la UI, así que no se pagina: son las plataformas efectivas
    del contrato (misma regla de herencia que §8.1) más 'arranque' y 'otro'. Los
    conteos incluyen los requisitos base proyectados, para que el número que ve
    el usuario sea el mismo que luego lista `GET /requisitos`.
    """
    c = contrato_de_empresa(db, cid, contrato_id, user)
    efectivas = plataformas_efectivas(db, cid, c)

    conteos: dict[str, dict[str, int]] = {}

    def acumula(tipo: str, ref, ambito: str) -> None:
        d = conteos.setdefault(_clave_vinculo(tipo, ref),
                               {a: 0 for a in AMBITOS})
        d[ambito] = d.get(ambito, 0) + 1

    for r in _reales(db, cid, c.id):
        acumula(r.vinculo_tipo, str(r.vinculo_ref) if r.vinculo_ref else None,
                r.ambito)
    for b in _proyectar_base(db, c, efectivas):
        acumula(b["vinculo_tipo"], b["vinculo_ref"], b["ambito"])

    vacio = {a: 0 for a in AMBITOS}
    items = [{
        "tipo": "plataforma",
        "ref": e["id"] or e["faena_plataforma_id"],
        "nombre": e["nombre"], "estado": e["estado"],
        "materializada": e["materializada"], "es_custom": e["es_custom"],
        "requisitos": conteos.get(
            _clave_vinculo("plataforma", e["id"] or e["faena_plataforma_id"]),
            dict(vacio)),
    } for e in efectivas]
    items += [{
        "tipo": tipo, "ref": None, "nombre": nombre, "estado": None,
        "materializada": True, "es_custom": False,
        "requisitos": conteos.get(_clave_vinculo(tipo, None), dict(vacio)),
    } for tipo, nombre in VINCULOS_FIJOS]

    return {"items": items, "total": len(items),
            "cargos": _cargos_del_contrato(db, cid, c.id)}


# =============================================================================
# Requisitos del contrato
# =============================================================================
@router.get("/requisitos")
def listar_requisitos(contrato_id: uuid.UUID,
                      vinculo_tipo: str | None = Query(None),
                      vinculo_ref: uuid.UUID | None = Query(None),
                      ambito: str | None = Query(None),
                      cargo_id: uuid.UUID | None = Query(None),
                      origen: str | None = Query(None),
                      p: Page = Depends(paginacion),
                      db: Session = Depends(get_db),
                      cid: uuid.UUID = Depends(get_company_id),
                      user: User = Depends(get_current_user)):
    """Checklist completo del contrato: base proyectado + personalizados reales.

    Los base no viven en la tabla, así que el conjunto se compone en memoria y
    se pagina aquí; los KPI se calculan sobre el total filtrado, no sobre la
    página.
    """
    c = contrato_de_empresa(db, cid, contrato_id, user)
    if ambito and ambito not in AMBITOS:
        raise err(400, "AMBITO_INVALIDO",
                  f"Ámbito debe ser uno de: {', '.join(AMBITOS)}")
    efectivas = plataformas_efectivas(db, cid, c)

    items: list[dict] = []
    if origen != "base":
        for r in _reales(db, cid, c.id):
            if origen and r.origen != origen:
                continue
            items.append(_out(r))
    if origen in (None, "base"):
        items += _proyectar_base(db, c, efectivas, ambito=ambito,
                                 cargo_id=cargo_id)

    ref_txt = str(vinculo_ref) if vinculo_ref else None
    if vinculo_tipo:
        items = [i for i in items if i["vinculo_tipo"] == vinculo_tipo]
    if ref_txt:
        items = [i for i in items if i["vinculo_ref"] == ref_txt]
    if ambito:
        items = [i for i in items if i["ambito"] == ambito]
    if cargo_id:
        # Un requisito sin cargo aplica a todos los cargos del ámbito personal.
        items = [i for i in items
                 if i["cargo_id"] in (None, str(cargo_id))]

    items.sort(key=lambda i: (i["ambito"], i["titulo"].lower()))
    kpis = {
        "vinculos": len({_clave_vinculo(i["vinculo_tipo"], i["vinculo_ref"])
                         for i in items}),
        "base": sum(1 for i in items if i["origen"] == "base"),
        "personalizados": sum(1 for i in items if i["origen"] != "base"),
        "cargos": len({i["cargo_id"] for i in items if i["cargo_id"]}),
    }
    total = len(items)
    pagina = items[p.offset:p.offset + p.page_size]
    salida = sobre(pagina, total, p)
    salida["kpis"] = kpis
    return salida


@router.post("/requisitos", status_code=201)
def crear_requisitos(contrato_id: uuid.UUID,
                     body: RequisitoIn | list[RequisitoIn],
                     bulk: bool = Query(False),
                     aplicar_retroactivo: bool = Query(False),
                     db: Session = Depends(get_db),
                     cid: uuid.UUID = Depends(get_company_id),
                     user: User = Depends(get_current_user)):
    """Añade requisitos personalizados al contrato.

    En modo `bulk` (el que usa la confirmación de la Carpeta de Arranque) los
    duplicados no son un error: se omiten y se informan, porque la extracción
    suele proponer requisitos que ya existían.

    Por defecto el requisito solo afecta a los sujetos que se creen después. Con
    `aplicar_retroactivo=true` se instancia además como documento en estado
    `falta` en los sujetos activos que ya están en el contrato.
    """
    c = contrato_de_empresa(db, cid, contrato_id, user)
    entradas = body if isinstance(body, list) else [body]
    es_bulk = bulk or isinstance(body, list)
    if not entradas:
        raise err(400, "SIN_DATOS", "No se envió ningún requisito")

    creados: list[ContratoRequisito] = []
    omitidos: list[str] = []
    materializadas = 0
    vistos: set[tuple] = set()

    for item in entradas:
        ref, n_mat = _resolver_entrada(db, cid, c, item)
        materializadas += n_mat
        titulo = item.titulo.strip()
        clave = (item.vinculo_tipo, str(ref), item.ambito, titulo.lower())
        duplicado = clave in vistos or _duplicado(
            db, cid, c.id, vinculo_tipo=item.vinculo_tipo, vinculo_ref=ref,
            ambito=item.ambito, titulo=titulo)
        if duplicado:
            if es_bulk:
                omitidos.append(titulo)
                continue
            raise err(409, "REQUISITO_DUPLICADO",
                      "El contrato ya tiene ese requisito en el mismo vínculo y ámbito")
        vistos.add(clave)

        r = ContratoRequisito(
            company_id=cid, contrato_id=c.id, vinculo_tipo=item.vinculo_tipo,
            vinculo_ref=ref, ambito=item.ambito, titulo=titulo,
            obligatorio=item.obligatorio, cargo_id=item.cargo_id,
            origen=item.origen, requisito_template_id=None,
            ejemplo_clave=item.ejemplo_clave,
            vigencia_meses=item.vigencia_meses, activo=True)
        try:
            # Savepoint por fila: en bulk un choque con el índice único no debe
            # tumbar la transacción de los demás.
            with db.begin_nested():
                db.add(r)
                db.flush()
        except IntegrityError:
            if es_bulk:
                omitidos.append(titulo)
                continue
            db.rollback()
            raise err(409, "REQUISITO_DUPLICADO",
                      "El contrato ya tiene ese requisito en el mismo vínculo y ámbito")
        creados.append(r)

    docs = _instanciar_retroactivo(db, cid, c, creados) if aplicar_retroactivo else 0

    actividad.log(db, cid, "creacion", "requisitos",
                  f"{len(creados)} requisitos añadidos al contrato '{c.nombre}'"
                  + (f"; {docs} documentos instanciados" if docs else ""),
                  user_id=user.id, entidad_tipo="contrato", entidad_id=c.id)
    db.commit()

    if es_bulk:
        return {"creados": len(creados), "omitidos": omitidos,
                "items": [_out(r) for r in creados],
                "documentos_creados": docs,
                "heredadas_materializadas": materializadas}
    salida = _out(creados[0])
    salida["documentos_creados"] = docs
    salida["heredadas_materializadas"] = materializadas
    return salida


@router.patch("/requisitos/{rid}")
def editar_requisito(contrato_id: uuid.UUID, rid: uuid.UUID,
                     body: RequisitoPatch, db: Session = Depends(get_db),
                     cid: uuid.UUID = Depends(get_company_id),
                     user: User = Depends(get_current_user)):
    """Edita un requisito personalizado del contrato."""
    c = contrato_de_empresa(db, cid, contrato_id, user)
    r = db.get(ContratoRequisito, rid)
    if not r or r.company_id != cid or r.contrato_id != c.id:
        raise err(404, "NO_ENCONTRADO", "Requisito no existe en el contrato")
    if r.origen == "base":
        raise err(403, "REQUISITO_BASE",
                  "Los requisitos base se administran en las plantillas maestras")

    data = body.model_dump(exclude_unset=True)
    if not data:
        raise err(400, "SIN_CAMBIOS", "No se indicó ningún campo a actualizar")

    if "titulo" in data:
        titulo = (data["titulo"] or "").strip()
        if not titulo:
            raise err(400, "TITULO_REQUERIDO", "El título no puede estar vacío")
        if _duplicado(db, cid, c.id, vinculo_tipo=r.vinculo_tipo,
                      vinculo_ref=r.vinculo_ref, ambito=r.ambito,
                      titulo=titulo, excluir=r.id):
            raise err(409, "REQUISITO_DUPLICADO",
                      "El contrato ya tiene ese requisito en el mismo vínculo y ámbito")
        r.titulo = titulo
    if "cargo_id" in data:
        if data["cargo_id"] is not None:
            if r.ambito != "personal":
                raise err(400, "CARGO_FUERA_DE_AMBITO",
                          "cargo_id solo aplica a requisitos de ámbito personal")
            _cargo_visible(db, cid, data["cargo_id"])
        r.cargo_id = data["cargo_id"]
    if "vigencia_meses" in data:
        v = data["vigencia_meses"]
        if v is not None and not 1 <= v <= 120:
            raise err(400, "VIGENCIA_INVALIDA",
                      "La vigencia debe estar entre 1 y 120 meses")
        r.vigencia_meses = v
    for campo in ("obligatorio", "activo"):
        if campo in data and data[campo] is not None:
            setattr(r, campo, data[campo])

    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise err(409, "REQUISITO_DUPLICADO",
                  "El contrato ya tiene ese requisito en el mismo vínculo y ámbito")
    actividad.log(db, cid, "actualizacion", "requisitos",
                  f"Requisito '{r.titulo}' actualizado "
                  f"({', '.join(sorted(data))})", user_id=user.id,
                  entidad_tipo="contrato_requisito", entidad_id=r.id)
    db.commit()
    return _out(r)


@router.delete("/requisitos/{rid}")
def eliminar_requisito(contrato_id: uuid.UUID, rid: uuid.UUID,
                       db: Session = Depends(get_db),
                       cid: uuid.UUID = Depends(get_company_id),
                       user: User = Depends(get_current_user)):
    """Elimina un requisito personalizado.

    No se tocan los documentos ya instanciados: son expediente y su borrado se
    decide documento a documento.
    """
    c = contrato_de_empresa(db, cid, contrato_id, user)
    r = db.get(ContratoRequisito, rid)
    if not r or r.company_id != cid or r.contrato_id != c.id:
        raise err(404, "NO_ENCONTRADO", "Requisito no existe en el contrato")
    if r.origen == "base":
        raise err(403, "REQUISITO_BASE",
                  "Los requisitos base se administran en las plantillas maestras")
    titulo = r.titulo
    db.delete(r)
    actividad.log(db, cid, "actualizacion", "requisitos",
                  f"Requisito '{titulo}' eliminado del contrato '{c.nombre}'",
                  user_id=user.id, entidad_tipo="contrato_requisito",
                  entidad_id=rid)
    db.commit()
    return {"ok": True, "id": str(rid), "titulo": titulo,
            "documentos_conservados": True}


# =============================================================================
# Plantilla del contrato por ámbito
# =============================================================================
@router.put("/plantillas/{ambito}")
def definir_plantilla(contrato_id: uuid.UUID, ambito: str, body: PlantillaIn,
                      db: Session = Depends(get_db),
                      cid: uuid.UUID = Depends(get_company_id),
                      user: User = Depends(get_current_user)):
    """Reemplaza la plantilla estándar de un ámbito para este contrato.

    Es el primer eslabón de la cadena de resolución de `plantilla_efectiva`, así
    que sustituye por completo a las plantillas de faena y estándar. Solo afecta
    a lo que se instancie a partir de ahora: no reescribe los documentos que ya
    tienen los sujetos.
    """
    c = contrato_de_empresa(db, cid, contrato_id, user)
    if ambito not in AMBITOS:
        raise err(400, "AMBITO_INVALIDO",
                  f"Ámbito debe ser uno de: {', '.join(AMBITOS)}")

    ids = list(dict.fromkeys(body.requisito_template_ids))
    encontrados = {t.id: t for t in db.scalars(
        select(RequisitoTemplate).where(RequisitoTemplate.id.in_(ids)))} if ids else {}
    faltan = [str(i) for i in ids if i not in encontrados]
    if faltan:
        raise err(400, "TEMPLATE_INVALIDO",
                  "Plantillas de requisito inexistentes", details=faltan)
    ajenos = [t.titulo for t in encontrados.values() if t.ambito != ambito]
    if ajenos:
        raise err(400, "AMBITO_INVALIDO",
                  f"Estas plantillas no son de ámbito '{ambito}'", details=ajenos)

    ov = db.scalar(select(ContratoPlantillaOverride).where(
        ContratoPlantillaOverride.company_id == cid,
        ContratoPlantillaOverride.contrato_id == c.id,
        ContratoPlantillaOverride.ambito == ambito))
    if ov is None:
        ov = ContratoPlantillaOverride(
            company_id=cid, contrato_id=c.id, ambito=ambito,
            requisito_template_ids=ids)
        db.add(ov)
    else:
        ov.requisito_template_ids = ids
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise err(409, "OVERRIDE_DUPLICADO",
                  "El contrato ya tiene una plantilla definida para ese ámbito")

    actividad.log(db, cid, "actualizacion", "requisitos",
                  f"Plantilla de ámbito '{ambito}' definida con {len(ids)} "
                  f"requisitos en el contrato '{c.nombre}'", user_id=user.id,
                  entidad_tipo="contrato_plantilla_override", entidad_id=ov.id)
    db.commit()
    return {
        "id": str(ov.id), "contrato_id": str(c.id), "ambito": ov.ambito,
        "requisito_template_ids": [str(i) for i in ov.requisito_template_ids],
        "total": len(ids),
        "nota": ("La plantilla se aplica a los sujetos que se creen desde ahora; "
                 "los documentos ya instanciados no se modifican."),
    }


# =============================================================================
# Carpeta de Arranque
# =============================================================================
@router.post("/carpeta-arranque", status_code=202)
def analizar_carpeta_arranque(contrato_id: uuid.UUID, body: CarpetaArranqueIn,
                              db: Session = Depends(get_db),
                              cid: uuid.UUID = Depends(get_company_id),
                              user: User = Depends(get_current_user)):
    """Encola la extracción de requisitos de la Carpeta de Arranque.

    La extracción **no crea requisitos**: propone. El usuario revisa el
    resultado del job y confirma lo que quiera con
    `POST /contratos/{id}/requisitos?bulk=true`, que ya omite los duplicados.
    """
    c = contrato_de_empresa(db, cid, contrato_id, user)
    ext = os.path.splitext(body.filename or "")[1].lower()
    if ext not in ALLOWED_EXTENSIONS_ARRANQUE:
        raise err(400, "EXTENSION_NO_PERMITIDA",
                  "Extensiones permitidas: "
                  f"{', '.join(sorted(ALLOWED_EXTENSIONS_ARRANQUE))}")
    if not (body.blob_path or "").strip():
        raise err(400, "BLOB_PATH_REQUERIDO",
                  "Falta la ruta del archivo ya subido")

    review = IaReview(
        company_id=cid, context="carpeta_arranque", status="queued",
        # IaReview no tiene contrato_id: la trazabilidad va en campos_extraidos.
        campos_extraidos={"contrato_id": str(c.id), "blob_path": body.blob_path,
                          "filename": body.filename},
    )
    db.add(review)
    db.flush()
    actividad.log(db, cid, "alerta_ia", "requisitos",
                  f"Carpeta de Arranque '{body.filename}' enviada a extracción "
                  f"para el contrato '{c.nombre}'", user_id=user.id,
                  entidad_tipo="ia_review", entidad_id=review.id)
    db.commit()

    # Después del commit: con QUEUE_BACKEND=inproc la tarea corre en el acto y
    # necesita ver la fila de ia_reviews ya confirmada.
    enqueue("extraer_carpeta_arranque", review_id=str(review.id),
            company_id=str(cid), contrato_id=str(c.id),
            blob_path=body.blob_path)
    logger.info("contrato %s: carpeta de arranque encolada review=%s",
                c.id, review.id)
    return {"job_id": str(review.id), "status": review.status,
            "nota": ("La extracción propone requisitos; confírmelos con "
                     "POST /contratos/{id}/requisitos?bulk=true")}
