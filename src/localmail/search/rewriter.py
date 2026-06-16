"""LLM query rewriter for the opt-in ``--smart`` search path (Phase 4).

Pure helpers plus a single IO class (:class:`OllamaLLMRewriter`). The IO
class raises typed exceptions; the :class:`~localmail.search.searcher.Searcher`
owns the graceful-degradation policy so the failure behaviour is testable in
one place.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field, ValidationError

from localmail.config import SearchConfig
from localmail.search.query import ParsedQuery, SearchFilters


class RewriteParseError(ValueError):
    """The LLM response was not valid JSON / did not match the schema."""


@dataclass(frozen=True)
class RewriteResult:
    rewritten_text: str
    expansion_terms: list[str]
    extracted_filters: SearchFilters


@runtime_checkable
class QueryRewriter(Protocol):
    name: str
    model: str

    def rewrite(self, free_text: str) -> RewriteResult: ...


_PROMPT_TEMPLATE = """\
You rewrite an email-search query into structured JSON. Today is {today}.

Return ONLY JSON with these keys:
- "rewritten_text": a cleaner, semantically richer restatement of the query
  for semantic (vector) search.
- "expansion_terms": up to {max_terms} synonyms or closely related terms that
  broaden a keyword search. Omit if none apply.
- "filters": object with optional keys "after" (YYYY-MM-DD inclusive lower
  bound), "before" (YYYY-MM-DD exclusive upper bound), "from", "to",
  "subject" (case-insensitive substrings), "has_attachment" (true/false).
  Resolve relative dates like "last summer" using today's date above. Use null
  for any filter you cannot infer. Never invent account, folder, or language
  filters.

Query: {query}
"""


def build_rewrite_prompt(
    free_text: str, *, today: date, max_expansion_terms: int
) -> str:
    """Render the deterministic rewrite prompt.

    ``today`` is injected (not read from the clock) so relative-date grounding
    is reproducible in tests.
    """
    return _PROMPT_TEMPLATE.format(
        today=today.isoformat(),
        max_terms=max_expansion_terms,
        query=free_text,
    )


class _FiltersSchema(BaseModel):
    after: date | None = None
    before: date | None = None
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None
    subject: str | None = None
    has_attachment: bool | None = None


class _RewriteSchema(BaseModel):
    rewritten_text: str
    expansion_terms: list[str] = Field(default_factory=list)
    filters: _FiltersSchema = Field(default_factory=_FiltersSchema)


def parse_rewrite_response(raw: str) -> RewriteResult:
    """Validate the LLM's JSON output into a :class:`RewriteResult`.

    Raises :class:`RewriteParseError` on invalid / malformed JSON so the
    caller can fall through to the un-rewritten query.
    """
    try:
        model = _RewriteSchema.model_validate_json(raw)
    except ValidationError as exc:
        raise RewriteParseError(str(exc)) from exc
    f = model.filters
    return RewriteResult(
        rewritten_text=model.rewritten_text,
        expansion_terms=list(model.expansion_terms),
        extracted_filters=SearchFilters(
            after=f.after,
            before=f.before,
            from_substr=f.from_,
            to_substr=f.to,
            subject_substr=f.subject,
            has_attachment=f.has_attachment,
        ),
    )


def _fill(user_value: Any, llm_value: Any) -> Any:
    """Explicit operators win: keep the user's value unless it is unset."""
    return user_value if user_value is not None else llm_value


def apply_rewrite(
    parsed: ParsedQuery, result: RewriteResult, *, max_expansion_terms: int
) -> ParsedQuery:
    """Merge an LLM :class:`RewriteResult` into the parsed query.

    - ``rewritten_text`` and ``expansion_terms`` are added (``free_text`` is
      left untouched so lexical exact-recall is preserved).
    - Scalar filter slots are filled only when the user left them empty
      (explicit operators win). List slots (accounts/folders/languages) are
      never touched by the LLM.
    """
    uf = parsed.filters
    lf = result.extracted_filters
    merged = replace(
        uf,
        after=_fill(uf.after, lf.after),
        before=_fill(uf.before, lf.before),
        from_substr=_fill(uf.from_substr, lf.from_substr),
        to_substr=_fill(uf.to_substr, lf.to_substr),
        subject_substr=_fill(uf.subject_substr, lf.subject_substr),
        has_attachment=_fill(uf.has_attachment, lf.has_attachment),
    )
    terms = [t for t in result.expansion_terms if t.strip()]
    return replace(
        parsed,
        rewritten_text=result.rewritten_text or None,
        expansion_terms=terms[:max_expansion_terms],
        filters=merged,
    )


OLLAMA_FORMAT_SCHEMA = _RewriteSchema.model_json_schema()
"""JSON schema passed as Ollama's ``format`` constraint. Public so the
Ollama backend in ``rewriter_backends`` can import it."""


if TYPE_CHECKING:  # pragma: no cover - import for type-checkers only
    from localmail.search.rewriter_backends import (  # noqa: F401
        AnthropicRewriter,
        MissingApiKey,
        OllamaLLMRewriter,
        OpenAICompatRewriter,
        build_rewriter,
    )

_BACKEND_REEXPORTS = frozenset(
    {
        "OllamaLLMRewriter",
        "OpenAICompatRewriter",
        "AnthropicRewriter",
        "build_rewriter",
        "MissingApiKey",
    }
)


def __getattr__(name: str):
    """Back-compat: re-export the backend classes from their new module.

    Lazy (PEP 562) so importing ``rewriter`` never triggers an import-time
    cycle with ``rewriter_backends`` (which imports the pure helpers above).
    """
    if name in _BACKEND_REEXPORTS:
        from localmail.search import rewriter_backends

        return getattr(rewriter_backends, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
