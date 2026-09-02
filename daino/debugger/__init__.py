"""Debugging, over the Debug Adapter Protocol.

DAP rather than driving ``pdb``: one client gets every language that ships an
adapter, and the protocol is what editors have standardised on. Nothing is
bundled — the adapters already installed in the project are used, and a language
without one says so rather than showing a button that does nothing.
"""

from daino.debugger.adapters import ADAPTERS, Adapter, available, language_of
from daino.debugger.protocol import DebugAdapterClient, DebugError
from daino.debugger.session import (
    Breakpoint,
    DebugManager,
    DebugSession,
    Scope,
    StackFrame,
    Variable,
)

__all__ = [
    "ADAPTERS",
    "Adapter",
    "Breakpoint",
    "DebugAdapterClient",
    "DebugError",
    "DebugManager",
    "DebugSession",
    "Scope",
    "StackFrame",
    "Variable",
    "available",
    "language_of",
]
