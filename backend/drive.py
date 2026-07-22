"""Integración con Google Drive vía cuenta de servicio.

El backend es el único que habla con Drive — ningún trabajador necesita
permisos propios sobre las carpetas de clientes. Esto evita depender de la
cuenta de Google personal de cada quien (fuente de los problemas de acceso
que tuvimos con el árbol de "Preparadores").

Requiere la variable de entorno GOOGLE_SERVICE_ACCOUNT_JSON con el contenido
completo del JSON de la cuenta de servicio. La carpeta raíz de clientes
nuevos se crea de forma perezosa (lazy) la primera vez que se usa y se
comparte automáticamente con documentos@solutionstaxes.com para que el
negocio conserve acceso nativo desde Drive como respaldo.
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
COMPARTIR_CON = "documentos@solutionstaxes.com"

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
                creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
                _service = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _service


def extraer_folder_id(drive_url: str):
    """Saca el id de carpeta de un link tipo https://drive.google.com/drive/folders/<id>."""
    if not drive_url:
        return None
    m = re.search(r"/folders/([a-zA-Z0-9_-]+)", drive_url)
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
    folder_id = carpeta["id"]
    _compartir(folder_id, COMPARTIR_CON)
    return folder_id


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


def _compartir(file_id: str, correo: str, rol: str = "writer"):
    svc = _get_service()
    try:
        svc.permissions().create(
            fileId=file_id, body={"type": "user", "role": rol, "emailAddress": correo},
            sendNotificationEmail=False,
        ).execute()
    except Exception:
        pass  # no crítico — el archivo/carpeta sigue siendo accesible vía la plataforma


def crear_carpeta_cliente(nombre: str, ssn_last4: str = None) -> str:
    """Crea la carpeta de un cliente nuevo bajo la carpeta raíz y la comparte
    con el negocio para acceso nativo de respaldo. Devuelve el folder id."""
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
    folder_id = carpeta["id"]
    _compartir(folder_id, COMPARTIR_CON)
    return folder_id


def crear_carpeta_trabajador(nombre: str) -> str:
    """Crea la carpeta de un trabajador (donde caen sus recibos de nómina)."""
    svc = _get_service()
    meta = {
        "name": nombre.strip(),
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [_root_trabajadores()],
    }
    carpeta = svc.files().create(body=meta, fields="id").execute()
    folder_id = carpeta["id"]
    _compartir(folder_id, COMPARTIR_CON)
    return folder_id


def crear_subcarpeta(parent_folder_id: str, nombre: str) -> dict:
    """Subcarpeta genérica dentro de cualquier carpeta ya existente (ej. una
    carpeta de cliente) — para separar documentos por año."""
    svc = _get_service()
    meta = {"name": nombre.strip(), "mimeType": "application/vnd.google-apps.folder", "parents": [parent_folder_id]}
    return svc.files().create(body=meta, fields="id, name, mimeType, createdTime").execute()


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
