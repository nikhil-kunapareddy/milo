import stat
from datetime import timedelta

import pytest

from milo.models import Intake, Session, SessionState
from milo.store import SessionNotFound, Store, now, slugify


def make_session(store: Store, what="ramen", where="Boston") -> Session:
    intake = Intake(what=what, where=where)
    created = now()
    return Session(
        id=store.new_id(intake),
        created=created,
        updated=created,
        state=SessionState.AUDIENCE,
        intake=intake,
        backend="claude-code",
    )


def test_ids_are_short_and_readable(home):
    session = make_session(Store())
    prefix, suffix = session.id.rsplit("-", 1)
    assert prefix == "ramen-boston"
    assert len(suffix) == 4


def test_round_trip(home):
    store = Store()
    session = make_session(store, "birria tacos", "East LA, Los Angeles")
    path = store.save(session)
    assert path == home / ".milo" / "sessions" / session.id / "session.json"
    assert store.load(session.id) == session


def test_files_are_private(home):
    store = Store()
    path = store.save(make_session(store))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_save_updates_the_timestamp(home):
    store = Store()
    session = make_session(store)
    session.updated = now() - timedelta(days=1)
    store.save(session)
    assert now() - store.load(session.id).updated < timedelta(seconds=5)


def test_latest_picks_the_most_recent_and_skips_damaged(home):
    store = Store()
    older, newer = make_session(store, "pho"), make_session(store, "tacos")
    store.save(older)
    store.save(newer)
    broken = store.root / "broken-x-0000"
    broken.mkdir()
    (broken / "session.json").write_text("{not json")
    assert store.latest().id == newer.id


def test_latest_with_no_sessions(home):
    assert Store().latest() is None


def test_missing_and_damaged_sessions(home):
    store = Store()
    with pytest.raises(SessionNotFound, match="No saved session"):
        store.load("nope-nope-0000")
    folder = store.root / "bad-bad-0000"
    folder.mkdir(parents=True)
    (folder / "session.json").write_text('{"id": 3}')
    with pytest.raises(SessionNotFound, match="damaged"):
        store.load("bad-bad-0000")


def test_session_ids_cannot_escape_the_store(home):
    with pytest.raises(SessionNotFound):
        Store().load("../../etc")


def test_work_dir_lives_inside_the_session(home):
    store = Store()
    assert store.work_dir("ramen-boston-7f3a") == store.root / "ramen-boston-7f3a" / "work"


@pytest.mark.parametrize(
    ("text", "slug"),
    [
        ("Fenway, Boston", "fenway-boston"),
        ("Café Crème", "cafe-creme"),
        ("  ##  ", "x"),
        ("São Paulo!!", "sao-paulo"),
    ],
)
def test_slugify(text, slug):
    assert slugify(text) == slug
