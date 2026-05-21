from localmail.api.errors import SearchCursorExpired


def test_search_cursor_expired_problem_shape() -> None:
    err = SearchCursorExpired("token abc not found")
    problem = err.to_problem()
    assert err.http_status == 409
    assert problem["type"] == "/problems/search-cursor-expired"
    assert problem["title"] == "Search cursor expired"
    assert problem["status"] == 409
    assert problem["detail"] == "token abc not found"
