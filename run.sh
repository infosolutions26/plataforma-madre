#!/usr/bin/env bash
# Arranca la Plataforma Madre (API).
set -e
cd "$(dirname "$0")"

if [ -f .env ]; then
  export $(grep -v '^#' .env | xargs)
fi

if [ -z "$SSN_ENCRYPTION_KEY" ]; then
  echo "⚠️  Falta SSN_ENCRYPTION_KEY. Genera una con:"
  echo "    python3 -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
  echo "    y ponla en tu .env (ver .env.example)"
  exit 1
fi

if [ ! -d .venv ]; then
  echo "📦 Creando entorno virtual e instalando dependencias..."
  python3 -m venv .venv
  ./.venv/bin/pip install -q --upgrade pip
  ./.venv/bin/pip install -q -r requirements.txt
fi

cd backend
if [ ! -f plataforma_madre.db ]; then
  echo "🌱 Sembrando datos de ejemplo..."
  ../.venv/bin/python seed.py
fi

echo "🚀 Abriendo en http://localhost:8000 (docs interactivos en /docs)"
exec ../.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
