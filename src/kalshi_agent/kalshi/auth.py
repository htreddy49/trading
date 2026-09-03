"""Kalshi API request signing.

Kalshi authenticates every REST/WebSocket request with three headers:

* ``KALSHI-ACCESS-KEY``       -- the API key id
* ``KALSHI-ACCESS-TIMESTAMP`` -- current time in milliseconds
* ``KALSHI-ACCESS-SIGNATURE`` -- base64(RSA-PSS-SHA256(timestamp + METHOD + path))

``path`` is the request path *without* query string but *with* the ``/trade-api/v2``
prefix, e.g. ``/trade-api/v2/portfolio/orders``.
"""

from __future__ import annotations

import base64
import time
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


class KalshiSigner:
    def __init__(self, api_key_id: str, private_key: rsa.RSAPrivateKey) -> None:
        self.api_key_id = api_key_id
        self._private_key = private_key

    # -- constructors ---------------------------------------------------------------
    @classmethod
    def from_pem(
        cls, api_key_id: str, pem: str | bytes, password: bytes | None = None
    ) -> KalshiSigner:
        if isinstance(pem, str):
            pem = pem.encode()
        key = serialization.load_pem_private_key(pem, password=password)
        if not isinstance(key, rsa.RSAPrivateKey):
            raise TypeError("Kalshi API keys must be RSA private keys")
        return cls(api_key_id, key)

    @classmethod
    def from_file(
        cls, api_key_id: str, path: str | Path, password: bytes | None = None
    ) -> KalshiSigner:
        return cls.from_pem(api_key_id, Path(path).read_bytes(), password=password)

    # -- signing ------------------------------------------------------------------
    def sign(self, timestamp_ms: int, method: str, path: str) -> str:
        message = f"{timestamp_ms}{method.upper()}{path}".encode()
        signature = self._private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode()

    def headers(
        self, method: str, url_or_path: str, timestamp_ms: int | None = None
    ) -> dict[str, str]:
        """Build auth headers for a request.

        ``url_or_path`` may be a full URL or a path; the query string is stripped.
        """
        path = urlsplit(url_or_path).path
        ts = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": str(ts),
            "KALSHI-ACCESS-SIGNATURE": self.sign(ts, method, path),
        }


def generate_test_key() -> rsa.RSAPrivateKey:
    """Generate a throwaway RSA key (used in tests and local demos)."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)
