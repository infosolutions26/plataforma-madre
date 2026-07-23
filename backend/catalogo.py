"""
Catálogo de servicio_tipo y sus campos de detalle propios (sección 03-04 del
documento de estrategia). Vive en código, no en tabla — es el mismo catálogo
que usa el prototipo interactivo, para que ambos queden sincronizados.
"""

SERVICE_TYPES: dict[str, dict] = {
    "Taxes 1040": {
        "linea": "Taxes",
        "extra": ["ssn", "anio", "tipoCita", "statusTaxes", "origen"],
    },
    "ITIN + Taxes": {
        "linea": "Taxes",
        "extra": ["ssn", "anio", "statusTaxes", "origen"],
    },
    "Contabilidad Completa": {
        "linea": "Contabilidad",
        "extra": ["nivel", "meses", "credito"],
    },
    "Contabilidad Express": {
        "linea": "Contabilidad",
        "extra": ["meses", "credito"],
    },
    "Asesoría": {
        "linea": "Capital",
        "extra": ["tipoCita"],
    },
    "Plan de Acción": {
        "linea": "Capital",
        "extra": ["progreso", "tipoCita"],
    },
    "Annual Fee": {
        "linea": "Otros servicios",
        "extra": ["formato", "fechaEnvio"],
    },
    "Statement of Information": {
        "linea": "Otros servicios",
        "extra": ["formato"],
    },
    "Good Standing": {
        "linea": "Otros servicios",
        "extra": ["formato"],
    },
}


def linea_de(tipo: str) -> str:
    return SERVICE_TYPES.get(tipo, {}).get("linea", "Otros")


# Estatus de servicio.estatus que significan "el cliente ya pagó" — solo estos
# habilitan que la comisión asociada pase a estar disponible para pagarle al
# trabajador en nómina. "Banco" (esperando confirmación bancaria) y "Cortesía"
# NO cuentan todavía.
ESTATUS_LIBERA_COMISION = ["Pagado", "Banco Pagado"]
