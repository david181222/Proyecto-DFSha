"""Pruebas de la tarea 04: scripts/gen_secrets.py."""

import importlib.util
import ipaddress
import shutil
import ssl
import subprocess
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID

from dfs_common.tls import client_ssl_context

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("gen_secrets", ROOT / "scripts" / "gen_secrets.py")
gen_secrets = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen_secrets)

NODES = "etcd1=10.88.0.11,meta1=10.88.0.21,dn1=10.88.0.31,dn2=10.88.0.32"


@pytest.fixture
def out(tmp_path) -> Path:
    """Carpeta generada con los nodos de NODES (fuera del repositorio)."""
    out = tmp_path / "secrets" / "local"
    gen_secrets.main(["--out", str(out), "--nodes", NODES])
    return out


def load_cert(path: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(path.read_bytes())


def all_files(folder: Path) -> dict[Path, tuple[bytes, int]]:
    return {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in folder.rglob("*") if path.is_file()}


def test_creates_every_file(out):
    expected = {"ca.crt", "ca.key", "jwt_secret", "pass_key", "admin_password"}
    for node in ("etcd1", "meta1", "dn1", "dn2"):
        expected |= {f"{node}/tls.crt", f"{node}/tls.key"}
    expected |= {"dn1/node_key", "dn2/node_key"}  # solo los DataNodes
    assert {path.relative_to(out).as_posix() for path in all_files(out)} == expected


def test_secrets_have_the_right_size_and_format(out):
    assert len((out / "jwt_secret").read_bytes()) == 32
    assert len((out / "pass_key").read_bytes()) == 32
    assert len((out / "dn1" / "node_key").read_bytes()) == 32
    assert (out / "dn1" / "node_key").read_bytes() != (out / "dn2" / "node_key").read_bytes()
    password = (out / "admin_password").read_bytes()
    assert password.isascii() and not password.endswith(b"\n") and len(password) >= 20


def test_node_certificate_is_signed_by_the_ca_with_ip_san_and_both_ekus(out):
    ca = load_cert(out / "ca.crt")
    cert = load_cert(out / "meta1" / "tls.crt")
    cert.verify_directly_issued_by(ca)  # lanza una excepción si la CA no lo firmó
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert ipaddress.ip_address("10.88.0.21") in san.get_values_for_type(x509.IPAddress)
    assert ipaddress.ip_address("127.0.0.1") in san.get_values_for_type(x509.IPAddress)
    assert set(san.get_values_for_type(x509.DNSName)) == {"meta1", "localhost"}
    eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert set(eku) == {ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH}
    assert ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is True


def handshake(client_context: ssl.SSLContext, server_context: ssl.SSLContext, server_hostname: str):
    """Handshake TLS en memoria, sin red: devuelve los dos extremos ya conectados."""
    to_server, to_client = ssl.MemoryBIO(), ssl.MemoryBIO()
    client = client_context.wrap_bio(to_client, to_server, server_hostname=server_hostname)
    server = server_context.wrap_bio(to_server, to_client, server_side=True)
    pending = [client, server]
    for _ in range(10):
        for side in list(pending):
            try:
                side.do_handshake()
                pending.remove(side)
            except ssl.SSLWantReadError:
                pass
        if not pending:
            return client, server
    raise AssertionError("el handshake no terminó")


def mtls_server_context(out: Path, node: str) -> ssl.SSLContext:
    """Como la API interna de la tarea 05: exige certificado del cliente firmado por la CA."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(out / node / "tls.crt", out / node / "tls.key")
    context.load_verify_locations(out / "ca.crt")
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def test_generated_certificates_pass_a_real_mtls_handshake(out):
    """dn1 se conecta por IP a meta1 con client_ssl_context (tarea 03), en modo estricto en Python 3.13+."""
    client_context = client_ssl_context(out / "ca.crt", out / "dn1" / "tls.crt", out / "dn1" / "tls.key")
    client, server = handshake(client_context, mtls_server_context(out, "meta1"), "10.88.0.21")
    assert client.version() == "TLSv1.3"
    assert ("commonName", "dn1") in server.getpeercert()["subject"][0]  # el servidor vio al cliente


def test_wrong_ip_fails_the_handshake(out):
    client_context = client_ssl_context(out / "ca.crt", out / "dn1" / "tls.crt", out / "dn1" / "tls.key")
    with pytest.raises(ssl.SSLCertVerificationError):
        handshake(client_context, mtls_server_context(out, "meta1"), "10.88.0.99")


def test_second_run_without_force_fails_and_changes_nothing(out):
    before = all_files(out)
    with pytest.raises(SystemExit):
        gen_secrets.main(["--out", str(out), "--nodes", NODES])
    assert all_files(out) == before


def test_force_regenerates(out):
    old_ca = (out / "ca.crt").read_bytes()
    gen_secrets.main(["--out", str(out), "--nodes", NODES, "--force"])
    assert (out / "ca.crt").read_bytes() != old_ca


def test_output_shows_paths_but_no_secret(tmp_path, capsys):
    out = tmp_path / "secrets" / "local"
    gen_secrets.main(["--out", str(out), "--nodes", NODES])
    printed = capsys.readouterr().out
    assert str(out / "jwt_secret") in printed
    assert "PRIVATE KEY" not in printed
    assert (out / "admin_password").read_text() not in printed
    for path in [out / "jwt_secret", out / "pass_key", out / "dn1" / "node_key"]:
        secret = path.read_bytes()
        assert secret.hex() not in printed
        assert repr(secret) not in printed


@pytest.mark.parametrize(
    "nodes",
    ["Meta1=10.88.0.21", "meta-1=10.88.0.21", "meta1=10.88.0.300", "meta1", "meta1=10.88.0.21,meta1=10.88.0.22"],
    ids=["mayuscula", "guion", "ip invalida", "sin ip", "repetido"],
)
def test_invalid_nodes_fail_without_creating_anything(tmp_path, nodes):
    out = tmp_path / "secrets" / "local"
    with pytest.raises(SystemExit):
        gen_secrets.main(["--out", str(out), "--nodes", nodes])
    assert not out.exists()


# --- Carpeta dentro del repositorio: debe quedar ignorada por git ---


@pytest.fixture
def fake_repo(tmp_path, monkeypatch) -> Path:
    if shutil.which("git") is None:
        pytest.skip("git no está instalado")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    monkeypatch.setattr(gen_secrets, "REPO_ROOT", tmp_path.resolve())
    return tmp_path


def test_adds_secrets_to_gitignore_inside_a_repo(fake_repo):
    gen_secrets.main(["--out", str(fake_repo / "secrets" / "local"), "--nodes", "dn1=10.88.0.31"])
    assert "secrets/" in (fake_repo / ".gitignore").read_text().splitlines()
    status = subprocess.run(["git", "status", "--porcelain"], cwd=fake_repo, capture_output=True, text=True).stdout
    assert "secrets" not in status


def test_refuses_a_folder_inside_the_repo_that_git_would_upload(fake_repo):
    out = fake_repo / "otra" / "local"
    with pytest.raises(SystemExit):
        gen_secrets.main(["--out", str(out), "--nodes", "dn1=10.88.0.31"])
    assert not out.exists()
