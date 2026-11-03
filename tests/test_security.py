"""Pruebas de la tarea 03: JWT, pases de bloque, secretos y contexto TLS de cliente."""

import base64
import datetime
import hashlib
import hmac
import json
import ssl
import time

import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from dfs_common import models
from dfs_common.errors import DFSError, ErrorCode
from dfs_common.models import BlockPass
from dfs_common.security import create_jwt, read_secret, sign_pass, verify_jwt, verify_pass
from dfs_common.tls import client_ssl_context

# 32 bytes, como los secretos que genera la tarea 04 (PyJWT avisa si la clave HS256 es corta).
SECRET = b"s" * 32
OTHER_SECRET = b"o" * 32
NOW = 1793628000

READ_EXAMPLE, WRITE_EXAMPLE = models.BlockPass.model_config["json_schema_extra"]["examples"]


def first_example(model) -> dict:
    return model.model_config["json_schema_extra"]["examples"][0]


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def error_code(call) -> ErrorCode:
    with pytest.raises(DFSError) as info:
        call()
    return info.value.code


# =====================================================================
# JWT (sección 7.1)
# =====================================================================


def test_jwt_round_trip():
    token = create_jwt("ana", True, SECRET, 3600)
    claims = verify_jwt(token, SECRET)
    assert (claims.sub, claims.admin) == ("ana", True)
    assert claims.exp - claims.iat == 3600
    assert abs(claims.iat - int(time.time())) <= 5


def test_expired_jwt_is_auth_error():
    token = create_jwt("ana", False, SECRET, 3600, now=int(time.time()) - 7200)
    assert error_code(lambda: verify_jwt(token, SECRET)) == ErrorCode.E_AUTH


def test_jwt_signed_with_another_key_is_auth_error():
    token = create_jwt("ana", False, OTHER_SECRET, 3600)
    assert error_code(lambda: verify_jwt(token, SECRET)) == ErrorCode.E_AUTH


def test_jwt_with_alg_none_is_auth_error():
    now = int(time.time())
    header = b64url(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload = b64url(json.dumps({"sub": "ana", "admin": True, "iat": now, "exp": now + 3600}).encode())
    assert error_code(lambda: verify_jwt(f"{header}.{payload}.", SECRET)) == ErrorCode.E_AUTH


@pytest.mark.parametrize("missing", ["sub", "admin", "iat", "exp"])
def test_jwt_without_a_claim_is_auth_error(missing):
    now = int(time.time())
    claims = {"sub": "ana", "admin": False, "iat": now, "exp": now + 3600}
    del claims[missing]
    token = jwt.encode(claims, SECRET, algorithm="HS256")
    assert error_code(lambda: verify_jwt(token, SECRET)) == ErrorCode.E_AUTH


@pytest.mark.parametrize("token", ["", "basura", "a.b.c"])
def test_empty_or_garbage_jwt_is_auth_error(token):
    assert error_code(lambda: verify_jwt(token, SECRET)) == ErrorCode.E_AUTH


# =====================================================================
# Pase de bloque (sección 7.2)
# =====================================================================


@pytest.mark.parametrize("example", [READ_EXAMPLE, WRITE_EXAMPLE], ids=["lectura", "escritura"])
def test_pass_round_trip(example):
    claims = BlockPass.model_validate(example)
    token = sign_pass(claims, SECRET)
    verified = verify_pass(token, SECRET, hash=claims.hash, op=claims.op, now=claims.exp - 1)
    assert verified == claims


def test_read_pass_has_no_secondaries():
    token = sign_pass(BlockPass.model_validate(READ_EXAMPLE), SECRET)
    payload = json.loads(base64.urlsafe_b64decode(token.split(".")[0] + "=="))
    assert "secondaries" not in payload


def test_sign_pass_serializes_like_the_contract():
    """La primera parte es idéntica a la de los pases de ejemplo de docs/contratos.md."""
    read_token = first_example(models.FileBlocksResponse)["blocks"][0]["pass"]
    write_token = first_example(models.UploadResponse)["missing"][0]["pass"]
    for example, token in [(READ_EXAMPLE, read_token), (WRITE_EXAMPLE, write_token)]:
        ours = sign_pass(BlockPass.model_validate(example), SECRET)
        assert ours.split(".")[0] == token.split(".")[0]


def valid_pass() -> tuple[str, BlockPass]:
    claims = BlockPass.model_validate(WRITE_EXAMPLE)
    return sign_pass(claims, SECRET), claims


def tampered_signature() -> str:
    token, _ = valid_pass()
    payload, signature = token.split(".")
    return f"{payload}.{b64url(bytes(b ^ 1 for b in base64.urlsafe_b64decode(signature + '=')))}"


def tampered_payload() -> str:
    """Otro usuario con la firma del pase original."""
    token, claims = valid_pass()
    other = claims.model_copy(update={"user": "bruno"}).model_dump_json(exclude_none=True).encode()
    return f"{b64url(other)}.{token.split('.')[1]}"


@pytest.mark.parametrize(
    "token_factory",
    [
        lambda: "sinpunto",
        lambda: "a.b.c",
        lambda: "!!!.???",
        lambda: f"{b64url(b'no es json')}.{b64url(b'x')}",
        tampered_signature,
        tampered_payload,
        lambda: sign_pass(BlockPass.model_validate(WRITE_EXAMPLE), OTHER_SECRET),
    ],
    ids=["sin punto", "tres partes", "base64 invalido", "firma que no corresponde", "firma alterada", "contenido alterado", "otra clave"],
)
def test_malformed_or_forged_pass_is_forbidden(token_factory):
    claims = BlockPass.model_validate(WRITE_EXAMPLE)
    code = error_code(lambda: verify_pass(token_factory(), SECRET, hash=claims.hash, op="w", now=NOW))
    assert code == ErrorCode.E_FORBIDDEN


def test_signed_payload_that_is_not_a_pass_is_forbidden():
    """Firma buena, pero el JSON no es un BlockPass: 403, no 500."""
    payload = b'{"user":"ana"}'
    token = f"{b64url(payload)}.{b64url(hmac.new(SECRET, payload, hashlib.sha256).digest())}"
    assert error_code(lambda: verify_pass(token, SECRET, hash="0" * 64, op="r", now=NOW)) == ErrorCode.E_FORBIDDEN


def test_pass_for_another_hash_is_forbidden():
    token, _ = valid_pass()
    code = error_code(lambda: verify_pass(token, SECRET, hash="0" * 64, op="w", now=NOW))
    assert code == ErrorCode.E_FORBIDDEN


def test_pass_for_another_operation_is_forbidden():
    token, claims = valid_pass()
    code = error_code(lambda: verify_pass(token, SECRET, hash=claims.hash, op="r", now=NOW))
    assert code == ErrorCode.E_FORBIDDEN


@pytest.mark.parametrize("offset", [0, 1], ids=["justo en exp", "despues de exp"])
def test_expired_pass_is_auth_error(offset):
    token, claims = valid_pass()
    code = error_code(lambda: verify_pass(token, SECRET, hash=claims.hash, op="w", now=claims.exp + offset))
    assert code == ErrorCode.E_AUTH


# =====================================================================
# Secretos
# =====================================================================


def test_read_secret_returns_the_bytes_as_they_are(tmp_path):
    path = tmp_path / "pass_key"
    path.write_bytes(b"\x00clave\n ")
    assert read_secret(path) == b"\x00clave\n "


def test_read_secret_with_the_wrong_size_names_the_path_but_not_the_content(tmp_path):
    path = tmp_path / "node_key"
    path.write_bytes(b"contenido-secreto")
    with pytest.raises(ValueError) as info:
        read_secret(path, size=32)
    assert str(path) in str(info.value)
    assert "contenido-secreto" not in str(info.value)
    assert read_secret(path, size=len(b"contenido-secreto")) == b"contenido-secreto"


def test_read_secret_of_a_missing_file_names_the_path(tmp_path):
    path = tmp_path / "no_existe"
    with pytest.raises(FileNotFoundError) as info:
        read_secret(path)
    assert str(path) in str(info.value)


# =====================================================================
# Contexto TLS de cliente
# =====================================================================


@pytest.fixture
def self_signed(tmp_path):
    """Un certificado autofirmado y su llave, que sirven de CA y de certificado de cliente."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "prueba")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_file, key_file = tmp_path / "tls.crt", tmp_path / "tls.key"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return cert_file, key_file


def test_client_context_verifies_the_server_and_requires_tls_1_3(self_signed):
    ca_file, _ = self_signed
    context = client_ssl_context(ca_file)
    assert context.minimum_version == ssl.TLSVersion.TLSv1_3
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert len(context.get_ca_certs()) == 1  # solo la CA propia


def test_client_context_presents_a_certificate_for_mtls(self_signed):
    cert_file, key_file = self_signed
    context = client_ssl_context(cert_file, cert_file, key_file)
    assert context.minimum_version == ssl.TLSVersion.TLSv1_3


def test_client_context_needs_both_cert_and_key(self_signed):
    cert_file, _ = self_signed
    with pytest.raises(ValueError):
        client_ssl_context(cert_file, cert_file=cert_file)
