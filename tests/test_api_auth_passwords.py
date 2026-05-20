import pytest

from localmail.api.auth import (
    change_password,
    hash_password,
    login,
    reset_login_rate_limiter,
    verify_password,
    verify_token,
)
from localmail.api.errors import AuthenticationFailed, ValidationFailed


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


def test_change_password_updates_hash(db_conn, api_user) -> None:
    change_password(db_conn, api_user.id, api_user.password, "new-secret")
    db_conn.commit()
    with db_conn.cursor() as cur:
        cur.execute("SELECT password_hash FROM api_users WHERE id = %s", (api_user.id,))
        row = cur.fetchone()
    assert row is not None
    assert verify_password("new-secret", row[0]) is True
    assert verify_password(api_user.password, row[0]) is False


def test_change_password_rejects_wrong_old(db_conn, api_user) -> None:
    with pytest.raises(AuthenticationFailed):
        change_password(db_conn, api_user.id, "wrong-old", "new-secret")


def test_change_password_rejects_empty_new(db_conn, api_user) -> None:
    with pytest.raises(ValidationFailed):
        change_password(db_conn, api_user.id, api_user.password, "")


def test_change_password_rejects_unknown_user(db_conn) -> None:
    """An id that does not exist must surface as auth failure, not 500."""
    with pytest.raises(AuthenticationFailed):
        change_password(db_conn, 99999, "anything", "new-secret")


def test_change_password_keeps_existing_tokens_valid(db_conn, api_user, api_token) -> None:
    change_password(db_conn, api_user.id, api_user.password, "new-secret")
    db_conn.commit()
    user = verify_token(db_conn, api_token)
    assert user is not None
    assert user.id == api_user.id


def test_change_password_allows_login_with_new_password(db_conn, api_user) -> None:
    change_password(db_conn, api_user.id, api_user.password, "new-secret")
    db_conn.commit()
    reset_login_rate_limiter(db_conn)
    db_conn.commit()
    token, _expires = login(db_conn, api_user.username, "new-secret")
    assert token


def test_change_password_blocks_login_with_old_password(db_conn, api_user) -> None:
    change_password(db_conn, api_user.id, api_user.password, "new-secret")
    db_conn.commit()
    reset_login_rate_limiter(db_conn)
    db_conn.commit()
    with pytest.raises(AuthenticationFailed):
        login(db_conn, api_user.username, api_user.password)
