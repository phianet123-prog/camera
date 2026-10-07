"""
Generates a self-signed TLS certificate on first run, so the dashboard
can serve HTTPS on your local network without needing a real
certificate authority. Browsers will show a "not secure" / self-signed
warning the first time you visit — that's expected and safe to accept
for a device on your own LAN; it's still real encryption, just not
verified by a public CA. If you expose this beyond your LAN, use a
real certificate (e.g. via a reverse proxy with Let's Encrypt) instead.
"""
import os
import datetime

import config as cfg

CERT_PATH = os.path.join(cfg.BASE_DIR, "cert.pem")
KEY_PATH = os.path.join(cfg.BASE_DIR, "key.pem")


def ensure_self_signed_cert():
    """Returns (cert_path, key_path), generating them first if missing."""
    if os.path.exists(CERT_PATH) and os.path.exists(KEY_PATH):
        return CERT_PATH, KEY_PATH

    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        raise RuntimeError(
            "HTTPS_ENABLED is set but the 'cryptography' package isn't installed. "
            "Run: pip install cryptography"
        )

    print("[HTTPS] Generating a self-signed certificate for local use (one-time)...")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "argus.local")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.utcnow())
        .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=3650))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.DNSName("argus.local")]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    with open(KEY_PATH, "wb") as f:
        f.write(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ))
    with open(CERT_PATH, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))

    print(f"[HTTPS] Certificate written to {CERT_PATH} (valid 10 years).")
    return CERT_PATH, KEY_PATH
