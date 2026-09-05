import os
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtWidgets

from gemini_translator.ui.dialogs import setup as setup_dialog
from gemini_translator.ui.dialogs.setup import InitialSetupDialog, _prepare_project_location


class _PathsWidget:
    def __init__(self):
        self.file_paths = []
        self.folder_paths = []
        self.chapter_counts = []

    def set_file_path(self, path):
        self.file_paths.append(path)

    def set_folder_path(self, path):
        self.folder_paths.append(path)

    def update_chapters_info(self, count):
        self.chapter_counts.append(count)


class _TaskManager:
    def __init__(self):
        self.clear_count = 0

    def clear_all_queues(self):
        self.clear_count += 1


class _SetupHarness:
    on_file_selected = InitialSetupDialog.on_file_selected
    _collect_previous_translated_chapter_titles = (
        InitialSetupDialog._collect_previous_translated_chapter_titles
    )

    def __init__(self, selected_file=None, output_folder=None, html_files=None):
        self.selected_file = selected_file
        self.output_folder = output_folder
        self.html_files = list(html_files or [])
        self.project_manager = object()
        self.task_manager = _TaskManager()
        self.paths_widget = _PathsWidget()
        self._pending_old_project_cleanup_offer = False
        self.process_calls = []
        self.initialization_calls = 0
        self.ready_checks = 0

    def _read_epub_chapter_titles(self, epub_path, chapter_paths):
        return InitialSetupDialog._read_epub_chapter_titles(
            epub_path,
            chapter_paths,
        )

    def _process_selected_file(
        self,
        pre_selected_chapters=None,
        previous_translated_chapter_titles=None,
    ):
        self.process_calls.append(
            (pre_selected_chapters, previous_translated_chapter_titles)
        )

    def _handle_project_initialization(self):
        self.initialization_calls += 1

    def check_ready(self):
        self.ready_checks += 1


class _SettingsManager:
    def __init__(self, history):
        self.history = list(history)
        self.added_projects = []

    def load_project_history(self):
        return list(self.history)

    def add_to_project_history(self, epub_path, output_folder):
        self.added_projects.append((epub_path, output_folder))


class _InitializationHarness:
    _handle_project_initialization = InitialSetupDialog._handle_project_initialization

    def __init__(self, selected_file, output_folder, history):
        self.selected_file = selected_file
        self.output_folder = output_folder
        self.html_files = ["Text/new.xhtml"]
        self.settings_manager = _SettingsManager(history)
        self.paths_widget = _PathsWidget()
        self.project_manager = None
        self._pending_old_project_cleanup_offer = True
        self.cleanup_calls = []
        self.filter_calls = 0
        self.data_changed_calls = []

    def _maybe_offer_old_project_chapter_cleanup(self, folder_path, file_path):
        self.cleanup_calls.append((folder_path, file_path))
        return True

    def _ask_and_filter_chapters(self):
        self.filter_calls += 1

    def _on_project_data_changed(self, offer_snapshot_restore=True):
        self.data_changed_calls.append(offer_snapshot_restore)


class _SnapshotTaskManager:
    def __init__(self, meta, current_sig="current"):
        self.meta = meta
        self.current_sig = current_sig
        self.meta_reads = 0

    def read_queue_snapshot_meta(self, snapshot_path):
        self.meta_reads += 1
        return dict(self.meta)

    def _get_epub_signature(self, epub_path):
        return self.current_sig


class _SnapshotHarness:
    _maybe_offer_snapshot_restore = InitialSetupDialog._maybe_offer_snapshot_restore

    def __init__(self, snapshot_path, meta):
        self._snapshot_restore_in_progress = False
        self.is_session_active = False
        self.selected_file = "current.epub"
        self.output_folder = os.path.dirname(snapshot_path)
        self.engine = type("Engine", (), {
            "task_manager": _SnapshotTaskManager(meta)
        })()
        self._snapshot_prompted_projects = set()

    def _get_snapshot_path(self):
        return os.path.join(self.output_folder, "queue_snapshot.db")


class _RetryFilesHarness:
    add_files_for_retry = InitialSetupDialog.add_files_for_retry

    def __init__(self):
        self.selected_file = "current.epub"
        self.html_files = []
        self.events = []
        self.data_changed_calls = []
        self.ready_checks = 0

    def _post_event(self, name, data=None):
        self.events.append((name, data or {}))

    def _on_project_data_changed(self, offer_snapshot_restore=True):
        self.data_changed_calls.append(offer_snapshot_restore)

    def check_ready(self):
        self.ready_checks += 1


def test_selecting_file_after_project_folder_opens_chapter_selection(tmp_path):
    project_folder = tmp_path / "project"
    project_folder.mkdir()
    new_file = tmp_path / "new.epub"

    harness = _SetupHarness(
        selected_file=None,
        output_folder=str(project_folder),
        html_files=["Text/old.xhtml"],
    )

    harness.on_file_selected(str(new_file))

    assert harness.selected_file == str(new_file)
    assert harness.html_files == []
    assert harness.paths_widget.chapter_counts == [0]
    assert len(harness.process_calls) == 1
    assert harness.initialization_calls == 0
    assert harness.ready_checks == 1


def test_switching_project_source_opens_chapter_selection_before_initialization(tmp_path):
    project_folder = tmp_path / "project"
    project_folder.mkdir()
    old_file = tmp_path / "old.epub"
    new_file = tmp_path / "new.epub"

    harness = _SetupHarness(
        selected_file=str(old_file),
        output_folder=str(project_folder),
        html_files=["Text/old.xhtml"],
    )

    harness.on_file_selected(str(new_file))

    assert harness.selected_file == str(new_file)
    assert harness.html_files == []
    assert harness.paths_widget.chapter_counts == [0]
    assert harness.task_manager.clear_count == 1
    assert harness.project_manager is None
    assert harness._pending_old_project_cleanup_offer is True
    assert len(harness.process_calls) == 1
    assert harness.initialization_calls == 0


def test_switching_source_passes_titles_of_previous_translated_batch(tmp_path):
    project_folder = tmp_path / "project"
    project_folder.mkdir()
    old_file = tmp_path / "old.epub"
    new_file = tmp_path / "new.epub"
    chapters = ["OEBPS/chapter_1.xhtml", "OEBPS/chapter_2.xhtml"]

    with zipfile.ZipFile(old_file, "w") as archive:
        archive.writestr(chapters[0], "<html><body><h1>Глава 99</h1></body></html>")
        archive.writestr(chapters[1], "<html><body><h1>Глава 100</h1></body></html>")
    with zipfile.ZipFile(new_file, "w") as archive:
        archive.writestr("OEBPS/chapter_1.xhtml", "<html><body><h1>Глава 100</h1></body></html>")

    class _ProjectManager:
        def get_full_map(self):
            return {
                chapters[1]: {
                    "_translated_gemini.html": "translated/chapter_2.html",
                }
            }

    harness = _SetupHarness(
        selected_file=str(old_file),
        output_folder=str(project_folder),
        html_files=chapters,
    )
    harness.project_manager = _ProjectManager()

    harness.on_file_selected(str(new_file))

    assert harness.process_calls == [(None, ["Глава 100"])]


def test_swap_selection_skips_repeated_titles_from_previous_batch(tmp_path):
    new_file = tmp_path / "new.epub"
    chapters = [
        "OEBPS/chapter_1.xhtml",
        "OEBPS/chapter_2.xhtml",
        "OEBPS/chapter_3.xhtml",
    ]
    with zipfile.ZipFile(new_file, "w") as archive:
        archive.writestr(chapters[0], "<html><body><h1>Глава 99</h1></body></html>")
        archive.writestr(chapters[1], "<html><body><h1>Глава 100</h1></body></html>")
        archive.writestr(chapters[2], "<html><body><h1>Глава 101</h1></body></html>")

    selected = InitialSetupDialog._chapters_after_previous_translated_titles(
        str(new_file),
        chapters,
        ["Глава 98", "Глава 99", "Глава 100"],
    )

    assert selected == [chapters[2]]


def test_reselecting_same_file_with_existing_chapters_keeps_initialization_path(tmp_path):
    project_folder = tmp_path / "project"
    project_folder.mkdir()
    current_file = tmp_path / "book.epub"

    harness = _SetupHarness(
        selected_file=str(current_file),
        output_folder=str(project_folder),
        html_files=["Text/chapter.xhtml"],
    )

    harness.on_file_selected(str(current_file))

    assert harness.html_files == ["Text/chapter.xhtml"]
    assert harness.paths_widget.chapter_counts == []
    assert harness.process_calls == []
    assert harness.initialization_calls == 1
    assert harness.ready_checks == 1


def test_pending_cleanup_offer_runs_even_when_new_source_is_already_in_history(tmp_path):
    project_folder = tmp_path / "project"
    project_folder.mkdir()
    new_file = tmp_path / "new.epub"
    history = [{
        "epub_path": str(new_file).replace(os.sep, "/"),
        "output_folder": str(project_folder).replace(os.sep, "/"),
    }]

    harness = _InitializationHarness(
        selected_file=str(new_file),
        output_folder=str(project_folder),
        history=history,
    )

    harness._handle_project_initialization()

    assert [(os.path.normpath(folder), os.path.normpath(file_path))
            for folder, file_path in harness.cleanup_calls] == [
        (os.path.normpath(str(project_folder)), os.path.normpath(str(new_file)))
    ]
    assert harness.settings_manager.added_projects
    assert harness.filter_calls == 1
    assert harness.data_changed_calls == [False]


def test_moving_source_into_project_removes_its_backup(tmp_path, monkeypatch):
    source_file = tmp_path / "book.epub"
    source_file.write_bytes(b"epub")
    backup_file = tmp_path / "book.epub.backup"
    backup_file.write_bytes(b"backup")
    project_folder = tmp_path / "translation"
    project_folder.mkdir()

    class _MoveDialog:
        choice = "current"
        copy_file_checked = True

        def __init__(self, *args, **kwargs):
            pass

        def exec(self):
            return True

    monkeypatch.setattr(setup_dialog, "ProjectFolderDialog", _MoveDialog)
    harness = _InitializationHarness(
        selected_file=str(source_file),
        output_folder=str(project_folder),
        history=[],
    )

    harness._handle_project_initialization()

    destination = project_folder / source_file.name
    assert destination.read_bytes() == b"epub"
    assert not source_file.exists()
    assert not backup_file.exists()
    assert os.path.normpath(harness.selected_file) == os.path.normpath(str(destination))


def test_background_project_location_creates_subfolder_and_moves_source(tmp_path):
    source_file = tmp_path / "book.epub"
    source_file.write_bytes(b"epub-data")
    backup_file = tmp_path / "book.epub.backup"
    backup_file.write_bytes(b"old-backup")
    project_root = tmp_path / "projects"
    project_root.mkdir()

    result = _prepare_project_location(
        str(project_root),
        str(source_file),
        "subfolder",
        True,
    )

    destination = project_root / "book" / "book.epub"
    assert result["ok"] is True
    assert destination.read_bytes() == b"epub-data"
    assert os.path.normpath(result["effective_folder"]) == os.path.normpath(str(project_root / "book"))
    assert os.path.normpath(result["effective_file_path"]) == os.path.normpath(str(destination))
    assert not source_file.exists()
    assert not backup_file.exists()


def test_snapshot_restore_is_not_offered_for_different_epub_signature(tmp_path, monkeypatch):
    snapshot_path = tmp_path / "queue_snapshot.db"
    snapshot_path.write_bytes(b"placeholder")
    harness = _SnapshotHarness(
        str(snapshot_path),
        {
            "epub_sig": "old",
            "saved_task_count": 3,
            "saved_at": 123.0,
        },
    )
    question_calls = []

    def fake_question(*args, **kwargs):
        question_calls.append((args, kwargs))
        return QtWidgets.QMessageBox.StandardButton.No

    monkeypatch.setattr(QtWidgets.QMessageBox, "question", fake_question)

    harness._maybe_offer_snapshot_restore()

    assert question_calls == []
    assert harness._snapshot_prompted_projects == set()


def test_snapshot_restore_is_not_offered_while_auto_workflow_is_running(tmp_path, monkeypatch):
    snapshot_path = tmp_path / "queue_snapshot.db"
    snapshot_path.write_bytes(b"placeholder")
    harness = _SnapshotHarness(
        str(snapshot_path),
        {
            "epub_sig": "current",
            "saved_task_count": 3,
            "saved_at": 123.0,
        },
    )
    harness._auto_workflow_enabled_for_session = True

    def fail_question(*args, **kwargs):
        raise AssertionError("snapshot restore must not interrupt the auto workflow")

    monkeypatch.setattr(QtWidgets.QMessageBox, "question", fail_question)

    harness._maybe_offer_snapshot_restore()

    assert harness.engine.task_manager.meta_reads == 0
    assert harness._snapshot_prompted_projects == set()


def test_retry_files_refresh_does_not_offer_snapshot_restore():
    harness = _RetryFilesHarness()
    chapters = ["Text/chapter-1.xhtml"]

    harness.add_files_for_retry("current.epub", chapters)

    assert harness.html_files == chapters
    assert harness.data_changed_calls == [False]
    assert harness.ready_checks == 1
