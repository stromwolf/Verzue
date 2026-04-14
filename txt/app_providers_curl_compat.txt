"""Compatibility shim for curl_cffi across versions."""
from curl_cffi.requests import AsyncSession, RequestsError

try:
    from curl_cffi.requests import ProxyError
except ImportError:
    # Older curl_cffi versions don't export ProxyError; it's a subclass of RequestsError
    ProxyError = RequestsError

__all__ = ["AsyncSession", "RequestsError", "ProxyError"]
