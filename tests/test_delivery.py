"""A missing ACK must not turn unrelated wakeups into a retransmission flood."""

from __future__ import annotations

import pytest

from chiliarch.delivery import DeliveryBackoff
from chiliarch.protocol import Envelope


def test_new_messages_bypass_backoff_but_wakeups_do_not_resend_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 0.0
    monkeypatch.setattr("chiliarch.delivery.time.monotonic", lambda: now)
    delivery = DeliveryBackoff()
    waiting = Envelope(type="agent.contact", payload={"message": "Waiting for ACK"})
    fresh = Envelope(type="agent.contact", payload={"message": "New work"})
    options = {"retry_seconds": 1.0, "max_retry_seconds": 4.0}
    assert delivery.ready([waiting], **options) == [waiting]
    for instant in (0.1, 0.2, 0.5, 0.9):
        now = instant
        assert delivery.ready([waiting], **options) == []
    assert delivery.ready([waiting, fresh], **options) == [fresh]
    now = 1.0
    assert delivery.ready([waiting, fresh], **options) == [waiting]
    # The fresh message was ACKed; the old one's second retry has its own deadline.
    now = 2.0
    assert delivery.ready([waiting], **options) == []
    now = 3.0
    assert delivery.ready([waiting], **options) == [waiting]
    now = 6.0
    assert delivery.ready([waiting], **options) == []
    for instant in (7.0, 11.0, 15.0):
        now = instant
        assert delivery.ready([waiting], **options) == [waiting]


def test_reconnect_replays_the_original_envelope_without_waiting_for_old_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("chiliarch.delivery.time.monotonic", lambda: 0.0)
    message = Envelope(type="agent.contact", payload={"message": "Durable work"})
    pending = [message]
    options = {"retry_seconds": 5.0, "max_retry_seconds": 60.0}
    original = message.canonical()
    connection = DeliveryBackoff()
    assert connection.ready(pending, **options) == pending
    assert connection.ready(pending, **options) == []
    assert pending[0].canonical() == original
    reconnected = DeliveryBackoff()
    assert reconnected.ready(pending, **options)[0].canonical() == original
    assert reconnected.ready([], **options) == []


def test_configured_retry_delay_can_be_longer_than_liveness_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 0.0
    monkeypatch.setattr("chiliarch.delivery.time.monotonic", lambda: now)
    delivery = DeliveryBackoff()
    pending = [Envelope(type="agent.contact", payload={"message": "Slow retry policy"})]
    options = {"retry_seconds": 10.0, "max_retry_seconds": 5.0}
    assert delivery.ready(pending, **options) == pending
    now = 5.0
    assert delivery.ready(pending, **options) == []
    now = 10.0
    assert delivery.ready(pending, **options) == pending
