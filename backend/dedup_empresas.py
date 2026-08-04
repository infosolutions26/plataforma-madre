"""
Deduplica empresas que quedaron repetidas en el CRM (misma compañía escrita como
'SOLORIO WINDOWS' y 'SOLORIO WINDOWS LLC', o 'ABRIL & CIA' y 'ABRIL & CIA LLC').

Agrupa por nombre normalizado (quita LLC/INC/&/puntuación). Por cada grupo elige
una CANÓNICA (la que ya tiene datos de contabilidad; si empatan, la que tiene
sufijo LLC; si aún empatan, el id más bajo) y reasigna a ella TODO lo que apunta
a las demás — incluidas las tablas nuevas de contabilidad que el endpoint viejo
de fusión no cubría (contabilidad_mensual_historica, cuenta_bancaria, gasto,
ingreso) — y luego borra las duplicadas.

    python3 dedup_empresas.py [--dry-run] [--fuzzy]

--dry-run: muestra qué se fusionaría, sin escribir.
--fuzzy:   ADEMÁS lista (no fusiona) los pares parecidos por typo (REMODELLING vs
           REMODELING) para que un humano decida — nunca se fusionan solos.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from difflib import SequenceMatcher

from database import SessionLocal
import models  # noqa: F401 — registra Empresa/Persona en el mismo Base
from models import Archivo, Empresa, EmpresaDueno, FeeAnualPago, NotaCliente, Servicio
from contabilidad import (
    ContabilidadMensualHistorica,
    CorteEstadoCuenta,
    CuentaBancaria,
    Gasto,
    Ingreso,
)


def norm(nombre: str) -> str:
    n = nombre.upper()
    n = re.sub(r"[.,&]", " ", n)
    n = re.sub(r"\b(LLC|INC|CORP|CORPORATION|CO|INCORPORATED)\b\.?", "", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


# Palabras mal escritas encontradas en los nombres reales; al fusionar, se
# prefiere el nombre que NO las trae.
_TYPOS = {"REMODELLING", "BUSSINESS", "SVICES", "PAITING", "INMIGRATION", "BRITHER", "LOGISTIC"}


def _es_ascii(s: str) -> bool:
    return all(ord(c) < 128 for c in s)


def _cuenta_typos(nombre: str) -> int:
    palabras = set(re.sub(r"[^A-Z ]", " ", nombre.upper()).split())
    return len(palabras & _TYPOS)


def mejor_nombre(a: str, b: str) -> str:
    """Elige el nombre mejor escrito entre dos variantes de la misma empresa:
    primero el que sea ASCII puro (evita letras cirílicas que rompen la
    búsqueda), luego el que tenga menos palabras con typo."""
    if _es_ascii(a) != _es_ascii(b):
        return a if _es_ascii(a) else b
    ta, tb = _cuenta_typos(a), _cuenta_typos(b)
    if ta != tb:
        return a if ta < tb else b
    return a


def _tiene_contabilidad(db, empresa_id: int) -> bool:
    return bool(
        db.query(ContabilidadMensualHistorica).filter_by(empresa_id=empresa_id).first()
        or db.query(CuentaBancaria).filter_by(empresa_id=empresa_id).first()
        or db.query(Gasto).filter_by(empresa_id=empresa_id).first()
    )


def elegir_canonica(db, empresas: list[Empresa]) -> Empresa:
    con_datos = [e for e in empresas if _tiene_contabilidad(db, e.id)]
    if len(con_datos) == 1:
        return con_datos[0]
    candidatos = con_datos or empresas
    # prefiere sufijo LLC/INC, luego id más bajo (registro más antiguo)
    con_sufijo = [e for e in candidatos if re.search(r"\b(LLC|INC|CORP)\b", e.nombre.upper())]
    pool = con_sufijo or candidatos
    return min(pool, key=lambda e: e.id)


def reasignar_y_borrar(db, keeper: Empresa, dup: Empresa) -> None:
    kid, did = keeper.id, dup.id
    # tablas del CRM
    db.query(Servicio).filter(Servicio.empresa_id == did).update({"empresa_id": kid})
    db.query(NotaCliente).filter(NotaCliente.empresa_id == did).update({"empresa_id": kid})
    db.query(Archivo).filter(Archivo.empresa_id == did).update({"empresa_id": kid})
    # fee anual: respeta unique(empresa_id, anio)
    for fee in db.query(FeeAnualPago).filter(FeeAnualPago.empresa_id == did).all():
        choca = db.query(FeeAnualPago).filter_by(empresa_id=kid, anio=fee.anio).first()
        db.delete(fee) if choca else setattr(fee, "empresa_id", kid)
    # dueños: respeta PK (persona_id, empresa_id)
    for ed in db.query(EmpresaDueno).filter(EmpresaDueno.empresa_id == did).all():
        choca = db.get(EmpresaDueno, {"persona_id": ed.persona_id, "empresa_id": kid})
        db.delete(ed) if choca else setattr(ed, "empresa_id", kid)
    # tablas nuevas de contabilidad
    db.query(CuentaBancaria).filter(CuentaBancaria.empresa_id == did).update({"empresa_id": kid})
    db.query(Gasto).filter(Gasto.empresa_id == did).update({"empresa_id": kid})
    db.query(Ingreso).filter(Ingreso.empresa_id == did).update({"empresa_id": kid})
    for h in db.query(ContabilidadMensualHistorica).filter_by(empresa_id=did).all():
        choca = db.query(ContabilidadMensualHistorica).filter_by(
            empresa_id=kid, anio=h.anio, mes=h.mes, fuente_archivo=h.fuente_archivo
        ).first()
        db.delete(h) if choca else setattr(h, "empresa_id", kid)
    # rellena huecos del keeper con datos del duplicado
    for campo in ("ein", "giro", "telefono", "correo", "drive_folder_id", "ghl_contact_id"):
        if not getattr(keeper, campo) and getattr(dup, campo):
            setattr(keeper, campo, getattr(dup, campo))
    # conserva el nombre mejor escrito de los dos
    keeper.nombre = mejor_nombre(keeper.nombre, dup.nombre)
    db.flush()
    db.delete(dup)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--fuzzy", action="store_true", help="También lista pares parecidos por typo (no los fusiona)")
    ap.add_argument("--confirmar-fuzzy", action="store_true", help="Fusiona los pares parecidos (≥0.88) MENOS los que el usuario marcó como distintos")
    args = ap.parse_args()

    db = SessionLocal()
    empresas = db.query(Empresa).all()
    grupos: dict[str, list[Empresa]] = defaultdict(list)
    for e in empresas:
        grupos[norm(e.nombre)].append(e)
    dups = {k: v for k, v in grupos.items() if len(v) > 1}

    print(f"{len(empresas)} empresas; {len(dups)} grupos de duplicados exactos (tras normalizar).\n")
    fusionadas = 0
    for k, grupo in sorted(dups.items()):
        keeper = elegir_canonica(db, grupo)
        otros = [e for e in grupo if e.id != keeper.id]
        marca = "📊" if _tiene_contabilidad(db, keeper.id) else "  "
        print(f"[{marca}] MANTENER #{keeper.id} {keeper.nombre!r}")
        for d in otros:
            print(f"       fusionar  #{d.id} {d.nombre!r} -> #{keeper.id}")
            if not args.dry_run:
                reasignar_y_borrar(db, keeper, d)
            fusionadas += 1

    # Pares parecidos que el usuario confirmó como EMPRESAS DISTINTAS: no fusionar.
    EXCLUIR_FUZZY = [
        frozenset({"H L MULTISERVICES", "H S MULTISERVICES"}),
        frozenset({"ROJAS SERVICES", "RS SERVICES"}),
    ]

    if args.fuzzy or args.confirmar_fuzzy:
        claves = sorted(grupos.keys())
        pares = []
        vistos = set()
        for i, a in enumerate(claves):
            for b in claves[i + 1:]:
                if a in vistos or b in vistos:
                    continue
                r = SequenceMatcher(None, a, b).ratio()
                if r >= 0.88 and a != b:
                    pares.append((r, a, b))
                    vistos.add(b)

        if args.confirmar_fuzzy:
            print("\n--- Fusionando pares parecidos confirmados ---")
            for r, a, b in pares:
                if frozenset({a, b}) in EXCLUIR_FUZZY:
                    print(f"  [SALTAR — distintas] {grupos[a][0].nombre}  <->  {grupos[b][0].nombre}")
                    continue
                combinados = grupos[a] + grupos[b]
                keeper = elegir_canonica(db, combinados)
                otros = [e for e in combinados if e.id != keeper.id]
                marca = "📊" if _tiene_contabilidad(db, keeper.id) else "  "
                print(f"[{marca}] MANTENER #{keeper.id} {keeper.nombre!r}")
                for d in otros:
                    print(f"       fusionar  #{d.id} {d.nombre!r} -> #{keeper.id}")
                    if not args.dry_run:
                        reasignar_y_borrar(db, keeper, d)
                    fusionadas += 1
        else:
            print("\n--- PARES PARECIDOS (posible typo) — NO se fusionan, revísalos a mano ---")
            for r, a, b in pares:
                print(f"  {r:.0%}  {[e.nombre for e in grupos[a]]}  <->  {[e.nombre for e in grupos[b]]}")

    if args.dry_run:
        db.rollback()
        print(f"\n--dry-run: se habrían fusionado {fusionadas} duplicadas. Nada se escribió.")
    else:
        db.commit()
        print(f"\nListo: {fusionadas} empresas duplicadas fusionadas y borradas.")
    db.close()


if __name__ == "__main__":
    main()
