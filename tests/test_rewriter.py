from datetime import date

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
