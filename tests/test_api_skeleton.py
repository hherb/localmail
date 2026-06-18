# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Skeleton import test — confirms api/serve packages exist."""


def test_api_package_imports() -> None:
    import localmail.api  # noqa: F401


def test_serve_package_imports() -> None:
    import localmail.serve  # noqa: F401
