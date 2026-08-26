"""Deprecated import shim: the ``vasuki`` package was renamed to ``daino``.

Importing ``vasuki`` (or any ``vasuki.*`` submodule) transparently resolves to
the matching ``daino`` module, so legacy imports such as
``from vasuki.agents import ToolLoop`` keep working and return the *same* objects
as ``daino``. A :class:`DeprecationWarning` is emitted once. Migrate by importing
from ``daino`` directly; this alias will be removed in a future release.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import sys
import warnings

_OLD = "vasuki"
_NEW = "daino"


class _RenameRedirector(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Redirect ``vasuki`` / ``vasuki.*`` imports to the ``daino`` package."""

    def find_spec(self, fullname, path=None, target=None):  # noqa: ARG002
        if fullname == _OLD or fullname.startswith(_OLD + "."):
            return importlib.util.spec_from_loader(fullname, self)
        return None

    def create_module(self, spec):
        target = _NEW + spec.name[len(_OLD) :]
        module = importlib.import_module(target)
        sys.modules[spec.name] = module
        return module

    def exec_module(self, module):  # noqa: ARG002 - already executed as the daino module
        return None


if not any(isinstance(finder, _RenameRedirector) for finder in sys.meta_path):
    warnings.warn(
        "The 'vasuki' package has been renamed to 'daino'. Import from 'daino' "
        "instead; the 'vasuki' alias will be removed in a future release.",
        DeprecationWarning,
        stacklevel=2,
    )
    sys.meta_path.insert(0, _RenameRedirector())

# Replace this shim module object with the real package so that attribute access
# (``import vasuki; vasuki.agents``) and submodule imports resolve to the exact
# same objects as ``daino`` rather than duplicate module instances.
sys.modules[_OLD] = importlib.import_module(_NEW)
