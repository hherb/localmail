import pytest

from localmail.api.auth import hash_password, verify_password


def test_hash_password_returns_argon2id_string() -> None:
    h = hash_password("hunter2")
    assert h.startswith("$argon2id$")
    assert len(h) > 40


def test_verify_password_accepts_correct() -> None:
    h = hash_password("hunter2")
    assert verify_password("hunter2", h) is True


def test_verify_password_rejects_wrong() -> None:
    h = hash_password("hunter2")
    assert verify_password("nope", h) is False


def test_verify_password_rejects_garbage_hash() -> None:
    assert verify_password("anything", "not a valid hash") is False


def test_hash_password_unique_per_call() -> None:
    """Salt should make two hashes of the same password differ."""
    assert hash_password("same") != hash_password("same")


def test_empty_password_rejected_at_hash() -> None:
    with pytest.raises(ValueError):
        hash_password("")
