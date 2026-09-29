"""Autenticación, ciclo de vida de la cuenta y autoservicio del perfil (§4)."""
from __future__ import annotations

import uuid

import pytest

from app.database import worker_session
from app.models import RefreshToken, User

from tests.conftest import API, CLAVE, crear_contract_admin, rut_valido


# ============================================================================
# Registro y aprobación
# ============================================================================
def test_registro_queda_pendiente(app_cliente):
    r = app_cliente.post(f"{API}/auth/register", json={
        "empresa": "Nueva SpA", "rut": rut_valido(50),
        "email": "nueva@empresa.cl", "password": CLAVE})
    assert r.status_code == 201, r.text
    cuerpo = r.json()
    assert cuerpo["status"] == "pending"
    assert uuid.UUID(cuerpo["company_id"]) and uuid.UUID(cuerpo["user_id"])

    # Sin aprobación no se puede entrar, y el motivo se distingue del rechazo.
    r = app_cliente.post(f"{API}/auth/login",
                         json={"email": "nueva@empresa.cl", "password": CLAVE})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ACCOUNT_PENDING"


def test_registro_email_y_rut_duplicados(app_cliente, empresa_a):
    r = app_cliente.post(f"{API}/auth/register", json={
        "empresa": "Otra", "rut": rut_valido(51),
        "email": empresa_a["email"], "password": CLAVE})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "EMAIL_EN_USO"

    r = app_cliente.post(f"{API}/auth/register", json={
        "empresa": "Otra", "rut": empresa_a["rut"],
        "email": "otra@empresa.cl", "password": CLAVE})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "RUT_EN_USO"


def test_registro_rut_invalido(app_cliente):
    r = app_cliente.post(f"{API}/auth/register", json={
        "empresa": "Mal RUT", "rut": "76.111.111-1",
        "email": "malrut@empresa.cl", "password": CLAVE})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "RUT_INVALIDO"


@pytest.mark.parametrize("clave, motivo", [
    ("minuscula1", "sin mayúscula"),
    ("SinDigitos", "sin dígito"),
    ("Ab1", "demasiado corta"),
])
def test_registro_rechaza_password_debil(app_cliente, clave, motivo):
    r = app_cliente.post(f"{API}/auth/register", json={
        "empresa": "Débil", "rut": rut_valido(52),
        "email": "debil@empresa.cl", "password": clave})
    assert r.status_code == 422, motivo
    assert r.json()["error"]["code"] == "PASSWORD_DEBIL"


def test_login_y_me(app_cliente, empresa_a):
    r = app_cliente.get(f"{API}/auth/me", headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    yo = r.json()
    assert yo["email"] == empresa_a["email"]
    assert yo["role"] == "company"
    assert yo["activo"] is True
    assert yo["contrato_id"] is None and yo["contrato"] is None
    assert yo["company"]["id"] == empresa_a["company_id"]
    # last_login_at se fija en el login que hizo el fixture.
    assert yo["last_login_at"] is not None


def test_me_sin_token(app_cliente):
    r = app_cliente.get(f"{API}/auth/me")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "TOKEN_AUSENTE"

    r = app_cliente.get(f"{API}/auth/me",
                        headers={"Authorization": "Bearer no-es-un-jwt"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "TOKEN_INVALIDO"


def test_login_credenciales_invalidas(app_cliente, empresa_a):
    r = app_cliente.post(f"{API}/auth/login",
                         json={"email": empresa_a["email"], "password": "Otra123456"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "CREDENCIALES_INVALIDAS"


def test_login_usuario_desactivado(app_cliente, empresa_a):
    """`activo=False` no es lo mismo que rechazado: 403 CUENTA_DESACTIVADA."""
    with worker_session(is_admin=True) as db:
        u = db.get(User, uuid.UUID(empresa_a["user_id"]))
        u.activo = False
        db.commit()

    r = app_cliente.post(f"{API}/auth/login",
                         json={"email": empresa_a["email"], "password": CLAVE})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "CUENTA_DESACTIVADA"

    # El token emitido antes de la desactivación deja de servir de inmediato.
    r = app_cliente.get(f"{API}/auth/me", headers=empresa_a["headers"])
    assert r.status_code == 401


def test_login_rechazado(app_cliente, admin):
    r = app_cliente.post(f"{API}/auth/register", json={
        "empresa": "Rechazada SpA", "rut": rut_valido(53),
        "email": "rechazada@empresa.cl", "password": CLAVE})
    company_id = r.json()["company_id"]
    r = app_cliente.post(f"{API}/admin/companies/{company_id}/reject",
                         headers=admin["headers"], json={"reason": "documentación falsa"})
    assert r.status_code == 200, r.text

    r = app_cliente.post(f"{API}/auth/login",
                         json={"email": "rechazada@empresa.cl", "password": CLAVE})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ACCOUNT_REJECTED"


# ============================================================================
# Refresh y logout
# ============================================================================
def test_refresh_rota_el_token(app_cliente, empresa_a):
    viejo = empresa_a["refresh_token"]
    r = app_cliente.post(f"{API}/auth/refresh", json={"refresh_token": viejo})
    assert r.status_code == 200, r.text
    nuevo = r.json()
    assert nuevo["refresh_token"] != viejo
    assert nuevo["token_type"] == "bearer"

    # El anterior queda revocado: reutilizarlo es un error, no un no-op.
    r = app_cliente.post(f"{API}/auth/refresh", json={"refresh_token": viejo})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "REFRESH_INVALIDO"

    # El access token nuevo sirve.
    r = app_cliente.get(f"{API}/auth/me",
                        headers={"Authorization": f"Bearer {nuevo['access_token']}"})
    assert r.status_code == 200


def test_refresh_de_usuario_desactivado(app_cliente, empresa_a):
    with worker_session(is_admin=True) as db:
        db.get(User, uuid.UUID(empresa_a["user_id"])).activo = False
        db.commit()
    r = app_cliente.post(f"{API}/auth/refresh",
                         json={"refresh_token": empresa_a["refresh_token"]})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "REFRESH_INVALIDO"


def test_logout_revoca_el_refresh(app_cliente, empresa_a):
    r = app_cliente.post(f"{API}/auth/logout", headers=empresa_a["headers"],
                         json={"refresh_token": empresa_a["refresh_token"]})
    assert r.status_code == 200 and r.json() == {"ok": True}

    r = app_cliente.post(f"{API}/auth/refresh",
                         json={"refresh_token": empresa_a["refresh_token"]})
    assert r.status_code == 401


# ============================================================================
# Recuperación de contraseña
# ============================================================================
def _publico(cuerpo: dict) -> dict:
    """Parte de la respuesta que un cliente puede observar en producción.

    Las claves `_dev_*` solo existen con `JWT_SECRET` de desarrollo; el contrato
    de opacidad se cumple sobre el resto.
    """
    return {k: v for k, v in cuerpo.items() if not k.startswith("_")}


def test_forgot_es_opaco(app_cliente, empresa_a):
    existente = app_cliente.post(f"{API}/auth/password/forgot",
                                 json={"email": empresa_a["email"]})
    inexistente = app_cliente.post(f"{API}/auth/password/forgot",
                                   json={"email": "nadie@empresa.cl"})
    assert existente.status_code == inexistente.status_code == 200
    assert _publico(existente.json()) == _publico(inexistente.json())
    assert _publico(existente.json())["ok"] is True


def test_forgot_invalida_el_token_anterior(app_cliente, empresa_a):
    primero = app_cliente.post(f"{API}/auth/password/forgot",
                               json={"email": empresa_a["email"]}).json()["_dev_token"]
    segundo = app_cliente.post(f"{API}/auth/password/forgot",
                               json={"email": empresa_a["email"]}).json()["_dev_token"]
    assert primero != segundo

    r = app_cliente.post(f"{API}/auth/password/reset",
                         json={"token": primero, "password": "OtraClave9"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "TOKEN_INVALIDO"

    r = app_cliente.post(f"{API}/auth/password/reset",
                         json={"token": segundo, "password": "OtraClave9"})
    assert r.status_code == 200, r.text


def test_reset_cambia_la_clave_y_revoca_sesiones(app_cliente, empresa_a):
    token = app_cliente.post(f"{API}/auth/password/forgot",
                             json={"email": empresa_a["email"]}).json()["_dev_token"]

    r = app_cliente.post(f"{API}/auth/password/reset",
                         json={"token": token, "password": "OtraClave9"})
    assert r.status_code == 200, r.text
    assert r.json()["sesiones_cerradas"] >= 1

    # La clave anterior ya no sirve; la nueva sí.
    assert app_cliente.post(f"{API}/auth/login", json={
        "email": empresa_a["email"], "password": CLAVE}).status_code == 401
    assert app_cliente.post(f"{API}/auth/login", json={
        "email": empresa_a["email"], "password": "OtraClave9"}).status_code == 200

    # Todos los refresh previos quedaron revocados.
    r = app_cliente.post(f"{API}/auth/refresh",
                         json={"refresh_token": empresa_a["refresh_token"]})
    assert r.status_code == 401

    # Y el token de reset es de un solo uso.
    r = app_cliente.post(f"{API}/auth/password/reset",
                         json={"token": token, "password": "Tercera99"})
    assert r.status_code == 400


@pytest.mark.parametrize("clave", ["minuscula1", "SinDigitos", "Ab1"])
def test_reset_aplica_politica_de_password(app_cliente, empresa_a, clave):
    token = app_cliente.post(f"{API}/auth/password/forgot",
                             json={"email": empresa_a["email"]}).json()["_dev_token"]
    r = app_cliente.post(f"{API}/auth/password/reset",
                         json={"token": token, "password": clave})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "PASSWORD_DEBIL"


def test_reset_con_token_inexistente(app_cliente):
    r = app_cliente.post(f"{API}/auth/password/reset",
                         json={"token": "inventado", "password": "OtraClave9"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "TOKEN_INVALIDO"


# ============================================================================
# PATCH /auth/me
# ============================================================================
def test_patch_me_cuerpo_vacio(app_cliente, empresa_a):
    r = app_cliente.patch(f"{API}/auth/me", headers=empresa_a["headers"], json={})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "SIN_CAMBIOS"


def test_patch_me_nombre_y_email(app_cliente, empresa_a):
    r = app_cliente.patch(f"{API}/auth/me", headers=empresa_a["headers"],
                          json={"nombre": "Ana Pérez", "email": "ana@empresa.cl"})
    assert r.status_code == 200, r.text
    salida = r.json()
    assert salida["nombre"] == "Ana Pérez"
    assert salida["email"] == "ana@empresa.cl"
    assert set(salida["cambios"]) == {"nombre", "email"}

    # Y el login pasa a hacerse con el email nuevo.
    assert app_cliente.post(f"{API}/auth/login", json={
        "email": "ana@empresa.cl", "password": CLAVE}).status_code == 200


def test_patch_me_email_en_uso(app_cliente, empresa_a, empresa_b):
    """El email es único a nivel de plataforma, aunque RLS oculte al otro usuario."""
    r = app_cliente.patch(f"{API}/auth/me", headers=empresa_a["headers"],
                          json={"email": empresa_b["email"]})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "EMAIL_EN_USO"


def test_patch_me_password_exige_la_actual(app_cliente, empresa_a):
    r = app_cliente.patch(f"{API}/auth/me", headers=empresa_a["headers"],
                          json={"password": "OtraClave9"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "PASSWORD_ACTUAL_REQUERIDA"

    r = app_cliente.patch(f"{API}/auth/me", headers=empresa_a["headers"],
                          json={"password": "OtraClave9", "password_actual": "Erronea1"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "PASSWORD_ACTUAL_INVALIDA"

    r = app_cliente.patch(f"{API}/auth/me", headers=empresa_a["headers"],
                          json={"password": "debil", "password_actual": CLAVE})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "PASSWORD_DEBIL"


def test_patch_me_password_conserva_la_sesion_indicada(app_cliente, empresa_a):
    """Enviar el refresh de la sesión en curso evita que se cierre."""
    r = app_cliente.patch(f"{API}/auth/me", headers=empresa_a["headers"], json={
        "password": "OtraClave9", "password_actual": CLAVE,
        "refresh_token": empresa_a["refresh_token"]})
    assert r.status_code == 200, r.text
    assert "contraseña" in r.json()["cambios"]

    r = app_cliente.post(f"{API}/auth/refresh",
                         json={"refresh_token": empresa_a["refresh_token"]})
    assert r.status_code == 200, "la sesión indicada no debía cerrarse"


def test_patch_me_password_cierra_las_demas_sesiones(app_cliente, empresa_a):
    otra = app_cliente.post(f"{API}/auth/login", json={
        "email": empresa_a["email"], "password": CLAVE}).json()["refresh_token"]

    r = app_cliente.patch(f"{API}/auth/me", headers=empresa_a["headers"], json={
        "password": "OtraClave9", "password_actual": CLAVE})
    assert r.status_code == 200, r.text
    assert r.json()["sesiones_cerradas"] >= 2

    assert app_cliente.post(f"{API}/auth/refresh",
                            json={"refresh_token": otra}).status_code == 401
    with worker_session(is_admin=True) as db:
        vivos = db.query(RefreshToken).filter(
            RefreshToken.user_id == uuid.UUID(empresa_a["user_id"]),
            RefreshToken.revoked_at.is_(None)).count()
    assert vivos == 0


# ============================================================================
# Roles
# ============================================================================
def test_company_no_puede_usar_endpoints_de_admin(app_cliente, empresa_a):
    r = app_cliente.get(f"{API}/admin/companies", headers=empresa_a["headers"])
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "SOLO_ADMIN"


def test_admin_necesita_company_id_para_datos_de_empresa(app_cliente, admin):
    r = app_cliente.get(f"{API}/contratos", headers=admin["headers"])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "COMPANY_ID_REQUERIDO"

    r = app_cliente.get(f"{API}/contratos", headers={
        **admin["headers"], "X-Company-Id": "no-es-uuid"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "COMPANY_ID_INVALIDO"


def test_admin_impersona_con_x_company_id(app_cliente, admin, empresa_a, contrato_a):
    r = app_cliente.get(f"{API}/contratos", headers={
        **admin["headers"], "X-Company-Id": empresa_a["company_id"]})
    assert r.status_code == 200, r.text
    assert [c["id"] for c in r.json()["items"]] == [contrato_a["id"]]


def test_contract_admin_no_muta_datos_de_empresa(app_cliente, empresa_a, contrato_a):
    """`require_company` excluye al contract_admin de las mutaciones de empresa."""
    jefe = crear_contract_admin(app_cliente, empresa_a, contrato_a["id"])

    r = app_cliente.get(f"{API}/auth/me", headers=jefe["headers"])
    assert r.status_code == 200, r.text
    yo = r.json()
    assert yo["role"] == "contract_admin"
    assert yo["contrato_id"] == contrato_a["id"]
    assert yo["contrato"]["nombre"] == contrato_a["nombre"]

    r = app_cliente.post(f"{API}/company/usuarios", headers=jefe["headers"], json={
        "email": "otro@empresa.cl", "nombre": "Otro", "role": "company"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ROL_INSUFICIENTE"


def test_invitar_usuario_valida_el_rol(app_cliente, empresa_a, contrato_a):
    r = app_cliente.post(f"{API}/company/usuarios", headers=empresa_a["headers"], json={
        "email": "ca@empresa.cl", "nombre": "CA", "role": "contract_admin"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "CONTRATO_REQUERIDO"

    r = app_cliente.post(f"{API}/company/usuarios", headers=empresa_a["headers"], json={
        "email": "co@empresa.cl", "nombre": "CO", "role": "company",
        "contrato_id": contrato_a["id"]})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "CONTRATO_NO_APLICA"

    r = app_cliente.post(f"{API}/company/usuarios", headers=empresa_a["headers"], json={
        "email": "ad@empresa.cl", "nombre": "AD", "role": "admin"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "ROL_INVALIDO"
