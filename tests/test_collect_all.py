import asyncio
import time

from milo.collectors.base import collect_all
from milo.models import CollectorResult, Intake

INTAKE = Intake(what="ramen", where="Boston")


class Fake:
    def __init__(self, name: str, *, available: bool = True, delay: float = 0, error=None):
        self.name = name
        self.label = name
        self._available = available
        self._delay = delay
        self._error = error

    def available(self) -> bool:
        return self._available

    async def collect(self, intake: Intake) -> CollectorResult:
        await asyncio.sleep(self._delay)
        if self._error:
            raise self._error
        return CollectorResult(
            source=self.name, status="ok", data={}, urls=[f"https://{self.name}"]
        )


async def test_every_outcome_comes_back_in_order():
    results = await collect_all(
        [
            Fake("ok"),
            Fake("no_key", available=False),
            Fake("slow", delay=5),
            Fake("broken", error=RuntimeError("bug")),
        ],
        INTAKE,
        timeout=0.2,
    )
    assert [(r.source, r.status, r.note) for r in results] == [
        ("ok", "ok", None),
        ("no_key", "skipped", "no key"),
        ("slow", "error", "timed out after 0.2s"),
        ("broken", "error", "unexpected error (RuntimeError)"),
    ]


async def test_collectors_run_concurrently():
    start = time.monotonic()
    await collect_all([Fake("a", delay=0.3), Fake("b", delay=0.3)], INTAKE)
    assert time.monotonic() - start < 0.5


async def test_unavailable_collector_is_never_called():
    class Exploding(Fake):
        async def collect(self, intake: Intake) -> CollectorResult:
            raise AssertionError("should not run")

    [result] = await collect_all([Exploding("x", available=False)], INTAKE)
    assert result.status == "skipped"
