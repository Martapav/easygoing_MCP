"""Post-processing shared by both adapters (section 4.1 conventions).

Not in the section 2 layout listing; added so the two adapters cannot drift
apart on the rules that are supposed to be format-independent — end-time
derivation, zero-duration anomalies and time-based chapter assignment.
"""

from __future__ import annotations

import bisect
from collections.abc import Sequence

from ..models.canonical import Anomaly, Chapter


def chapter_id_for(live_id: str, index: int) -> str:
    """`<live_id>-C<NN>`, numbered from 1 (section 4.1)."""
    return f"{live_id}-C{index + 1:02d}"


def derive_end_times(starts: Sequence[int], content_end_s: int) -> list[int]:
    """`end_s` is the next segment's `start_s`; the last one ends at content end.

    This is what produces the zero-duration segments (25 in MB20261028, 31 in
    MB20261029): wherever consecutive ASR chunks share a start time, the earlier
    one gets zero length. That is a property of the source timecodes, reproduced
    faithfully rather than smoothed over (section 4.1).
    """
    if not starts:
        return []
    ends = list(starts[1:]) + [content_end_s]
    if ends[-1] < starts[-1]:
        raise ValueError(
            f"content_end_s={content_end_s} precedes the last segment start {starts[-1]}"
        )
    return ends


def zero_duration_anomalies(segment_ids: Sequence[str], starts: Sequence[int], ends: Sequence[int]) -> list[Anomaly]:
    """One `zero_duration` anomaly per zero-length segment (section 4.1)."""
    return [
        Anomaly(
            kind="zero_duration",
            segment_ids=[segment_id],
            detail=(
                f"segment starts and ends at {start} s: the following segment shares its "
                "start timecode, so no duration can be derived from the source"
            ),
        )
        for index, (segment_id, start, end) in enumerate(zip(segment_ids, starts, ends))
        # Only segments that have a successor. The final segment's end comes from
        # coverage.content_end_s, so in MB20261029 — where content ends at the last
        # segment's own start — it is zero-length because the broadcast end is
        # unknown, not because a timecode is shared. That is truncation, reported
        # through coverage.truncated, and counting it here would both overstate
        # this anomaly and misstate its cause.
        if end == start and index + 1 < len(segment_ids)
    ]


def chapter_at_time(chapters: Sequence[Chapter], start_s: int) -> str | None:
    """The last chapter whose `start_s <= start_s` (section 4.2).

    Returns None when the segment precedes every chapter, which is a real state
    worth preserving rather than forcing into the first chapter.
    """
    if not chapters:
        return None
    starts = [c.start_s for c in chapters]
    position = bisect.bisect_right(starts, start_s) - 1
    if position < 0:
        return None
    return chapters[position].chapter_id


def split_tickers(raw: str | None) -> list[str]:
    """`GOOGL / GOOG` and `GOOGL/GOOG` both become `[GOOGL, GOOG]`.

    Splits on `/` only. An exchange suffix such as `000660 (KRX)` is preserved
    verbatim, because dropping it would invent a bare ticker the source never
    stated (section 4.4, "record each ticker as given").
    """
    if not raw:
        return []
    return [part.strip() for part in raw.split("/") if part.strip()]
