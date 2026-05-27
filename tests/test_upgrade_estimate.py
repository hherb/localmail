"""Unit tests for localmail.upgrade_estimate (issue #2)."""

from __future__ import annotations

import pytest

from localmail.upgrade_estimate import (
    ESTIMATORS,
    EstimateResult,
)


def test_estimate_result_is_immutable_dataclass():
    """EstimateResult is frozen — accidental mutation must fail."""
    r = EstimateResult(
        revision="0006_search_indexes",
        status="pending",
        current_bytes={},
        projected_bytes={"fts_v2": 100, "gin_messages": 40, "gin_chunks": 20},
        projected_duration_s=1.5,
        warnings=[],
    )
    with pytest.raises((AttributeError, Exception)):  # frozen dataclass raises FrozenInstanceError
        r.revision = "other"  # type: ignore[misc]


def test_estimate_result_fields_present():
    """All wire-stable fields exist on EstimateResult."""
    r = EstimateResult(
        revision="0006_search_indexes",
        status="applied",
        current_bytes={"fts_v2_idx": 1000, "chunks_fts_idx": 500},
        projected_bytes={},
        projected_duration_s=0.0,
        warnings=[],
    )
    assert r.revision == "0006_search_indexes"
    assert r.status == "applied"
    assert r.current_bytes == {"fts_v2_idx": 1000, "chunks_fts_idx": 500}
    assert r.projected_bytes == {}
    assert r.projected_duration_s == 0.0
    assert r.warnings == []


def test_estimators_registry_has_0006():
    """ESTIMATORS is a dict[str, Callable]; 0006 is registered."""
    assert "0006_search_indexes" in ESTIMATORS
    assert callable(ESTIMATORS["0006_search_indexes"])


def test_unknown_revision_raises_keyerror():
    """Documented contract: missing key raises KeyError (not silent miss)."""
    with pytest.raises(KeyError):
        ESTIMATORS["0099_nonsense"]
