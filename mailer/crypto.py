from cryptography.fernet import Fernet


class CredCipher:
    def __init__(self, key: bytes):
        self._f = Fernet(key)

    def encrypt(self, plaintext: str) -> bytes:
        return self._f.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, token: bytes) -> str:
        return self._f.decrypt(token).decode("utf-8")
