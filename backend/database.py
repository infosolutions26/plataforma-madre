import os
from typing import Optional

from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./plataforma_madre.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


_fernet: Optional[Fernet] = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        key = os.environ.get("SSN_ENCRYPTION_KEY")
        if not key:
            raise RuntimeError(
                "Falta SSN_ENCRYPTION_KEY en el entorno. Genera una con:\n"
                "  python3 -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )
        _fernet = Fernet(key.encode())
    return _fernet


def encrypt_ssn(ssn_full: Optional[str]) -> Optional[str]:
    """Cifra un SSN/ITIN completo antes de guardarlo. None pasa igual."""
    if not ssn_full:
        return None
    return _get_fernet().encrypt(ssn_full.encode()).decode()


def decrypt_ssn(ssn_encrypted: Optional[str]) -> Optional[str]:
    """Descifra un SSN/ITIN completo. Úsese solo en el endpoint de 'revelar', nunca en listas."""
    if not ssn_encrypted:
        return None
    return _get_fernet().decrypt(ssn_encrypted.encode()).decode()


# Nota de diseño: ssn_last4 se guarda como columna plana aparte (no cifrada) en el
# momento de crear/editar la persona — así el perfil y las listas nunca necesitan
# descifrar nada para mostrar "•••-••-1234". Solo el botón "revelar" llama a
# decrypt_ssn(), y ese evento debería quedar en un log de acceso (pendiente de
# implementar una tabla log_acceso cuando se conecte la auth real).
