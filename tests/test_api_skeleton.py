"""Skeleton import test — confirms api/serve packages exist."""


def test_api_package_imports() -> None:
    import localmail.api  # noqa: F401


def test_serve_package_imports() -> None:
    import localmail.serve  # noqa: F401
