from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CERTS_DIR = Path("certs")
CERTS_DIR.mkdir(exist_ok=True)


@dataclass
class CertConfig:
    filename: str
    common_name: str
    is_ca: bool = False
    parent_cert: x509.Certificate | None = None
    parent_key: rsa.RSAPrivateKey | None = None
    hosts: list[str] | None = None


def generate_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def save_key(key: rsa.RSAPrivateKey, filename: str):
    path = CERTS_DIR / f"{filename}.key"
    with open(path, "wb") as f:
        f.write(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
    print(f"Key saved: {path}")


def save_cert(cert: x509.Certificate, filename: str):
    path = CERTS_DIR / f"{filename}.crt"
    with open(path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    print(f"Cert saved: {path}")


def create_certificate(cfg: CertConfig) -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    key = generate_key()
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cfg.common_name)])

    issuer = cfg.parent_cert.subject if cfg.parent_cert else subject
    signing_key = cfg.parent_key if cfg.parent_key else key

    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=3650))
    )

    builder = builder.add_extension(
        x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
    )

    if cfg.parent_key:
        builder = builder.add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(
                cfg.parent_key.public_key()
            ),
            critical=False,
        )
    else:
        builder = builder.add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()),
            critical=False,
        )

    if cfg.is_ca:
        builder = builder.add_extension(
            x509.BasicConstraints(ca=True, path_length=None), critical=True
        )
        builder = builder.add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
    else:
        builder = builder.add_extension(
            x509.BasicConstraints(ca=False, path_length=None), critical=True
        )
        builder = builder.add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        builder = builder.add_extension(
            x509.ExtendedKeyUsage(
                [
                    ExtendedKeyUsageOID.SERVER_AUTH,
                    ExtendedKeyUsageOID.CLIENT_AUTH,
                ]
            ),
            critical=False,
        )

    if cfg.hosts:
        alt_names = [x509.DNSName(h) for h in cfg.hosts]
        alt_names.append(x509.DNSName("localhost"))
        alt_names.append(x509.DNSName("nats"))
        # Добавляем IP адреса тоже, на всякий случай
        alt_names.append(x509.IPAddress(import_ip_address("127.0.0.1")))

        builder = builder.add_extension(
            x509.SubjectAlternativeName(alt_names), critical=False
        )

    cert = builder.sign(signing_key, hashes.SHA256())

    save_key(key, cfg.filename)
    save_cert(cert, cfg.filename)

    return key, cert


def import_ip_address(ip: str):
    import ipaddress

    return ipaddress.ip_address(ip)


def main():
    print("Starting PKI Generation (With SKI/AKI)...")

    ca_key, ca_cert = create_certificate(
        CertConfig("ca", "PingPal Root CA", is_ca=True)
    )

    create_certificate(
        CertConfig(
            "server",
            "nats-server",
            parent_cert=ca_cert,
            parent_key=ca_key,
            hosts=["localhost", "nats", "127.0.0.1", "pingpal-nats"],
        )
    )

    create_certificate(
        CertConfig(
            "client-core", "PingPal Core", parent_cert=ca_cert, parent_key=ca_key
        )
    )

    create_certificate(
        CertConfig(
            "client-agent", "PingPal Agent", parent_cert=ca_cert, parent_key=ca_key
        )
    )

    print("\nAll certificates generated in ./certs/")


if __name__ == "__main__":
    main()
