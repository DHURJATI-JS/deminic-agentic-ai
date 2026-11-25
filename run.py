"""Deminic launcher.

Serves the same app on two listeners in one process:
  * HTTP  - http://127.0.0.1:<APP_PORT>      (this device; also reachable on the LAN)
  * HTTPS - https://<lan-ip>:<HTTPS_PORT>      (every device on your network; needed
            because browsers only allow microphone + geolocation on HTTPS/localhost)
A local certificate authority is generated in data/tls/. Install
data/tls/deminic-local-ca.crt (also downloadable at /ca.crt) on your other
devices once to remove the browser warning.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import ipaddress
import json
import os
import signal
import socket

import uvicorn

from app.config import settings


# --------------------------------------------------------------------------- TLS
def local_ips() -> list[str]:
    ips = {"127.0.0.1"}
    # Inside Docker the container cannot see the host's LAN IPs - the installer
    # passes them via EXTRA_CERT_HOSTS so the HTTPS certificate covers them.
    for extra in os.environ.get("EXTRA_CERT_HOSTS", "").split(","):
        extra = extra.strip()
        if extra:
            with contextlib.suppress(ValueError):
                ipaddress.ip_address(extra)
                ips.add(extra)
    with contextlib.suppress(Exception):
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    with contextlib.suppress(Exception):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    return sorted(i for i in ips if not i.startswith("169.254."))


def lan_ips() -> list[str]:
    return [i for i in local_ips() if not i.startswith("127.") and ipaddress.ip_address(i).is_private
            and not i.startswith(("172.17.", "172.18.", "172.29.", "172.31."))] or [i for i in local_ips() if not i.startswith("127.")]


def ensure_tls() -> tuple[str, str]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    d = settings.tls_dir
    ca_key_f, ca_crt_f = d / "deminic-local-ca.key", d / "deminic-local-ca.crt"
    key_f, crt_f, meta_f = d / "server.key", d / "server.crt", d / "server.json"
    now = dt.datetime.now(dt.timezone.utc)
    if not ca_key_f.exists() or not ca_crt_f.exists():
        ca_key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Deminic Local CA"),
                          x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Deminic (self-hosted)")])
        ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(ca_key.public_key())
              .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
              .not_valid_after(now + dt.timedelta(days=3650))
              .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
              .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                                           key_encipherment=False, data_encipherment=False, key_agreement=False,
                                           encipher_only=False, decipher_only=False), critical=True)
              .sign(ca_key, hashes.SHA256()))
        ca_key_f.write_bytes(ca_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                  serialization.NoEncryption()))
        ca_crt_f.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    ips = local_ips()
    host = socket.gethostname()
    want = {"ips": ips, "host": host}
    if key_f.exists() and crt_f.exists() and meta_f.exists() and json.loads(meta_f.read_text()) == want:
        return str(crt_f), str(key_f)
    ca_key = serialization.load_pem_private_key(ca_key_f.read_bytes(), password=None)
    ca = x509.load_pem_x509_certificate(ca_crt_f.read_bytes())
    key = ec.generate_private_key(ec.SECP256R1())
    san = [x509.DNSName("localhost"), x509.DNSName(host)] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"Deminic on {host}")]))
            .issuer_name(ca.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=397))
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    key_f.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()))
    crt_f.write_bytes(cert.public_bytes(serialization.Encoding.PEM) + ca_crt_f.read_bytes())
    meta_f.write_text(json.dumps(want))
    return str(crt_f), str(key_f)


# --------------------------------------------------------------------------- servers
class _Server(uvicorn.Server):
    """Signals are handled once for both listeners (see main)."""

    @contextlib.contextmanager
    def capture_signals(self):  # noqa: D401
        yield


def banner(https: bool) -> None:
    line = "=" * 74
    lans = lan_ips()
    print(line)
    print("  DEMINIC AGENTIC AI is running  (open it from any device on your network)")
    print(f"    This device : http://127.0.0.1:{settings.app_port}")
    if settings.app_host in ("0.0.0.0", "::"):
        for ip in lans:
            print(f"    LAN (HTTP)  : http://{ip}:{settings.app_port}")
            if https:
                print(f"    LAN (HTTPS) : https://{ip}:{settings.https_port}   <- mic + location work here")
        if https and lans:
            print(f"    Trust cert  : http://{lans[0]}:{settings.app_port}/ca.crt  (install once per device)")
    print(f"    Status      : http://127.0.0.1:{settings.app_port}/healthz")
    print("  Ctrl+C stops the app: cancellable tasks are cancelled, protected tasks are")
    print("  rolled back to their restore points.")
    print(line, flush=True)


async def serve() -> None:
    https = settings.enable_lan_https
    servers: list[_Server] = []
    common = dict(app="app.main:asgi", log_level="info", access_log=False, timeout_graceful_shutdown=25,
                  ws_ping_interval=20, ws_ping_timeout=25, forwarded_allow_ips="127.0.0.1")
    servers.append(_Server(uvicorn.Config(host=settings.app_host, port=settings.app_port, lifespan="on", **common)))
    if https:
        cert, key = ensure_tls()
        servers.append(_Server(uvicorn.Config(host=settings.app_host, port=settings.https_port, lifespan="off",
                                              ssl_certfile=cert, ssl_keyfile=key, **common)))

    def _stop(*_a):
        for s in servers:
            s.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
        if sig is not None:
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, _stop)
    banner(https)
    await asyncio.gather(*(s.serve() for s in servers))


if __name__ == "__main__":
    # Default loop (Proactor on Windows) - required for asyncio subprocesses (git, locust).
    asyncio.run(serve())
