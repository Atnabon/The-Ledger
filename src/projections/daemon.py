"""
ProjectionDaemon — async background task that keeps projections current.

Polls the events table from the last processed global_position,
routes events to subscribed projections, and updates checkpoints.
Fault-tolerant: if a projection handler fails, logs error, skips event
(with configurable retry), and continues.
"""
import asyncio
import logging
import time
from typing import Protocol

import asyncpg

logger = logging.getLogger(__name__)


class Projection(Protocol):
    """Interface that all projections must implement."""
    name: str
    event_types: list[str]  # event types this projection subscribes to

    async def handle(self, event: "StoredEvent", conn: asyncpg.Connection) -> None: ...
    async def rebuild(self, conn: asyncpg.Connection) -> None: ...


class ProjectionDaemon:
    def __init__(self, pool: asyncpg.Pool, projections: list, max_retries: int = 3):
        self._pool = pool
        self._projections = {p.name: p for p in projections}
        self._running = False
        self._max_retries = max_retries
        self._lag: dict[str, float] = {}  # projection_name -> lag_ms
        self._last_global_position: int = 0
        self._errors: dict[str, list] = {}

    async def run_forever(self, poll_interval_ms: int = 100) -> None:
        self._running = True
        while self._running:
            try:
                await self._process_batch()
            except Exception as e:
                logger.error(f"Daemon batch error: {e}")
            await asyncio.sleep(poll_interval_ms / 1000)

    def stop(self):
        self._running = False

    async def _process_batch(self, batch_size: int = 500) -> int:
        """Process a batch of events. Returns number of events processed."""
        # Get the lowest checkpoint across all projections
        async with self._pool.acquire() as conn:
            checkpoints = {}
            for name, proj in self._projections.items():
                row = await conn.fetchrow(
                    "SELECT last_position FROM projection_checkpoints WHERE projection_name = $1",
                    name
                )
                checkpoints[name] = row["last_position"] if row else 0

            min_position = min(checkpoints.values()) if checkpoints else 0

            # Load events from that position
            rows = await conn.fetch(
                """SELECT event_id, stream_id, stream_position, global_position,
                          event_type, event_version, payload, metadata, recorded_at
                   FROM events WHERE global_position > $1
                   ORDER BY global_position ASC LIMIT $2""",
                min_position, batch_size
            )

            if not rows:
                return 0

            processed = 0
            for row in rows:
                from src.models.events import StoredEvent
                event = StoredEvent(
                    event_id=row["event_id"],
                    stream_id=row["stream_id"],
                    stream_position=row["stream_position"],
                    global_position=row["global_position"],
                    event_type=row["event_type"],
                    event_version=row["event_version"],
                    payload=row["payload"],
                    metadata=row["metadata"],
                    recorded_at=row["recorded_at"],
                )

                for name, proj in self._projections.items():
                    if event.global_position <= checkpoints.get(name, 0):
                        continue
                    if event.event_type not in proj.event_types:
                        # Update checkpoint even for unsubscribed events
                        await conn.execute(
                            """INSERT INTO projection_checkpoints (projection_name, last_position, updated_at)
                               VALUES ($1, $2, NOW())
                               ON CONFLICT (projection_name)
                               DO UPDATE SET last_position = $2, updated_at = NOW()
                               WHERE projection_checkpoints.last_position < $2""",
                            name, event.global_position
                        )
                        checkpoints[name] = event.global_position
                        continue

                    retries = 0
                    while retries <= self._max_retries:
                        try:
                            await proj.handle(event, conn)
                            # Update checkpoint
                            await conn.execute(
                                """INSERT INTO projection_checkpoints (projection_name, last_position, updated_at)
                                   VALUES ($1, $2, NOW())
                                   ON CONFLICT (projection_name)
                                   DO UPDATE SET last_position = $2, updated_at = NOW()""",
                                name, event.global_position
                            )
                            checkpoints[name] = event.global_position
                            break
                        except Exception as e:
                            retries += 1
                            logger.error(f"Projection {name} failed on event {event.global_position}: {e} (retry {retries}/{self._max_retries})")
                            if retries > self._max_retries:
                                self._errors.setdefault(name, []).append({
                                    "global_position": event.global_position,
                                    "error": str(e)
                                })
                                # Skip this event for this projection
                                await conn.execute(
                                    """INSERT INTO projection_checkpoints (projection_name, last_position, updated_at)
                                       VALUES ($1, $2, NOW())
                                       ON CONFLICT (projection_name)
                                       DO UPDATE SET last_position = $2, updated_at = NOW()""",
                                    name, event.global_position
                                )
                                checkpoints[name] = event.global_position

                processed += 1

            # Update lag metrics
            max_global = await conn.fetchval("SELECT COALESCE(MAX(global_position), 0) FROM events")
            self._last_global_position = max_global
            for name in self._projections:
                self._lag[name] = max_global - checkpoints.get(name, 0)

            return processed

    def get_lag(self, projection_name: str) -> float:
        return self._lag.get(projection_name, -1)

    def get_all_lags(self) -> dict[str, float]:
        return dict(self._lag)

    async def rebuild_projection(self, projection_name: str) -> None:
        """Rebuild a specific projection from scratch."""
        proj = self._projections.get(projection_name)
        if not proj:
            raise ValueError(f"Unknown projection: {projection_name}")
        async with self._pool.acquire() as conn:
            await proj.rebuild(conn)
            await conn.execute(
                """INSERT INTO projection_checkpoints (projection_name, last_position, updated_at)
                   VALUES ($1, 0, NOW())
                   ON CONFLICT (projection_name)
                   DO UPDATE SET last_position = 0, updated_at = NOW()""",
                projection_name
            )
