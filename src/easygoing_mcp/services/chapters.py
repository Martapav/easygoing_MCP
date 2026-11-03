"""Chapter projections (REQ-0002)."""

from __future__ import annotations

from collections import Counter

from ..models.outputs import ChaptersView, ChapterView
from ..store.base import ContentStore
from .lives import live_caveats, timestamp_label


def chapters_view(store: ContentStore, live_id: str) -> ChaptersView:
    """REQ-0002: the chapters of one live, in broadcast order.

    `start_quality` travels with every start time because one of MB20261029's
    published chapter times is annotated as unverified and disagrees with the
    transcript by about 298 s — a start time without its provenance would read
    as more precise than it is (section 4.3).
    """
    live = store.get_live(live_id)
    per_chapter = Counter(
        segment.chapter_id for segment in live.segments if segment.chapter_id is not None
    )

    chapters = [
        ChapterView(
            chapter_id=chapter.chapter_id,
            start_s=chapter.start_s,
            timestamp_label=timestamp_label(chapter.start_s, chapter.start_quality),
            start_quality=chapter.start_quality,
            label_raw=chapter.label_raw,
            rubric_id=chapter.rubric_id,
            rubric_name=store.get_rubric(chapter.rubric_id).name if chapter.rubric_id else None,
            speaker_ids=list(chapter.speaker_ids),
            speaker_names=[store.get_speaker(sid).name for sid in chapter.speaker_ids],
            topics_raw=chapter.topics_raw,
            segment_count=per_chapter.get(chapter.chapter_id, 0),
        )
        for chapter in live.chapters
    ]

    return ChaptersView(
        live_id=live.live.live_id,
        date=live.live.date,
        chapters=chapters,
        caveats=live_caveats(live, live.segments),
    )
