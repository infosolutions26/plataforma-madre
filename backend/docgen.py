"""
Genera el entregable de contabilidad como Google Doc, reusando la plantilla de
membrete del usuario (encabezado/pie) SIN recrearla.

Estrategia (solo necesita el scope de Drive que ya tenemos, no el de Docs API):
1. Exporta la plantilla (un Google Doc) a .docx vía Drive — trae su encabezado
   y pie de página tal cual.
2. Abre ese .docx con python-docx y le ANEXA las tablas, las gráficas (como
   imágenes PNG que manda el navegador) y las notas. python-docx conserva el
   encabezado/pie de la sección al guardar.
3. Sube el .docx resultante a Drive convirtiéndolo a Google Doc, en la carpeta
   del cliente. Queda editable y se puede exportar a PDF desde ahí.

El id del Google Doc de la plantilla se guarda en Configuracion
(clave='plantilla_contabilidad_doc_id').
"""
from __future__ import annotations

import base64
import io
from typing import Optional

from docx import Document
from docx.shared import Inches, Pt, RGBColor
from googleapiclient.http import MediaIoBaseUpload

import drive

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
GDOC_MIME = "application/vnd.google-apps.document"


def _titulo(doc, texto: str):
    p = doc.add_paragraph()
    r = p.add_run(texto)
    r.bold = True
    r.font.size = Pt(13)
    r.font.color.rgb = RGBColor(0x07, 0x2E, 0x7D)  # azul Solutions
    return p


def _tabla(doc, columnas: list[str], filas: list[list[str]]):
    t = doc.add_table(rows=1, cols=len(columnas))
    try:
        t.style = "Table Grid"
    except KeyError:
        pass  # la plantilla no trae ese estilo; queda sin bordes, no es fatal
    hdr = t.rows[0].cells
    for i, c in enumerate(columnas):
        hdr[i].text = str(c)
        for par in hdr[i].paragraphs:
            for run in par.runs:
                run.bold = True
    for fila in filas:
        cells = t.add_row().cells
        for i, val in enumerate(fila):
            if i < len(cells):
                cells[i].text = str(val)
    return t


def generar_entregable_gdoc(
    plantilla_doc_id: str,
    nombre_cliente: str,
    periodo: str,
    bloques: list[dict],
    notas: str,
    carpeta_id: Optional[str] = None,
) -> dict:
    """Devuelve {id, webViewLink} del Google Doc creado."""
    svc = drive._get_service()
    docx_bytes = svc.files().export(fileId=plantilla_doc_id, mimeType=DOCX_MIME).execute()
    doc = Document(io.BytesIO(docx_bytes))

    doc.add_paragraph()
    _titulo(doc, f"Contabilidad — {nombre_cliente}")
    if periodo:
        doc.add_paragraph(periodo)

    for b in bloques:
        doc.add_paragraph()
        _titulo(doc, b.get("titulo", ""))
        if b.get("tipo") == "grafica" and b.get("png"):
            png = base64.b64decode(b["png"])
            doc.add_picture(io.BytesIO(png), width=Inches(6.0))
        elif b.get("tipo") == "tabla":
            _tabla(doc, b.get("columnas", []), b.get("filas", []))
        if b.get("subtitulo"):
            p = doc.add_paragraph(b["subtitulo"])
            for r in p.runs:
                r.font.size = Pt(9)
                r.italic = True

    if notas and notas.strip():
        doc.add_paragraph()
        _titulo(doc, "Notas")
        for linea in notas.split("\n"):
            doc.add_paragraph(linea)

    out = io.BytesIO()
    doc.save(out)
    out.seek(0)
    media = MediaIoBaseUpload(out, mimetype=DOCX_MIME, resumable=False)
    meta = {"name": f"Entregable Contabilidad — {nombre_cliente}", "mimeType": GDOC_MIME}
    if carpeta_id:
        meta["parents"] = [carpeta_id]
    f = svc.files().create(body=meta, media_body=media, fields="id, webViewLink").execute()
    return f
