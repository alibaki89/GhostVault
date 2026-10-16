"""TLS for GhostVault's own HTTPS requests.

GhostVault verifies GitHub's certificate with the operating system's own trust store,
through the ``truststore`` package: the Windows certificate store, the macOS Keychain, or
the system CA bundle that OpenSSL uses on Linux. Certificate authorities that an
organisation installs (for example for a TLS-inspecting proxy) are therefore trusted
exactly as the browser trusts them.

If ``truststore`` is unavailable, Python's default context is used, which loads the
system's CA locations (and, on Windows, its certificate store).

One context is built and passed explicitly to every request; Python's process-wide
defaults are never changed.
"""

from __future__ import annotations

import ssl
import threading

_lock = threading.Lock()
_context: ssl.SSLContext | None = None


def build_context() -> ssl.SSLContext:
    try:
        import truststore
    except ImportError:
        return ssl.create_default_context()
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def ssl_context() -> ssl.SSLContext:
    global _context
    with _lock:
        if _context is None:
            _context = build_context()
        return _context
