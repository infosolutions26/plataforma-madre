"""Integración con Google Drive vía cuenta de servicio con delegación de dominio.

El backend es el único que habla con Drive — ningún trabajador necesita
permisos propios sobre las carpetas de clientes. Esto evita depender de la
cuenta de Google personal de cada quien (fuente de los problemas de acceso
que tuvimos con el árbol de "Preparadores").

Las cuentas de servicio no tienen cuota de almacenamiento propia (Google
bloquea la subida de archivos con "storageQuotaExceeded" aunque listar/leer
sí funcione). Por eso la cuenta de servicio actúa POR DELEGACIÓN como
IMPERSONAR (domain-wide delegation, ya configurada en el Workspace) — todo
lo que se crea queda con dueño real y cuota real, no hay que compartir nada
después. Requiere GOOGLE_SERVICE_ACCOUNT_JSON con el JSON de la cuenta de
servicio.

Las carpetas raíz ("Plataforma Madre - Clientes/Trabajadores") se crean de
forma perezosa la primera vez que se usan, ya directamente bajo la cuenta
impersonada — es una estructura nueva, independiente del árbol viejo de
"Preparadores" (ese se deja intacto, solo se sigue leyendo para los clientes
que ya tienen su drive_folder_id apuntando ahí).
"""

import io
import json
import os
import re
import threading

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

SCOPES = ["https://www.googleapis.com/auth/drive"]
ROOT_FOLDER_NAME = "Plataforma Madre - Clientes"
ROOT_TRABAJADORES_NAME = "Plataforma Madre - Trabajadores"
IMPERSONAR = "documentos@solutionstaxes.com"

_lock = threading.Lock()
_service = None
_root_folder_id = None
_root_trabajadores_id = None


def disponible() -> bool:
    return bool(os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"))


def _get_service():
    global _service
    if _service is None:
        with _lock:
            if _service is None:
                raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
                if not raw:
                    raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON no está configurada")
                info = json.loads(raw)
                creds = service_account.Credentials.from_service_account_info(
                    info, scopes=SCOPES
                ).with_subject(IMPERSONAR)
                _service = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _service


def extraer_folder_id(drive_url: str):
    """Saca el id de carpeta de un link tipo https://drive.google.com/drive/folders/<id>."""
    if not drive_url:
        return None
    m = re.search(r"/folders/([a-zA-Z0-9_-]+)", drive_url)
    return m.group(1) if m else None


def extraer_file_id(drive_url: str):
    """Saca el id de archivo de un link tipo https://drive.google.com/file/d/<id>/view."""
    if not drive_url:
        return None
    m = re.search(r"/file/d/([a-zA-Z0-9_-]+)", drive_url)
    return m.group(1) if m else None


def _get_or_create_root(nombre: str):
    svc = _get_service()
    q = (
        f"name = '{nombre}' and mimeType = 'application/vnd.google-apps.folder' "
        "and trashed = false and 'me' in owners"
    )
    res = svc.files().list(q=q, fields="files(id)", pageSize=1).execute()
    files = res.get("files", [])
    if files:
        return files[0]["id"]
    meta = {"name": nombre, "mimeType": "application/vnd.google-apps.folder"}
    carpeta = svc.files().create(body=meta, fields="id").execute()
    return carpeta["id"]


def _root_folder():
    global _root_folder_id
    if not _root_folder_id:
        _root_folder_id = _get_or_create_root(ROOT_FOLDER_NAME)
    return _root_folder_id


def _root_trabajadores():
    global _root_trabajadores_id
    if not _root_trabajadores_id:
        _root_trabajadores_id = _get_or_create_root(ROOT_TRABAJADORES_NAME)
    return _root_trabajadores_id


def crear_carpeta_cliente(nombre: str, ssn_last4: str = None) -> str:
    """Crea la carpeta de un cliente nuevo bajo la carpeta raíz. Devuelve el folder id."""
    svc = _get_service()
    titulo = nombre.strip()
    if ssn_last4:
        titulo = f"{titulo} ({ssn_last4})"
    meta = {
        "name": titulo,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [_root_folder()],
    }
    carpeta = svc.files().create(body=meta, fields="id").execute()
    return carpeta["id"]


def crear_carpeta_trabajador(nombre: str) -> str:
    """Crea la carpeta de un trabajador (donde caen sus recibos de nómina)."""
    svc = _get_service()
    meta = {
        "name": nombre.strip(),
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [_root_trabajadores()],
    }
    carpeta = svc.files().create(body=meta, fields="id").execute()
    return carpeta["id"]


def crear_subcarpeta(parent_folder_id: str, nombre: str) -> dict:
    """Subcarpeta genérica dentro de cualquier carpeta ya existente (ej. una
    carpeta de cliente) — para separar documentos por año."""
    svc = _get_service()
    meta = {"name": nombre.strip(), "mimeType": "application/vnd.google-apps.folder", "parents": [parent_folder_id]}
    return svc.files().create(body=meta, fields="id, name, mimeType, createdTime").execute()


def obtener_o_crear_subcarpeta(parent_folder_id: str, nombre: str) -> str:
    """Como crear_subcarpeta, pero primero busca si ya existe una con ese
    nombre bajo el mismo padre — para no duplicar la carpeta del año cada
    vez que se sube una forma nueva."""
    svc = _get_service()
    nombre = nombre.strip()
    q = (
        f"name = '{nombre}' and '{parent_folder_id}' in parents "
        "and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    )
    res = svc.files().list(q=q, fields="files(id)", pageSize=1).execute()
    files = res.get("files", [])
    if files:
        return files[0]["id"]
    return crear_subcarpeta(parent_folder_id, nombre)["id"]


def listar_archivos(folder_id: str):
    svc = _get_service()
    res = svc.files().list(
        q=f"'{folder_id}' in parents and trashed = false",
        fields="files(id, name, mimeType, webViewLink, thumbnailLink, createdTime, size)",
        orderBy="createdTime desc",
        pageSize=200,
    ).execute()
    return res.get("files", [])


def subir_archivo(folder_id: str, filename: str, content: bytes, mimetype: str):
    svc = _get_service()
    media = MediaIoBaseUpload(io.BytesIO(content), mimetype=mimetype or "application/octet-stream", resumable=False)
    meta = {"name": filename, "parents": [folder_id]}
    archivo = svc.files().create(body=meta, media_body=media, fields="id, name, mimeType, webViewLink, createdTime, size").execute()
    return archivo


def obtener_contenido(file_id: str):
    """Devuelve (bytes, mimetype, filename) descargando el archivo desde Drive."""
    svc = _get_service()
    meta = svc.files().get(fileId=file_id, fields="name, mimeType").execute()
    buf = io.BytesIO()
    request = svc.files().get_media(fileId=file_id)
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue(), meta.get("mimeType", "application/octet-stream"), meta.get("name", "archivo")


def eliminar_archivo(file_id: str):
    svc = _get_service()
    svc.files().delete(fileId=file_id).execute()


def info_carpeta(folder_id: str) -> dict:
    """Diagnóstico: dueño, si está compartida, y con quién — para investigar
    por qué una carpeta se ve vacía al abrirla directo en Drive aunque la
    API sí liste archivos dentro (típicamente un tema de permisos, no de
    que el archivo no exista)."""
    svc = _get_service()
    meta = svc.files().get(
        fileId=folder_id, fields="id,name,owners,parents,webViewLink,shared,trashed", supportsAllDrives=True
    ).execute()
    permisos = svc.permissions().list(
        fileId=folder_id, fields="permissions(id,type,role,emailAddress,displayName)", supportsAllDrives=True
    ).execute()
    return {"meta": meta, "permisos": permisos.get("permissions", [])}
