# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT

"""Run an actual Lua 5.3/5.4 shared interpreter when no CLI is installed.

This standalone subprocess runner needs only Python's standard library.
It does not emulate Lua or mock its process/file APIs.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import sys


def library_name():
    for name in ("lua5.4", "lua5.3", "lua-5.4", "lua-5.3"):
        library = ctypes.util.find_library(name)
        if library:
            return library
    return None


def main():
    library = library_name()
    if not library:
        raise SystemExit("Lua 5.3/5.4 interpreter is unavailable")
    lua = ctypes.CDLL(library)
    lua.luaL_newstate.restype = ctypes.c_void_p
    lua.luaL_openlibs.argtypes = [ctypes.c_void_p]
    lua.luaL_loadfilex.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
    lua.luaL_loadfilex.restype = ctypes.c_int
    lua.lua_pcallk.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_ssize_t, ctypes.c_void_p,
    ]
    lua.lua_pcallk.restype = ctypes.c_int
    lua.lua_tolstring.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_size_t),
    ]
    lua.lua_tolstring.restype = ctypes.c_char_p
    lua.lua_close.argtypes = [ctypes.c_void_p]
    state = lua.luaL_newstate()
    if not state:
        raise SystemExit("Could not allocate Lua state")
    try:
        lua.luaL_openlibs(state)
        status = lua.luaL_loadfilex(state, sys.argv[1].encode(), None)
        if status == 0:
            status = lua.lua_pcallk(state, 0, -1, 0, 0, None)
        if status:
            error = lua.lua_tolstring(state, -1, None)
            print(error.decode(errors="replace") if error else "Lua failed", file=sys.stderr)
            return 1
        return 0
    finally:
        lua.lua_close(state)


if __name__ == "__main__":
    sys.exit(main())
