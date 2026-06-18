# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Path-allowlist guard tests (pure, no DB)."""
from __future__ import annotations

import os

import pytest

from localmail.importer.paths import ImportPathError, resolve_import_path


def test_accepts_path_inside_root(tmp_path):
    root = tmp_path / "imports"
    root.mkdir()
    f = root / "archive.mbox"
    f.write_bytes(b"x")
    assert resolve_import_path(str(f), [root]) == f.resolve()


def test_rejects_empty_roots(tmp_path):
    f = tmp_path / "a.mbox"
    f.write_bytes(b"x")
    with pytest.raises(ImportPathError):
        resolve_import_path(str(f), [])


def test_rejects_path_outside_root(tmp_path):
    root = tmp_path / "imports"
    root.mkdir()
    outside = tmp_path / "secret.mbox"
    outside.write_bytes(b"x")
    with pytest.raises(ImportPathError):
        resolve_import_path(str(outside), [root])


def test_rejects_dotdot_traversal(tmp_path):
    root = tmp_path / "imports"
    root.mkdir()
    sneaky = str(root / ".." / "secret.mbox")
    with pytest.raises(ImportPathError):
        resolve_import_path(sneaky, [root])


def test_rejects_symlink_escape(tmp_path):
    root = tmp_path / "imports"
    root.mkdir()
    target = tmp_path / "outside.mbox"
    target.write_bytes(b"x")
    link = root / "link.mbox"
    os.symlink(target, link)
    with pytest.raises(ImportPathError):
        resolve_import_path(str(link), [root])
