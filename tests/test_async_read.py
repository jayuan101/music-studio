"""Slow disk reads must not build a backlog when selection changes."""

from threading import Event

import pytest
from PySide6.QtCore import QCoreApplication

from musicstudio.ui.widgets.async_read import AsyncFileReader


@pytest.mark.parametrize("cancel_latest", [False, True])
def test_skipped_reads_do_not_delay_latest_selection(tmp_path, cancel_latest):
    app = QCoreApplication.instance() or QCoreApplication([])
    started, release = Event(), Event()
    calls, delivered = [], []

    def read(path):
        calls.append(path.name)
        if path.name == "first":
            started.set()
            assert release.wait(5)
        return path.name

    reader = AsyncFileReader(read)
    reader.ready.connect(lambda path, result: delivered.append(result))

    def request(name):
        reader.request(tmp_path / name)
        # Trigger the debounce timeout deterministically.
        reader._timer.stop()
        reader._start()

    request("first")
    try:
        assert started.wait(5)
        for index in range(30):
            request(f"skipped-{index}")
        request("latest")
        if cancel_latest:
            reader.cancel()
    finally:
        release.set()
        assert reader._pool.waitForDone(5000)
    app.processEvents()
    assert calls == (["first"] if cancel_latest else ["first", "latest"])
    assert delivered == ([] if cancel_latest else ["latest"])
