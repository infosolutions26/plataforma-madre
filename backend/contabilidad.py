"""
Módulo de Contabilidad — separado del resto de plataforma-madre a propósito
(ver estrategia-producto-contabilidad-unificado): esto es lo que eventualmente
se podría desacoplar y vender aparte, así que no mezcla sus tablas con las de
CRM/nómina más allá de la FK a `empresa`/`persona` (la entidad_contable ya
existente).

Dos capas de datos:
- `ContabilidadMensualHistorica`: solo totales, para importar el histórico de
  ~110 compañías desde Drive (Sheets de vaciado viejos). No guarda gasto por
  gasto — el usuario confirmó que no hace falta para el histórico.
- Las tablas de la plataforma NUEVA (corte_bancario, gasto, comercio_grupo,
  etc., con detalle transacción por transacción) vienen en una fase posterior;
  esto es intencionalmente solo lo necesario para el import histórico.
"""

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
