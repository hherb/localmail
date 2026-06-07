import pytest

from datetime import date
from datetime import date as _date

from localmail.search.rewriter import build_rewrite_prompt


def test_prompt_includes_injected_today_and_free_text():
    prompt = build_rewrite_prompt(
        "tax return last summer", today=date(2026, 6, 7), max_expansion_terms=8
    )
    assert "2026-06-07" in prompt
    assert "tax return last summer" in prompt
    assert "8" in prompt


def test_prompt_is_deterministic():
    a = build_rewrite_prompt("x", today=date(2026, 1, 1), max_expansion_terms=5)
    b = build_rewrite_prompt("x", today=date(2026, 1, 1), max_expansion_terms=5)
    assert a == b


from localmail.search.rewriter import RewriteParseError, parse_rewrite_response


def test_parse_full_response():
    raw = (
        '{"rewritten_text": "quarterly revenue report",'
        ' "expansion_terms": ["earnings", "Q3"],'
        ' "filters": {"after": "2025-06-01", "before": "2025-09-01",'
        ' "from": "bob", "to": null, "subject": null,'
        ' "has_attachment": true}}'
    )
    r = parse_rewrite_response(raw)
    assert r.rewritten_text == "quarterly revenue report"
    assert r.expansion_terms == ["earnings", "Q3"]
    assert r.extracted_filters.after == _date(2025, 6, 1)
    assert r.extracted_filters.before == _date(2025, 9, 1)
    assert r.extracted_filters.from_substr == "bob"
    assert r.extracted_filters.to_substr is None
    assert r.extracted_filters.has_attachment is True


def test_parse_minimal_response_defaults_empty():
    r = parse_rewrite_response('{"rewritten_text": "hello"}')
    assert r.rewritten_text == "hello"
    assert r.expansion_terms == []
    assert r.extracted_filters.after is None
    assert r.extracted_filters.has_attachment is None


def test_parse_invalid_json_raises():
    with pytest.raises(RewriteParseError):
        parse_rewrite_response("not json at all")


def test_parse_missing_required_field_raises():
    with pytest.raises(RewriteParseError):
        parse_rewrite_response('{"expansion_terms": []}')


from localmail.search.query import ParsedQuery, SearchFilters
from localmail.search.rewriter import RewriteResult, apply_rewrite


def _result(**filter_kw):
    return RewriteResult(
        rewritten_text="rich query",
        expansion_terms=["a", "b", "c"],
        extracted_filters=SearchFilters(**filter_kw),
    )


def test_apply_sets_rewritten_text_and_expansion():
    parsed = ParsedQuery(free_text="orig")
    out = apply_rewrite(parsed, _result(), max_expansion_terms=8)
    assert out.free_text == "orig"
    assert out.rewritten_text == "rich query"
    assert out.expansion_terms == ["a", "b", "c"]


def test_apply_caps_expansion_terms():
    parsed = ParsedQuery(free_text="orig")
    out = apply_rewrite(parsed, _result(), max_expansion_terms=2)
    assert out.expansion_terms == ["a", "b"]


def test_apply_fills_empty_filter_slot():
    from datetime import date
    parsed = ParsedQuery(free_text="orig")
    out = apply_rewrite(
        parsed, _result(after=date(2023, 6, 1)), max_expansion_terms=8
    )
    assert out.filters.after == date(2023, 6, 1)


def test_apply_preserves_explicit_operator():
    from datetime import date
    parsed = ParsedQuery(
        free_text="orig", filters=SearchFilters(after=date(2024, 1, 1))
    )
    out = apply_rewrite(
        parsed, _result(after=date(2023, 6, 1)), max_expansion_terms=8
    )
    assert out.filters.after == date(2024, 1, 1)


def test_apply_llm_empty_filters_leave_user_filters_untouched():
    parsed = ParsedQuery(
        free_text="orig", filters=SearchFilters(subject_substr="invoice")
    )
    out = apply_rewrite(parsed, _result(), max_expansion_terms=8)
    assert out.filters.subject_substr == "invoice"
