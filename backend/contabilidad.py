"""
Módulo de Contabilidad — separado del resto de plataforma-madre a propósito
(ver estrategia-producto-contabilidad-unificado): esto es lo que eventualmente
se podría desacoplar y vender aparte, así que no mezcla sus tablas con las de
CRM/nómina más allá de la FK a `empresa`/`persona` (la entidad_contable ya
existente).

Dos capas de datos:
- `ContabilidadMensualHistorica`: solo totales por mes. Es el archivo — de ahí
  llegó el histórico de ~110 compañías importado de Drive (Sheets de vaciado
  viejos), y ahí mismo se consolidan los totales mensuales que vaya
  produciendo el sistema nuevo (tanto express como personalizada), para que
  la tabla de "histórico de años anteriores" del entregable siempre consulte
  un solo lugar sin importar cómo se generaron los datos.
- El pipeline NUEVO de 3 fases (cargar PDFs -> clasificar -> armar entregable):
  `CuentaBancaria`, `CorteEstadoCuenta`, `Comercio`, `Gasto`, `Ingreso`. Regla
  central confirmada por el usuario: el mes de cada gasto/ingreso SIEMPRE sale
  de su propia fecha, nunca del periodo del corte — así un estado de cuenta
  que va del 28 feb al 30 mar se reparte solo entre esos dos meses naturales
  sin que nadie tenga que cortarlo a mano. `CorteEstadoCuenta` solo guarda el
  periodo/saldos que declara el banco para VALIDAR que la extracción del PDF
  sumó bien, no para agrupar el reporte.
"""

import enum
from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


class TipoContabilidad(str, enum.Enum):
    express = "express"          # solo totales, sin clasificar gasto por gasto
    personalizada = "personalizada"  # se clasifica cada gasto por comercio y categoría


class CategoriaGasto(Base):
    """Taxonomía fija (confirmada por el usuario: sin categorías custom por
    cliente). `es_deducible=False` marca los buckets de control (Pagos a
    Tarjetas, ATM, Savings, Personal, Cheques) que se registran pero NO
    cuentan en el total de gastos deducibles del reporte."""

    __tablename__ = "categoria_gasto"

    id: Mapped[int] = mapped_column(primary_key=True)
    nombre: Mapped[str] = mapped_column(String(60), unique=True)
    es_deducible: Mapped[bool] = mapped_column(Boolean, default=True)
    orden: Mapped[int] = mapped_column(Integer, default=0)  # para reproducir el orden fijo del entregable


# Orden y nombres tomados tal cual de las hojas RESUMEN reales (Solorio, R&I) y
# del archivo "CLASIFICACION GASTOS" del Drive. es_deducible=False = categoría
# de control (no entra al total de gastos ni al % del entregable).
CATEGORIAS_SEED = [
    ("ADVERTISING", True),
    ("CAR EXPENSES", True),
    ("COMISION/FEES", True),
    ("CONTRACT LABOR", True),
    ("DEPRECIATION", True),
    ("INSURANCE", True),
    ("LEGAL SERVICES", True),
    ("OFFICE EXPENSES", True),
    ("RENT/LEASE", True),
    ("REPAIRS/MAINT", True),
    ("SUPPLIES", True),
    ("TAXES/LICENSES", True),
    ("TRAVEL", True),
    ("MEALS", True),
    ("UTILITIES", True),
    ("OTHER", True),
    ("PAGOS A TARJETAS", False),  # pago a la tarjeta de crédito: mueve dinero, no es gasto real
    ("ATM", False),
    ("PERSONAL", False),
    ("SAVINGS", False),
    ("CHEQUES", False),  # pago por cheque sin comercio identificable
]


class ContabilidadMensualHistorica(Base):
    """Un renglón por compañía+mes, importado de los Sheets de vaciado viejos.

    `empresa_id` puede ser null momentáneamente durante el import si la
    compañía todavía no existe en plataforma-madre (el import la crea sola,
    ver script), pero SIEMPRE debe quedar poblado al final del proceso.
    `nombre_empresa_original` se conserva siempre, aunque haya match, como
    registro de auditoría de qué carpeta de Drive originó el dato.
    """

    __tablename__ = "contabilidad_mensual_historica"
    __table_args__ = (UniqueConstraint("empresa_id", "anio", "mes", "fuente_archivo"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[Optional[int]] = mapped_column(ForeignKey("empresa.id"))
    nombre_empresa_original: Mapped[str] = mapped_column(String(200))
    anio: Mapped[int] = mapped_column(Integer)
    mes: Mapped[int] = mapped_column(Integer)  # 1-12, mes natural

    ingreso_total: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    gasto_total_deducible: Mapped[float] = mapped_column(Numeric(12, 2), default=0)  # excluye categorías de control
    saldo_inicial: Mapped[Optional[float]] = mapped_column(Numeric(12, 2))
    saldo_final: Mapped[Optional[float]] = mapped_column(Numeric(12, 2))

    # {"ADVERTISING": 0, "CONTRACT LABOR": 23078.3, ...} — incluye deducibles Y de control,
    # gasto_total_deducible ya viene pre-calculado excluyendo las de control.
    gasto_por_categoria: Mapped[dict] = mapped_column(JSON, default=dict)

    fuente_archivo: Mapped[str] = mapped_column(String(300))  # nombre del spreadsheet de Drive, trazabilidad
    fuente_file_id: Mapped[Optional[str]] = mapped_column(String(120))
    importado_en: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    empresa: Mapped[Optional["Empresa"]] = relationship()  # noqa: F821 — Empresa vive en models.py, misma metadata de Base


# ==================== pipeline nuevo: cargar -> clasificar -> armar ====================

class CuentaBancaria(Base):
    """Una cuenta de banco (débito o tarjeta de crédito) de un cliente. Un
    cliente puede tener varias, cada una con sus propias fechas de corte —
    por eso el reparto por mes natural se hace a nivel de Gasto/Ingreso, no
    de cuenta."""

    __tablename__ = "cuenta_bancaria"

    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[Optional[int]] = mapped_column(ForeignKey("empresa.id"))
    persona_id: Mapped[Optional[int]] = mapped_column(ForeignKey("persona.id"))
    banco: Mapped[str] = mapped_column(String(80))
    ultimos4: Mapped[Optional[str]] = mapped_column(String(4))
    tipo: Mapped[str] = mapped_column(String(20))  # debito | credito
    apodo: Mapped[Optional[str]] = mapped_column(String(80))  # ej. "Chase #1801", para mostrar en tablas
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    empresa: Mapped[Optional["Empresa"]] = relationship()  # noqa: F821
    persona: Mapped[Optional["Persona"]] = relationship()  # noqa: F821


class CorteEstadoCuenta(Base):
    """Un PDF de estado de cuenta subido = un corte. Guarda el periodo y los
    saldos/totales que DECLARA el banco, para validar que lo que se extrajo
    del PDF cuadra (`validado`) antes de dar la clasificación por buena — no
    se usa para agrupar el entregable por mes, eso sale de la fecha de cada
    gasto/ingreso. `tipo_contabilidad` se elige una vez por la persona que
    sube el lote de PDFs (todos deben ser del mismo cliente, confirmado)."""

    __tablename__ = "corte_estado_cuenta"

    id: Mapped[int] = mapped_column(primary_key=True)
    cuenta_id: Mapped[int] = mapped_column(ForeignKey("cuenta_bancaria.id"))
    fecha_inicio: Mapped[date] = mapped_column(Date)
    fecha_fin: Mapped[date] = mapped_column(Date)
    saldo_inicial: Mapped[Optional[float]] = mapped_column(Numeric(12, 2))
    saldo_final: Mapped[Optional[float]] = mapped_column(Numeric(12, 2))
    ingreso_total_declarado: Mapped[Optional[float]] = mapped_column(Numeric(12, 2))  # lo que dice el banco
    gasto_total_declarado: Mapped[Optional[float]] = mapped_column(Numeric(12, 2))
    tipo_contabilidad: Mapped[str] = mapped_column(String(20), default=TipoContabilidad.personalizada.value)
    fuente_archivo: Mapped[Optional[str]] = mapped_column(String(300))  # nombre del PDF subido
    fuente_file_id: Mapped[Optional[str]] = mapped_column(String(120))  # Drive, si se archiva ahí
    validado: Mapped[bool] = mapped_column(Boolean, default=False)  # true cuando la suma extraída cuadra contra lo declarado
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    cuenta: Mapped[CuentaBancaria] = relationship()


class Comercio(Base):
    """Diccionario global que aprende con el uso: normaliza el texto crudo
    del OCR ('SQ *STARBUCKS #4021 CHICAGO IL') a un nombre editable
    ('Starbucks') y sugiere categoría según lo clasificado antes — entre más
    se usa el sistema (de cualquier cliente), mejor sugiere. `veces_usado`
    sirve tanto de métrica de confianza como para ordenar sugerencias."""

    __tablename__ = "comercio"

    id: Mapped[int] = mapped_column(primary_key=True)
    nombre_normalizado: Mapped[str] = mapped_column(String(200), unique=True)  # clave de match (mayúsculas, sin ruido)
    nombre_editado: Mapped[str] = mapped_column(String(200))  # lo que se muestra y se imprime en el entregable
    categoria_sugerida_id: Mapped[Optional[int]] = mapped_column(ForeignKey("categoria_gasto.id"))
    veces_usado: Mapped[int] = mapped_column(Integer, default=0)
    actualizado_en: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    categoria_sugerida: Mapped[Optional[CategoriaGasto]] = relationship()


class Gasto(Base):
    """Un renglón de gasto individual, ya vinculado a cuenta+fecha+comercio+
    categoría. `clasificado=False` = pendiente de revisar en la pantalla de
    clasificación (así llega recién extraído por OCR); en contabilidad
    express no se crean Gasto — solo se suben totales directo a
    ContabilidadMensualHistorica."""

    __tablename__ = "gasto"

    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[Optional[int]] = mapped_column(ForeignKey("empresa.id"))
    persona_id: Mapped[Optional[int]] = mapped_column(ForeignKey("persona.id"))
    cuenta_id: Mapped[int] = mapped_column(ForeignKey("cuenta_bancaria.id"))
    corte_id: Mapped[int] = mapped_column(ForeignKey("corte_estado_cuenta.id"))
    fecha: Mapped[date] = mapped_column(Date)  # define el mes del entregable — nunca el periodo del corte
    monto: Mapped[float] = mapped_column(Numeric(12, 2))
    metodo: Mapped[Optional[str]] = mapped_column(String(20))  # zelle|deposito|cheque|tarjeta|atm|transferencia|cargo|otro
    comercio_raw: Mapped[str] = mapped_column(String(300))  # texto tal cual vino del OCR, para re-normalizar si hace falta
    comercio_id: Mapped[Optional[int]] = mapped_column(ForeignKey("comercio.id"))
    categoria_id: Mapped[Optional[int]] = mapped_column(ForeignKey("categoria_gasto.id"))
    clasificado: Mapped[bool] = mapped_column(Boolean, default=False)
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    empresa: Mapped[Optional["Empresa"]] = relationship()  # noqa: F821
    persona: Mapped[Optional["Persona"]] = relationship()  # noqa: F821
    cuenta: Mapped[CuentaBancaria] = relationship()
    corte: Mapped[CorteEstadoCuenta] = relationship()
    comercio: Mapped[Optional[Comercio]] = relationship()
    categoria: Mapped[Optional[CategoriaGasto]] = relationship()


class Ingreso(Base):
    """Un depósito individual — solo fecha y monto (confirmado: de ingresos
    nada más hace falta el total, el foco está en los gastos). Se guarda por
    fecha, no como un total por corte, por la misma regla de mes natural que
    Gasto: un corte que cruza fin de mes debe repartir sus depósitos entre
    los dos meses que corresponde."""

    __tablename__ = "ingreso"

    id: Mapped[int] = mapped_column(primary_key=True)
    empresa_id: Mapped[Optional[int]] = mapped_column(ForeignKey("empresa.id"))
    persona_id: Mapped[Optional[int]] = mapped_column(ForeignKey("persona.id"))
    cuenta_id: Mapped[int] = mapped_column(ForeignKey("cuenta_bancaria.id"))
    corte_id: Mapped[int] = mapped_column(ForeignKey("corte_estado_cuenta.id"))
    fecha: Mapped[date] = mapped_column(Date)
    monto: Mapped[float] = mapped_column(Numeric(12, 2))
    metodo: Mapped[Optional[str]] = mapped_column(String(20))  # zelle|deposito|cheque|transferencia|otro
    concepto: Mapped[Optional[str]] = mapped_column(String(300))  # nombre/beneficiario del ingreso (para unificar depósitos de la misma persona)
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    cuenta: Mapped[CuentaBancaria] = relationship()
    corte: Mapped[CorteEstadoCuenta] = relationship()
