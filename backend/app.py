import io
import os
import pathlib
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

import drive
import recibos
from auth import current_trabajador, require_admin, require_permiso, router as auth_router
from catalogo import ESTATUS_LIBERA_COMISION, SERVICE_TYPES, linea_de
from database import Base, engine, encrypt_ssn, decrypt_ssn, get_db, hash_password
from models import (
    Archivo,
    Comision,
    Configuracion,
    Empresa,
    EmpresaDueno,
    EstadoComision,
    NotaCliente,
    PagoNomina,
    PeriodoCustom,
    Persona,
    RolUsuario,
    Servicio,
    Trabajador,
)

Base.metadata.create_all(bind=engine)

_MIGRACIONES = [
    "ALTER TABLE trabajador ADD COLUMN password_hash VARCHAR(120)",
    "ALTER TABLE trabajador ADD COLUMN tipo_pago VARCHAR(20) DEFAULT 'comision'",
    "ALTER TABLE trabajador ADD COLUMN sueldo_fijo NUMERIC(10,2) DEFAULT 0",
    "ALTER TABLE trabajador ADD COLUMN drive_folder_id VARCHAR(120)",
    "ALTER TABLE comision ADD COLUMN pago_nomina_id INTEGER REFERENCES pago_nomina(id)",
    "ALTER TABLE pago_nomina ADD COLUMN tipo VARCHAR(20) DEFAULT 'pago'",
    "ALTER TABLE pago_nomina ADD COLUMN sueldo_incluido NUMERIC(10,2) DEFAULT 0",
    "ALTER TABLE pago_nomina ADD COLUMN comisiones_incluidas NUMERIC(10,2) DEFAULT 0",
    "ALTER TABLE pago_nomina ADD COLUMN extra_monto NUMERIC(10,2) DEFAULT 0",
    "ALTER TABLE pago_nomina ADD COLUMN extra_concepto VARCHAR(200)",
    "ALTER TABLE pago_nomina ADD COLUMN concepto TEXT",
    "ALTER TABLE pago_nomina ADD COLUMN drive_url VARCHAR(400)",
    "ALTER TABLE trabajador ADD COLUMN permisos JSON DEFAULT '[]'",
]
for _sql in _MIGRACIONES:
    try:
        with engine.begin() as _conn:
            _conn.execute(text(_sql))
    except DBAPIError:
        pass  # la columna ya existe — migración de una sola vez, idempotente

app = FastAPI(title="Plataforma Madre — API")
app.add_middleware(
    SessionMiddleware, secret_key=os.environ.get("SESSION_SECRET_KEY", "dev-secret-cambiar-en-produccion")
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # ajustar a la URL real del frontend antes de producción
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth_router)


@app.get("/api/health")
def health():
    return {"ok": True}


class SetPasswordIn(BaseModel):
    correo: str
    password: str


@app.post("/api/_set_initial_password")
def set_initial_password(body: SetPasswordIn, db: Session = Depends(get_db)):
    """Pone la contraseña inicial de un trabajador — SOLO si todavía no tiene
    ninguna. Se autodesactiva por persona; después de la primera vez, el cambio
    de contraseña se hace autenticado desde Trabajadores. Endpoint temporal para
    el primer despliegue — se puede quitar después."""
    t = db.query(Trabajador).filter(Trabajador.correo == body.correo).first()
    if not t:
        raise HTTPException(status_code=404, detail="No existe ese trabajador.")
    if t.password_hash:
        raise HTTPException(status_code=400, detail="Ese trabajador ya tiene contraseña — no se reemplaza por aquí.")
    t.password_hash = hash_password(body.password)
    db.commit()
    return {"ok": True}


@app.post("/api/_bootstrap_admins")
def bootstrap_admins(db: Session = Depends(get_db)):
    """Siembra los admins reales UNA sola vez en una base de datos nueva y vacía.
    Se desactiva sola: si ya hay algún trabajador, no hace nada. Endpoint temporal
    para el primer despliegue — se puede quitar después."""
    if db.query(Trabajador).count() > 0:
        raise HTTPException(status_code=400, detail="Ya hay trabajadores — no se vuelve a sembrar.")
    db.add_all([
        Trabajador(nombre="Admin", correo="info@solutionstaxes.com", rol=RolUsuario.admin, config_servicios=[]),
        Trabajador(nombre="Andrés", correo="andres.tec.unam@gmail.com", rol=RolUsuario.admin, config_servicios=[]),
        Trabajador(nombre="Carolina", correo="carolina@solutionstaxes.com", rol=RolUsuario.admin, config_servicios=[]),
    ])
    db.commit()
    return {"ok": True, "sembrado": 3}


# ---------- import histórico (admin, un solo uso) ----------

class ImportPersonaIn(BaseModel):
    temp_id: str
    nombre: str
    telefono: Optional[str] = None
    ssn_full: Optional[str] = None


class ImportEmpresaIn(BaseModel):
    temp_id: str
    nombre: str
    ein: Optional[str] = None
    giro: Optional[str] = None
    tipo: Optional[str] = None
    telefono: Optional[str] = None
    correo: Optional[str] = None
    actividad: Optional[str] = "Activa"
    estado_registro: Optional[str] = None


class ImportEmpresaDuenoIn(BaseModel):
    persona_temp_id: str
    empresa_temp_id: str
    rol: str = "Dueño"


class ImportEntidadesIn(BaseModel):
    personas: list[ImportPersonaIn] = []
    empresas: list[ImportEmpresaIn] = []
    empresa_duenos: list[ImportEmpresaDuenoIn] = []


@app.post("/api/_import_entidades")
def import_entidades(body: ImportEntidadesIn, db: Session = Depends(get_db), _=Depends(require_admin)):
    """Primer paso del import histórico: crea personas/empresas/dueños y regresa
    el mapeo temp_id -> id real, para poder mandar los servicios después ya con
    ids reales. Endpoint temporal — se puede quitar cuando termine el import."""
    persona_ids, empresa_ids = {}, {}
    for p in body.personas:
        obj = Persona(
            nombre=p.nombre, telefono=p.telefono,
            ssn_encrypted=encrypt_ssn(p.ssn_full) if p.ssn_full else None,
            ssn_last4=p.ssn_full[-4:] if p.ssn_full else None,
        )
        db.add(obj)
        db.flush()
        persona_ids[p.temp_id] = obj.id
    for e in body.empresas:
        obj = Empresa(
            nombre=e.nombre, ein=e.ein, giro=e.giro, tipo=e.tipo, telefono=e.telefono,
            correo=e.correo, actividad=e.actividad or "Activa", estado_registro=e.estado_registro,
        )
        db.add(obj)
        db.flush()
        empresa_ids[e.temp_id] = obj.id
    n_duenos = 0
    for ed in body.empresa_duenos:
        pid, eid = persona_ids.get(ed.persona_temp_id), empresa_ids.get(ed.empresa_temp_id)
        if pid and eid:
            db.add(EmpresaDueno(persona_id=pid, empresa_id=eid, rol=ed.rol))
            n_duenos += 1
    db.commit()
    return {"ok": True, "persona_ids": persona_ids, "empresa_ids": empresa_ids, "empresa_duenos": n_duenos}


class ImportComisionIn(BaseModel):
    trabajador_id: int
    rol: str = "Responsable"
    monto: float = 0
    estado: str = "pendiente"
    fecha_pago: Optional[date] = None


class ImportServicioIn(BaseModel):
    persona_id: Optional[int] = None
    empresa_id: Optional[int] = None
    tipo: str
    fecha: date
    trabajador_id: int
    cobro: float = 0
    metodo_pago: Optional[str] = None
    estatus: str
    notas: Optional[str] = None
    detalle: dict = {}
    comisiones: list[ImportComisionIn] = []


class ImportServiciosIn(BaseModel):
    servicios: list[ImportServicioIn]


@app.post("/api/_import_servicios")
def import_servicios(body: ImportServiciosIn, db: Session = Depends(get_db), _=Depends(require_admin)):
    """Segundo paso: recibe servicios con persona_id/empresa_id/trabajador_id ya
    reales (resueltos por el llamador) y los crea en lote. Pensado para llamarse
    varias veces con lotes chicos. Endpoint temporal — se puede quitar después."""
    n = 0
    for s in body.servicios:
        if not s.persona_id and not s.empresa_id:
            continue
        obj = Servicio(
            persona_id=s.persona_id, empresa_id=s.empresa_id, tipo=s.tipo, fecha=s.fecha,
            trabajador_id=s.trabajador_id, cobro=s.cobro, metodo_pago=s.metodo_pago,
            estatus=s.estatus, notas=s.notas, detalle=s.detalle,
        )
        db.add(obj)
        db.flush()
        for c in s.comisiones:
            db.add(Comision(
                servicio_id=obj.id, trabajador_id=c.trabajador_id, rol=c.rol, monto=c.monto,
                estado=EstadoComision(c.estado), fecha_pago=c.fecha_pago,
            ))
        n += 1
    db.commit()
    return {"ok": True, "servicios": n}


METODO_PAGO_NORMALIZA = {
    "zelle": "Zelle", "cash": "Cash", "card": "Card",
    "card - square": "Card - Square", "card - chase": "Card - Chase", "card - solutions": "Card - Solutions",
    "banco": "Banco", "banco sta barbara": "Banco Sta Barbara", "banco pagado": "Banco Pagado",
    "cortesia / referidos": "Cortesía / Referidos", "cortesia/referidos": "Cortesía / Referidos",
    "cortesia": "Cortesía / Referidos",
    "cortesía / referidos": "Cortesía / Referidos", "cortesía/referidos": "Cortesía / Referidos",
    "cortesía": "Cortesía / Referidos",
}
TIPO_CITA_NORMALIZA = {
    "presencial": "Presencial", "virtual": "Virtual", "llamada / mensaje": "Llamada / mensaje",
}


@app.post("/api/_normalizar_metodo_tipocita")
def normalizar_metodo_tipocita(db: Session = Depends(get_db), _=Depends(require_admin)):
    """Corrige mayúsculas/minúsculas de metodo_pago y detalle.tipoCita en los
    servicios ya importados, para que coincidan con las opciones del selector y
    se vean en la interfaz. Idempotente — se puede correr más de una vez sin
    riesgo. Endpoint temporal, se puede quitar después."""
    n_metodo, n_cita = 0, 0
    for s in db.query(Servicio).all():
        if s.metodo_pago:
            key = s.metodo_pago.strip().lower()
            nuevo = METODO_PAGO_NORMALIZA.get(key)
            if nuevo and nuevo != s.metodo_pago:
                s.metodo_pago = nuevo
                n_metodo += 1
        if s.detalle and s.detalle.get("tipoCita"):
            key = s.detalle["tipoCita"].strip().lower()
            nuevo = TIPO_CITA_NORMALIZA.get(key)
            if nuevo and nuevo != s.detalle["tipoCita"]:
                s.detalle = {**s.detalle, "tipoCita": nuevo}
                n_cita += 1
    db.commit()
    return {"ok": True, "metodo_pago_corregidos": n_metodo, "tipo_cita_corregidos": n_cita}


# Estatus heredados de datos viejos, capturados de forma muy informal (notas
# de cómo se cobró, montos sueltos, etc. en vez de un estatus limpio). Mapeo
# hecho a mano, valor por valor — no es un normalizador genérico porque
# adivinar mal aquí dispararía comisión sobre dinero que no se cobró.
# Confirmado con el dueño del negocio: todo lo que no diga explícitamente
# "pendiente" o "cancelado" se trata como cobrado.
ESTATUS_LEGACY_A_PAGADO = [
    "Credit Card Chase", "CASH", "Cash", "cash", "Chase Credit Card", "Chase Card", "Card Chase",
    "CREDIT CHASE", "CHASE Credit", "CREdit Card Chase",
    "$200", "$180", "$250", "$360", "180", "225", "$300", "230", "$260", "$225",
    "STA BARBARA 2023", "BMO SAVINGS", "BMO REEMBOLSO", "BMO 03/20/2024", "$700 STA BARBARA",
    "$300 FREETAX CITIBANK", "225 STA BARBARA", "$350 BANCO FREETAX", "$600 BANCO FREETAX",
    "$180 BANCO FREETAX", "BANCO BOFA C&C",
    "PAGO CON REFERENCIAS", "10 REFERENCIAS",
    "$40 CASH $100 BANCO", "$200 ZELLE $150 BANCO",
    "pago taxes y conta x zelle en partes", "$250 / Pago en dos partes",
    "$75 EXTENSION", "EXTENSION",
    "HIJA DE ARTURO PELALLO", "FALTAN FORMAS",
]
ESTATUS_LEGACY_A_PENDIENTE = [
    "Pendiente", "$160 PENDIENTE VIERNES", "$180 PENDIENTE", "PENDIENTE  $200", "PENDIENTE -  $250",
    "PENDIENTE $50 - $350 / 1120 - $250", "$220 PENDIENTE", "$150 PENDIENTE", "PENDIENTE $220",
    "PENDIENTE $50 - $300",
]
ESTATUS_LEGACY_A_CANCELADO = ["CANCELADO"]

ESTATUS_NORMALIZA = {}
for _v in ESTATUS_LEGACY_A_PAGADO:
    ESTATUS_NORMALIZA[_v.strip().lower()] = "Pagado"
for _v in ESTATUS_LEGACY_A_PENDIENTE:
    ESTATUS_NORMALIZA[_v.strip().lower()] = "Pendiente de pago"
for _v in ESTATUS_LEGACY_A_CANCELADO:
    ESTATUS_NORMALIZA[_v.strip().lower()] = "Cancelado"


@app.post("/api/_normalizar_estatus_legacy")
def normalizar_estatus_legacy(db: Session = Depends(get_db), _=Depends(require_admin)):
    """Limpia los ~100 valores de estatus heredados de la captura informal de
    antes (notas de cobro, montos sueltos) a los 6 valores reales del
    catálogo. Mapeo explícito valor por valor, confirmado con el dueño del
    negocio — nada se adivina. Idempotente. Endpoint temporal."""
    cambios = {"Pagado": 0, "Pendiente de pago": 0, "Cancelado": 0}
    sin_mapear = {}
    for s in db.query(Servicio).all():
        if not s.estatus:
            continue
        if s.estatus in ("Pagado", "Pendiente de pago", "Cortesía", "Banco", "Banco Pagado", "Cancelado"):
            continue  # ya es un valor válido del catálogo, con el casing correcto
        key = s.estatus.strip().lower()
        nuevo = ESTATUS_NORMALIZA.get(key)
        if nuevo:
            s.estatus = nuevo
            cambios[nuevo] += 1
        else:
            sin_mapear[s.estatus] = sin_mapear.get(s.estatus, 0) + 1
    db.commit()
    return {"cambios": cambios, "sin_mapear": sin_mapear}


class DriveMatchIn(BaseModel):
    nombre: str
    ssn: Optional[str] = None
    drive_url: str
    title_original: str


class DriveMatchBatchIn(BaseModel):
    items: list[DriveMatchIn]


@app.post("/api/_asociar_archivos_drive")
def asociar_archivos_drive(body: DriveMatchBatchIn, db: Session = Depends(get_db), trabajador: Trabajador = Depends(require_admin)):
    """Empareja carpetas de Google Drive (nombre + SSN) contra personas ya
    existentes y crea un Archivo por cada match confirmado. Match primero por
    SSN completo (descifrado, no solo últimos 4); si no hay SSN, respaldo por
    nombre exacto. No crea nada si hay más de un candidato — lo reporta como
    ambiguo para revisión manual. Endpoint temporal, se puede quitar después."""
    asociados, ambiguos, sin_match = [], [], []
    for item in body.items:
        candidatos = []
        if item.ssn:
            last4 = item.ssn[-4:]
            posibles = db.query(Persona).filter(Persona.ssn_last4 == last4).all()
            for p in posibles:
                if p.ssn_encrypted and decrypt_ssn(p.ssn_encrypted) == item.ssn:
                    candidatos.append(p)
        if not candidatos:
            nombre_norm = item.nombre.strip().lower()
            candidatos = db.query(Persona).filter(func.lower(Persona.nombre) == nombre_norm).all()
        if len(candidatos) == 1:
            p = candidatos[0]
            db.add(Archivo(
                persona_id=p.id, nombre=item.title_original, tipo="archivo",
                drive_url=item.drive_url, subido_por_id=trabajador.id,
            ))
            asociados.append({"persona_id": p.id, "persona_nombre": p.nombre, "drive_title": item.title_original})
        elif len(candidatos) > 1:
            ambiguos.append({"drive_title": item.title_original, "candidatos": [{"id": c.id, "nombre": c.nombre} for c in candidatos]})
        else:
            sin_match.append({"drive_title": item.title_original})
    db.commit()
    return {"asociados": asociados, "ambiguos": ambiguos, "sin_match": sin_match}


@app.post("/api/_limpiar_archivos_drive")
def limpiar_archivos_drive(db: Session = Depends(get_db), _=Depends(require_admin)):
    """Corrige dos problemas detectados tras la asociación masiva de carpetas
    de Drive: (1) filas duplicadas de Archivo cuando el mismo lote se corrió
    dos veces (mismo persona_id + drive_url), y (2) un match incorrecto
    causado por un SSN placeholder ('000-00-0001') compartido entre dos
    personas distintas, que asoció la carpeta de un cliente al expediente de
    otro. Endpoint temporal, se puede quitar después."""
    duplicados_borrados = []
    vistos = {}
    for a in db.query(Archivo).filter(Archivo.drive_url.isnot(None)).order_by(Archivo.id).all():
        key = (a.persona_id, a.empresa_id, a.drive_url)
        if key in vistos:
            duplicados_borrados.append({"id": a.id, "persona_id": a.persona_id, "nombre": a.nombre})
            db.delete(a)
        else:
            vistos[key] = a.id

    placeholder_borrados = []
    for a in db.query(Archivo).filter(Archivo.nombre.like("%000-00-0001%")).all():
        placeholder_borrados.append({"id": a.id, "persona_id": a.persona_id, "nombre": a.nombre})
        db.delete(a)

    db.commit()
    return {"duplicados_borrados": duplicados_borrados, "placeholder_borrados": placeholder_borrados}


# ---------- clientes ----------

@app.get("/api/_export_clientes_dedup")
def export_clientes_dedup(db: Session = Depends(get_db), _=Depends(require_admin)):
    """Dump completo de personas/empresas con las señales necesarias para
    detectar duplicados (ssn_last4, teléfono, correo) — para análisis, no
    modifica nada. Endpoint temporal."""
    personas = [
        {
            "id": p.id, "nombre": p.nombre, "ssn_last4": p.ssn_last4, "telefono": p.telefono,
            "correo": p.correo, "drive_folder_id": p.drive_folder_id, "actividad": p.actividad,
            "n_servicios": db.query(Servicio).filter(Servicio.persona_id == p.id).count(),
            "n_notas": db.query(NotaCliente).filter(NotaCliente.persona_id == p.id).count(),
            "n_archivos": db.query(Archivo).filter(Archivo.persona_id == p.id).count(),
        }
        for p in db.query(Persona).all()
    ]
    empresas = [
        {
            "id": e.id, "nombre": e.nombre, "ein": e.ein, "telefono": e.telefono,
            "correo": e.correo, "drive_folder_id": e.drive_folder_id, "actividad": e.actividad,
            "n_servicios": db.query(Servicio).filter(Servicio.empresa_id == e.id).count(),
            "n_notas": db.query(NotaCliente).filter(NotaCliente.empresa_id == e.id).count(),
            "n_archivos": db.query(Archivo).filter(Archivo.empresa_id == e.id).count(),
        }
        for e in db.query(Empresa).all()
    ]
    return {"personas": personas, "empresas": empresas}


class ClienteIn(BaseModel):
    tipo: str  # persona | empresa
    nombre: str
    telefono: Optional[str] = None
    correo: Optional[str] = None
    ssn_full: Optional[str] = None  # solo si tipo=persona
    giro: Optional[str] = None  # solo si tipo=empresa
    ein: Optional[str] = None


@app.get("/api/clientes")
def listar_clientes(
    q: Optional[str] = None, db: Session = Depends(get_db), trabajador: Trabajador = Depends(current_trabajador)
):
    """Admin ve todos los clientes. Un trabajador normal ve solo los suyos —
    aquellos a los que les ha hecho al menos un servicio (su 'Mis clientes')."""
    mis_persona_ids, mis_empresa_ids = None, None
    if trabajador.rol != RolUsuario.admin:
        mis_persona_ids = {
            s.persona_id
            for s in db.query(Servicio).filter(Servicio.trabajador_id == trabajador.id, Servicio.persona_id.isnot(None))
        }
        mis_empresa_ids = {
            s.empresa_id
            for s in db.query(Servicio).filter(Servicio.trabajador_id == trabajador.id, Servicio.empresa_id.isnot(None))
        }

    out = []
    personas = db.query(Persona).all()
    empresas = db.query(Empresa).all()
    for p in personas:
        if mis_persona_ids is not None and p.id not in mis_persona_ids:
            continue
        if q and q.lower() not in p.nombre.lower():
            continue
        ultimo = (
            db.query(Servicio)
            .filter(Servicio.persona_id == p.id)
            .order_by(Servicio.fecha.desc())
            .first()
        )
        out.append({
            "tipo": "persona", "id": p.id, "nombre": p.nombre, "telefono": p.telefono,
            "actividad": p.actividad,
            "ultimo_servicio": f"{ultimo.tipo} · {ultimo.fecha.strftime('%m/%d/%Y')}" if ultimo else None,
        })
    for e in empresas:
        if mis_empresa_ids is not None and e.id not in mis_empresa_ids:
            continue
        if q and q.lower() not in e.nombre.lower():
            continue
        ultimo = (
            db.query(Servicio)
            .filter(Servicio.empresa_id == e.id)
            .order_by(Servicio.fecha.desc())
            .first()
        )
        out.append({
            "tipo": "empresa", "id": e.id, "nombre": e.nombre, "giro": e.giro,
            "actividad": e.actividad,
            "ultimo_servicio": f"{ultimo.tipo} · {ultimo.fecha.strftime('%m/%d/%Y')}" if ultimo else None,
        })
    return out


@app.get("/api/clientes/buscar")
def buscar_cualquier_cliente(q: str = "", db: Session = Depends(get_db), _=Depends(current_trabajador)):
    """Búsqueda SIN filtrar por 'mis clientes' — para poder asignar un servicio nuevo a
    un cliente que ya existe pero que atendió alguien más (ej. un referido interno).
    Devuelve solo nombre/tipo/id, no datos sensibles."""
    out = []
    ql = q.lower().strip()
    personas = db.query(Persona).all()
    empresas = db.query(Empresa).all()
    for p in personas:
        if ql and ql not in p.nombre.lower():
            continue
        out.append({"tipo": "persona", "id": p.id, "nombre": p.nombre})
    for e in empresas:
        if ql and ql not in e.nombre.lower():
            continue
        out.append({"tipo": "empresa", "id": e.id, "nombre": e.nombre})
    return sorted(out, key=lambda x: x["nombre"])[:500]


@app.post("/api/clientes")
def crear_cliente(body: ClienteIn, db: Session = Depends(get_db), _=Depends(current_trabajador)):
    if body.tipo == "persona":
        p = Persona(
            nombre=body.nombre, telefono=body.telefono, correo=body.correo,
            ssn_encrypted=encrypt_ssn(body.ssn_full) if body.ssn_full else None,
            ssn_last4=body.ssn_full[-4:] if body.ssn_full else None,
        )
        db.add(p)
        db.commit()
        db.refresh(p)
        return {"tipo": "persona", "id": p.id, "nombre": p.nombre}
    elif body.tipo == "empresa":
        e = Empresa(nombre=body.nombre, telefono=body.telefono, correo=body.correo, giro=body.giro, ein=body.ein)
        db.add(e)
        db.commit()
        db.refresh(e)
        return {"tipo": "empresa", "id": e.id, "nombre": e.nombre}
    raise HTTPException(status_code=400, detail="tipo debe ser 'persona' o 'empresa'")


@app.get("/api/clientes/{tipo}/{cliente_id}")
def perfil_cliente(tipo: str, cliente_id: int, db: Session = Depends(get_db), _=Depends(current_trabajador)):
    if tipo == "persona":
        c = db.get(Persona, cliente_id)
        if not c:
            raise HTTPException(status_code=404)
        return {
            "tipo": "persona", "id": c.id, "nombre": c.nombre, "telefono": c.telefono,
            "correo": c.correo, "ssn_last4": c.ssn_last4, "actividad": c.actividad,
            "ghl_contact_id": c.ghl_contact_id, "drive_folder_id": c.drive_folder_id,
        }
    elif tipo == "empresa":
        c = db.get(Empresa, cliente_id)
        if not c:
            raise HTTPException(status_code=404)
        return {
            "tipo": "empresa", "id": c.id, "nombre": c.nombre, "giro": c.giro, "ein": c.ein,
            "telefono": c.telefono, "correo": c.correo, "actividad": c.actividad,
            "ghl_contact_id": c.ghl_contact_id, "drive_folder_id": c.drive_folder_id,
        }
    raise HTTPException(status_code=400)


@app.get("/api/clientes/persona/{persona_id}/ssn")
def revelar_ssn(persona_id: int, db: Session = Depends(get_db), trabajador: Trabajador = Depends(current_trabajador)):
    """Revela el SSN completo. En producción, este evento debe quedar en un log de acceso."""
    p = db.get(Persona, persona_id)
    if not p:
        raise HTTPException(status_code=404)
    print(f"[log de acceso] SSN de persona {persona_id} revelado por {trabajador.correo} a las {datetime.utcnow()}")
    return {"ssn_full": decrypt_ssn(p.ssn_encrypted)}


@app.get("/api/clientes/{tipo}/{cliente_id}/archivos")
def listar_archivos_cliente(tipo: str, cliente_id: int, db: Session = Depends(get_db), _=Depends(current_trabajador)):
    col = Archivo.persona_id if tipo == "persona" else Archivo.empresa_id
    archivos = db.query(Archivo).filter(col == cliente_id).order_by(Archivo.creado_en.desc()).all()
    return [{"id": a.id, "nombre": a.nombre, "tipo": a.tipo, "drive_url": a.drive_url} for a in archivos]


def _get_cliente(tipo: str, cliente_id: int, db: Session):
    modelo = Persona if tipo == "persona" else Empresa
    cliente = db.get(modelo, cliente_id)
    if not cliente:
        raise HTTPException(status_code=404, detail="Cliente no encontrado")
    return cliente


@app.get("/api/clientes/{tipo}/{cliente_id}/documentos")
def listar_documentos_drive(
    tipo: str, cliente_id: int, folder_id: Optional[str] = None,
    db: Session = Depends(get_db), _=Depends(current_trabajador),
):
    """Lista en vivo los archivos dentro de la carpeta de Drive del cliente
    (viejos y nuevos conviven en la misma carpeta), o de una subcarpeta suya
    (ej. una carpeta de año) si se pasa folder_id."""
    cliente = _get_cliente(tipo, cliente_id, db)
    if folder_id:
        return {"tiene_carpeta": True, "archivos": drive.listar_archivos(folder_id)}
    if not cliente.drive_folder_id:
        return {"tiene_carpeta": False, "archivos": []}
    archivos = drive.listar_archivos(cliente.drive_folder_id)
    return {"tiene_carpeta": True, "archivos": archivos}


class CarpetaIn(BaseModel):
    nombre: str
    parent_folder_id: Optional[str] = None


@app.post("/api/clientes/{tipo}/{cliente_id}/carpetas")
def crear_carpeta_cliente_endpoint(
    tipo: str, cliente_id: int, body: CarpetaIn,
    db: Session = Depends(get_db), _=Depends(current_trabajador),
):
    """Subcarpeta dentro de la carpeta del cliente — ej. para separar
    documentos por año."""
    cliente = _get_cliente(tipo, cliente_id, db)
    if not cliente.drive_folder_id:
        cliente.drive_folder_id = drive.crear_carpeta_cliente(cliente.nombre, getattr(cliente, "ssn_last4", None))
        db.commit()
    parent = body.parent_folder_id or cliente.drive_folder_id
    carpeta = drive.crear_subcarpeta(parent, body.nombre)
    return carpeta


@app.post("/api/clientes/{tipo}/{cliente_id}/documentos")
async def subir_documento_drive(
    tipo: str, cliente_id: int, file: UploadFile = File(...), folder_id: Optional[str] = Form(None),
    db: Session = Depends(get_db), _=Depends(current_trabajador),
):
    cliente = _get_cliente(tipo, cliente_id, db)
    try:
        if not cliente.drive_folder_id:
            cliente.drive_folder_id = drive.crear_carpeta_cliente(cliente.nombre, getattr(cliente, "ssn_last4", None))
            db.commit()
        destino = folder_id or cliente.drive_folder_id
        contenido = await file.read()
        archivo = drive.subir_archivo(destino, file.filename, contenido, file.content_type)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"No se pudo subir a Drive: {exc}")
    return archivo


@app.post("/api/servicios/{servicio_id}/documentos")
async def subir_documento_servicio(
    servicio_id: int, file: UploadFile = File(...), categoria: str = Form("id"),  # "id" -> raíz | "forma" -> carpeta del año
    db: Session = Depends(get_db), _=Depends(current_trabajador),
):
    """Sube un documento ligado a un servicio directo a la carpeta de Drive
    del cliente. 'id' (identificaciones) va a la carpeta raíz del cliente;
    'forma' (formas fiscales) va a la subcarpeta del año del servicio,
    creada automáticamente si no existe."""
    s = db.get(Servicio, servicio_id)
    if not s:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    cliente = None
    if s.persona_id:
        cliente = db.get(Persona, s.persona_id)
    elif s.empresa_id:
        cliente = db.get(Empresa, s.empresa_id)
    if not cliente:
        raise HTTPException(status_code=400, detail="El servicio no tiene cliente asociado")
    try:
        if not cliente.drive_folder_id:
            cliente.drive_folder_id = drive.crear_carpeta_cliente(cliente.nombre, getattr(cliente, "ssn_last4", None))
            db.commit()
        destino = cliente.drive_folder_id
        if categoria == "forma":
            anio = (s.detalle or {}).get("anio")
            if not anio:
                raise HTTPException(status_code=400, detail="Este servicio no tiene año de servicio definido — no se puede archivar por año.")
            destino = drive.obtener_o_crear_subcarpeta(cliente.drive_folder_id, str(anio))
        contenido = await file.read()
        archivo = drive.subir_archivo(destino, file.filename, contenido, file.content_type)
    except HTTPException:
        raise
    except Exception as exc:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"No se pudo subir a Drive: {exc}")
    return archivo


@app.get("/api/documentos/{file_id}/contenido")
def contenido_documento_drive(file_id: str, _=Depends(current_trabajador)):
    """Descarga el archivo desde Drive con la cuenta de servicio y lo transmite
    al navegador — así se puede ver embebido en la plataforma sin que el
    archivo sea público ni depender del acceso de Drive del trabajador."""
    contenido, mimetype, nombre = drive.obtener_contenido(file_id)
    return StreamingResponse(
        io.BytesIO(contenido), media_type=mimetype,
        headers={"Content-Disposition": f'inline; filename="{nombre}"'},
    )


@app.delete("/api/documentos/{file_id}")
def eliminar_documento_drive(file_id: str, _=Depends(current_trabajador)):
    drive.eliminar_archivo(file_id)
    return {"ok": True}


@app.post("/api/_backfill_drive_folder_ids")
def backfill_drive_folder_ids(db: Session = Depends(get_db), _=Depends(require_admin)):
    """Rellena Persona/Empresa.drive_folder_id a partir de los Archivo ya
    asociados en el lote masivo (su drive_url apunta a la carpeta completa
    del cliente, no a un archivo individual). Endpoint temporal, se puede
    quitar después."""
    actualizados = 0
    for a in db.query(Archivo).filter(Archivo.drive_url.isnot(None)).all():
        folder_id = drive.extraer_folder_id(a.drive_url)
        if not folder_id:
            continue
        if a.persona_id:
            p = db.get(Persona, a.persona_id)
            if p and not p.drive_folder_id:
                p.drive_folder_id = folder_id
                actualizados += 1
        elif a.empresa_id:
            e = db.get(Empresa, a.empresa_id)
            if e and not e.drive_folder_id:
                e.drive_folder_id = folder_id
                actualizados += 1
    db.commit()
    return {"actualizados": actualizados}


class ArchivoIn(BaseModel):
    persona_id: Optional[int] = None
    empresa_id: Optional[int] = None
    servicio_id: Optional[int] = None
    nombre: str
    tipo: str = "archivo"  # archivo | foto
    drive_url: Optional[str] = None


@app.post("/api/archivos")
def crear_archivo(body: ArchivoIn, db: Session = Depends(get_db), trabajador: Trabajador = Depends(current_trabajador)):
    a = Archivo(
        persona_id=body.persona_id, empresa_id=body.empresa_id, servicio_id=body.servicio_id,
        nombre=body.nombre, tipo=body.tipo, drive_url=body.drive_url, subido_por_id=trabajador.id,
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    return {"id": a.id}


@app.delete("/api/archivos/{archivo_id}")
def eliminar_archivo(archivo_id: int, db: Session = Depends(get_db), _=Depends(current_trabajador)):
    a = db.get(Archivo, archivo_id)
    if not a:
        raise HTTPException(status_code=404)
    db.delete(a)
    db.commit()
    return {"ok": True}


@app.get("/api/clientes/{tipo}/{cliente_id}/notas")
def listar_notas_cliente(tipo: str, cliente_id: int, db: Session = Depends(get_db), _=Depends(current_trabajador)):
    col = NotaCliente.persona_id if tipo == "persona" else NotaCliente.empresa_id
    notas = db.query(NotaCliente).filter(col == cliente_id).order_by(NotaCliente.creado_en.desc()).all()
    return [
        {"id": n.id, "texto": n.texto, "trabajador": n.trabajador.nombre, "creado_en": n.creado_en}
        for n in notas
    ]


class NotaClienteIn(BaseModel):
    persona_id: Optional[int] = None
    empresa_id: Optional[int] = None
    texto: str


@app.post("/api/notas")
def crear_nota(body: NotaClienteIn, db: Session = Depends(get_db), trabajador: Trabajador = Depends(current_trabajador)):
    if not body.persona_id and not body.empresa_id:
        raise HTTPException(status_code=400, detail="Falta cliente (persona_id o empresa_id).")
    n = NotaCliente(persona_id=body.persona_id, empresa_id=body.empresa_id, texto=body.texto, trabajador_id=trabajador.id)
    db.add(n)
    db.commit()
    db.refresh(n)
    return {"id": n.id}


@app.delete("/api/notas/{nota_id}")
def eliminar_nota(nota_id: int, db: Session = Depends(get_db), _=Depends(current_trabajador)):
    n = db.get(NotaCliente, nota_id)
    if not n:
        raise HTTPException(status_code=404)
    db.delete(n)
    db.commit()
    return {"ok": True}


@app.get("/api/clientes/{tipo}/{cliente_id}/servicios")
def historial_servicios(
    tipo: str, cliente_id: int, db: Session = Depends(get_db), trabajador: Trabajador = Depends(current_trabajador)
):
    """Admin ve todo el historial. Un trabajador normal solo ve sus propios servicios
    con este cliente — aunque el cliente tenga servicios de otros compañeros."""
    col = Servicio.persona_id if tipo == "persona" else Servicio.empresa_id
    query = db.query(Servicio).filter(col == cliente_id)
    if trabajador.rol != RolUsuario.admin:
        query = query.filter(Servicio.trabajador_id == trabajador.id)
    servicios = query.order_by(Servicio.fecha.desc()).all()
    out = []
    for s in servicios:
        comisiones = [
            {"trabajador": c.trabajador.nombre, "monto": float(c.monto), "estado": c.estado.value}
            for c in s.comisiones
        ]
        out.append({
            "id": s.id, "tipo": s.tipo, "linea": linea_de(s.tipo), "fecha": s.fecha,
            "trabajador": s.trabajador.nombre, "cobro": float(s.cobro), "metodo_pago": s.metodo_pago,
            "estatus": s.estatus, "notas": s.notas, "detalle": s.detalle or {}, "comisiones": comisiones,
        })
    return out


# ---------- "mis servicios" (dashboard personal de un trabajador) ----------

@app.get("/api/servicios/mios")
def mis_servicios(db: Session = Depends(get_db), trabajador: Trabajador = Depends(current_trabajador)):
    servicios = (
        db.query(Servicio)
        .filter(Servicio.trabajador_id == trabajador.id)
        .order_by(Servicio.fecha.desc())
        .all()
    )
    out = []
    for s in servicios:
        cliente = None
        if s.persona_id:
            p = db.get(Persona, s.persona_id)
            cliente = p.nombre if p else None
        elif s.empresa_id:
            e = db.get(Empresa, s.empresa_id)
            cliente = e.nombre if e else None
        mi_comision = next((c for c in s.comisiones if c.trabajador_id == trabajador.id), None)
        out.append({
            "id": s.id, "tipo": s.tipo, "fecha": s.fecha, "cobro": float(s.cobro),
            "estatus": s.estatus, "cliente": cliente,
            "comision_estado": mi_comision.estado.value if mi_comision else None,
            "comision_monto": float(mi_comision.monto) if mi_comision else None,
        })
    return out


# ---------- servicios ----------

class ServicioIn(BaseModel):
    persona_id: Optional[int] = None
    empresa_id: Optional[int] = None
    tipo: str
    fecha: date
    cobro: float = 0
    metodo_pago: Optional[str] = None
    estatus: str = "Pendiente de pago"
    notas: Optional[str] = None
    detalle: dict = {}
    trabajador_id: Optional[int] = None  # solo admin puede fijarlo distinto a sí mismo


def _comision_auto(trabajador: Trabajador, tipo: str, cobro: float) -> float:
    """Comisión calculada del % configurado del trabajador para este tipo — no se
    captura a mano en el servicio, se edita configurando el % en Trabajadores."""
    cfg = next((c for c in (trabajador.config_servicios or []) if c.get("tipo") == tipo), None)
    return round(cobro * cfg["pct"] / 100, 2) if cfg else 0.0


@app.get("/api/catalogo/servicio-tipos")
def catalogo_servicio_tipos(trabajador: Trabajador = Depends(current_trabajador)):
    """Tipos de servicio permitidos para el trabajador en sesión — admin ve todos."""
    if trabajador.rol == RolUsuario.admin:
        return list(SERVICE_TYPES.keys())
    permitidos = [c["tipo"] for c in (trabajador.config_servicios or [])]
    return [t for t in SERVICE_TYPES if t in permitidos]


@app.post("/api/servicios")
def crear_servicio(body: ServicioIn, db: Session = Depends(get_db), trabajador: Trabajador = Depends(current_trabajador)):
    if not body.persona_id and not body.empresa_id:
        raise HTTPException(status_code=400, detail="Falta cliente (persona_id o empresa_id).")
    permitidos = None if trabajador.rol == RolUsuario.admin else [c["tipo"] for c in (trabajador.config_servicios or [])]
    if permitidos is not None and body.tipo not in permitidos:
        raise HTTPException(status_code=403, detail="No tienes ese tipo de servicio configurado.")
    trabajador_id = body.trabajador_id if trabajador.rol == RolUsuario.admin and body.trabajador_id else trabajador.id

    s = Servicio(
        persona_id=body.persona_id, empresa_id=body.empresa_id, tipo=body.tipo, fecha=body.fecha,
        trabajador_id=trabajador_id, cobro=body.cobro, metodo_pago=body.metodo_pago,
        estatus=body.estatus, notas=body.notas, detalle=body.detalle,
    )
    db.add(s)
    db.flush()
    asignado = db.get(Trabajador, trabajador_id)
    monto = _comision_auto(asignado, body.tipo, body.cobro)
    db.add(Comision(servicio_id=s.id, trabajador_id=trabajador_id, rol="Responsable", monto=monto))
    db.commit()
    db.refresh(s)
    return {"id": s.id}


@app.get("/api/servicios/{servicio_id}")
def obtener_servicio(
    servicio_id: int, db: Session = Depends(get_db), trabajador: Trabajador = Depends(current_trabajador)
):
    """Detalle completo de un servicio, para prellenar el formulario de edición."""
    s = db.get(Servicio, servicio_id)
    if not s:
        raise HTTPException(status_code=404)
    if trabajador.rol != RolUsuario.admin and s.trabajador_id != trabajador.id:
        raise HTTPException(status_code=403, detail="Solo puedes ver/editar tus propios servicios.")
    return {
        "id": s.id, "persona_id": s.persona_id, "empresa_id": s.empresa_id, "tipo": s.tipo,
        "fecha": s.fecha, "cobro": float(s.cobro), "metodo_pago": s.metodo_pago, "estatus": s.estatus,
        "notas": s.notas, "detalle": s.detalle, "trabajador_id": s.trabajador_id, "trabajador": s.trabajador.nombre,
        "comisiones": [
            {"trabajador_id": c.trabajador_id, "trabajador": c.trabajador.nombre, "rol": c.rol, "monto": float(c.monto), "estado": c.estado.value}
            for c in s.comisiones
        ],
        "archivos": [{"id": a.id, "nombre": a.nombre, "tipo": a.tipo} for a in s.archivos],
    }


@app.delete("/api/servicios/{servicio_id}")
def eliminar_servicio(
    servicio_id: int, db: Session = Depends(get_db), trabajador: Trabajador = Depends(current_trabajador)
):
    s = db.get(Servicio, servicio_id)
    if not s:
        raise HTTPException(status_code=404)
    if trabajador.rol != RolUsuario.admin and s.trabajador_id != trabajador.id:
        raise HTTPException(status_code=403, detail="Solo puedes eliminar tus propios servicios.")
    db.delete(s)
    db.commit()
    return {"ok": True}


@app.put("/api/servicios/{servicio_id}")
def editar_servicio(
    servicio_id: int, body: ServicioIn, db: Session = Depends(get_db), trabajador: Trabajador = Depends(current_trabajador)
):
    s = db.get(Servicio, servicio_id)
    if not s:
        raise HTTPException(status_code=404)
    if trabajador.rol != RolUsuario.admin and s.trabajador_id != trabajador.id:
        raise HTTPException(status_code=403, detail="Solo puedes editar tus propios servicios.")
    nuevo_trabajador_id = body.trabajador_id if (trabajador.rol == RolUsuario.admin and body.trabajador_id) else s.trabajador_id
    s.tipo, s.fecha, s.cobro = body.tipo, body.fecha, body.cobro
    s.metodo_pago, s.estatus, s.notas, s.detalle = body.metodo_pago, body.estatus, body.notas, body.detalle
    s.trabajador_id = nuevo_trabajador_id

    comision = db.query(Comision).filter(Comision.servicio_id == s.id).first()
    if comision and comision.estado == EstadoComision.pagada:
        pass  # ya pagada — no se recalcula ni se reasigna aunque cambie el servicio
    else:
        asignado = db.get(Trabajador, nuevo_trabajador_id)
        monto = _comision_auto(asignado, body.tipo, body.cobro)
        if comision:
            comision.trabajador_id = nuevo_trabajador_id
            comision.monto = monto
        else:
            db.add(Comision(servicio_id=s.id, trabajador_id=nuevo_trabajador_id, rol="Responsable", monto=monto))
    db.commit()
    return {"ok": True}


# ---------- trabajadores (admin) ----------

PERMISOS_VALIDOS = ["nomina", "estadisticas", "trabajadores"]


class TrabajadorIn(BaseModel):
    nombre: str
    correo: str
    rol: str = "trabajador"
    config_servicios: list[dict] = []
    password: Optional[str] = None  # si viene, se guarda (hasheada) o se actualiza
    tipo_pago: str = "comision"  # comision | sueldo | mixto
    sueldo_fijo: float = 0  # semanal — solo aplica si tipo_pago es sueldo/mixto
    activo: bool = True
    permisos: list[str] = []  # extra además de Servicios+Clientes: nomina | estadisticas | trabajadores


@app.get("/api/trabajadores")
def listar_trabajadores(db: Session = Depends(get_db), _=Depends(require_permiso("trabajadores"))):
    return [
        {
            "id": t.id, "nombre": t.nombre, "correo": t.correo, "rol": t.rol.value,
            "config_servicios": t.config_servicios, "tipo_pago": t.tipo_pago,
            "sueldo_fijo": float(t.sueldo_fijo or 0), "drive_folder_id": t.drive_folder_id,
            "activo": t.activo, "permisos": t.permisos or [],
        }
        for t in db.query(Trabajador).order_by(Trabajador.activo.desc(), Trabajador.nombre).all()
    ]


@app.get("/api/trabajadores/basico")
def listar_trabajadores_basico(db: Session = Depends(get_db), _=Depends(current_trabajador)):
    """Nombre + id de cada trabajador activo, para armar selects de comisión —
    disponible para cualquier sesión válida, no solo Admin (sin correo ni config)."""
    return [
        {"id": t.id, "nombre": t.nombre}
        for t in db.query(Trabajador).filter(Trabajador.activo).order_by(Trabajador.nombre).all()
    ]


@app.post("/api/trabajadores")
def crear_trabajador(body: TrabajadorIn, db: Session = Depends(get_db), _=Depends(require_permiso("trabajadores"))):
    permisos = [p for p in body.permisos if p in PERMISOS_VALIDOS]
    t = Trabajador(
        nombre=body.nombre, correo=body.correo, rol=RolUsuario(body.rol), config_servicios=body.config_servicios,
        password_hash=hash_password(body.password) if body.password else None,
        tipo_pago=body.tipo_pago, sueldo_fijo=body.sueldo_fijo, activo=body.activo, permisos=permisos,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    if drive.disponible():
        try:
            t.drive_folder_id = drive.crear_carpeta_trabajador(t.nombre)
            db.commit()
        except Exception:
            pass  # no bloquea el alta del trabajador — se puede reintentar con el backfill
    return {"id": t.id}


@app.put("/api/trabajadores/{trabajador_id}")
def editar_trabajador(trabajador_id: int, body: TrabajadorIn, db: Session = Depends(get_db), _=Depends(require_permiso("trabajadores"))):
    t = db.get(Trabajador, trabajador_id)
    if not t:
        raise HTTPException(status_code=404)
    t.nombre, t.correo, t.config_servicios = body.nombre, body.correo, body.config_servicios
    t.tipo_pago, t.sueldo_fijo, t.activo = body.tipo_pago, body.sueldo_fijo, body.activo
    t.permisos = [p for p in body.permisos if p in PERMISOS_VALIDOS]
    if body.password:
        t.password_hash = hash_password(body.password)
    db.commit()
    return {"ok": True}


@app.post("/api/_backfill_trabajador_drive_folders")
def backfill_trabajador_drive_folders(db: Session = Depends(get_db), _=Depends(require_admin)):
    """Crea la carpeta de Drive a los trabajadores existentes que no la
    tengan todavía. Endpoint temporal, se puede quitar después."""
    creadas = []
    for t in db.query(Trabajador).filter(Trabajador.drive_folder_id.is_(None)).all():
        t.drive_folder_id = drive.crear_carpeta_trabajador(t.nombre)
        creadas.append({"id": t.id, "nombre": t.nombre})
    db.commit()
    return {"creadas": creadas}


@app.delete("/api/trabajadores/{trabajador_id}")
def eliminar_trabajador(trabajador_id: int, db: Session = Depends(get_db), _=Depends(require_permiso("trabajadores"))):
    t = db.get(Trabajador, trabajador_id)
    if not t:
        raise HTTPException(status_code=404)
    t.activo = False  # baja lógica — sus servicios/comisiones históricas se quedan intactas
    db.commit()
    return {"ok": True}


# ---------- nómina (admin) ----------
#
# Regla de oro: una comisión solo cuenta como "debida" al trabajador cuando el
# SERVICIO que la generó tiene estatus "Pagado" o "Banco Pagado" (el cliente ya
# pagó). Antes de eso existe (Comision.estado sigue "pendiente") pero no se
# suma al saldo de nómina — así nunca se le paga a alguien por un servicio que
# el cliente todavía no ha pagado.

def _nombre_cliente_servicio(db: Session, s: Servicio) -> str:
    if s.persona_id:
        p = db.get(Persona, s.persona_id)
        return p.nombre if p else "—"
    if s.empresa_id:
        e = db.get(Empresa, s.empresa_id)
        return e.nombre if e else "—"
    return "—"


def _comisiones_elegibles(db: Session, trabajador_id: int):
    return (
        db.query(Comision)
        .join(Servicio, Servicio.id == Comision.servicio_id)
        .filter(
            Comision.trabajador_id == trabajador_id,
            Comision.estado == EstadoComision.pendiente,
            Servicio.estatus.in_(ESTATUS_LIBERA_COMISION),
        )
        .all()
    )


def _comisiones_en_espera(db: Session, trabajador_id: int):
    """Comisiones ya generadas pero cuyo servicio todavía no está pagado por
    el cliente — informativo, no cuenta para el saldo a pagar."""
    return (
        db.query(Comision)
        .join(Servicio, Servicio.id == Comision.servicio_id)
        .filter(
            Comision.trabajador_id == trabajador_id,
            Comision.estado == EstadoComision.pendiente,
            Servicio.estatus.notin_(ESTATUS_LIBERA_COMISION),
        )
        .all()
    )


TRABAJADOR_PLACEHOLDER = "Histórico / Sin preparador"  # no es un trabajador real — se usa en servicios sin dueño


@app.get("/api/nomina/resumen")
def nomina_resumen(db: Session = Depends(get_db), _=Depends(require_permiso("nomina"))):
    out = []
    q = db.query(Trabajador).filter(Trabajador.activo, Trabajador.nombre != TRABAJADOR_PLACEHOLDER)
    for t in q.order_by(Trabajador.nombre).all():
        elegibles = _comisiones_elegibles(db, t.id)
        en_espera = _comisiones_en_espera(db, t.id)
        sueldo = float(t.sueldo_fijo or 0) if t.tipo_pago in ("sueldo", "mixto") else 0
        monto_comisiones = sum(float(c.monto) for c in elegibles)
        ultimo_pago = (
            db.query(PagoNomina)
            .filter(PagoNomina.trabajador_id == t.id, PagoNomina.tipo == "pago")
            .order_by(PagoNomina.fecha.desc())
            .first()
        )
        out.append({
            "trabajador_id": t.id, "trabajador": t.nombre, "tipo_pago": t.tipo_pago,
            "sueldo_sugerido": sueldo,
            "comisiones_monto": monto_comisiones, "comisiones_n": len(elegibles),
            "en_espera_monto": sum(float(c.monto) for c in en_espera), "en_espera_n": len(en_espera),
            "total_sugerido": sueldo + monto_comisiones,
            "ultimo_pago": ultimo_pago.fecha.isoformat() if ultimo_pago else None,
        })
    return out


@app.get("/api/nomina/{trabajador_id}/detalle")
def nomina_detalle(trabajador_id: int, db: Session = Depends(get_db), _=Depends(require_permiso("nomina"))):
    t = db.get(Trabajador, trabajador_id)
    if not t:
        raise HTTPException(status_code=404)

    def _fila(c):
        s = c.servicio
        return {
            "comision_id": c.id, "servicio_id": s.id, "fecha": s.fecha.isoformat(),
            "cliente": _nombre_cliente_servicio(db, s), "tipo_servicio": s.tipo,
            "cobro": float(s.cobro), "estatus_servicio": s.estatus, "monto": float(c.monto),
        }

    return {
        "trabajador": t.nombre, "tipo_pago": t.tipo_pago, "sueldo_fijo": float(t.sueldo_fijo or 0),
        "elegibles": [_fila(c) for c in _comisiones_elegibles(db, trabajador_id)],
        "en_espera": [_fila(c) for c in _comisiones_en_espera(db, trabajador_id)],
    }


class PagoIn(BaseModel):
    trabajador_id: int
    sueldo: float = 0
    extra_monto: float = 0
    extra_concepto: Optional[str] = None
    concepto: Optional[str] = None


@app.post("/api/nomina/pagar")
def marcar_pagado(body: PagoIn, db: Session = Depends(get_db), trabajador_admin: Trabajador = Depends(require_permiso("nomina"))):
    t = db.get(Trabajador, body.trabajador_id)
    if not t:
        raise HTTPException(status_code=404, detail="Trabajador no encontrado")
    elegibles = _comisiones_elegibles(db, body.trabajador_id)
    monto_comisiones = sum(float(c.monto) for c in elegibles)
    total = round(body.sueldo + monto_comisiones + body.extra_monto, 2)
    if total <= 0:
        raise HTTPException(status_code=400, detail="El total a pagar debe ser mayor a $0.")

    hoy = date.today()
    pago = PagoNomina(
        fecha=hoy, trabajador_id=t.id, monto=total, n_servicios=len(elegibles),
        tipo="pago", sueldo_incluido=body.sueldo, comisiones_incluidas=monto_comisiones,
        extra_monto=body.extra_monto, extra_concepto=body.extra_concepto, concepto=body.concepto,
    )
    db.add(pago)
    db.flush()  # necesitamos pago.id antes de vincular las comisiones
    detalle_comisiones = []
    for c in elegibles:
        c.estado = EstadoComision.pagada
        c.fecha_pago = hoy
        c.pago_nomina_id = pago.id
        s = c.servicio
        detalle_comisiones.append({
            "fecha": s.fecha.strftime("%m/%d/%Y"), "cliente": _nombre_cliente_servicio(db, s),
            "tipo_servicio": s.tipo, "monto": float(c.monto),
        })
    db.commit()
    db.refresh(pago)

    if drive.disponible():
        try:
            pdf_bytes = recibos.generar_recibo_pdf(pago, t, detalle_comisiones)
            if not t.drive_folder_id:
                t.drive_folder_id = drive.crear_carpeta_trabajador(t.nombre)
            archivo = drive.subir_archivo(
                t.drive_folder_id, f"Recibo {hoy.strftime('%Y-%m-%d')} - {t.nombre}.pdf",
                pdf_bytes, "application/pdf",
            )
            pago.drive_url = archivo.get("webViewLink")
            db.commit()
        except Exception as exc:
            print(f"[nomina] no se pudo generar/subir el recibo del pago {pago.id}: {exc}")

    return {"ok": True, "pago_id": pago.id, "monto": total, "n_servicios": len(elegibles), "drive_url": pago.drive_url}


@app.get("/api/nomina/{trabajador_id}/historial")
def nomina_historial_trabajador(
    trabajador_id: int, fecha_inicio: Optional[date] = None, fecha_fin: Optional[date] = None,
    db: Session = Depends(get_db), _=Depends(require_permiso("nomina")),
):
    q = db.query(PagoNomina).filter(PagoNomina.trabajador_id == trabajador_id, PagoNomina.tipo == "pago")
    if fecha_inicio:
        q = q.filter(PagoNomina.fecha >= fecha_inicio)
    if fecha_fin:
        q = q.filter(PagoNomina.fecha <= fecha_fin)
    pagos = (
        q
        .order_by(PagoNomina.fecha.desc(), PagoNomina.id.desc())
        .all()
    )
    out = []
    for p in pagos:
        servicios = []
        for c in p.comisiones:
            s = c.servicio
            servicios.append({
                "fecha": s.fecha.strftime("%m/%d/%Y"), "cliente": _nombre_cliente_servicio(db, s),
                "tipo_servicio": s.tipo, "monto": float(c.monto),
            })
        out.append({
            "id": p.id, "fecha": p.fecha.isoformat(), "tipo": p.tipo, "monto": float(p.monto),
            "sueldo_incluido": float(p.sueldo_incluido or 0), "comisiones_incluidas": float(p.comisiones_incluidas or 0),
            "extra_monto": float(p.extra_monto or 0), "extra_concepto": p.extra_concepto,
            "concepto": p.concepto, "n_servicios": p.n_servicios, "drive_url": p.drive_url,
            "servicios": servicios,
        })
    return out


@app.get("/api/nomina/dashboard")
def nomina_dashboard(
    fecha_inicio: Optional[date] = None, fecha_fin: Optional[date] = None,
    db: Session = Depends(get_db), _=Depends(require_permiso("nomina")),
):
    q = db.query(PagoNomina).filter(PagoNomina.tipo == "pago")
    if fecha_inicio:
        q = q.filter(PagoNomina.fecha >= fecha_inicio)
    if fecha_fin:
        q = q.filter(PagoNomina.fecha <= fecha_fin)
    pagos = q.order_by(PagoNomina.fecha).all()

    por_trabajador = {}
    semanas = {}
    total_general = 0.0
    for p in pagos:
        monto = float(p.monto)
        total_general += monto
        nombre = p.trabajador.nombre
        por_trabajador.setdefault(nombre, 0.0)
        por_trabajador[nombre] += monto
        inicio_semana = p.fecha - timedelta(days=p.fecha.weekday())  # lunes de esa semana
        key = inicio_semana.isoformat()
        semanas.setdefault(key, {})
        semanas[key].setdefault(nombre, 0.0)
        semanas[key][nombre] += monto

    por_semana = []
    for key in sorted(semanas.keys()):
        por_trab = semanas[key]
        por_semana.append({
            "semana_inicio": key, "total": sum(por_trab.values()),
            "por_trabajador": [{"trabajador": k, "monto": v} for k, v in sorted(por_trab.items())],
        })

    return {
        "total_general": total_general,
        "por_trabajador": [{"trabajador": k, "monto": v} for k, v in sorted(por_trabajador.items())],
        "por_semana": por_semana,
    }


@app.get("/api/config/{clave}")
def obtener_config(clave: str, db: Session = Depends(get_db), _=Depends(require_permiso("nomina"))):
    c = db.get(Configuracion, clave)
    return {"clave": clave, "valor": c.valor if c else None}


class ConfigIn(BaseModel):
    valor: str


@app.put("/api/config/{clave}")
def guardar_config(clave: str, body: ConfigIn, db: Session = Depends(get_db), _=Depends(require_permiso("nomina"))):
    c = db.get(Configuracion, clave)
    if c:
        c.valor = body.valor
    else:
        db.add(Configuracion(clave=clave, valor=body.valor))
    db.commit()
    return {"ok": True}


@app.post("/api/_reset_saldos_nomina")
def reset_saldos_nomina(db: Session = Depends(get_db), _=Depends(require_admin)):
    """Pone en $0 el saldo pendiente de comisión de todos los trabajadores,
    dejando un registro de ajuste auditable por cada uno (no es un pago real).
    Pensado para correrse UNA vez al activar el nuevo control de nómina.
    Endpoint temporal, se puede quitar después."""
    hoy = date.today()
    ajustes = []
    for t in db.query(Trabajador).filter(Trabajador.activo).all():
        pendientes = db.query(Comision).filter(
            Comision.trabajador_id == t.id, Comision.estado == EstadoComision.pendiente,
        ).all()
        if not pendientes:
            continue
        monto = sum(float(c.monto) for c in pendientes)
        pago = PagoNomina(
            fecha=hoy, trabajador_id=t.id, monto=monto, n_servicios=len(pendientes),
            tipo="ajuste_inicial", comisiones_incluidas=monto,
            concepto="Ajuste inicial — saldo puesto en $0 al activar el nuevo control de nómina "
                     "(comisiones de servicios registrados antes de este cambio). No representa una "
                     "transferencia de dinero real.",
        )
        db.add(pago)
        db.flush()
        for c in pendientes:
            c.estado = EstadoComision.pagada
            c.fecha_pago = hoy
            c.pago_nomina_id = pago.id
        ajustes.append({"trabajador": t.nombre, "monto": monto, "n_servicios": len(pendientes)})
    db.commit()
    return {"ajustes": ajustes}


class PagoHistoricoIn(BaseModel):
    trabajador_id: int
    fecha: date
    sueldo: float = 0
    comision: float = 0
    extra: float = 0
    extra_concepto: Optional[str] = None
    concepto: Optional[str] = None


class PagoHistoricoBatchIn(BaseModel):
    pagos: list[PagoHistoricoIn]


@app.post("/api/_importar_pagos_historicos")
def importar_pagos_historicos(body: PagoHistoricoBatchIn, db: Session = Depends(get_db), _=Depends(require_admin)):
    """Carga en bloque pagos de nómina reales de antes de esta plataforma
    (de las hojas de cálculo históricas) como registros independientes —
    no genera recibo ni se vincula a Comision (no hay servicio individual
    detrás de un pago histórico de nómina en bloque). Endpoint temporal."""
    creados = 0
    for p in body.pagos:
        monto = round(p.sueldo + p.comision + p.extra, 2)
        if monto <= 0:
            continue
        db.add(PagoNomina(
            fecha=p.fecha, trabajador_id=p.trabajador_id, monto=monto, n_servicios=0,
            tipo="pago", sueldo_incluido=p.sueldo, comisiones_incluidas=p.comision,
            extra_monto=p.extra, extra_concepto=p.extra_concepto, concepto=p.concepto,
        ))
        creados += 1
    db.commit()
    return {"creados": creados}


# ---------- estadísticas (admin) ----------
#
# Reemplaza el viejo sistema de "temporadas" (nombres auto-generados a partir
# del año de la fecha, que se corrompían con typos de fecha en datos viejos —
# ej. "Temporada 203"). Ahora todo se filtra directo por rango de fecha del
# servicio, sin inventar nombres de periodo.

@app.get("/api/estadisticas")
def estadisticas(
    fecha_inicio: Optional[date] = None, fecha_fin: Optional[date] = None,
    tipo: Optional[str] = None, db: Session = Depends(get_db), _=Depends(require_permiso("estadisticas")),
):
    q = db.query(Servicio)
    if fecha_inicio:
        q = q.filter(Servicio.fecha >= fecha_inicio)
    if fecha_fin:
        q = q.filter(Servicio.fecha <= fecha_fin)
    if tipo:
        q = q.filter(Servicio.tipo == tipo)
    servicios = q.all()

    trabajador_nombres = {t.id: t.nombre for t in db.query(Trabajador).all()}
    ingresos = 0.0
    por_linea, por_trabajador, por_tipo_cita, por_metodo_pago, por_estatus = {}, {}, {}, {}, {}
    for s in servicios:
        cobro = float(s.cobro)
        ingresos += cobro
        linea = linea_de(s.tipo)
        por_linea[linea] = por_linea.get(linea, 0) + cobro
        tn = trabajador_nombres.get(s.trabajador_id, "—")
        por_trabajador[tn] = por_trabajador.get(tn, 0) + cobro
        cita = (s.detalle or {}).get("tipoCita") or "Sin especificar"
        por_tipo_cita[cita] = por_tipo_cita.get(cita, 0) + 1
        metodo = s.metodo_pago or "Sin especificar"
        por_metodo_pago[metodo] = por_metodo_pago.get(metodo, 0) + 1
        por_estatus[s.estatus] = por_estatus.get(s.estatus, 0) + 1

    servicio_ids = {s.id for s in servicios}
    comisiones = (
        db.query(Comision).filter(Comision.servicio_id.in_(servicio_ids)).all() if servicio_ids else []
    )
    comisiones_pendientes = sum(float(c.monto) for c in comisiones if c.estado == EstadoComision.pendiente)
    comisiones_pagadas = sum(float(c.monto) for c in comisiones if c.estado == EstadoComision.pagada)

    return {
        "ingresos": ingresos,
        "servicios": len(servicios),
        "clientes_atendidos": len({(s.persona_id, s.empresa_id) for s in servicios}),
        "comisiones_pendientes": comisiones_pendientes,
        "comisiones_pagadas": comisiones_pagadas,
        "por_linea": por_linea,
        "por_trabajador": por_trabajador,
        "por_tipo_cita": por_tipo_cita,
        "por_metodo_pago": por_metodo_pago,
        "por_estatus": por_estatus,
    }


@app.get("/api/reportes/servicios")
def buscar_servicios(
    trabajador_id: Optional[int] = None, tipo: Optional[str] = None,
    fecha_inicio: Optional[date] = None, fecha_fin: Optional[date] = None,
    tipo_cita: Optional[str] = None, metodo_pago: Optional[str] = None,
    estatus: Optional[str] = None, estatus_comision: Optional[str] = None,  # pendiente | pagada
    db: Session = Depends(get_db), _=Depends(require_permiso("estadisticas")),
):
    """Filtros combinables para armar cualquier tabla de servicios (ej. 'Taxes
    1040 de Erik de tal fecha a tal fecha, de cita remota, pendientes de pago
    de comisión'). Alimenta la sección de filtros avanzados, exportable a CSV
    desde el frontend."""
    q = db.query(Servicio)
    if trabajador_id:
        q = q.filter(Servicio.trabajador_id == trabajador_id)
    if tipo:
        q = q.filter(Servicio.tipo == tipo)
    if fecha_inicio:
        q = q.filter(Servicio.fecha >= fecha_inicio)
    if fecha_fin:
        q = q.filter(Servicio.fecha <= fecha_fin)
    if metodo_pago:
        q = q.filter(Servicio.metodo_pago == metodo_pago)
    if estatus:
        q = q.filter(Servicio.estatus == estatus)
    servicios = q.order_by(Servicio.fecha.desc()).all()
    if tipo_cita:
        servicios = [s for s in servicios if (s.detalle or {}).get("tipoCita") == tipo_cita]

    trabajador_nombres = {t.id: t.nombre for t in db.query(Trabajador).all()}
    comisiones_by_servicio = {c.servicio_id: c for c in db.query(Comision).filter(
        Comision.servicio_id.in_([s.id for s in servicios])
    ).all()} if servicios else {}

    out = []
    for s in servicios:
        comision = comisiones_by_servicio.get(s.id)
        if estatus_comision and (not comision or comision.estado.value != estatus_comision):
            continue
        out.append({
            "id": s.id, "fecha": s.fecha.strftime("%m/%d/%Y"), "cliente": _nombre_cliente_servicio(db, s),
            "tipo": s.tipo, "trabajador": trabajador_nombres.get(s.trabajador_id, "—"),
            "cobro": float(s.cobro), "metodo_pago": s.metodo_pago, "estatus": s.estatus,
            "tipo_cita": (s.detalle or {}).get("tipoCita"),
            "comision_monto": float(comision.monto) if comision else 0,
            "comision_estado": comision.estado.value if comision else None,
        })
    return out


# ---------- frontend (debe ir al final: es un catch-all de "/") ----------

_frontend_dir = pathlib.Path(__file__).resolve().parent.parent / "frontend"
if _frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(_frontend_dir), html=True), name="frontend")
