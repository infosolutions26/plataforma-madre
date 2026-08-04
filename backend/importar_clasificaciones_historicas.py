"""
Siembra el diccionario global `Comercio` con las clasificaciones ya hechas a
mano en los Sheets de vaciado de Drive (hojas de mes, detalle transacción por
transacción). Esto es el "aprendizaje inicial" del sistema nuevo: al terminar,
cada comercio conocido (HOME DEPOT, STUDIO 41, JOSE...) ya trae una categoría
sugerida basada en cómo se clasificó históricamente, de TODOS los clientes
juntos (el diccionario es global, no por cliente — ver contabilidad.py).

    python3 importar_clasificaciones_historicas.py [--dry-run] [--limite N]

--dry-run: recorre y agrega todo, imprime stats, NO escribe en la base.

Idempotente: usa nombre_normalizado como llave única. Correrlo otra vez sobre
los mismos archivos re-suma los conteos, así que para re-importar limpio hay
que vaciar la tabla comercio primero (el script avisa y pide --reset explícito).
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from collections import Counter, defaultdict

from database import Base, SessionLocal, engine
from contabilidad import CATEGORIAS_SEED, CategoriaGasto, Comercio
from comercio_parser import normaliza_comercio, parse_workbook_mensual
from importar_historico_contabilidad import (
    FOLDER_MIME,
    RAIZ_DEFAULT,
    SPREADSHEET_MIME,
    descargar_como_xlsx_bytes,
    elegir_spreadsheet_vaciado,
    listar_hijos,
)
import drive
import re


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--carpeta-raiz", default=RAIZ_DEFAULT)
    ap.add_argument("--limite", type=int, default=None, help="Solo las primeras N compañías (para probar)")
    ap.add_argument("--reset", action="store_true", help="Vacía la tabla comercio antes de importar (para re-seed limpio)")
    args = ap.parse_args()

    if not drive.disponible():
        print("ERROR: falta GOOGLE_SERVICE_ACCOUNT_JSON en el entorno.", file=sys.stderr)
        sys.exit(1)

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    # Categorías (para resolver categoria_sugerida_id por nombre)
    if db.query(CategoriaGasto).count() == 0:
        for orden, (nombre, deducible) in enumerate(CATEGORIAS_SEED):
            db.add(CategoriaGasto(nombre=nombre, es_deducible=deducible, orden=orden))
        db.commit()
    cat_por_nombre = {c.nombre: c for c in db.query(CategoriaGasto).all()}

    if args.reset and not args.dry_run:
        n = db.query(Comercio).delete()
        db.commit()
        print(f"--reset: {n} comercios borrados antes de importar.\n")

    svc = drive._get_service()
    carpetas = [f for f in listar_hijos(svc, args.carpeta_raiz) if f["mimeType"] == FOLDER_MIME]
    if args.limite:
        carpetas = carpetas[: args.limite]
    print(f"{len(carpetas)} carpetas de compañía.\n")

    dicc: dict[str, Counter] = defaultdict(Counter)   # comercio_norm -> Counter(categoria)
    tx_total = 0
    archivos_leidos = 0

    for carpeta in carpetas:
        hijos = listar_hijos(svc, carpeta["id"])
        anios = [f for f in hijos if f["mimeType"] == FOLDER_MIME and re.fullmatch(r"20\d{2}", f["name"].strip())]
        contenedores = [(f["name"], f["id"]) for f in anios] or [(None, carpeta["id"])]
        tx_compania = 0
        for _nombre_anio, cont_id in contenedores:
            archs = listar_hijos(svc, cont_id)
            hoja = elegir_spreadsheet_vaciado(archs)  # mismo criterio que el import de totales: prefiere combinado
            if hoja is None:
                continue
            try:
                xlsx = descargar_como_xlsx_bytes(svc, hoja["id"])
            except Exception as e:  # noqa: BLE001
                print(f"  [{carpeta['name']}] error descargando '{hoja['name']}': {e}")
                continue
            with tempfile.NamedTemporaryFile(suffix=".xlsx") as tmp:
                tmp.write(xlsx)
                tmp.flush()
                txs = parse_workbook_mensual(tmp.name)
            archivos_leidos += 1
            for _monto, raw, cat in txs:
                clave = normaliza_comercio(raw)
                if clave:
                    dicc[clave][cat] += 1
            tx_compania += len(txs)
        tx_total += tx_compania
        print(f"[{carpeta['name'][:45]:45s}] {tx_compania} transacciones")

    # ---- resumen ----
    print(f"\n{'='*60}")
    print(f"{archivos_leidos} archivos leídos, {tx_total} transacciones, {len(dicc)} comercios distintos.")
    dist_cat = Counter()
    ambiguos = 0
    for cnt in dicc.values():
        dist_cat[cnt.most_common(1)[0][0]] += 1
        if len(cnt) > 1:
            ambiguos += 1
    print(f"{ambiguos} comercios con más de una categoría en su historial (gana la más frecuente).")
    print("\nComercios por categoría sugerida:")
    for cat, n in dist_cat.most_common():
        print(f"  {n:5d}  {cat}")
    print("\nTop 30 comercios por frecuencia:")
    for com, cnt in sorted(dicc.items(), key=lambda kv: -sum(kv[1].values()))[:30]:
        cat_top = cnt.most_common(1)[0][0]
        print(f"  {sum(cnt.values()):4d}x  {com[:38]:38s} -> {cat_top}")

    if args.dry_run:
        print("\n--dry-run: nada se escribió en la base.")
        db.rollback()
        db.close()
        return

    # ---- escribir a la tabla Comercio (upsert por nombre_normalizado) ----
    existentes = {c.nombre_normalizado: c for c in db.query(Comercio).all()}
    creados, actualizados = 0, 0
    for clave, cnt in dicc.items():
        cat_top = cnt.most_common(1)[0][0]
        cat_obj = cat_por_nombre.get(cat_top)
        total = sum(cnt.values())
        com = existentes.get(clave)
        if com is None:
            db.add(Comercio(
                nombre_normalizado=clave,
                nombre_editado=clave.title(),  # default legible; el encargado lo puede afinar
                categoria_sugerida_id=cat_obj.id if cat_obj else None,
                veces_usado=total,
            ))
            creados += 1
        else:
            com.veces_usado += total
            if cat_obj:
                com.categoria_sugerida_id = cat_obj.id
            actualizados += 1
    db.commit()
    print(f"\nListo: {creados} comercios creados, {actualizados} actualizados en la tabla comercio.")
    db.close()


if __name__ == "__main__":
    main()
