import base64

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from kalshi_agent.kalshi.auth import KalshiSigner, generate_test_key


def test_signature_verifies_with_public_key():
    key = generate_test_key()
    signer = KalshiSigner("key-id", key)
    ts = 1_700_000_000_000
    sig = signer.sign(ts, "get", "/trade-api/v2/portfolio/balance")
    key.public_key().verify(
        base64.b64decode(sig),
        f"{ts}GET/trade-api/v2/portfolio/balance".encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_headers_strip_query_string():
    # PSS signatures are randomised, so verify the signed message rather than compare bytes.
    key = generate_test_key()
    signer = KalshiSigner("key-id", key)
    ts = 1_700_000_000_000
    with_query = signer.headers("GET", "https://x/trade-api/v2/markets?limit=5", ts)
    key.public_key().verify(
        base64.b64decode(with_query["KALSHI-ACCESS-SIGNATURE"]),
        f"{ts}GET/trade-api/v2/markets".encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    assert with_query["KALSHI-ACCESS-KEY"] == "key-id"
    assert with_query["KALSHI-ACCESS-TIMESTAMP"] == str(ts)


def test_from_pem_roundtrip():
    from cryptography.hazmat.primitives import serialization

    key = generate_test_key()
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    signer = KalshiSigner.from_pem("k", pem)
    assert signer.api_key_id == "k"
