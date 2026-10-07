"""Real process writers retain intact JSONL through shared log rotation."""

from __future__ import annotations

import json
import multiprocessing
from multiprocessing.synchronize import Barrier
from pathlib import Path
from typing import Any

from chiliarch.config import LogSettings
from chiliarch.logging import close_logging, configure_logging


def _write_events(directory: Path, writer: int, count: int, start: Barrier) -> None:
    settings = LogSettings(directory=directory, max_bytes=1024, backup_count=count * 4)
    logger = configure_logging(settings, shared=True)
    try:
        # Every process opens its stream before concurrent rollover begins.
        logger.info(
            "worker ready",
            extra={"writer": writer, "index": 0, "credential": "fixture-secret"},
        )
        start.wait(timeout=10)
        for index in range(1, count):
            logger.info(
                "并发写入完整事件 " + "payload" * 20,
                extra={"writer": writer, "index": index, "credential": "fixture-secret"},
            )
    finally:
        close_logging()


def test_shared_process_rotation_preserves_all_events_and_reopens_old_stream(
    tmp_path: Path,
) -> None:
    workers, count = 3, 24
    # One backup per event guarantees no record can be discarded by retention.
    settings = LogSettings(
        directory=tmp_path,
        max_bytes=1024,
        backup_count=count * 4,
    )
    logger = configure_logging(settings, shared=True)
    context = multiprocessing.get_context("spawn")
    start = context.Barrier(workers)
    processes = [
        context.Process(target=_write_events, args=(tmp_path, writer, count, start))
        for writer in range(workers)
    ]
    try:
        # This parent stream must become stale when the children rotate the file.
        logger.info("parent before rotation", extra={"writer": "parent", "index": 0})
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=15)
            assert not process.is_alive(), "Concurrent log writer did not complete"
            assert process.exitcode == 0
        logger.info("parent after rotation", extra={"writer": "parent", "index": 1})
    finally:
        close_logging()
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
            if process.pid is not None:
                process.close()

    files = [tmp_path / "chiliarch.jsonl", *tmp_path.glob("chiliarch.jsonl.[0-9]*")]
    assert 1 < len(files) <= settings.backup_count + 1
    events: list[dict[str, Any]] = []
    for path in files:
        content = path.read_bytes()
        assert content.endswith(b"\n"), f"Incomplete JSONL line in {path.name}"
        events.extend(json.loads(line) for line in content.splitlines())
    observed = [(event["writer"], event["index"]) for event in events]
    expected = {(writer, index) for writer in range(workers) for index in range(count)}
    expected.update({("parent", 0), ("parent", 1)})
    assert set(observed) == expected
    assert len(observed) == len(expected), "Duplicated or lost events during rotation"
    assert all(
        event["credential"] == "[redacted]" for event in events if event["writer"] != "parent"
    )
    current = [json.loads(line) for line in (tmp_path / "chiliarch.jsonl").read_text().splitlines()]
    assert (current[-1]["writer"], current[-1]["index"]) == ("parent", 1)
