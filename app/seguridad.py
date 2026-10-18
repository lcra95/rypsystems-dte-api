"""Cifrado de secretos y autenticación del sistema principal."""

import hmac

from cryptography.fernet import Fernet, InvalidToken


class Cifrador:
    def __init__(self, clave_maestra: str):
        self._fernet = Fernet(clave_maestra.encode() if isinstance(clave_maestra, str) else clave_maestra)

    def cifrar(self, datos: bytes) -> bytes:
        return self._fernet.encrypt(datos)

    def descifrar(self, datos: bytes) -> bytes:
        try:
            return self._fernet.decrypt(datos)
        except InvalidToken as e:
            raise RuntimeError('No se pudo descifrar un secreto: la clave maestra no corresponde.') from e


def token_valido(recibido: str, esperado: str) -> bool:
    return bool(esperado) and hmac.compare_digest(recibido.encode(), esperado.encode())


def firmar_webhook(cuerpo: bytes, secreto: str) -> str:
    return 'sha256=' + hmac.new(secreto.encode(), cuerpo, 'sha256').hexdigest()
