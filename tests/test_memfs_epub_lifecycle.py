import os
import zipfile
from io import BytesIO

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import fs
from PyQt6 import QtWidgets

import os_patch
from gemini_translator.ui.dialogs import epub as epub_dialog


def test_isolated_memfs_copy_does_not_share_queue_resource(tmp_path, monkeypatch):
    source = tmp_path / "book.epub"
    source.write_bytes(b"epub-data")
    mem_fs = fs.open_fs("mem://")
    monkeypatch.setattr(os_patch, "_get_or_create_mem_fs", lambda: mem_fs)

    try:
        queue_path = os_patch.copy_to_mem(str(source))
        dialog_path = os_patch.copy_to_mem(str(source), unique=True)

        assert queue_path != dialog_path
        assert mem_fs.exists(queue_path.removeprefix("mem://"))
        assert mem_fs.exists(dialog_path.removeprefix("mem://"))

        mem_fs.remove(dialog_path.removeprefix("mem://"))

        assert mem_fs.exists(queue_path.removeprefix("mem://"))
        with mem_fs.openbin(queue_path.removeprefix("mem://")) as queue_file:
            assert queue_file.read() == b"epub-data"
    finally:
        mem_fs.close()


def _epub_bytes(chapter_count):
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for chapter_number in range(1, chapter_count + 1):
            archive.writestr(
                f"OEBPS/chapter_{chapter_number}.xhtml",
                f"<html><body>Chapter {chapter_number}</body></html>",
            )
    return output.getvalue()


def test_shared_memfs_copy_refreshes_when_epub_at_same_path_is_replaced(tmp_path, monkeypatch):
    source = tmp_path / "book.epub"
    source.write_bytes(_epub_bytes(499))
    mem_fs = fs.open_fs("mem://")
    monkeypatch.setattr(os_patch, "_get_or_create_mem_fs", lambda: mem_fs)

    try:
        queue_path = os_patch.copy_to_mem(str(source))
        source.write_bytes(_epub_bytes(500))

        refreshed_path = os_patch.copy_to_mem(str(source))

        assert refreshed_path == queue_path
        with mem_fs.openbin(queue_path.removeprefix("mem://")) as queue_file:
            with zipfile.ZipFile(queue_file) as archive:
                assert "OEBPS/chapter_500.xhtml" in archive.namelist()
    finally:
        mem_fs.close()


def test_shared_memfs_copy_repairs_truncated_cached_bytes(tmp_path, monkeypatch):
    source = tmp_path / "book.epub"
    source.write_bytes(_epub_bytes(500))
    mem_fs = fs.open_fs("mem://")
    monkeypatch.setattr(os_patch, "_get_or_create_mem_fs", lambda: mem_fs)

    try:
        queue_path = os_patch.copy_to_mem(str(source))
        internal_path = queue_path.removeprefix("mem://")
        mem_fs.writebytes(internal_path, _epub_bytes(499))

        os_patch.copy_to_mem(str(source))

        with mem_fs.openbin(internal_path) as queue_file:
            with zipfile.ZipFile(queue_file) as archive:
                assert "OEBPS/chapter_500.xhtml" in archive.namelist()
    finally:
        mem_fs.close()


def test_shared_memfs_copy_reuses_unchanged_source(tmp_path, monkeypatch):
    source = tmp_path / "book.epub"
    source.write_bytes(_epub_bytes(500))
    mem_fs = fs.open_fs("mem://")
    monkeypatch.setattr(os_patch, "_get_or_create_mem_fs", lambda: mem_fs)

    try:
        queue_path = os_patch.copy_to_mem(str(source))
        original_writebytes = mem_fs.writebytes
        writes = []

        def tracked_writebytes(*args, **kwargs):
            writes.append(args[0])
            return original_writebytes(*args, **kwargs)

        monkeypatch.setattr(mem_fs, "writebytes", tracked_writebytes)

        assert os_patch.copy_to_mem(str(source)) == queue_path
        assert writes == []
    finally:
        mem_fs.close()


def test_epub_selector_requests_its_own_memfs_copy(monkeypatch, tmp_path):
    _ = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    source = str(tmp_path / "book.epub")
    calls = []

    class SelectorHarness:
        virtual_epub_path = None
        real_epub_path = source
        all_chapters = []

        def _load_chapter_title_cache(self):
            pass

        def _populate_list_widget_preview(self):
            pass

        def _async_stage_3_load_details(self):
            pass

        def reject(self):
            raise AssertionError("selector should not reject a valid isolated copy")

    def fake_copy_to_mem(path, **kwargs):
        calls.append((path, kwargs))
        return "mem://isolated/dialog/book.epub"

    monkeypatch.setattr(epub_dialog.os, "copy_to_mem", fake_copy_to_mem, raising=False)
    monkeypatch.setattr(epub_dialog, "get_epub_chapter_order", lambda _path: [])
    monkeypatch.setattr(epub_dialog.QtCore.QTimer, "singleShot", lambda *_args: None)

    harness = SelectorHarness()
    epub_dialog.EpubHtmlSelectorDialog._async_stage_2_get_filelist(harness)

    assert calls == [(source, {"unique": True})]
