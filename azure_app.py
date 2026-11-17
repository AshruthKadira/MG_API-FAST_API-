"""Expose the application as an importable ASGI target on Azure App Service.

The repository directory name contains hyphens, so it cannot be imported as a
Python package by its directory name. Load the existing package under a stable
name so its relative imports continue to work.
"""

import importlib.util
from pathlib import Path
import sys


_package_name = "mg_receipt_api"
_package_path = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    _package_name,
    _package_path / "__init__.py",
    submodule_search_locations=[str(_package_path)],
)
if _spec is None or _spec.loader is None:
    raise ImportError("Could not load the application package")

_package = importlib.util.module_from_spec(_spec)
sys.modules[_package_name] = _package
_spec.loader.exec_module(_package)

app = _package.app
