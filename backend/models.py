"""
Modelo de datos de la Plataforma Madre.

Núcleo fijo + detalle flexible (ver sección 04 del documento de estrategia):
`Servicio` tiene las columnas comunes a las 4 líneas de negocio (Taxes,
Contabilidad, Capital, Otros Servicios); lo específico de cada línea vive en
`Servicio.detalle` (JSON), no en columnas nuevas cada vez que cambia un campo.

Persona/Empresa son entidades separadas (no una tabla polimórfica) unidas por
EmpresaDueno, porque en los datos reales hay dueños con más de una LLC.
"""

import enum
from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


class RolUsuario(str, enum.Enum):
    admin = "admin"
    trabajador = "trabajador"


class EstadoComision(str, enum.Enum):
    pendiente = "pendiente"
    pagada = "pagada"


class TipoPago(str, enum.Enum):
    comision = "comision"
    sueldo = "sueldo"
    mixto = "mixto"


class TipoPagoNomina(str, enum.Enum):
    pago = "pago"
    ajuste_inicial = "ajuste_inicial"


class Persona(Base):
    __tablename__ = "persona"

    id: Mapped[int] = mapped_column(primary_key=True)
    nombre: Mapped[str] = mapped_column(String(200))
    telefono: Mapped[Optional[str]] = mapped_column(String(40))
    correo: Mapped[Optional[str]] = mapped_column(String(200))
    ssn_encrypted: Mapped[Optional[str]] = mapped_column(Text)  # SSN/ITIN completo, cifrado
    ssn_last4: Mapped[Optional[str]] = mapped_column(String(4))  # sin cifrar, uso normal en UI
    ghl_contact_id: Mapped[Optional[str]] = mapped_column(String(60))
    drive_folder_id: Mapped[Optional[str]] = mapped_column(String(120))
    actividad: Mapped[str] = mapped_column(String(60), default="Activa")
    # Dirección — solo para el dashboard demográfico (estados/ciudades alcanzados),
    # no se muestra en el portal del cliente. Se llena importando el Marketing
    # Report de TaxSlayer, no se captura a mano en el flujo normal.
    direccion: Mapped[Optional[str]] = mapped_column(String(200))
    apartamento: Mapped[Optional[str]] = mapped_column(String(40))
    ciudad: Mapped[Optional[str]] = mapped_column(String(100))
    estado: Mapped[Optional[str]] = mapped_column(String(40))
    zip: Mapped[Optional[str]] = mapped_column(String(12))
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    empresas: Mapped[list["EmpresaDueno"]] = relationship(back_populates="persona")


class Empresa(Base):
    __tablename__ = "empresa"

    id: Mapped[int] = mapped_column(primary_key=True)
    nombre: Mapped[str] = mapped_column(String(200))
    ein: Mapped[Optional[str]] = mapped_column(String(20))
    giro: Mapped[Optional[str]] = mapped_column(String(100))
    tipo: Mapped[Optional[str]] = mapped_column(String(60))  # LLC sole member, S-Corp, Inc...
    estado_registro: Mapped[Optional[str]] = mapped_column(String(60))
    telefono: Mapped[Optional[str]] = mapped_column(String(40))
    correo: Mapped[Optional[str]] = mapped_column(String(200))
    ghl_contact_id: Mapped[Optional[str]] = mapped_column(String(60))
    drive_folder_id: Mapped[Optional[str]] = mapped_column(String(120))
    actividad: Mapped[str] = mapped_column(String(60), default="Activa")
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    duenos: Mapped[list["EmpresaDueno"]] = relationship(back_populates="empresa")


class EmpresaDueno(Base):
    """Join N:M — un dueño puede tener varias empresas; una empresa puede tener socios."""

    __tablename__ = "empresa_dueno"

    persona_id: Mapped[int] = mapped_column(ForeignKey("persona.id"), primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresa.id"), primary_key=True)
    rol: Mapped[str] = mapped_column(String(40), default="Dueño")

    persona: Mapped[Persona] = relationship(back_populates="empresas")
    empresa: Mapped[Empresa] = relationship(back_populates="duenos")


class FeeAnualPago(Base):
    """Historial real de pagos de fee anual, en vez del campo de texto concatenado del sheet viejo."""

    __tablename__ = "fee_anual_pago"
    __table_args__ = (UniqueConstraint("empresa_id", "anio"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey("empresa.id"))
    anio: Mapped[int] = mapped_column(Integer)
    fecha_pago: Mapped[Optional[date]] = mapped_column(Date)


class Trabajador(Base):
    """El 'usuario' de la plataforma. rol=admin ve/modifica todo; rol=trabajador, solo lo suyo."""

    __tablename__ = "trabajador"

    id: Mapped[int] = mapped_column(primary_key=True)
    nombre: Mapped[str] = mapped_column(String(120))
    correo: Mapped[str] = mapped_column(String(200), unique=True)
    password_hash: Mapped[Optional[str]] = mapped_column(String(120))
    rol: Mapped[RolUsuario] = mapped_column(Enum(RolUsuario), default=RolUsuario.trabajador)
    # config: [{"tipo": "Taxes 1040", "pct": 47}, ...] — default sugerido, no regla forzada
    config_servicios: Mapped[list] = mapped_column(JSON, default=list)
    tipo_pago: Mapped[str] = mapped_column(String(20), default=TipoPago.comision.value)
    sueldo_fijo: Mapped[float] = mapped_column(Numeric(10, 2), default=0)  # semanal — solo si tipo_pago es sueldo/mixto
    drive_folder_id: Mapped[Optional[str]] = mapped_column(String(120))
    # permisos extra más allá de lo básico (Servicios+Clientes): ej. ["nomina","estadisticas","trabajadores"]
    permisos: Mapped[list] = mapped_column(JSON, default=list)
    activo: Mapped[bool] = mapped_column(default=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Servicio(Base):
    """Núcleo fijo. Lo específico de cada línea (Taxes/Contas/Capital/Otros) va en `detalle`."""

    __tablename__ = "servicio"

    id: Mapped[int] = mapped_column(primary_key=True)
    persona_id: Mapped[Optional[int]] = mapped_column(ForeignKey("persona.id"))
    empresa_id: Mapped[Optional[int]] = mapped_column(ForeignKey("empresa.id"))
    tipo: Mapped[str] = mapped_column(String(80))  # ej. "Taxes 1040", "Contabilidad Completa"
    fecha: Mapped[date] = mapped_column(Date)
    trabajador_id: Mapped[int] = mapped_column(ForeignKey("trabajador.id"))
    cobro: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    metodo_pago: Mapped[Optional[str]] = mapped_column(String(40))
    estatus: Mapped[str] = mapped_column(String(60), default="En proceso")
    notas: Mapped[Optional[str]] = mapped_column(Text)
    detalle: Mapped[dict] = mapped_column(JSON, default=dict)  # campos propios de la línea
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    actualizado_en: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    trabajador: Mapped[Trabajador] = relationship()
    comisiones: Mapped[list["Comision"]] = relationship(back_populates="servicio", cascade="all, delete-orphan")
    archivos: Mapped[list["Archivo"]] = relationship(back_populates="servicio", cascade="all, delete-orphan")


class Comision(Base):
    """Una fila por (servicio, trabajador, rol). Estado propio, independiente del estatus del servicio."""

    __tablename__ = "comision"

    id: Mapped[int] = mapped_column(primary_key=True)
    servicio_id: Mapped[int] = mapped_column(ForeignKey("servicio.id"))
    trabajador_id: Mapped[int] = mapped_column(ForeignKey("trabajador.id"))
    rol: Mapped[str] = mapped_column(String(60), default="Responsable")
    monto: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    estado: Mapped[EstadoComision] = mapped_column(Enum(EstadoComision), default=EstadoComision.pendiente)
    fecha_pago: Mapped[Optional[date]] = mapped_column(Date)
    pago_nomina_id: Mapped[Optional[int]] = mapped_column(ForeignKey("pago_nomina.id"))  # qué pago la liquidó

    servicio: Mapped[Servicio] = relationship(back_populates="comisiones")
    trabajador: Mapped[Trabajador] = relationship()


class PagoNomina(Base):
    """Una fila por corrida de nómina — 'marcar como pagado' crea una de estas."""

    __tablename__ = "pago_nomina"

    id: Mapped[int] = mapped_column(primary_key=True)
    fecha: Mapped[date] = mapped_column(Date, default=date.today)
    trabajador_id: Mapped[int] = mapped_column(ForeignKey("trabajador.id"))
    monto: Mapped[float] = mapped_column(Numeric(10, 2))  # total pagado = sueldo + comisiones + extra
    n_servicios: Mapped[int] = mapped_column(Integer)
    tipo: Mapped[str] = mapped_column(String(20), default=TipoPagoNomina.pago.value)
    sueldo_incluido: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    comisiones_incluidas: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    extra_monto: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    extra_concepto: Mapped[Optional[str]] = mapped_column(String(200))
    concepto: Mapped[Optional[str]] = mapped_column(Text)
    drive_url: Mapped[Optional[str]] = mapped_column(String(400))  # link al recibo PDF

    trabajador: Mapped[Trabajador] = relationship()
    comisiones: Mapped[list["Comision"]] = relationship(foreign_keys="[Comision.pago_nomina_id]")


class PeriodoCustom(Base):
    """Temporadas creadas a mano por el Admin cuando el cálculo automático no aplica."""

    __tablename__ = "periodo_custom"

    id: Mapped[int] = mapped_column(primary_key=True)
    nombre: Mapped[str] = mapped_column(String(80))
    desde: Mapped[date] = mapped_column(Date)
    hasta: Mapped[date] = mapped_column(Date)


class Archivo(Base):
    """Referencia a un archivo — subido o foto tomada en la plataforma. El binario vive en Drive."""

    __tablename__ = "archivo"

    id: Mapped[int] = mapped_column(primary_key=True)
    persona_id: Mapped[Optional[int]] = mapped_column(ForeignKey("persona.id"))
    empresa_id: Mapped[Optional[int]] = mapped_column(ForeignKey("empresa.id"))
    servicio_id: Mapped[Optional[int]] = mapped_column(ForeignKey("servicio.id"))
    nombre: Mapped[str] = mapped_column(String(200))
    tipo: Mapped[str] = mapped_column(String(20), default="archivo")  # archivo | foto
    drive_url: Mapped[Optional[str]] = mapped_column(String(400))
    subido_por_id: Mapped[Optional[int]] = mapped_column(ForeignKey("trabajador.id"))
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    servicio: Mapped[Optional[Servicio]] = relationship(back_populates="archivos")


class Configuracion(Base):
    """Ajustes globales simples tipo llave-valor (ej. fecha_inicio fija del
    dashboard de nómina) — persisten en servidor, no en el navegador."""

    __tablename__ = "configuracion"

    clave: Mapped[str] = mapped_column(String(80), primary_key=True)
    valor: Mapped[str] = mapped_column(String(200))


class ReporteMarketingFila(Base):
    """Filas del Marketing Report de TaxSlayer, tal cual — para el dashboard
    demográfico (a cuántos estados/ciudades llegamos). No se empareja contra
    Persona; cada import reemplaza el contenido anterior (el reporte es una
    foto completa, no incremental)."""

    __tablename__ = "reporte_marketing_fila"

    id: Mapped[int] = mapped_column(primary_key=True)
    nombre: Mapped[Optional[str]] = mapped_column(String(200))
    ssn_last4: Mapped[Optional[str]] = mapped_column(String(4))
    direccion: Mapped[Optional[str]] = mapped_column(String(200))
    ciudad: Mapped[Optional[str]] = mapped_column(String(100))
    estado: Mapped[Optional[str]] = mapped_column(String(40))
    zip: Mapped[Optional[str]] = mapped_column(String(12))
    importado_en: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class NotaCliente(Base):
    """Bloques de nota independientes en el perfil de un cliente — un renglón
    por actualización, no un solo campo que se sobrescribe."""

    __tablename__ = "nota_cliente"

    id: Mapped[int] = mapped_column(primary_key=True)
    persona_id: Mapped[Optional[int]] = mapped_column(ForeignKey("persona.id"))
    empresa_id: Mapped[Optional[int]] = mapped_column(ForeignKey("empresa.id"))
    texto: Mapped[str] = mapped_column(Text)
    trabajador_id: Mapped[int] = mapped_column(ForeignKey("trabajador.id"))
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    trabajador: Mapped[Trabajador] = relationship()
