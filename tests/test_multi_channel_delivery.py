"""
Test 5 — Multi-channel delivery (PRD §7.5 + §3.2 + §4.4).

Verifies that when the resolver yields multiple channels:
  - `enqueue_delivery` is called once per channel.
  - HIGH priority deliveries are routed to the dedicated `priority` queue.
  - NORMAL priority deliveries flow to the per-channel queue (default routing).

The Celery tasks are patched at `apply_async` / `delay` so nothing actually
hits a broker. We assert on the call records.
"""

from __future__ import annotations

import uuid

import pytest

from app.models.enums import ChannelType, NotificationPriority
from app.tasks.deliver import DELIVERY_TASKS, enqueue_delivery


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[ChannelType, list[dict]]:
    """Replace each per-channel task's `delay` and `apply_async`."""
    records: dict[ChannelType, list[dict]] = {c: [] for c in DELIVERY_TASKS}

    def _make_capture(channel: ChannelType):
        def _delay(*args, **kwargs):
            records[channel].append({"mode": "delay", "args": args, "kwargs": kwargs})

        def _apply_async(*, args=None, queue=None, **kwargs):  # noqa: ANN001
            records[channel].append({
                "mode": "apply_async",
                "args": args,
                "queue": queue,
            })

        return _delay, _apply_async

    for channel, task in DELIVERY_TASKS.items():
        delay_fn, apply_fn = _make_capture(channel)
        monkeypatch.setattr(task, "delay", delay_fn)
        monkeypatch.setattr(task, "apply_async", apply_fn)
    return records


def test_normal_priority_routes_to_default_per_channel_queue(
    captured: dict[ChannelType, list[dict]]
) -> None:
    delivery_id = uuid.uuid4()
    enqueue_delivery(
        delivery_id=delivery_id,
        channel=ChannelType.EMAIL,
        priority=NotificationPriority.NORMAL,
    )
    assert len(captured[ChannelType.EMAIL]) == 1
    record = captured[ChannelType.EMAIL][0]
    # enqueue_delivery now always uses apply_async so correlation headers can
    # be forwarded. NORMAL priority does not set a queue (routes via the
    # per-channel task_routes config in worker.py instead).
    assert record["mode"] == "apply_async"
    assert record["args"] == [str(delivery_id)]
    assert record["queue"] is None


def test_high_priority_routes_to_priority_queue(
    captured: dict[ChannelType, list[dict]]
) -> None:
    delivery_id = uuid.uuid4()
    enqueue_delivery(
        delivery_id=delivery_id,
        channel=ChannelType.SMS,
        priority=NotificationPriority.HIGH,
    )
    record = captured[ChannelType.SMS][0]
    assert record["mode"] == "apply_async"
    assert record["queue"] == "priority"
    assert record["args"] == [str(delivery_id)]


def test_fan_out_to_three_channels_calls_each_task_once(
    captured: dict[ChannelType, list[dict]]
) -> None:
    """Simulate the service-layer loop dispatching to multiple channels."""
    fanout = [ChannelType.EMAIL, ChannelType.SMS, ChannelType.PUSH]
    for ch in fanout:
        enqueue_delivery(
            delivery_id=uuid.uuid4(),
            channel=ch,
            priority=NotificationPriority.NORMAL,
        )
    for ch in fanout:
        assert len(captured[ch]) == 1, f"channel {ch} not enqueued"
    # Webhook was not in the fanout — must remain untouched.
    assert captured[ChannelType.WEBHOOK] == []
