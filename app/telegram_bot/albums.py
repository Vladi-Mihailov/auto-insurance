"""Telegram album (media group) aggregation.

Telegram delivers an album as SEPARATE Message updates sharing one
media_group_id, with no "album complete" marker. The collector gives every
(bot, customer, media_group_id) exactly ONE processing owner: the first
item starts a single debounce task, later items only push its deadline
back, and when the album has been quiet for `debounce_seconds` (or
`max_wait_seconds` have passed since the first item) the owner runs the
finalizer once.

Deliberately in-process: album assembly is transient transport state.
Every accepted file reference is already persisted in the customer's draft
(pending_document_files) as it arrives, so a restart only loses the timer --
the files stay attached, and resending the album (or pressing "✅ Все
документы загружены") processes them. The finalizer takes the same
per-customer isolation lock as ordinary handlers, so it never interleaves
with that customer's other updates.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

DEFAULT_DEBOUNCE_SECONDS = 1.5
DEFAULT_MAX_WAIT_SECONDS = 8.0

Finalizer = Callable[[list[str]], Awaitable[None]]


@dataclass
class _Group:
    last_seen: float
    started: float
    finalizer: Finalizer
    notes: list[str] = field(default_factory=list)
    task: asyncio.Task | None = None


class AlbumCollector:
    def __init__(self, *, debounce_seconds: float = DEFAULT_DEBOUNCE_SECONDS, max_wait_seconds: float = DEFAULT_MAX_WAIT_SECONDS):
        self._debounce = debounce_seconds
        self._max_wait = max_wait_seconds
        self._groups: dict[tuple, _Group] = {}

    def touch(self, key: tuple, finalizer: Finalizer) -> None:
        """Register one arrived album item. Starts the group's single owner
        task on the first item; afterwards only extends its quiet window."""
        loop = asyncio.get_running_loop()
        now = loop.time()
        group = self._groups.get(key)
        if group is not None:
            group.last_seen = now
            return
        group = _Group(last_seen=now, started=now, finalizer=finalizer)
        self._groups[key] = group
        group.task = loop.create_task(self._own(key, group))

    def add_note(self, key: tuple, note: str) -> None:
        """A customer-facing note for this album (e.g. a skipped file),
        shown once with the album's result -- never one message per item."""
        group = self._groups.get(key)
        if group is not None and note not in group.notes:
            group.notes.append(note)

    def is_collecting(self, key: tuple) -> bool:
        return key in self._groups

    def is_idle(self) -> bool:
        return not self._groups

    async def _own(self, key: tuple, group: _Group) -> None:
        loop = asyncio.get_running_loop()
        try:
            while True:
                now = loop.time()
                wait = min(group.last_seen + self._debounce, group.started + self._max_wait) - now
                if wait <= 0:
                    break
                await asyncio.sleep(wait)
            # Leave the registry BEFORE finalizing: an item arriving after
            # this point belongs to a group that is being/has been processed
            # and is handled by the caller's "already processed" check.
            self._groups.pop(key, None)
            await group.finalizer(list(group.notes))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- never let a background task die silently
            logger.error("Album finalization failed (%s)", type(exc).__name__)
        finally:
            self._groups.pop(key, None)

    async def drain(self) -> None:
        """Wait for every in-flight album (tests, graceful shutdown)."""
        while self._groups:
            tasks = [g.task for g in self._groups.values() if g.task is not None]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            else:
                await asyncio.sleep(0)
