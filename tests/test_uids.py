# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Pure UID-numbering helpers (issues #215, #222A)."""

import pytest

from localmail.fetch_retry import fetch_budget_exhausted
from localmail.uids import (
    ARCHIVE_AUTH_METHOD,
    checkpoint_uidnext,
    next_uid_after,
    should_reallocate_uid,
)


class TestNextUidAfter:
    def test_empty_mailbox_starts_at_one(self):
        assert next_uid_after(0) == 1

    def test_none_is_treated_as_empty(self):
        """COALESCE should make this unreachable, but a NULL must not crash."""
        assert next_uid_after(None) == 1

    def test_continues_past_the_highest_stored_uid(self):
        assert next_uid_after(500) == 501


class TestShouldReallocateUid:
    def test_archive_uids_are_synthetic_and_reallocated(self):
        assert should_reallocate_uid(ARCHIVE_AUTH_METHOD) is True

    @pytest.mark.parametrize("auth_method", ["password", "oauth2"])
    def test_real_imap_uids_are_preserved(self, auth_method):
        """A live account's UID is the server's truth; replaying it is correct."""
        assert should_reallocate_uid(auth_method) is False


class TestCheckpointUidnext:
    def test_without_a_hold_it_resumes_after_the_highest_seen(self):
        assert checkpoint_uidnext(500, None) == 501

    def test_a_hold_clamps_the_resume_point_to_the_stuck_uid(self):
        """Later UIDs in the same run must not carry the watermark past it."""
        assert checkpoint_uidnext(500, 42) == 42

    def test_a_hold_beyond_the_highest_seen_does_not_push_it_forward(self):
        assert checkpoint_uidnext(10, 900) == 11

    def test_a_hold_on_the_very_first_uid_resumes_at_that_uid(self):
        assert checkpoint_uidnext(0, 1) == 1


class TestFetchBudgetExhausted:
    """Bounds the #222A watermark hold so a permanently unfetchable UID cannot
    pin a mailbox forever (mirrors `transient_budget_exhausted` from #153)."""

    def test_first_failure_is_within_budget(self):
        assert fetch_budget_exhausted(1, 5) is False

    def test_the_attempt_before_the_cap_is_within_budget(self):
        assert fetch_budget_exhausted(4, 5) is False

    def test_reaching_the_cap_exhausts_the_budget(self):
        assert fetch_budget_exhausted(5, 5) is True

    def test_overshooting_the_cap_stays_exhausted(self):
        assert fetch_budget_exhausted(6, 5) is True

    def test_a_zero_cap_never_holds(self):
        """An operator can opt out of holding entirely."""
        assert fetch_budget_exhausted(1, 0) is True
