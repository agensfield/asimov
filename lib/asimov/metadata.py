"""Small stdlib-only extended-attribute adapter for Darwin and Linux."""
import ctypes
import os
import sys


def _darwin(name, path, key, value=None):
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, name)
    function.restype = ctypes.c_ssize_t if name == "getxattr" else ctypes.c_int
    path = os.fsencode(path)
    key = os.fsencode(key)
    if name == "getxattr":
        function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
                             ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
        size = function(path, key, None, 0, 0, 1)
        if size < 0:
            raise OSError(ctypes.get_errno(), "getxattr", os.fsdecode(path))
        buffer = ctypes.create_string_buffer(size)
        read = function(path, key, buffer, size, 0, 1)
        if read < 0:
            raise OSError(ctypes.get_errno(), "getxattr", os.fsdecode(path))
        return buffer.raw[:read]
    if name == "setxattr":
        function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
                             ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
        result = function(path, key, value, len(value), 0, 1)
    else:
        function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
        result = function(path, key, 1)
    if result < 0:
        raise OSError(ctypes.get_errno(), name, os.fsdecode(path))
    return None


def read(path, key):
    if sys.platform == "darwin":
        return _darwin("getxattr", path, key)
    return os.getxattr(path, key, follow_symlinks=False)


def write(path, key, value):
    if sys.platform == "darwin":
        return _darwin("setxattr", path, key, value)
    return os.setxattr(path, key, value, follow_symlinks=False)


def remove(path, key):
    if sys.platform == "darwin":
        return _darwin("removexattr", path, key)
    return os.removexattr(path, key, follow_symlinks=False)
