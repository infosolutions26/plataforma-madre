"""
Importación masiva del histórico de contabilidades desde Drive (~110 carpetas
de compañía, cada una con subcarpetas por año y un Sheet de "vaciado" por año).

Requiere GOOGLE_SERVICE_ACCOUNT_JSON en el entorno (la misma cuenta de
servicio que ya usa drive.py). Correr una sola vez:

    python3 importar_historico_contabilidad.py [--dry-run] [--carpeta-raiz ID]

--dry-run: recorre Drive, parsea todo, imprime el reporte, pero NO escribe
nada en la base de datos. Úsalo primero para revisar antes de confirmar.

Es idempotente: usa (empresa_id, anio, mes, fuente_archivo) como llave única,
así que correrlo dos veces no duplica filas — si un año ya se importó, lo
vuelve a leer pero el INSERT se salta por el conflicto (ON CONFLICT DO NOTHING
vía chequeo previo con SELECT).
"""
from __future__ import annotations

import argparse
import io
import re
import sys
import tempfile

from database import Base, SessionLocal, engine
from models import Empresa
from contabilidad import CATEGORIAS_SEED, CategoriaGasto, ContabilidadMensualHistorica
from vaciado_parser import parse_vaciado
import drive

RAIZ_DEFAULT = "1utxkCspaQDpvmF3ZxjPd5oilrxvOA0AU"  # carpeta de Drive compartida por el usuario
SPREADSHEET_MIME = "application/vnd.google-apps.spreadsheet"
FOLDER_MIME = "application/vnd.google-apps.folder"


def listar_hijos(svc, folder_id: str) -> list[dict]:
    archivos, page_token = [], None
    while True:
        res = svc.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields="nextPageToken, files(id, name, mimeType)",
            pageSize=200,
            pageToken=page_token,
        ).execute()
        archivos.extend(res.get("files", []))
        page_token = res.get("nextPageToken")
        if not page_token:
            break
    return archivos


def limpia_nombre_carpeta(nombre: str) -> str:
    return re.sub(r"[-\s]*CONTAS?$", "", nombre, flags=re.IGNORECASE).strip()


def normaliza_para_match(nombre: str) -> str:
    n = nombre.upper()
    n = re.sub(r"[.,&]", " ", n)
    n = re.sub(r"\b(LLC|INC|CORP|CORPORATION|CO|INCORPORATED)\b\.?", "", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def elegir_spreadsheet_vaciado(archivos: list[dict]) -> dict | None:
    """De los archivos de una carpeta (año), elige el spreadsheet de vaciado.
    Prefiere el que combina crédito y débito si hay más de uno, por indicación
    explícita del usuario."""
    hojas = [f for f in archivos if f["mimeType"] == SPREADSHEET_MIME]
    if not hojas:
        return None
    combinadas = [f for f in hojas if "CRÉDITO" in f["name"].upper() and "DÉBITO" in f["name"].upper()]
    if combinadas:
        return combinadas[0]
    debito = [f for f in hojas if "DÉBITO" in f["name"].upper() or "DEBITO" in f["name"].upper()]
    if debito:
        return debito[0]
    # último recurso: cualquier spreadsheet que mencione "contabilidad"
    con_nombre = [f for f in hojas if "CONTABILIDAD" in f["name"].upper()]
    return con_nombre[0] if con_nombre else hojas[0]


def extraer_anio(nombre_carpeta_anio: str | None, nombre_archivo: str) -> int | None:
    fuente = nombre_carpeta_anio or nombre_archivo
    m = re.search(r"20\d{2}", fuente)
    if m:
        return int(m.group(0))
    m = re.search(r"20\d{2}", nombre_archivo)
    return int(m.group(0)) if m else None


def descargar_como_xlsx_bytes(svc, file_id: str) -> bytes:
    from googleapiclient.http import MediaIoBaseDownload

    request = svc.files().export_media(
        fileId=file_id,
        mimeType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--carpeta-raiz", default=RAIZ_DEFAULT)
    ap.add_argument("--limite", type=int, default=None, help="Solo procesa las primeras N compañías (para probar)")
    args = ap.parse_args()

    if not drive.disponible():
        print("ERROR: falta GOOGLE_SERVICE_ACCOUNT_JSON en el entorno.", file=sys.stderr)
        sys.exit(1)

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    # Semilla de categorías si no existen
    if db.query(CategoriaGasto).count() == 0:
        for orden, (nombre, deducible) in enumerate(CATEGORIAS_SEED):
            db.add(CategoriaGasto(nombre=nombre, es_deducible=deducible, orden=orden))
        db.commit()
        print(f"Sembradas {len(CATEGORIAS_SEED)} categorías.")

    deducibles = {n for n, ded in CATEGORIAS_SEED if ded}

    empresas_existentes = {normaliza_para_match(e.nombre): e for e in db.query(Empresa).all()}

    svc = drive._get_service()
    carpetas_empresa = [f for f in listar_hijos(svc, args.carpeta_raiz) if f["mimeType"] == FOLDER_MIME]
    if args.limite:
        carpetas_empresa = carpetas_empresa[: args.limite]

    print(f"{len(carpetas_empresa)} carpetas de compañía encontradas.\n")

    resumen_ok, resumen_advertencias, filas_nuevas = 0, 0, 0

    for carpeta in carpetas_empresa:
        nombre_limpio = limpia_nombre_carpeta(carpeta["name"])
        clave = normaliza_para_match(nombre_limpio)
        empresa = empresas_existentes.get(clave)
        creada = False
        if empresa is None:
            empresa = Empresa(nombre=nombre_limpio)
            db.add(empresa)
            db.flush()  # asigna empresa.id sin commitear todavía
            empresas_existentes[clave] = empresa
            creada = True

        hijos = listar_hijos(svc, carpeta["id"])
        carpetas_anio = [f for f in hijos if f["mimeType"] == FOLDER_MIME and re.fullmatch(r"20\d{2}", f["name"].strip())]
        # Si no hay subcarpetas de año, trata la carpeta misma como el único "año" a revisar.
        contenedores = [(f["name"], f["id"]) for f in carpetas_anio] or [(None, carpeta["id"])]

        archivos_procesados = 0
        for nombre_anio, contenedor_id in contenedores:
            archivos = listar_hijos(svc, contenedor_id)
            hoja = elegir_spreadsheet_vaciado(archivos)
            if hoja is None:
                continue
            anio = extraer_anio(nombre_anio, hoja["name"])
            if anio is None:
                print(f"  [{carpeta['name']}] no se pudo determinar el año de '{hoja['name']}', se omite.")
                resumen_advertencias += 1
                continue

            try:
                xlsx_bytes = descargar_como_xlsx_bytes(svc, hoja["id"])
            except Exception as e:  # noqa: BLE001
                print(f"  [{carpeta['name']}] error descargando '{hoja['name']}': {e}")
                resumen_advertencias += 1
                continue

            with tempfile.NamedTemporaryFile(suffix=".xlsx") as tmp:
                tmp.write(xlsx_bytes)
                tmp.flush()
                resultado = parse_vaciado(tmp.name, deducibles)

            if resultado.advertencias:
                print(f"  [{carpeta['name']} {anio}] {hoja['name']}: {resultado.advertencias}")
                resumen_advertencias += 1

            for mes, datos in resultado.meses.items():
                ya_existe = (
                    db.query(ContabilidadMensualHistorica)
                    .filter_by(empresa_id=empresa.id, anio=anio, mes=mes, fuente_archivo=hoja["name"])
                    .first()
                )
                if ya_existe:
                    continue
                fila = ContabilidadMensualHistorica(
                    empresa_id=empresa.id,
                    nombre_empresa_original=carpeta["name"],
                    anio=anio,
                    mes=mes,
                    ingreso_total=datos["ingreso_total"],
                    gasto_total_deducible=datos["gasto_total_deducible"],
                    saldo_final=datos.get("saldo_final"),
                    gasto_por_categoria=datos["gasto_por_categoria"],
                    fuente_archivo=hoja["name"],
                    fuente_file_id=hoja["id"],
                )
                if not args.dry_run:
                    db.add(fila)
                filas_nuevas += 1
            archivos_procesados += 1

        estado = "creada" if creada else "match"
        print(f"[{estado:6s}] {carpeta['name']!r} -> Empresa #{empresa.id or '?'} {nombre_limpio!r} "
              f"({archivos_procesados} archivo(s) leído(s))")
        resumen_ok += 1

    if args.dry_run:
        db.rollback()
        print(f"\n--dry-run: {resumen_ok} compañías revisadas, {filas_nuevas} filas SE HABRÍAN creado, "
              f"{resumen_advertencias} advertencias. Nada se escribió en la base.")
    else:
        db.commit()
        print(f"\nListo: {resumen_ok} compañías procesadas, {filas_nuevas} filas nuevas de "
              f"contabilidad_mensual_historica, {resumen_advertencias} advertencias.")

    db.close()


if __name__ == "__main__":
    main()
