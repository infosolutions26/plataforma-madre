"""
Fase 1 del pipeline: leer un PDF de estado de cuenta (débito o crédito, cualquier
banco) con Gemini y volcarlo al esquema de contabilidad.

El motor de extracción es el mismo que ya corre en producción en el extractor de
estados de cuenta del usuario (Gemini nativo de PDF + salida estructurada) — se
reusa aquí casi tal cual porque ya está probado contra bancos reales. Lo nuevo es
`ingerir_estado`: toma lo extraído y crea un CorteEstadoCuenta + sus Gasto/Ingreso,
aplicando las reglas confirmadas por el usuario:

- El mes de cada gasto/ingreso sale de SU fecha (tx["date"]), no del periodo del
  corte. Un estado que cruza fin de mes se reparte solo al armar el entregable.
- De los ingresos solo importa el total, pero se guardan por fecha igual (para
  poder repartirlos por mes natural).
- A cada gasto se le pre-asigna la categoría sugerida del diccionario Comercio
  (aprendizaje histórico); queda clasificado=False para que el humano confirme
  en contabilidad personalizada. En express se marcan clasificado=True de una vez
  (no se categoriza, solo cuentan para los totales).
- Se compara la suma de lo extraído contra los totales que declara el banco para
  marcar corte.validado (así se detecta una extracción incompleta antes de armar).

Necesita GEMINI_API_KEY en el entorno (igual que el extractor). Sin ella, el
endpoint de subida responde un error claro en vez de fallar en silencio.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from comercio_parser import normaliza_comercio
from contabilidad import (
    Comercio,
    CorteEstadoCuenta,
    CuentaBancaria,
    Gasto,
    Ingreso,
    TipoContabilidad,
)

MODEL = "gemini-3.1-flash-lite"  # económico vigente (2.5-flash-lite retirado para altas nuevas)

# Esquema de salida estructurada — idéntico al del extractor en producción, para
# aprovechar el prompt/mapeo ya validado contra débito y crédito de varios bancos.
STATEMENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "bank_name": {"type": "string", "description": "Banco en MAYÚSCULAS tal cual aparece (ej. BANK OF AMERICA, CHASE)."},
        "account_number": {"type": "string", "description": "Número de cuenta/tarjeta tal cual impreso (o enmascarado como venga). '' si no aparece."},
        "account_type": {"type": "string", "enum": ["debito", "credito"], "description": "'credito' si es un estado de TARJETA DE CRÉDITO (tiene 'previous balance'/'new balance'/'minimum payment'); 'debito' si es cuenta de cheques/ahorro o tarjeta de débito."},
        "customer": {"type": "string", "description": "Titular de la cuenta o negocio, tal cual aparece."},
        "period_start": {"type": "string", "description": "Inicio del periodo, formato YYYY-MM-DD."},
        "period_end": {"type": "string", "description": "Fin del periodo, formato YYYY-MM-DD."},
        "beginning_balance": {"type": "number", "description": "Saldo inicial. Para crédito, el 'previous balance'. 0 si no aparece."},
        "deposits_total": {"type": "number", "description": "Total de depósitos y créditos (dinero que entra). Para crédito, pagos/créditos recibidos. Súmalo de las transacciones si no viene explícito."},
        "withdrawals_total": {"type": "number", "description": "Total de retiros y débitos (dinero que sale). Para crédito, compras/cargos. Súmalo si no viene explícito."},
        "checks_total": {"type": "number", "description": "Total de cheques pagados. 0 si no aplica."},
        "service_fees_total": {"type": "number", "description": "Comisiones/cargos por servicio. Para crédito, intereses y cargos financieros. 0 si no aplica."},
        "ending_balance": {"type": "number", "description": "Saldo final. Para crédito, el 'new balance'."},
        "transactions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["Deposits and Credits", "Withdrawals and Debits", "Checks"],
                        "description": "Entra (depósitos/pagos recibidos/créditos/reembolsos), Sale (compras/retiros/pagos/comisiones), o Cheques pagados.",
                    },
                    "date": {"type": "string", "description": "Fecha de la transacción, YYYY-MM-DD. Si solo hay mes/día, usa el año del periodo."},
                    "description": {"type": "string", "description": "Descripción/concepto tal cual aparece, sin modificar."},
                    "merchant": {"type": "string", "description": "El COMERCIO o beneficiario LIMPIO, sin la fecha, ciudad, estado, número de tarjeta, número de referencia ni el tipo de transacción. Ej. de 'Card purchase 01/06 STARBUCKS #123 AURORA IL Card 3916' extrae 'STARBUCKS'; de 'Zelle payment to Gustavo' extrae 'GUSTAVO'; de 'WM SUPERCENTER' extrae 'WALMART' si es reconocible, si no déjalo como aparece. En MAYÚSCULAS. Para cargos genéricos del banco (monthly service fee, atm fee, withdrawal) usa una etiqueta corta del concepto."},
                    "amount": {"type": "number", "description": "Monto SIEMPRE positivo; el signo lo da 'type'."},
                },
                "required": ["type", "date", "description", "merchant", "amount"],
            },
        },
    },
    "required": [
        "bank_name", "account_number", "account_type", "customer", "period_start", "period_end",
        "beginning_balance", "deposits_total", "withdrawals_total", "checks_total",
        "service_fees_total", "ending_balance", "transactions",
    ],
}

SYSTEM_PROMPT = """Eres un experto en lectura de estados de cuenta bancarios de Estados Unidos \
(cuentas de cheques/ahorro, tarjetas de débito y tarjetas de crédito, de cualquier banco).

Tu tarea es EXTRAER la información del estado y NORMALIZARLA a un esquema único, igual sin \
importar el banco ni si la cuenta es de débito o de crédito:

- bank_name, account_number (tal cual impreso) y customer (titular).
- period_start / period_end: el periodo que cubre el estado.
- Los 6 totales del resumen (beginning_balance, deposits_total, withdrawals_total, \
checks_total, service_fees_total, ending_balance). Tómalos de la sección de resumen del \
estado si están impresos ahí; si el estado no trae alguno explícito, súmalo tú a partir de \
las transacciones correspondientes.
- Para CUENTAS DE CHEQUES/AHORRO O DÉBITO: deposits_total = depósitos y créditos; \
withdrawals_total = retiros y débitos; checks_total = cheques pagados; service_fees_total = \
comisiones/cargos por servicio.
- Para TARJETAS DE CRÉDITO: mapea beginning_balance = saldo anterior ('previous balance'); \
deposits_total = pagos y créditos recibidos; withdrawals_total = compras y cargos \
('purchases'/'debits'); checks_total = 0 (no aplica); service_fees_total = intereses + \
cargos financieros/anuales; ending_balance = saldo nuevo ('new balance').
- TODAS las transacciones del periodo, en el orden en que aparecen, clasificadas en: \
'Deposits and Credits', 'Withdrawals and Debits' o 'Checks'.
- Por cada transacción, además de la descripción tal cual, extrae 'merchant': el comercio o \
beneficiario LIMPIO, quitando la fecha, la ciudad, el estado, el número de tarjeta, los \
números de referencia y el tipo de transacción. Es lo que se usa para agrupar gastos del \
mismo comercio, así que dos compras en el mismo lugar deben dar el MISMO 'merchant'. Para \
pagos a personas (Zelle, transferencias) usa el nombre de la persona. En MAYÚSCULAS.
- 'amount' siempre positivo; el signo lo determina 'type'.
- Cada transacción DEBE tener su fecha real (la fecha en que ocurrió, no la del corte). Es \
crítico: si el estado solo trae mes/día, complétala con el año del periodo.
- No inventes, no resumas, no omitas transacciones ni descripciones. Copia la descripción \
tal cual está escrita en el documento.
- Si un dato no aparece en el documento, usa "" (texto) o 0 (número) según corresponda.

Devuelve únicamente el JSON con el esquema solicitado."""


def gemini_disponible() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY"))


def _to_gemini_schema(schema: Any) -> Any:
    """Quita 'additionalProperties' (no es del subconjunto de JSON Schema de Gemini)."""
    if isinstance(schema, dict):
        return {k: _to_gemini_schema(v) for k, v in schema.items() if k != "additionalProperties"}
    if isinstance(schema, list):
        return [_to_gemini_schema(v) for v in schema]
    return schema


async def extraer_estado(filename: str, pdf_bytes: bytes) -> dict:
    """Una llamada a Gemini por PDF. Devuelve el dict del esquema (+ _source_file)."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    pdf_part = types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf")
    resp = await client.aio.models.generate_content(
        model=MODEL,
        contents=[pdf_part, "Analiza el estado de cuenta y extrae su información, totales y todas sus transacciones según el esquema."],
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=_to_gemini_schema(STATEMENT_SCHEMA),
        ),
    )
    data = json.loads(resp.text)
    data["_source_file"] = filename
    return data


def extraer_estado_sync(filename: str, pdf_bytes: bytes) -> dict:
    return asyncio.run(extraer_estado(filename, pdf_bytes))


def _parse_fecha(s: str, fallback: date) -> date:
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except (ValueError, AttributeError):
            continue
    return fallback


def _ultimos4(account_number: str) -> Optional[str]:
    digitos = "".join(ch for ch in str(account_number) if ch.isdigit())
    return digitos[-4:] if len(digitos) >= 4 else (digitos or None)


def resolver_cuenta(
    db: Session, data: dict, empresa_id: Optional[int], persona_id: Optional[int],
) -> CuentaBancaria:
    """Empareja el estado con una CuentaBancaria del cliente por banco+últimos4+tipo,
    o crea una nueva si no existe. Así el encargado no administra cuentas a mano —
    solo elige el cliente y sube PDFs, y el sistema arma las cuentas de lo extraído."""
    banco = (data.get("bank_name") or "BANCO").strip().upper()
    tipo = data.get("account_type") or "debito"
    u4 = _ultimos4(data.get("account_number", ""))
    q = db.query(CuentaBancaria)
    if empresa_id is not None:
        q = q.filter(CuentaBancaria.empresa_id == empresa_id)
    else:
        q = q.filter(CuentaBancaria.persona_id == persona_id)
    q = q.filter(CuentaBancaria.banco == banco, CuentaBancaria.tipo == tipo)
    if u4:
        q = q.filter(CuentaBancaria.ultimos4 == u4)
    cuenta = q.first()
    if cuenta is None:
        cuenta = CuentaBancaria(
            empresa_id=empresa_id, persona_id=persona_id, banco=banco,
            ultimos4=u4, tipo=tipo, apodo=f"{banco.title()} #{u4}" if u4 else banco.title(),
        )
        db.add(cuenta)
        db.flush()
    return cuenta


def ingerir_estado(
    db: Session,
    data: dict,
    cuenta: CuentaBancaria,
    tipo_contabilidad: str,
    filename: str,
    fuente_file_id: Optional[str] = None,
) -> CorteEstadoCuenta:
    """Crea el CorteEstadoCuenta + sus Gasto/Ingreso a partir de lo extraído.
    Devuelve el corte (con .validado ya calculado). No commitea — el caller decide."""
    ini = _parse_fecha(data.get("period_start", ""), date.today())
    fin = _parse_fecha(data.get("period_end", ""), date.today())
    gasto_declarado = (
        float(data.get("withdrawals_total") or 0)
        + float(data.get("checks_total") or 0)
        + float(data.get("service_fees_total") or 0)
    )
    corte = CorteEstadoCuenta(
        cuenta_id=cuenta.id,
        fecha_inicio=ini,
        fecha_fin=fin,
        saldo_inicial=data.get("beginning_balance"),
        saldo_final=data.get("ending_balance"),
        ingreso_total_declarado=data.get("deposits_total"),
        gasto_total_declarado=gasto_declarado,
        tipo_contabilidad=tipo_contabilidad,
        fuente_archivo=filename,
        fuente_file_id=fuente_file_id,
    )
    db.add(corte)
    db.flush()  # asigna corte.id

    es_express = tipo_contabilidad == TipoContabilidad.express.value
    # cache del diccionario para no consultar por cada gasto
    dicc = {c.nombre_normalizado: c for c in db.query(Comercio).all()}

    suma_gasto, suma_ingreso = 0.0, 0.0
    for tx in data.get("transactions", []):
        fecha_tx = _parse_fecha(tx.get("date", ""), fin)
        monto = abs(float(tx.get("amount") or 0))
        if not monto:
            continue
        if tx.get("type") == "Deposits and Credits":
            db.add(Ingreso(
                empresa_id=cuenta.empresa_id, persona_id=cuenta.persona_id,
                cuenta_id=cuenta.id, corte_id=corte.id, fecha=fecha_tx, monto=monto,
            ))
            suma_ingreso += monto
        else:  # Withdrawals and Debits / Checks
            # 'merchant' (comercio limpio que da el modelo) es lo que agrupa y matchea
            # contra el diccionario; si no vino, cae a la descripción completa.
            comercio_txt = (tx.get("merchant") or tx.get("description") or "").strip()
            com = dicc.get(normaliza_comercio(comercio_txt))
            db.add(Gasto(
                empresa_id=cuenta.empresa_id, persona_id=cuenta.persona_id,
                cuenta_id=cuenta.id, corte_id=corte.id, fecha=fecha_tx, monto=monto,
                comercio_raw=comercio_txt or tx.get("description", ""),
                comercio_id=com.id if com else None,
                categoria_id=com.categoria_sugerida_id if com else None,
                clasificado=es_express,  # express no se clasifica a mano
            ))
            suma_gasto += monto

    # Validación por ECUACIÓN DE SALDOS (no por los subtotales declarados: al
    # probar con Chase real, su resumen separa "electronic" de "card withdrawals"
    # y el subtotal que extrae el modelo puede ser parcial — pero la suma de
    # TODAS las transacciones sí cuadra contra el saldo final). Esto detecta de
    # verdad si faltó o sobró alguna transacción. El signo depende del tipo:
    #   débito : saldo_final = saldo_inicial + ingresos - gastos
    #   crédito: saldo_final = saldo_inicial + gastos(compras) - ingresos(pagos)
    si = float(data.get("beginning_balance") or 0)
    sf = data.get("ending_balance")
    es_credito = (cuenta.tipo == "credito")
    if sf is None:
        corte.validado = False
    else:
        esperado = si + (suma_gasto - suma_ingreso) if es_credito else si + (suma_ingreso - suma_gasto)
        tol = max(1.0, abs(float(sf)) * 0.01)  # 1% del saldo final o $1
        corte.validado = abs(esperado - float(sf)) <= tol
    return corte
