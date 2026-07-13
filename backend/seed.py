"""
Datos de ejemplo (ficticios) — los mismos del prototipo interactivo, para
poder probar la API real con algo parecido. Correr una sola vez:
    python3 seed.py
"""

from datetime import date

from database import Base, SessionLocal, encrypt_ssn, engine
from models import Comision, Empresa, EstadoComision, Persona, RolUsuario, Servicio, Trabajador

Base.metadata.create_all(bind=engine)
db = SessionLocal()

if db.query(Trabajador).count() > 0:
    print("Ya hay datos — no se vuelve a sembrar. Borra plataforma_madre.db si quieres empezar de cero.")
else:
    admin = Trabajador(nombre="Admin", correo="info@solutionstaxes.com", rol=RolUsuario.admin, config_servicios=[])
    admin2 = Trabajador(nombre="Andrés", correo="andres.tec.unam@gmail.com", rol=RolUsuario.admin, config_servicios=[])
    caro = Trabajador(nombre="Caro", correo="carolina@solutionstaxes.com", rol=RolUsuario.admin,
                       config_servicios=[{"tipo": "Taxes 1040", "pct": 47}, {"tipo": "Asesoría", "pct": 20}])
    erik = Trabajador(nombre="Erik", correo="erik@solutionsmultiservices.com", rol=RolUsuario.trabajador,
                       config_servicios=[{"tipo": "Taxes 1040", "pct": 47}, {"tipo": "ITIN + Taxes", "pct": 47}])
    mariana = Trabajador(nombre="Mariana", correo="mariana@solutionsmultiservices.com", rol=RolUsuario.trabajador,
                          config_servicios=[{"tipo": "Contabilidad Completa", "pct": 20}])
    db.add_all([admin, admin2, caro, erik, mariana])
    db.flush()

    maria = Persona(nombre="María Torres", telefono="(312) 555-0142",
                     ssn_encrypted=encrypt_ssn("482-19-4821"), ssn_last4="4821")
    vega = Empresa(nombre="Vega Construction LLC", telefono="(773) 555-0187", giro="Construction")
    db.add_all([maria, vega])
    db.flush()

    s1 = Servicio(persona_id=maria.id, tipo="Taxes 1040", fecha=date(2026, 2, 14), trabajador_id=caro.id,
                  cobro=220, metodo_pago="Zelle", estatus="Transmitido Aceptado",
                  detalle={"anio": "2026", "tipoCita": "Presencial", "statusTaxes": "Transmitido Aceptado"})
    s2 = Servicio(empresa_id=vega.id, tipo="Contabilidad Completa", fecha=date(2026, 3, 2), trabajador_id=mariana.id,
                  cobro=300, metodo_pago="Zelle", estatus="Pagado",
                  detalle={"nivel": "2", "meses": "3", "credito": "No"})
    s3 = Servicio(persona_id=maria.id, tipo="ITIN + Taxes", fecha=date(2025, 3, 8), trabajador_id=erik.id,
                  cobro=250, metodo_pago="Cash", estatus="En proceso",
                  detalle={"anio": "2025", "statusTaxes": "En proceso / Incompleto"})
    db.add_all([s1, s2, s3])
    db.flush()

    db.add_all([
        Comision(servicio_id=s1.id, trabajador_id=caro.id, rol="Preparador", monto=100, estado=EstadoComision.pagada, fecha_pago=date(2026, 2, 28)),
        Comision(servicio_id=s2.id, trabajador_id=mariana.id, rol="Responsable", monto=60, estado=EstadoComision.pagada, fecha_pago=date(2026, 2, 28)),
        Comision(servicio_id=s3.id, trabajador_id=erik.id, rol="Preparador", monto=118, estado=EstadoComision.pendiente),
    ])
    db.commit()
    print("Sembrado: 5 trabajadores (3 admin: info@solutionstaxes.com, andres.tec.unam@gmail.com, carolina@solutionstaxes.com), 2 clientes, 3 servicios, 3 comisiones.")
    print("Entra a http://localhost:8000/auth/login y loguéate con Google usando andres.tec.unam@gmail.com.")

db.close()
