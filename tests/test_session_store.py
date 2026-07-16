"""Session store tests: the durable record a review is carried across turns by.

The store is what makes the measured resume contract usable — a stable directory that outlives the
handler, and a resume handle written down *before* the call that creates the session.
"""

from datetime import datetime, timedelta, timezone

import pytest

from contextbot.core.session_store import (
    PendingReview,
    ReviewState,
    SessionStore,
    build_store,
)

NOW = datetime(2026, 7, 15, 14, 30, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path) -> SessionStore:
    return SessionStore(tmp_path / "reviews")


def make(
    store: SessionStore,
    *,
    message_id: int = 7,
    created_at=NOW,
    session_id: str = "sess-1",
    state: ReviewState = ReviewState.AWAITING_REVIEW,
    note_type: str = "note",
) -> PendingReview:
    """A review in the store.

    ``create`` always makes one QUEUED — the producer activates it once a draft exists — so this
    defaults to *asked about*, which is the state the tests below are mostly about. Pass
    ``state=ReviewState.QUEUED`` for one still waiting its turn.
    """
    review = store.create(
        message_id=message_id,
        session_id=session_id,
        title="검토 중",
        source_date=NOW,
        created_at=created_at,
        note_type=note_type,
    )
    if state is not ReviewState.QUEUED:
        review.state = state
        store.update(review)
    return review


def test_no_pending_review_initially(store):
    assert store.pending() is None
    assert store.has_pending() is False


def test_create_makes_the_work_dir_and_records_the_handle(store):
    review = make(store)

    # The directory must exist *now*, not when the first turn runs: it is the session's cwd, and
    # the session is keyed by that path.
    assert review.work_dir.is_dir()
    assert store.pending().session_id == "sess-1"


def test_the_work_dir_is_empty_so_the_isolation_rule_still_holds(store):
    """cwd is the session's identity *and* the model's view of the filesystem.

    Increments 2-3 stage the input alone in the job's cwd because the model fabricates from a
    neighbouring file rather than admitting it cannot read the one it was asked for. A review's dir
    is persistent rather than temporary, but it is the same cwd — so the draft lives *outside* it.
    """
    review = make(store)
    review.write_draft("초안 본문")

    assert list(review.work_dir.iterdir()) == []
    assert review.draft_path.parent != review.work_dir
    assert review.draft_path not in review.work_dir.rglob("*")


def test_the_handle_is_readable_by_a_second_store_over_the_same_path(tmp_path):
    """The file is the authority, not an in-memory dict.

    The capture handler and the client service each hold their own store over the same path. If
    either cached, one would create a review the other could not see — the client would never start
    polling, and the owner's reply would land nowhere.
    """
    writer = SessionStore(tmp_path / "reviews")
    reader = SessionStore(tmp_path / "reviews")
    make(writer)

    assert reader.has_pending() is True
    assert reader.pending().session_id == "sess-1"


def test_a_review_survives_a_restart(tmp_path):
    """Row 5 of the resume contract: the transcript is on disk, so the review must be too."""
    make(SessionStore(tmp_path / "reviews")).write_draft("살아남은 초안")

    restarted = SessionStore(tmp_path / "reviews").pending()
    assert restarted is not None
    assert restarted.draft == "살아남은 초안"
    assert restarted.state is ReviewState.AWAITING_REVIEW


def test_update_persists_state_and_failures(store):
    review = make(store)
    review.state = ReviewState.FINALIZING
    review.failures = 2
    review.title = "새 제목"
    store.update(review)

    reloaded = store.pending()
    assert reloaded.state is ReviewState.FINALIZING
    assert reloaded.failures == 2
    assert reloaded.title == "새 제목"


def test_remove_ends_the_review_and_deletes_its_tree(store):
    review = make(store)
    review.write_draft("초안")
    base = review.work_dir.parent

    store.remove(review.message_id)

    assert store.pending() is None
    assert not base.exists()


def test_remove_is_idempotent(store):
    make(store)
    store.remove(7)
    store.remove(7)  # a crash between the index write and the rmtree must not wedge the next call
    assert store.pending() is None


def test_created_at_and_source_date_are_distinct(store):
    """A memo captured at catch-up can be hours older than the review it triggers.

    The note is dated by the memo (source_date); expiry and the poller's backlog filter run off
    when the *review* began (created_at). Conflating them would date notes wrongly and — worse —
    let a bot-DM message sent in that gap be read as an answer.
    """
    started = NOW + timedelta(hours=3)
    review = make(store, created_at=started)

    assert review.source_date == NOW
    assert review.created_at == started
    assert store.pending().created_at == started


def test_created_at_defaults_to_now(store):
    review = store.create(message_id=7, session_id="s", title="t", source_date=NOW)
    assert review.age_hours() < 0.01


def test_age_hours_measures_from_the_review_start(store):
    review = make(store, created_at=NOW - timedelta(hours=30))
    assert review.age_hours(now=NOW) == pytest.approx(30.0)


def test_a_naive_timestamp_does_not_break_arithmetic(store, tmp_path):
    """A hand-edited index must not crash the expiry check with a naive/aware comparison."""
    make(store)
    index = tmp_path / "reviews" / "index.json"
    index.write_text(index.read_text().replace("+00:00", ""), encoding="utf-8")

    assert store.pending().age_hours(now=NOW) == pytest.approx(0.0)


def test_a_missing_draft_reads_as_empty_rather_than_raising(store):
    """Never let a vanished draft file take down the turn that would have delivered it."""
    assert make(store).draft == ""


def test_an_unreadable_entry_is_dropped_not_raised(store, tmp_path):
    make(store)
    (tmp_path / "reviews" / "index.json").write_text(
        '{"reviews": [{"message_id": 7}], "update_offset": 0}', encoding="utf-8"
    )
    assert store.pending() is None


def test_a_corrupt_index_reads_as_empty(store, tmp_path):
    (tmp_path / "reviews").mkdir(parents=True)
    (tmp_path / "reviews" / "index.json").write_text("{not json", encoding="utf-8")
    assert store.pending() is None
    assert store.update_offset == 0


def test_update_offset_round_trips(store):
    assert store.update_offset == 0
    store.set_update_offset(12345)
    assert store.update_offset == 12345


def test_the_offset_outlives_the_reviews(store):
    """The offset must not be reset by a review ending, or the next one replays old updates."""
    make(store)
    store.set_update_offset(99)
    store.remove(7)
    assert store.update_offset == 99


def test_build_store_sits_beside_the_hwm(settings):
    assert build_store(settings).root == settings.session_path.parent / "reviews"
