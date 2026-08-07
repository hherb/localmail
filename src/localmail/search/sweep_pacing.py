# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Horst Herb

"""Pacing rule for the embed worker's sweep loop (#259).

Owns *both* halves of one decision — what counts as a sweep having done work,
and how long to sleep after it — because #259 is exactly what happens when
those halves are written apart. `run_embed_worker_once` returned a bare count
of embedded chunks; the loop read that count as "did this sweep do work". But
the sweep also runs one `body_lang_detect_batch_size` slice of language
detection, so a sweep that laboured through 200 rows reported 0 and the loop
slept for the full backoff. On a large backlog that is ~340 rows/minute, an
order of magnitude below what `localmail lang-backfill` achieves on the same
queue.

Keeping the progress predicate next to the arithmetic that reads it is the
same reasoning as `blob_temps.py` (minting beside matching) and
`shutdown_budget.py` (the child's budget beside the supervisor's kill
deadline): the two drifted apart once, and co-locating them is what stops it
happening again.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NoReturn


@dataclass(frozen=True, slots=True)
class SweepOutcome:
    """What one `run_embed_worker_once` sweep actually did.

    The two counts answer different questions and must not be conflated:
    `embedded` is progress on the *embedding* queue, `lang_visited` progress
    on the *language-detection* queue. A sweep can drain one while the other
    is empty, and either means the worker should come straight back rather
    than back off.

    `lang_visited` counts rows *visited*, not rows labelled — the #251
    distinction. A batch the detector declines outright still makes real
    progress, because every claimed row is stamped `body_lang_attempted_at`
    and leaves the queue for good.

    The sweep's third pass — lazy chunking — is deliberately **not** here, for
    two reasons. It feeds the embedding queue rather than draining one of its
    own, so under a working backend the chunks it makes are claimed by
    `_embed_table` in the same sweep and already reported as `embedded` —
    folding it in would double-report the same work. And its counts are
    claim-shaped, not drain-shaped: both passes return the number of rows
    *selected*, which is what made them untrustworthy as a progress signal
    while a zero-chunk row could be re-selected on every sweep (the #266
    defect — since fixed by healing such rows to the '' sentinel on first
    claim, but the counts still measure claims, which is the reason
    `lang_visited` can be trusted where these cannot: every row
    `run_lang_detect_pass` claims is stamped).
    """

    embedded: int
    lang_visited: int

    def __bool__(self) -> NoReturn:
        """Always raises — ask for `made_progress` (or a named count) instead.

        `LangDetectPass` merely declines to define this, which leaves
        `if not result:` silently always-False. Raising goes one step
        further and makes the implicit read that caused #251 and #259
        impossible rather than just discouraged.
        """
        raise TypeError(
            "SweepOutcome has no truth value; use .made_progress, .embedded,"
            " or .lang_visited explicitly"
        )

    @property
    def made_progress(self) -> bool:
        """True when either queue advanced — the loop's 'do not back off' signal."""
        return self.embedded > 0 or self.lang_visited > 0


def next_idle_streak(streak: int, *, made_progress: bool, max_steps: int) -> int:
    """Advance the consecutive-empty-sweep counter.

    Resets to 0 on any progress, otherwise climbs to `max_steps` and holds
    there. `max_steps` is `SearchConfig.embed_worker_idle_backoff_max_steps`
    and has no default here on purpose: it is the one authority for the
    ceiling, and a default would quietly become a second one.

    `max_steps=0` pins the streak at 0, i.e. disables the backoff.

    The result is clamped at 0 for the same reason `shutdown_budget`'s
    `remaining_seconds` is: a negative streak yields a negative sleep, and
    `Event.wait(timeout=<negative>)` returns *immediately* rather than raising
    — so the worker would busy-poll while the code still read as a wait.
    `SearchConfig` already rejects a negative `max_steps` (`ge=0`); the clamp
    is what makes this module's claim to own the ceiling true on its own terms
    rather than by the caller remembering to validate.
    """
    if made_progress:
        return 0
    return max(0, min(streak + 1, max_steps))


def sweep_sleep_seconds(streak: int, poll_interval_s: float) -> float:
    """How long to sleep after a sweep that left the streak at `streak`.

    Linear in the streak — one poll interval when fresh, `max_steps + 1`
    intervals when saturated (5 s x 7 = 35 s at the defaults).
    """
    return poll_interval_s * (1 + streak)
