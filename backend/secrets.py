"""Windows DPAPI protects integration secrets at rest for the current user."""
import base64
import ctypes
import os
from ctypes import wintypes


class Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_char))]


def transform(value, encrypt):
    if os.name != 'nt':
        raise RuntimeError('当前密钥存储仅支持 Windows DPAPI')
    buffer = ctypes.create_string_buffer(value)
    source = Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    target = Blob()
    func = ctypes.windll.crypt32.CryptProtectData if encrypt else ctypes.windll.crypt32.CryptUnprotectData
    if not func(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        ctypes.windll.kernel32.LocalFree(target.data)


def seal(text):
    return base64.b64encode(transform(text.encode('utf-8'), True)).decode('ascii')


def unseal(text):
    return transform(base64.b64decode(text), False).decode('utf-8') if text else ''
