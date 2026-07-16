"""Small Windows Credential Manager wrapper for GUI-only LLM API keys.

The GUI session is deliberately not a secret store.  On Windows a key is held in the current
user's Credential Manager as a generic credential, scoped to the normalized service endpoint.  A
hash keeps the Credential Manager target name stable without placing a copied endpoint in the
target; the human-readable comment retains the endpoint metadata for an operator inspecting the
credential store.  Other platforms do not persist keys at all.
"""
from __future__ import annotations

import ctypes
import hashlib
import logging
import sys
from ctypes import wintypes

from isar.workspace_api import validate_endpoint

log = logging.getLogger(__name__)

_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_NOT_FOUND = 1168
_TARGET_PREFIX = "ISAR GUI LLM API Key/"


class CredentialError(RuntimeError):
    """Credential Manager could not complete the requested operation."""


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", ctypes.c_byte * 8),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


PCREDENTIALW = ctypes.POINTER(_CREDENTIALW)


def credential_target(endpoint: str) -> str:
    """Return the stable non-secret Credential Manager target for ``endpoint``."""
    normalized = validate_endpoint(endpoint)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"{_TARGET_PREFIX}{digest}"


def _endpoint_comment(endpoint: str) -> str:
    return f"ISAR GUI API key for endpoint: {validate_endpoint(endpoint)}"


def _functions():
    """Load and type the native functions lazily, keeping imports safe off Windows."""
    if sys.platform != "win32":
        raise CredentialError("Windows Credential Manager is unavailable on this platform")
    try:
        advapi = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    except OSError as exc:
        raise CredentialError(f"could not load Windows Credential Manager: {exc}") from exc
    cred_write = advapi.CredWriteW
    cred_write.argtypes = [PCREDENTIALW, wintypes.DWORD]
    cred_write.restype = wintypes.BOOL
    cred_read = advapi.CredReadW
    cred_read.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                          ctypes.POINTER(PCREDENTIALW)]
    cred_read.restype = wintypes.BOOL
    cred_delete = advapi.CredDeleteW
    cred_delete.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    cred_delete.restype = wintypes.BOOL
    cred_free = advapi.CredFree
    cred_free.argtypes = [ctypes.c_void_p]
    cred_free.restype = None
    return cred_write, cred_read, cred_delete, cred_free


def supported() -> bool:
    """Whether this process can use Windows Credential Manager persistence."""
    return sys.platform == "win32"


def store_api_key(endpoint: str, api_key: str) -> None:
    """Store a non-empty API key for the validated endpoint, or raise ``CredentialError``."""
    if not isinstance(api_key, str) or not api_key:
        raise CredentialError("API key must be a non-empty string")
    normalized = validate_endpoint(endpoint)
    target = credential_target(normalized)
    raw = api_key.encode("utf-8")
    blob = (ctypes.c_byte * len(raw)).from_buffer_copy(raw)
    credential = _CREDENTIALW(
        Flags=0,
        Type=_CRED_TYPE_GENERIC,
        TargetName=target,
        Comment=_endpoint_comment(normalized),
        LastWritten=(ctypes.c_byte * 8)(),
        CredentialBlobSize=len(raw),
        CredentialBlob=ctypes.cast(blob, ctypes.POINTER(ctypes.c_byte)),
        Persist=_CRED_PERSIST_LOCAL_MACHINE,
        AttributeCount=0,
        Attributes=None,
        TargetAlias=None,
        UserName="ISAR GUI",
    )
    cred_write, _, _, _ = _functions()
    if not cred_write(ctypes.byref(credential), 0):
        error = ctypes.get_last_error()
        raise CredentialError(f"Windows Credential Manager write failed ({error})")


def load_api_key(endpoint: str) -> str | None:
    """Load the key for ``endpoint``; return ``None`` when no credential exists."""
    target = credential_target(endpoint)
    _, cred_read, _, cred_free = _functions()
    result = PCREDENTIALW()
    if not cred_read(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(result)):
        error = ctypes.get_last_error()
        if error == _ERROR_NOT_FOUND:
            return None
        raise CredentialError(f"Windows Credential Manager read failed ({error})")
    try:
        credential = result.contents
        raw = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CredentialError("stored API key is not valid UTF-8") from exc
    finally:
        cred_free(result)


def delete_api_key(endpoint: str) -> bool:
    """Delete the endpoint-scoped key.  ``False`` means it was already absent."""
    target = credential_target(endpoint)
    _, _, cred_delete, _ = _functions()
    if cred_delete(target, _CRED_TYPE_GENERIC, 0):
        return True
    error = ctypes.get_last_error()
    if error == _ERROR_NOT_FOUND:
        return False
    raise CredentialError(f"Windows Credential Manager delete failed ({error})")


def api_key_present(endpoint: str) -> bool:
    """Return only whether a key is available, never the key itself."""
    return load_api_key(endpoint) is not None
