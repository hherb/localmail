"""Pure path-allowlist guard for archive imports (no DB, no FastAPI).

`resolve_import_path` resolves the operator-supplied path to a realpath and
requires it to live under one of the configured roots. `..` traversal is
normalised away by `.resolve()`; symlink escape is caught because `.resolve()`
follows links before the containment check.
"""
from __future__ import annotations

from pathlib import Path


class ImportPathError(ValueError):
    """The requested source path is outside the configured import allowlist."""


def resolve_import_path(raw: str, roots: list[Path]) -> Path:
    """Resolve `raw` and require it under one of `roots`.

    Returns the resolved absolute Path. Raises ImportPathError when `roots`
    is empty (imports disabled), or the resolved path is not contained in any
    root (covers `..` traversal and symlink escape).
    """
    if not roots:
        raise ImportPathError("imports are disabled (no [imports].roots configured)")
    resolved = Path(raw).resolve()
    resolved_roots = [r.resolve() for r in roots]
    if not any(resolved == root or resolved.is_relative_to(root) for root in resolved_roots):
        raise ImportPathError(f"path {raw!r} is not under an allowed import root")
    return resolved
