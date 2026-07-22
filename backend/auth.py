"""
Login con Google Workspace, restringido al dominio de la organización.

En producción usa el flujo OAuth real de Google (requiere GOOGLE_CLIENT_ID/
GOOGLE_CLIENT_SECRET reales — ver README). Como este entorno no tiene esas
credenciales, hay un modo de desarrollo (AUTH_DEV_MODE=true) que deja elegir
con qué correo "iniciar sesión" sin pasar por Google — es lo que se usó para
probar el backend en esta sesión. Debe quedar apagado en producción.
"""

import os

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db, verify_password
from models import Trabajador

router = APIRouter()

AUTH_DEV_MODE = os.environ.get("AUTH_DEV_MODE", "false").lower() == "true"
ALLOWED_DOMAIN = os.environ.get("ALLOWED_GOOGLE_DOMAIN", "")

oauth = OAuth()
if not AUTH_DEV_MODE:
    oauth.register(
        name="google",
        client_id=os.environ.get("GOOGLE_CLIENT_ID"),
        client_secret=os.environ.get("GOOGLE_CLIENT_SECRET"),
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )


def current_trabajador(request: Request, db: Session = Depends(get_db)) -> Trabajador:
    correo = request.session.get("correo")
    if not correo:
        raise HTTPException(status_code=401, detail="No hay sesión iniciada.")
    trabajador = db.query(Trabajador).filter(Trabajador.correo == correo, Trabajador.activo).first()
    if not trabajador:
        raise HTTPException(status_code=401, detail="Sesión inválida.")
    return trabajador


def require_admin(trabajador: Trabajador = Depends(current_trabajador)) -> Trabajador:
    if trabajador.rol.value != "admin":
        raise HTTPException(status_code=403, detail="Solo el Admin puede hacer esto.")
    return trabajador


def require_permiso(permiso: str):
    """Admin siempre pasa. Un trabajador normal solo si tiene ese permiso
    explícito en su perfil (ej. 'nomina', 'estadisticas', 'trabajadores')."""
    def checker(trabajador: Trabajador = Depends(current_trabajador)) -> Trabajador:
        if trabajador.rol.value == "admin" or permiso in (trabajador.permisos or []):
            return trabajador
        raise HTTPException(status_code=403, detail="No tienes permiso para ver esta sección.")
    return checker


def require_permiso_any(*permisos: str):
    """Como require_permiso, pero pasa con cualquiera de varios permisos —
    para endpoints que alimentan más de una pantalla (ej. estadísticas y
    seguimiento comparten el mismo buscador de servicios)."""
    def checker(trabajador: Trabajador = Depends(current_trabajador)) -> Trabajador:
        if trabajador.rol.value == "admin" or any(p in (trabajador.permisos or []) for p in permisos):
            return trabajador
        raise HTTPException(status_code=403, detail="No tienes permiso para ver esta sección.")
    return checker


@router.get("/auth/login")
async def login(request: Request):
    if AUTH_DEV_MODE:
        return {
            "modo": "desarrollo",
            "instrucciones": "POST /auth/dev-login con {\"correo\": \"caro@solutionsmultiservices.com\"} para simular el login.",
        }
    redirect_uri = request.url_for("auth_callback")
    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/auth/callback", name="auth_callback")
async def auth_callback(request: Request, db: Session = Depends(get_db)):
    token = await oauth.google.authorize_access_token(request)
    userinfo = token.get("userinfo") or {}
    correo = userinfo.get("email", "")
    dominio = correo.split("@")[-1] if "@" in correo else ""
    if ALLOWED_DOMAIN and dominio != ALLOWED_DOMAIN:
        raise HTTPException(status_code=403, detail=f"Solo se permite iniciar sesión con correos @{ALLOWED_DOMAIN}.")
    trabajador = db.query(Trabajador).filter(Trabajador.correo == correo).first()
    if not trabajador:
        raise HTTPException(status_code=403, detail="Este correo no está dado de alta como trabajador.")
    request.session["correo"] = trabajador.correo
    return RedirectResponse(url="/")


class LoginIn(BaseModel):
    correo: str
    password: str


@router.post("/auth/login-password")
async def login_password(request: Request, body: LoginIn, db: Session = Depends(get_db)):
    trabajador = db.query(Trabajador).filter(Trabajador.correo == body.correo, Trabajador.activo).first()
    if not trabajador or not verify_password(body.password, trabajador.password_hash):
        raise HTTPException(status_code=401, detail="Correo o contraseña incorrectos.")
    request.session["correo"] = trabajador.correo
    return {"ok": True, "trabajador": trabajador.nombre, "rol": trabajador.rol.value}


@router.post("/auth/dev-login")
async def dev_login(request: Request, db: Session = Depends(get_db)):
    if not AUTH_DEV_MODE:
        raise HTTPException(status_code=404)
    body = await request.json()
    correo = body.get("correo", "")
    trabajador = db.query(Trabajador).filter(Trabajador.correo == correo).first()
    if not trabajador:
        raise HTTPException(status_code=404, detail="No existe un trabajador con ese correo.")
    request.session["correo"] = trabajador.correo
    return {"ok": True, "trabajador": trabajador.nombre, "rol": trabajador.rol.value}


@router.post("/auth/logout")
async def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@router.get("/auth/me")
async def me(trabajador: Trabajador = Depends(current_trabajador)):
    return {
        "id": trabajador.id,
        "nombre": trabajador.nombre,
        "correo": trabajador.correo,
        "rol": trabajador.rol.value,
        "config_servicios": trabajador.config_servicios,
        "permisos": trabajador.permisos or [],
    }
