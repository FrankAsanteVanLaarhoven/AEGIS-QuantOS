from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from aegis.store import Store


def test_store_multithreaded_reads_writes_and_events(tmp_path):
    store = Store(tmp_path / "concurrent.sqlite")
    errors: list[Exception] = []

    def worker(worker_id: int) -> None:
        try:
            for i in range(40):
                with store.tx():
                    store.put(f"k:{worker_id}:{i}", {"w": worker_id, "i": i})
                    store.append("tick", {"w": worker_id, "i": i})
                _ = store.get(f"k:{worker_id}:{i}")
                _ = store.events(limit=10)
                _ = store.result("probe", {"w": worker_id, "i": i})
                _ = store.results("probe")
        except Exception as exc:  # pragma: no cover - test fails if populated
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(worker, range(8)))

    assert not errors
    verified = store.verify()
    assert verified["valid"] is True
    assert verified["events"] == 320


def test_store_tx_rollback_when_inner_fails(tmp_path):
    store = Store(tmp_path / "rollback.sqlite")
    store.put("initial", {"ok": True})
    try:
        with store.tx():
            store.put("initial", {"ok": False})
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert store.get("initial") == {"ok": True}

