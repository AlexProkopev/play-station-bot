import ssl
import sys


def windows_ssl_context() -> ssl.SSLContext:
    """Windows TLS context: OS store + optional certifi extras, relaxed 3.13 flags.

    On this machine HTTPS is intercepted by antivirus (Avast Web/Mail Shield).
    Using certifi as the only CA bundle fails; the Windows store has the
    interceptor root. Python 3.13 also rejects that root unless STRICT is off.
    """
    ctx = ssl.create_default_context()
    try:
        import certifi

        ctx.load_verify_locations(cafile=certifi.where())
    except Exception:
        pass
    if sys.platform == "win32" and hasattr(ssl, "VERIFY_X509_STRICT"):
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        if hasattr(ssl, "VERIFY_X509_PARTIAL_CHAIN"):
            ctx.verify_flags &= ~ssl.VERIFY_X509_PARTIAL_CHAIN
    return ctx


def apply_process_ssl() -> None:
    ssl._create_default_https_context = windows_ssl_context
