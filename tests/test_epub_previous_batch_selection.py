import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtCore, QtWidgets

from gemini_translator.ui.dialogs.epub import EpubHtmlSelectorDialog


def _make_dialog(chapter_titles, previous_titles):
    chapters = [
        f"OEBPS/new/chapter_{index}.xhtml"
        for index in range(1, len(chapter_titles) + 1)
    ]
    dialog = EpubHtmlSelectorDialog(
        "new-book.epub",
        previous_translated_chapter_titles=previous_titles,
    )
    dialog.all_chapters = chapters
    dialog.untranslated_chapters = list(chapters)
    dialog._chapter_title_cache = dict(zip(chapters, chapter_titles))
    dialog.list_widget = QtWidgets.QListWidget()
    dialog.list_widget.setSelectionMode(
        QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection
    )
    dialog._size_cache = {path: 100 for path in chapters}
    return dialog, chapters


def _selection_state(dialog):
    return {
        dialog.list_widget.item(index).data(QtCore.Qt.ItemDataRole.UserRole):
            dialog.list_widget.item(index).isSelected()
        for index in range(dialog.list_widget.count())
    }


def test_new_source_selects_only_chapters_after_repeated_translated_tail(qapp):
    dialog, chapters = _make_dialog(
        ["Глава 99", "Глава 100", "Глава 101", "Глава 102"],
        ["Глава 98", "Глава 99", "Глава 100"],
    )
    try:
        assert dialog._apply_previous_source_title_cutoff() is True
        dialog._show_all_chapters()

        assert dialog._previous_source_selection_cutoff == 1
        assert _selection_state(dialog) == {
            chapters[0]: False,
            chapters[1]: False,
            chapters[2]: True,
            chapters[3]: True,
        }
    finally:
        dialog.close()


def test_new_source_uses_latest_previous_title_that_is_actually_repeated(qapp):
    dialog, chapters = _make_dialog(
        ["  ГЛАВА   99  ", "Глава 101"],
        ["Глава 98", "Глава 99", "Глава 100"],
    )
    try:
        assert dialog._apply_previous_source_title_cutoff() is True
        dialog._show_all_chapters()

        assert _selection_state(dialog) == {
            chapters[0]: False,
            chapters[1]: True,
        }
    finally:
        dialog.close()


def test_repeated_title_is_disambiguated_by_previous_chapter_context(qapp):
    cutoff = EpubHtmlSelectorDialog.find_previous_title_cutoff(
        ["Другой раздел", "Финал", "Арка 1", "Финал", "Новая глава"],
        ["Арка 1", "Финал"],
    )

    assert cutoff == 3


def test_ambiguous_repeated_title_does_not_hide_chapters(qapp):
    cutoff = EpubHtmlSelectorDialog.find_previous_title_cutoff(
        ["Пролог", "Пролог", "Новая глава"],
        ["Пролог"],
    )

    assert cutoff is None
