"""Genera la CA propia, un certificado por nodo y los secretos (tarea 04; DA-06, DA-10, DA-11, DA-15).

Uso, desde la raíz del repositorio:

    python scripts/gen_secrets.py --out secrets/local --nodes etcd1=10.88.0.11,meta1=10.88.0.21,dn1=10.88.0.31

Crea:

    <out>/ca.crt  ca.key
    <out>/jwt_secret  pass_key  admin_password
    <out>/<nodo>/tls.crt  <nodo>/tls.key      uno por cada nodo de --nodes
    <out>/<dn>/node_key                       solo los nodos cuyo nombre empieza con dn

Está en Python con cryptography, sin openssl, para que corra igual en Windows y en
Linux. Si <out> ya existe, se detiene sin tocar nada, salvo con --force: una
node_key nueva deja ilegibles los bloques guardados (DA-09) y una CA nueva
invalida todos los certificados. Imprime las rutas que crea, nunca el contenido.
"""

import argparse
import datetime
import ipaddress
import re
import secrets
import shutil
import subprocess
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

REPO_ROOT = Path(__file__).resolve().parents[1]
NODE_NAME = re.compile(r"^[a-z0-9]+$")
VALID_DAYS = 365
SECRET_BYTES = 32  # jwt_secret, pass_key y node_key (AES-256, DA-09)


def parse_nodes(text: str) -> dict[str, ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Convierte "etcd1=10.88.0.11,dn1=10.88.0.31" en {nombre: ip}; valida nombres e IP."""
    nodes = {}
    for item in text.split(","):
        name, sep, ip = item.strip().partition("=")
        if not sep:
            raise SystemExit(f"nodo sin IP: {item!r} (formato nombre=ip)")
        if not NODE_NAME.match(name):
            raise SystemExit(f"nombre de nodo inválido: {name!r} (solo a-z y 0-9)")
        if name in nodes:
            raise SystemExit(f"nodo repetido: {name}")
        try:
            nodes[name] = ipaddress.ip_address(ip)
        except ValueError:
            raise SystemExit(f"IP inválida para {name}: {ip!r}") from None
    return nodes


def check_ignored_by_git(out: Path) -> None:
    """Si <out> cae dentro del repositorio, exige que git lo ignore (el repositorio es público)."""
    if not out.resolve().is_relative_to(REPO_ROOT):
        return
    if shutil.which("git") is None:
        # Sin git no se puede comprobar: no se escribe nada, ni secretos ni .gitignore.
        raise SystemExit(f"no se encontró git para comprobar que {out} esté ignorado; instala git o usa --out fuera del repositorio")
    gitignore = REPO_ROOT / ".gitignore"
    lines = gitignore.read_text(encoding="utf-8").splitlines() if gitignore.exists() else []
    if "secrets/" not in lines:
        with gitignore.open("a", encoding="utf-8", newline="\n") as file:
            file.write("\n# Secretos generados por scripts/gen_secrets.py\nsecrets/\n")
        print(f"agregado secrets/ a {gitignore}")
    # check-ignore: 0 = ignorado, 1 = no ignorado, 128 = sin repositorio git.
    result = subprocess.run(["git", "check-ignore", "-q", str(out.resolve())], cwd=REPO_ROOT)
    if result.returncode == 1:
        raise SystemExit(f"{out} no está ignorado por git; usa una carpeta dentro de secrets/")


def new_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def build_ca(key: ec.EllipticCurvePrivateKey, now: datetime.datetime) -> x509.Certificate:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "DFSha CA")])
    return (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=VALID_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False, content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )  # fmt: skip
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )


def build_node_cert(
    name: str,
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
    key: ec.EllipticCurvePrivateKey,
    ca: x509.Certificate,
    ca_key: ec.EllipticCurvePrivateKey,
    now: datetime.datetime,
) -> x509.Certificate:
    """Certificado de servidor y de cliente (mTLS) con la IP del nodo en el SAN.

    Python 3.13+ verifica en modo estricto, que exige KeyUsage, SKI y AKI.
    """
    ips = list(dict.fromkeys([ip, ipaddress.ip_address("127.0.0.1")]))  # sin repetir si ip es 127.0.0.1
    san = [x509.IPAddress(address) for address in ips] + [x509.DNSName(name), x509.DNSName("localhost")]
    return (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=VALID_DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=False, crl_sign=False,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )  # fmt: skip
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]),
            critical=False,
        )
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )


def write_file(path: Path, data: bytes, private: bool) -> None:
    """Escribe el archivo; los privados quedan solo para el dueño (en Windows chmod no hace nada)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if private:
        path.chmod(0o600)
    print(f"escrito {path}")


def cert_pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def key_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    # Sin contraseña: los servicios la leen al arrancar, sin nadie que la escriba.
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Genera la CA, los certificados de nodo y los secretos de DFSha.")
    parser.add_argument("--out", type=Path, required=True, help="carpeta de salida, por ejemplo secrets/local")
    parser.add_argument("--nodes", required=True, help="nombre=ip separados por coma: etcd1=10.88.0.11,dn1=10.88.0.31")
    parser.add_argument("--force", action="store_true", help="sobrescribe una carpeta existente")
    args = parser.parse_args(argv)

    # Todo se valida antes de escribir el primer archivo.
    nodes = parse_nodes(args.nodes)
    out: Path = args.out
    if out.exists() and not args.force:
        raise SystemExit(f"{out} ya existe; no se tocó nada. Usa --force para regenerar todo")
    check_ignored_by_git(out)

    now = datetime.datetime.now(datetime.UTC)
    ca_key = new_key()
    ca = build_ca(ca_key, now)
    write_file(out / "ca.crt", cert_pem(ca), private=False)
    write_file(out / "ca.key", key_pem(ca_key), private=True)

    write_file(out / "jwt_secret", secrets.token_bytes(SECRET_BYTES), private=True)
    write_file(out / "pass_key", secrets.token_bytes(SECRET_BYTES), private=True)
    write_file(out / "admin_password", secrets.token_urlsafe(18).encode("ascii"), private=True)

    for name, ip in nodes.items():
        key = new_key()
        write_file(out / name / "tls.crt", cert_pem(build_node_cert(name, ip, key, ca, ca_key, now)), private=False)
        write_file(out / name / "tls.key", key_pem(key), private=True)
        if name.startswith("dn"):
            write_file(out / name / "node_key", secrets.token_bytes(SECRET_BYTES), private=True)


if __name__ == "__main__":
    main()
