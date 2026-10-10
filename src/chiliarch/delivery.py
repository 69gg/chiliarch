"""Connection-local pacing; the durable outbox remains the delivery authority."""

from __future__ import annotations

import time

from chiliarch.protocol import Envelope


class DeliveryBackoff:
    """Let new messages through while slowing retries that haven't received an ACK."""

    def __init__(self) -> None:
        self._attempts: dict[str, tuple[float, float]] = {}

    def ready(
        self, pending: list[Envelope], *, retry_seconds: float, max_retry_seconds: float
    ) -> list[Envelope]:
        now = time.monotonic()
        identities = {message.id for message in pending}
        self._attempts = {
            key: attempt for key, attempt in self._attempts.items() if key in identities
        }
        ready: list[Envelope] = []
        for message in pending:
            deadline, previous_delay = self._attempts.get(message.id, (now, 0.0))
            if now < deadline:
                continue
            delay = min(
                max(retry_seconds, max_retry_seconds), max(retry_seconds, previous_delay * 2)
            )
            self._attempts[message.id] = (now + delay, delay)
            ready.append(message)
        return ready
