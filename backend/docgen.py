"""
Genera el entregable de contabilidad como Google Doc, RELLENANDO la plantilla de
membrete del usuario en su lugar (no recreándola ni anexando al final).

La plantilla es un Google Doc con encabezado/pie y tablas de EJEMPLO en posiciones
fijas, más placeholders de texto (NOMBRE DE COMPAÑÍA, Mes - Mes, etc.). Aquí:
1. Se exporta a .docx vía Drive (solo scope de Drive, ya autorizado).
2. Se rellenan los placeholders de texto y cada tabla EN SU LUGAR: se leen las
   etiquetas reales de la plantilla (nombre de mes / categoría en la 1a columna,
   encabezados de mes en la 1a fila) y se escriben los montos que correspondan,
   conservando el formato de la plantilla. Los renglones sin datos quedan en $0.
3. Se reemplazan las gráficas de ejemplo por las que manda el navegador (PNG).
4. Se sube a Drive convertido a Google Doc en la carpeta del cliente.

Robusto a que el usuario cambie el orden o el diseño: no asume índices fijos,
empareja por etiqueta.
"""
from __future__ import annotations

import base64
import io
import re
from typing import Optional

from docx import Document
from googleapiclient.http import MediaIoBaseUpload

import drive

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
GDOC_MIME = "application/vnd.google-apps.document"

MESES = {"ENERO": 1, "FEBRERO": 2, "MARZO": 3, "ABRIL": 4, "MAYO": 5, "JUNIO": 6,
         "JULIO": 7, "AGOSTO": 8, "SEPTIEMBRE": 9, "OCTUBRE": 10, "NOVIEMBRE": 11, "DICIEMBRE": 12}


def _money(v) -> str:
    return "$" + f"{float(v or 0):,.0f}"


def _pct(v) -> str:
    return f"{float(v or 0):.2f}%"


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().upper())


def _set_cell(cell, text: str):
    """Escribe en la celda conservando el formato del primer run existente."""
    text = str(text)
    paras = cell.paragraphs
    if paras and paras[0].runs:
        paras[0].runs[0].text = text
        for r in paras[0].runs[1:]:
            r.text = ""
    elif paras:
        paras[0].add_run(text)
    else:
        cell.add_paragraph(text)


def _reemplazar_texto(doc, mapa: dict):
    """Reemplaza párrafos cuyo texto sea (o contenga) un placeholder conocido,
    conservando el formato del primer run."""
    for p in doc.paragraphs:
        txt = p.text
        for clave, valor in mapa.items():
            if clave and clave in txt and valor:
                nuevo = txt.replace(clave, str(valor))
                if p.runs:
                    p.runs[0].text = nuevo
                    for r in p.runs[1:]:
                        r.text = ""
                break


def _cols_por_encabezado(tabla) -> dict:
    """{ENCABEZADO_NORMALIZADO: indice_columna} de la fila 0."""
    return {_norm(c.text): i for i, c in enumerate(tabla.rows[0].cells)}


def _fill_mensual(tabla, datos):
    """Tabla TOTAL/DEPÓSITOS/GASTOS/(blank)/SALDO FINAL — 1a col = nombre de mes."""
    cols = _cols_por_encabezado(tabla)
    ci_dep = cols.get("DEPÓSITOS", cols.get("DEPOSITOS"))
    ci_gas = cols.get("GASTOS")
    ci_sal = cols.get("SALDO FINAL")
    monthly = datos["monthly"]
    for row in tabla.rows[1:]:
        etq = _norm(row.cells[0].text)
        if etq in MESES:
            m = MESES[etq]; d = monthly.get(m)
            if ci_dep is not None: _set_cell(row.cells[ci_dep], _money(d["dep"]) if d else "$0")
            if ci_gas is not None: _set_cell(row.cells[ci_gas], _money(d["gasto"]) if d else "$0")
            if ci_sal is not None: _set_cell(row.cells[ci_sal], _money(d["saldo"]) if d else "$0")
        elif etq == "TOTAL":
            if ci_dep is not None: _set_cell(row.cells[ci_dep], _money(datos["total_dep"]))
            if ci_gas is not None: _set_cell(row.cells[ci_gas], _money(datos["total_gasto"]))


def _fill_cat_mes(tabla, datos):
    """Tabla GASTOS×MESES — 1a col = categoría, encabezados = meses."""
    cols = _cols_por_encabezado(tabla)  # {ENERO:1, ...}
    mes_cols = {MESES[k]: v for k, v in cols.items() if k in MESES}
    cat_mes = {_norm(k): v for k, v in datos["cat_mes"].items()}
    for row in tabla.rows[1:]:
        cat = _norm(row.cells[0].text)
        porm = cat_mes.get(cat, {})
        for mnum, ci in mes_cols.items():
            _set_cell(row.cells[ci], _money(porm.get(mnum, 0)))


def _fill_cat_totales(tabla, datos):
    """Tabla GASTOS/TOTAL/PORCENTAJES — 1a col = categoría."""
    cols = _cols_por_encabezado(tabla)
    ci_tot = cols.get("TOTAL"); ci_pct = cols.get("PORCENTAJES", cols.get("PORCENTAJE"))
    cat_tot = {_norm(k): v for k, v in datos["cat_tot"].items()}
    total = datos["total_gastos"] or 1.0
    for row in tabla.rows[1:]:
        cat = _norm(row.cells[0].text)
        t = cat_tot.get(cat, 0.0)
        if ci_tot is not None: _set_cell(row.cells[ci_tot], _money(t))
        if ci_pct is not None: _set_cell(row.cells[ci_pct], _pct(t / total * 100))


def _fill_dist_mes(tabla, datos):
    """Tabla MES/PORCENTAJES — 1a col = nombre de mes."""
    cols = _cols_por_encabezado(tabla)
    ci_pct = cols.get("PORCENTAJES", cols.get("PORCENTAJE"))
    dist = datos["dist_mes"]
    for row in tabla.rows[1:]:
        etq = _norm(row.cells[0].text)
        if etq in MESES and ci_pct is not None:
            _set_cell(row.cells[ci_pct], _pct(dist.get(MESES[etq], 0)))


def _fill_colaboradores(tabla, datos):
    """Tabla NOMBRE/MONTO — longitud variable: rellena las filas de ejemplo con
    los colaboradores reales; sobra -> se vacían; falta -> se clonan filas."""
    colab = datos["colaboradores"]
    filas_datos = tabla.rows[1:]
    import copy
    # asegura suficientes filas clonando la primera fila de datos como molde
    if filas_datos and len(colab) > len(filas_datos):
        molde = filas_datos[-1]._tr
        for _ in range(len(colab) - len(filas_datos)):
            tabla._tbl.append(copy.deepcopy(molde))
    filas_datos = tabla.rows[1:]
    for idx, row in enumerate(filas_datos):
        if idx < len(colab):
            _set_cell(row.cells[0], colab[idx]["nombre"])
            _set_cell(row.cells[1], _money(colab[idx]["total"]))
        else:
            _set_cell(row.cells[0], "")
            _set_cell(row.cells[1], "")


def _identificar_y_rellenar(doc, datos):
    """Empareja cada tabla de la plantilla con su relleno por la firma de sus
    encabezados (robusto al orden)."""
    for t in doc.tables:
        heads = set(_cols_por_encabezado(t).keys())
        if {"DEPÓSITOS", "SALDO FINAL"} & heads or {"DEPOSITOS", "SALDO FINAL"} & heads:
            _fill_mensual(t, datos)
        elif "GASTOS" in heads and (heads & set(MESES.keys())):
            _fill_cat_mes(t, datos)
        elif {"GASTOS", "TOTAL", "PORCENTAJES"} <= heads or {"GASTOS", "TOTAL"} <= heads and "PORCENTAJES" in heads:
            _fill_cat_totales(t, datos)
        elif "MES" in heads and ("PORCENTAJES" in heads or "PORCENTAJE" in heads):
            _fill_dist_mes(t, datos)
        elif {"NOMBRE", "MONTO"} <= heads:
            _fill_colaboradores(t, datos)


def _reemplazar_graficas(doc, graficas_png_b64: list[str]):
    """Sobrescribe los bytes de las imágenes del cuerpo (en orden) con las
    gráficas nuevas. Solo toca tantas como gráficas haya; si sobran slots de
    ejemplo, se dejan (el encargado los borra si quiere)."""
    try:
        shapes = doc.inline_shapes
    except Exception:  # noqa: BLE001
        return
    for i, png_b64 in enumerate(graficas_png_b64):
        if i >= len(shapes):
            break
        try:
            rId = shapes[i]._inline.graphic.graphicData.pic.blipFill.blip.embed
            parte = doc.part.related_parts[rId]
            parte._blob = base64.b64decode(png_b64)
        except Exception:  # noqa: BLE001 — si una imagen no se puede reemplazar, seguimos
            continue


def rellenar_plantilla(
    plantilla_doc_id: str,
    nombre_cliente: str,
    datos: dict,
    graficas_png: list[str],
    notas: str,
    carpeta_id: Optional[str] = None,
) -> dict:
    svc = drive._get_service()
    docx_bytes = svc.files().export(fileId=plantilla_doc_id, mimeType=DOCX_MIME).execute()
    doc = Document(io.BytesIO(docx_bytes))

    _reemplazar_texto(doc, {
        "NOMBRE DE COMPAÑÍA": nombre_cliente,
        "Mes - Mes": datos.get("periodo_meses", ""),
        "Mes y Día, 2026": datos.get("fecha_hoy", ""),
    })
    _identificar_y_rellenar(doc, datos)
    _reemplazar_graficas(doc, graficas_png or [])

    if notas and notas.strip():
        # inserta las notas antes del cierre "Quedamos atentos..." si existe
        cierre = None
        for p in doc.paragraphs:
            if "QUEDAMOS ATENTOS" in _norm(p.text):
                cierre = p
                break
        for linea in notas.split("\n"):
            if cierre is not None:
                nuevo = cierre.insert_paragraph_before(linea)
            else:
                doc.add_paragraph(linea)

    out = io.BytesIO()
    doc.save(out)
    out.seek(0)
    media = MediaIoBaseUpload(out, mimetype=DOCX_MIME, resumable=False)
    meta = {"name": f"Entregable Contabilidad — {nombre_cliente} ({datos.get('periodo_meses','')})", "mimeType": GDOC_MIME}
    if carpeta_id:
        meta["parents"] = [carpeta_id]
    return svc.files().create(body=meta, media_body=media, fields="id, webViewLink").execute()
