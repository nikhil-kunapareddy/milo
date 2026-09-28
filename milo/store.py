"""Session store: one JSON file per session under ~/.milo/sessions/<id>/."""

from __future__ import annotations

import os
import re
import secrets
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from milo.models import Intake, Session


class SessionNotFound(Exception):
    pass


def milo_home() -> Path:
    return Path.home() / ".milo"


def slugify(text: str, max_length: int = 40) -> str:
    """Turn "Fenway, Boston" into "fenway-boston". Accents are folded; never empty."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return slug[:max_length].rstrip("-") or "x"


def now() -> datetime:
    return datetime.now(UTC)


class Store:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or milo_home() / "sessions"

    def new_id(self, intake: Intake) -> str:
        base = f"{slugify(intake.what, 24)}-{slugify(intake.where, 24)}"
        while True:
            session_id = f"{base}-{secrets.token_hex(2)}"
            if not self._dir(session_id).exists():
                return session_id

    def work_dir(self, session_id: str) -> Path:
        """The agent's empty working folder: nothing of the user's project is reachable."""
        return self._dir(session_id) / "work"

    def scratch_dir(self) -> Path:
        """Working folder for one-off calls that don't belong to a session yet."""
        return self.root.parent / "scratch"

    def save(self, session: Session) -> Path:
        session.updated = now()
        folder = self._dir(session.id)
        folder.mkdir(parents=True, exist_ok=True)
        os.chmod(folder, 0o700)
        path = folder / "session.json"
        tmp = path.with_name("session.json.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(session.model_dump_json(indent=2))
        os.replace(tmp, path)  # atomic: a crash mid-write never leaves half a file
        return path

    def load(self, session_id: str) -> Session:
        path = self._dir(session_id) / "session.json"
        try:
            return Session.model_validate_json(path.read_text())
        except FileNotFoundError as exc:
            raise SessionNotFound(f"No saved session called {session_id!r}.") from exc
        except (ValidationError, ValueError) as exc:
            raise SessionNotFound(
                f"Session {session_id!r} is damaged and can't be opened."
            ) from exc

    def latest(self) -> Session | None:
        """The most recently updated session that can still be opened."""
        sessions = []
        for path in self.root.glob("*/session.json"):
            try:
                sessions.append(self.load(path.parent.name))
            except SessionNotFound:
                continue
        return max(sessions, key=lambda s: s.updated, default=None)

    def _dir(self, session_id: str) -> Path:
        if not re.fullmatch(r"[a-z0-9-]+", session_id):
            raise SessionNotFound(f"{session_id!r} isn't a valid session id.")
        return self.root / session_id
