def test_api_user_fixture_seeds_user(api_user) -> None:
    assert api_user.username == "alice"
    assert api_user.id > 0


def test_api_token_fixture_yields_valid_token(db_conn, api_token) -> None:
    from localmail.api.auth import verify_token
    user = verify_token(db_conn, api_token)
    assert user is not None
    assert user.username == "alice"
