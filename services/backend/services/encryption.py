from __future__ import annotations

import base64
import binascii
import hashlib
import hmac

from cryptography.fernet import Fernet, InvalidToken


class EncryptionError(RuntimeError):
    pass


class EncryptionService:
    def __init__(self, key: str) -> None:
        try:
            decoded = base64.urlsafe_b64decode(key.encode())
            if len(decoded) != 32:
                raise ValueError
            self._fernet = Fernet(key.encode())
            self._fingerprint_key = decoded
        except (ValueError, TypeError, binascii.Error) as exc:
            raise EncryptionError("APP_ENCRYPTION_KEY must be a urlsafe base64 Fernet key") from exc

    def encrypt(self, plaintext: str) -> bytes:
        if not plaintext:
            raise EncryptionError("cannot encrypt an empty value")
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, ciphertext: bytes) -> str:
        try:
            return self._fernet.decrypt(ciphertext).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise EncryptionError("encrypted value cannot be decrypted") from exc

    def fingerprint(self, plaintext: str) -> str:
        return hmac.new(self._fingerprint_key, plaintext.encode(), hashlib.sha256).hexdigest()
