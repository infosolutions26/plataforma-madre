"""Genera el PDF del recibo de nómina — sube a la carpeta de Drive del
trabajador cuando se marca un pago como realizado."""

import io

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet


def _money(n):
    return "${:,.2f}".format(float(n or 0))


def generar_recibo_pdf(pago, trabajador, detalle_comisiones):
    """pago: PagoNomina. trabajador: Trabajador. detalle_comisiones: lista de
    dicts {fecha, cliente, tipo_servicio, monto}."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, topMargin=0.6 * inch, bottomMargin=0.6 * inch)
    styles = getSampleStyleSheet()
    story = []

    story.append(Paragraph("Solutions Taxes — Recibo de pago", styles["Title"]))
    story.append(Spacer(1, 6))
    if pago.tipo == "ajuste_inicial":
        story.append(Paragraph("AJUSTE INICIAL — no representa una transferencia real de dinero", styles["Heading3"]))
    story.append(Paragraph(f"Trabajador: <b>{trabajador.nombre}</b>", styles["Normal"]))
    story.append(Paragraph(f"Fecha de pago: <b>{pago.fecha.strftime('%m/%d/%Y')}</b>", styles["Normal"]))
    story.append(Paragraph(f"Recibo #: {pago.id}", styles["Normal"]))
    story.append(Spacer(1, 14))

    resumen_rows = [["Concepto", "Monto"]]
    if float(pago.sueldo_incluido or 0) > 0:
        resumen_rows.append(["Sueldo fijo", _money(pago.sueldo_incluido)])
    if float(pago.comisiones_incluidas or 0) > 0:
        resumen_rows.append([f"Comisiones ({pago.n_servicios} servicio(s))", _money(pago.comisiones_incluidas)])
    if float(pago.extra_monto or 0) > 0:
        resumen_rows.append([pago.extra_concepto or "Extra / bono", _money(pago.extra_monto)])
    resumen_rows.append(["TOTAL", _money(pago.monto)])

    t = Table(resumen_rows, colWidths=[4 * inch, 1.8 * inch])
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.75, colors.black),
        ("LINEABOVE", (0, -1), (-1, -1), 0.75, colors.black),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(t)
    story.append(Spacer(1, 18))

    if pago.concepto:
        story.append(Paragraph(f"<b>Nota:</b> {pago.concepto}", styles["Normal"]))
        story.append(Spacer(1, 14))

    if detalle_comisiones:
        story.append(Paragraph("Servicios incluidos en este pago", styles["Heading3"]))
        rows = [["Fecha", "Cliente", "Servicio", "Comisión"]]
        for d in detalle_comisiones:
            rows.append([d["fecha"], d["cliente"], d["tipo_servicio"], _money(d["monto"])])
        dt = Table(rows, colWidths=[0.9 * inch, 2.4 * inch, 1.7 * inch, 0.9 * inch])
        dt.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.black),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("ALIGN", (3, 0), (3, -1), "RIGHT"),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(dt)

    doc.build(story)
    return buf.getvalue()
