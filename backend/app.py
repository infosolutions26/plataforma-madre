import os
import pathlib
from datetime import date, datetime
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from auth import current_trabajador, require_admin, router as auth_router
from catalogo import SERVICE_TYPES, linea_de
from database import Base, engine, encrypt_ssn, decrypt_ssn, get_db, hash_password
from models import (
    Archivo,
    Comision,
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

try:
    with engine.begin() as _conn:
        _conn.execute(text("ALTER TABLE trabajador ADD COLUMN password_hash VARCHAR(120)"))
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
    "banco": "Banco", "banco sta barbara": "Banco Sta Barbara",
    "cortesia / referidos": "Cortesía / Referidos", "cortesia/referidos": "Cortesía / Referidos",
    "cortesia": "Cortesía / Referidos",
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


# ---------- clientes ----------

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
            "ultimo_servicio": f"{ultimo.tipo} · {ultimo.fecha}" if ultimo else None,
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
            "ultimo_servicio": f"{ultimo.tipo} · {ultimo.fecha}" if ultimo else None,
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
            "ghl_contact_id": c.ghl_contact_id,
        }
    elif tipo == "empresa":
        c = db.get(Empresa, cliente_id)
        if not c:
            raise HTTPException(status_code=404)
        return {
            "tipo": "empresa", "id": c.id, "nombre": c.nombre, "giro": c.giro, "ein": c.ein,
            "telefono": c.telefono, "correo": c.correo, "actividad": c.actividad,
            "ghl_contact_id": c.ghl_contact_id,
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
    return [
        {
            "id": s.id, "tipo": s.tipo, "fecha": s.fecha, "trabajador": s.trabajador.nombre,
            "cobro": float(s.cobro), "estatus": s.estatus,
        }
        for s in servicios
    ]


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

class TrabajadorIn(BaseModel):
    nombre: str
    correo: str
    rol: str = "trabajador"
    config_servicios: list[dict] = []
    password: Optional[str] = None  # si viene, se guarda (hasheada) o se actualiza


@app.get("/api/trabajadores")
def listar_trabajadores(db: Session = Depends(get_db), _=Depends(require_admin)):
    return [
        {"id": t.id, "nombre": t.nombre, "correo": t.correo, "rol": t.rol.value, "config_servicios": t.config_servicios}
        for t in db.query(Trabajador).filter(Trabajador.activo).all()
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
def crear_trabajador(body: TrabajadorIn, db: Session = Depends(get_db), _=Depends(require_admin)):
    t = Trabajador(
        nombre=body.nombre, correo=body.correo, rol=RolUsuario(body.rol), config_servicios=body.config_servicios,
        password_hash=hash_password(body.password) if body.password else None,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return {"id": t.id}


@app.put("/api/trabajadores/{trabajador_id}")
def editar_trabajador(trabajador_id: int, body: TrabajadorIn, db: Session = Depends(get_db), _=Depends(require_admin)):
    t = db.get(Trabajador, trabajador_id)
    if not t:
        raise HTTPException(status_code=404)
    t.nombre, t.correo, t.config_servicios = body.nombre, body.correo, body.config_servicios
    if body.password:
        t.password_hash = hash_password(body.password)
    db.commit()
    return {"ok": True}


@app.delete("/api/trabajadores/{trabajador_id}")
def eliminar_trabajador(trabajador_id: int, db: Session = Depends(get_db), _=Depends(require_admin)):
    t = db.get(Trabajador, trabajador_id)
    if not t:
        raise HTTPException(status_code=404)
    t.activo = False  # baja lógica — sus servicios/comisiones históricas se quedan intactas
    db.commit()
    return {"ok": True}


# ---------- nómina (admin) ----------

@app.get("/api/nomina/pendientes")
def nomina_pendientes(db: Session = Depends(get_db), _=Depends(require_admin)):
    rows = (
        db.query(Trabajador.id, Trabajador.nombre, func.sum(Comision.monto), func.count(Comision.id))
        .join(Comision, Comision.trabajador_id == Trabajador.id)
        .filter(Comision.estado == EstadoComision.pendiente)
        .group_by(Trabajador.id)
        .all()
    )
    return [{"trabajador_id": r[0], "trabajador": r[1], "monto": float(r[2]), "n_servicios": r[3]} for r in rows]


class PagoIn(BaseModel):
    trabajador_id: int


@app.post("/api/nomina/pagar")
def marcar_pagado(body: PagoIn, db: Session = Depends(get_db), _=Depends(require_admin)):
    pendientes = (
        db.query(Comision)
        .filter(Comision.trabajador_id == body.trabajador_id, Comision.estado == EstadoComision.pendiente)
        .all()
    )
    if not pendientes:
        raise HTTPException(status_code=400, detail="No hay comisiones pendientes para este trabajador.")
    monto = sum(float(c.monto) for c in pendientes)
    hoy = date.today()
    for c in pendientes:
        c.estado = EstadoComision.pagada
        c.fecha_pago = hoy
    pago = PagoNomina(fecha=hoy, trabajador_id=body.trabajador_id, monto=monto, n_servicios=len(pendientes))
    db.add(pago)
    db.commit()
    return {"ok": True, "monto": monto, "n_servicios": len(pendientes)}


@app.get("/api/nomina/historial")
def nomina_historial(db: Session = Depends(get_db), _=Depends(require_admin)):
    pagos = db.query(PagoNomina).order_by(PagoNomina.fecha.desc()).all()
    return [
        {"fecha": p.fecha, "trabajador": p.trabajador.nombre, "monto": float(p.monto), "n_servicios": p.n_servicios}
        for p in pagos
    ]


# ---------- periodos / estadísticas (admin) ----------

class PeriodoIn(BaseModel):
    nombre: str
    desde: date
    hasta: date


@app.get("/api/periodos")
def listar_periodos(db: Session = Depends(get_db), _=Depends(require_admin)):
    return [{"nombre": p.nombre, "desde": p.desde, "hasta": p.hasta} for p in db.query(PeriodoCustom).all()]


@app.post("/api/periodos")
def crear_periodo(body: PeriodoIn, db: Session = Depends(get_db), _=Depends(require_admin)):
    p = PeriodoCustom(nombre=body.nombre, desde=body.desde, hasta=body.hasta)
    db.add(p)
    db.commit()
    return {"ok": True}


def _periodo_de(fecha: date, periodos_custom: list[PeriodoCustom]) -> str:
    for p in periodos_custom:
        if p.desde <= fecha <= p.hasta:
            return p.nombre
    return f"{'Temporada' if 1 <= fecha.month <= 4 else 'Post-temporada'} {fecha.year}"


@app.get("/api/periodos/detectados")
def periodos_detectados(db: Session = Depends(get_db), _=Depends(require_admin)):
    """Lista de periodos que realmente existen — las temporadas auto-calculadas
    (a partir de las fechas de los servicios) más las personalizadas. Para llenar
    el segmented-control de Estadísticas sin inventar temporadas vacías."""
    periodos_custom = db.query(PeriodoCustom).all()
    nombres = {p.nombre for p in periodos_custom}
    for (fecha,) in db.query(Servicio.fecha).all():
        nombres.add(_periodo_de(fecha, periodos_custom))
    return ["Todo"] + sorted(nombres, reverse=True)


@app.get("/api/estadisticas")
def estadisticas(periodo: str = "Todo", db: Session = Depends(get_db), _=Depends(require_admin)):
    periodos_custom = db.query(PeriodoCustom).all()
    servicios = db.query(Servicio).all()
    if periodo != "Todo":
        servicios = [s for s in servicios if _periodo_de(s.fecha, periodos_custom) == periodo]

    ingresos = sum(float(s.cobro) for s in servicios)
    por_linea: dict[str, float] = {}
    for s in servicios:
        linea = linea_de(s.tipo)
        por_linea[linea] = por_linea.get(linea, 0) + float(s.cobro)

    servicio_ids = {s.id for s in servicios}
    comisiones_pend = (
        db.query(Comision)
        .filter(Comision.estado == EstadoComision.pendiente, Comision.servicio_id.in_(servicio_ids))
        .all()
        if servicio_ids
        else []
    )
    por_trabajador: dict[str, float] = {}
    for c in comisiones_pend:
        por_trabajador[c.trabajador.nombre] = por_trabajador.get(c.trabajador.nombre, 0) + float(c.monto)

    return {
        "periodo": periodo,
        "ingresos": ingresos,
        "comisiones_pendientes": sum(por_trabajador.values()),
        "servicios": len(servicios),
        "clientes_atendidos": len({(s.persona_id, s.empresa_id) for s in servicios}),
        "por_linea": por_linea,
        "por_trabajador": por_trabajador,
    }


# ---------- frontend (debe ir al final: es un catch-all de "/") ----------

_frontend_dir = pathlib.Path(__file__).resolve().parent.parent / "frontend"
if _frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(_frontend_dir), html=True), name="frontend")
