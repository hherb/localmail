# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Unit tests for the embed worker's pure pacing rules (#259)."""

from __future__ import annotations

import pytest

from localmail.search.sweep_pacing import (
    SweepOutcome,
    next_idle_streak,
    sweep_sleep_seconds,
)

_MAX_STEPS = 6


# --- what counts as a sweep having done work ---------------------------------


def test_a_sweep_that_embedded_chunks_made_progress():
    assert SweepOutcome(embedded=7, lang_visited=0).made_progress is True


def test_a_sweep_that_only_visited_language_rows_made_progress():
    """#259: labelling 200 rows is work, even though nothing was embedded."""
    assert SweepOutcome(embedded=0, lang_visited=200).made_progress is True


def test_a_sweep_that_did_nothing_made_no_progress():
    assert SweepOutcome(embedded=0, lang_visited=0).made_progress is False


def test_sweep_outcome_has_no_bool():
    """Mirrors LangDetectPass: an implicit read of this value is the defect.

    #251 was caused by a drain loop reading a two-meaning count as a truthy
    scalar. Callers must name the field (or `made_progress`) they mean.
    """
    with pytest.raises(TypeError):
        bool(SweepOutcome(embedded=0, lang_visited=0))


def test_sweep_outcome_is_immutable():
    outcome = SweepOutcome(embedded=1, lang_visited=1)
    with pytest.raises(AttributeError):
        outcome.embedded = 2  # type: ignore[misc]


# --- the idle streak ---------------------------------------------------------


def test_progress_resets_the_streak():
    assert next_idle_streak(4, made_progress=True, max_steps=_MAX_STEPS) == 0


def test_an_empty_sweep_advances_the_streak():
    assert next_idle_streak(0, made_progress=False, max_steps=_MAX_STEPS) == 1
    assert next_idle_streak(3, made_progress=False, max_steps=_MAX_STEPS) == 4


def test_the_streak_saturates_at_max_steps():
    assert next_idle_streak(_MAX_STEPS, made_progress=False, max_steps=_MAX_STEPS) == _MAX_STEPS


def test_a_zero_max_steps_disables_the_backoff_entirely():
    """`max_steps=0` pins every sleep at the base poll interval."""
    assert next_idle_streak(0, made_progress=False, max_steps=0) == 0


# --- the sleep length --------------------------------------------------------


def test_a_fresh_streak_sleeps_exactly_one_poll_interval():
    assert sweep_sleep_seconds(0, 5.0) == 5.0


def test_the_sleep_grows_linearly_with_the_streak():
    assert sweep_sleep_seconds(1, 5.0) == 10.0
    assert sweep_sleep_seconds(3, 5.0) == 20.0


def test_the_saturated_streak_sleeps_max_steps_plus_one_intervals():
    """The documented ceiling: 5 s x 7 = 35 s at the default max_steps."""
    assert sweep_sleep_seconds(_MAX_STEPS, 5.0) == 35.0
