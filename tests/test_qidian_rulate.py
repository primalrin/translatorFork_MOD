import json
from threading import Event

import pytest

from PyQt6.QtGui import QImage

from qidian_rulate.models import QidianBookMetadata
from qidian_rulate.models import PreparedRulateMetadata, RulateBookDraft
from qidian_rulate import workers
from gemini_translator.ui.dialogs import qidian_rulate_creator as creator_module
from gemini_translator.ui.dialogs.qidian_rulate_creator import QidianRulateCreatorWindow
from qidian_rulate.workers import (
    CODEX_COVER_MODEL,
    _append_codex_prompt,
    _build_codex_cover_translation_prompt,
    _build_codex_cover_exec_command,
    _find_generated_cover,
    _first_meaningful_select_option,
    _load_cover_image_from_data,
    _load_cover_image_from_file,
    _cover_url_candidates,
    _fetch_qimao_chapter_links,
    _is_browser_missing_error,
    _clean_ciweimao_chapter_text,
    _clean_qidian_description,
    _clean_qidian_chapter_text,
    _CIWEIMAO_CHAPTER_LINKS_SCRIPT,
    _CIWEIMAO_CHAPTER_TEXT_SCRIPT,
    _CIWEIMAO_EXTRACT_SCRIPT,
    _extract_qidian_description_from_body,
    _FANQIE_CHAPTER_LINKS_SCRIPT,
    _FANQIE_CHAPTER_TEXT_SCRIPT,
    _FANQIE_EXTRACT_SCRIPT,
    _fanqie_book_id,
    _find_tomato_executable,
    _qimao_book_id,
    _QIMAO_CHAPTER_TEXT_SCRIPT,
    _QIMAO_EXTRACT_SCRIPT,
    _QIDIAN_CHAPTER_LINKS_SCRIPT,
    _read_tomato_chapters_from_folder,
    _select_qidian_description,
    _source_name,
    _tag_file_candidates,
    _tomato_bind_addr_from_base_url,
    _tomato_web_is_local,
    _wait_for_ciweimao_human_verification,
    RULATE_BOOK_TYPE_DESCRIPTION,
    RULATE_BOOK_TYPE_SELECTOR,
    RULATE_BOOK_TYPE_TITLE,
    RULATE_CATEGORY_URL,
    RULATE_CHINESE_CATEGORY_TITLE,
    RULATE_INFO_URL,
    RULATE_PROFILE_DIR,
    CodexCoverTranslateWorker,
    RulateFillWorker,
    build_ai_prompt,
    build_catalog_prompt,
    build_cover_prompt_request,
    clean_cover_prompt_response,
    normalize_rulate_tags,
    parse_catalog_metadata,
    parse_prepared_metadata,
    parse_translation_metadata,
    validate_ciweimao_url,
    validate_fanqie_url,
    validate_qimao_url,
    validate_qidian_url,
    validate_source_url,
)


FANTASY = "\u0444\u044d\u043d\u0442\u0435\u0437\u0438"
MYSTIC = "\u043c\u0438\u0441\u0442\u0438\u043a\u0430"
ADVENTURE = "\u043f\u0440\u0438\u043a\u043b\u044e\u0447\u0435\u043d\u0438\u044f"


class _QidianCreatorHarness:
    _return_to_menu = QidianRulateCreatorWindow._return_to_menu

    def __init__(self, handler=None):
        self._return_to_menu_handler = handler
        self.calls = []

    def hide(self):
        self.calls.append("hide")

    def close(self):
        self.calls.append("close")


class _FillDescriptionHarness:
    _fill_description = RulateFillWorker._fill_description

    def __init__(self):
        self.logs = []
        self.draft = RulateBookDraft(
            qidian=QidianBookMetadata(
                source_url="https://www.qidian.com/book/1041604040/",
                title_original="\u5f02\u5ea6\u65c5\u793e",
                author_name="\u8fdc\u77b3",
                description="\u63cf\u8ff0",
                cover_url="https://example.com/cover.webp",
            ),
            prepared=PreparedRulateMetadata(
                translated_description="\u041e\u043f\u0438\u0441\u0430\u043d\u0438\u0435",
                genres=[],
                tags=[],
            ),
        )

    def log(self, level, message):
        self.logs.append((level, message))


class _UploadCoverHarness:
    _upload_generated_cover = RulateFillWorker._upload_generated_cover
    _confirm_cover_cropper = RulateFillWorker._confirm_cover_cropper

    def __init__(self, cover_path):
        self.logs = []
        self.draft = RulateBookDraft(
            qidian=QidianBookMetadata(),
            prepared=PreparedRulateMetadata(generated_cover_path=str(cover_path)),
        )

    def log(self, level, message):
        self.logs.append((level, message))


class _SocialLinksHarness:
    _fill_social_links = RulateFillWorker._fill_social_links

    def __init__(self, prepared=None):
        self.logs = []
        self.draft = RulateBookDraft(
            qidian=QidianBookMetadata(),
            prepared=prepared or PreparedRulateMetadata(),
        )

    def log(self, level, message):
        self.logs.append((level, message))


class _CheckedField:
    def __init__(self, selector, checked):
        self.selector = selector
        self.checked = checked

    def check(self):
        self.checked.append(self.selector)

    def count(self):
        return 1

    def is_visible(self):
        return True

    def is_enabled(self):
        return True

    def get_attribute(self, name):
        return None


class _SchedulePage:
    def __init__(self):
        self.selected = []
        self.checked = []

    def select_option(self, selector, value):
        self.selected.append((selector, value))

    def locator(self, selector):
        if "book_settings_mode" in selector:
            return _EmptyLocator()
        return _CheckedField(selector, self.checked)


class _PublicationScheduleHarness:
    _fill_publication_schedule = RulateFillWorker._fill_publication_schedule

    def __init__(self):
        self.logs = []

    def log(self, level, message):
        self.logs.append((level, message))


class _EmptyLocator:
    @property
    def first(self):
        return self

    @property
    def last(self):
        return self

    def count(self):
        return 0

    def wait_for(self, **kwargs):
        return None


class _VisibleLocator(_EmptyLocator):
    def count(self):
        return 1


class _UploadFileLocator(_VisibleLocator):
    def __init__(self):
        self.files = []

    def set_input_files(self, path):
        self.files.append(path)


class _UploadButtonLocator(_VisibleLocator):
    def __init__(self):
        self.clicks = []

    def click(self, **kwargs):
        self.clicks.append(kwargs)


class _UploadCoverPage:
    def __init__(self):
        self.file_input = _UploadFileLocator()
        self.ok_button = _UploadButtonLocator()
        self.empty = _EmptyLocator()
        self.visible = _VisibleLocator()
        self.evaluated = []
        self.timeouts = []

    def evaluate(self, script, arg=None):
        self.evaluated.append((script, arg))

    def locator(self, selector):
        if selector == "#general":
            return self.visible
        if 'input[type="file"]' in selector:
            return self.file_input
        if 'button[data-action="ok"]' in selector:
            return self.ok_button
        return self.empty

    def wait_for_timeout(self, timeout):
        self.timeouts.append(timeout)


class _DescriptionPage:
    def __init__(self):
        self.filled_selectors = []
        self.selected_options = []
        self.evaluated = []

    def evaluate(self, script, arg=None):
        self.evaluated.append((script, arg))

    def locator(self, selector):
        class _Locator:
            def wait_for(self, **kwargs):
                return None

        return _Locator()

    def wait_for_timeout(self, timeout):
        return None

    def select_option(self, selector, value):
        self.selected_options.append((selector, value))


def test_validate_qidian_url_accepts_book_links_only():
    assert validate_qidian_url("https://www.qidian.com/book/1041604040/")
    assert validate_qidian_url("http://qidian.com/book/1041604040")
    assert validate_qidian_url("https://www.qidian.com/book/1041604040/?source=m")
    assert not validate_qidian_url("https://www.qidian.com/author/4362948/")
    assert not validate_qidian_url("https://www.qidian.com/book/1041604040/catalog/")
    assert not validate_qidian_url("https://example.com/book/1041604040/")


def test_validate_source_url_accepts_supported_book_links():
    assert validate_fanqie_url("https://fanqienovel.com/page/7229603492648717324")
    assert validate_fanqie_url("https://www.fanqienovel.com/page/7229603492648717324?enter_from=search")
    assert not validate_fanqie_url("https://fanqienovel.com/reader/7233607619578233396")
    assert not validate_fanqie_url("https://example.com/page/7229603492648717324")
    assert validate_source_url("https://www.qidian.com/book/1041604040/")
    assert validate_source_url("https://fanqienovel.com/page/7229603492648717324")
    assert _fanqie_book_id("https://fanqienovel.com/page/7229603492648717324") == "7229603492648717324"

    assert validate_ciweimao_url("https://www.ciweimao.com/book/100441110")
    assert validate_ciweimao_url("https://www.ciweimao.com/book/100441110/?from=search")
    assert validate_ciweimao_url("http://ciweimao.com/book/100441110")
    assert not validate_ciweimao_url("https://wap.ciweimao.com/book/100441110")
    assert not validate_ciweimao_url("https://www.ciweimao.com/chapter/113404377")
    assert not validate_ciweimao_url("https://example.com/book/100441110")
    assert validate_source_url("https://www.ciweimao.com/book/100441110")
    assert _source_name("https://www.ciweimao.com/book/100441110") == "Ciweimao"

    assert validate_qimao_url("https://www.qimao.com/shuku/195958/")
    assert validate_qimao_url("http://qimao.com/shuku/195958?source=search")
    assert not validate_qimao_url("https://www.qimao.com/shuku/195958-499610/")
    assert not validate_qimao_url("https://example.com/shuku/195958/")
    assert validate_source_url("https://www.qimao.com/shuku/195958/")
    assert _qimao_book_id("https://www.qimao.com/shuku/195958/") == "195958"
    assert _source_name("https://www.qimao.com/shuku/195958/") == "Qimao"


def test_fetch_qimao_chapter_links_uses_public_catalog_api(monkeypatch):
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": {
                    "chapters": [
                        {"id": "499610", "title": "第1章  红杏出墙"},
                        {"id": "520725", "title": "第2章  漂亮的女上司"},
                        {"id": "invalid", "title": "skip"},
                    ]
                }
            }

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr(workers.requests, "get", fake_get)

    links = _fetch_qimao_chapter_links("https://www.qimao.com/shuku/195958/", limit=2)

    assert links == [
        {
            "href": "https://www.qimao.com/shuku/195958-499610/",
            "title": "第1章 红杏出墙",
        },
        {
            "href": "https://www.qimao.com/shuku/195958-520725/",
            "title": "第2章 漂亮的女上司",
        },
    ]
    assert calls[0][0] == "https://www.qimao.com/api/book/chapter-list"
    assert calls[0][1]["params"] == {"book_id": "195958"}
    assert calls[0][1]["headers"]["Referer"] == "https://www.qimao.com/shuku/195958/"


def test_single_request_worker_reads_external_cancel_event():
    cancel_event = Event()
    worker = workers._SingleRequestWorker(
        settings_manager=None,
        provider_config={},
        model_config={"id": "test-model"},
        api_key="test-key",
        model_settings={},
        log_callback=lambda level, message: None,
        cancel_event=cancel_event,
    )

    assert not worker.is_cancelled

    cancel_event.set()

    assert worker.is_cancelled


def test_tomato_autostart_helpers_find_env_executable(monkeypatch, tmp_path):
    exe = tmp_path / "TomatoNovelDownloader-Win64-v2.4.11.exe"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setenv("TOMATO_NOVEL_DOWNLOADER_EXE", str(exe))

    assert _find_tomato_executable() == exe


def test_tomato_autostart_prefers_bundled_tools_dir(monkeypatch, tmp_path):
    bundled = tmp_path / "program" / "tools" / "tomato"
    bundled.mkdir(parents=True)
    bundled_exe = bundled / "TomatoNovelDownloader-Win64-v2.4.11.exe"
    bundled_exe.write_text("", encoding="utf-8")
    monkeypatch.delenv("TOMATO_NOVEL_DOWNLOADER_EXE", raising=False)
    monkeypatch.setattr(
        workers.api_config,
        "get_resource_path",
        lambda relative_path: bundled if relative_path == "tools/tomato" else tmp_path / "missing",
    )

    assert _find_tomato_executable() == bundled_exe


def test_tomato_web_autostart_is_limited_to_local_urls():
    assert _tomato_web_is_local("http://127.0.0.1:18423")
    assert _tomato_web_is_local("http://localhost:18423")
    assert not _tomato_web_is_local("https://example.com:18423")
    assert _tomato_bind_addr_from_base_url("http://127.0.0.1:18424") == "127.0.0.1:18424"


def test_qidian_rulate_profile_is_separate_from_ranobelib_uploader():
    assert ".qidian_rulate_creator" in str(RULATE_PROFILE_DIR)
    assert ".ranobelib_uploader" not in str(RULATE_PROFILE_DIR)


def test_tag_file_candidates_use_program_area(monkeypatch):
    monkeypatch.delenv("RULATE_TAGS_FILE", raising=False)

    candidates = list(_tag_file_candidates())
    candidate_strings = [str(path).lower() for path in candidates]

    assert any("qidian_rulate" in path and path.endswith("tags.txt") for path in candidate_strings)
    assert not any(
        path.name.lower() == "tags.txt" and path.parent.name.lower() == "downloads"
        for path in candidates
    )


def test_rulate_fill_uses_category_page_before_info_page():
    assert RULATE_CATEGORY_URL == "https://tl.rulate.ru/book/0/edit/cat"
    assert RULATE_BOOK_TYPE_TITLE == "Книга"
    assert RULATE_BOOK_TYPE_DESCRIPTION == "Публикуйте свои произведения"
    assert RULATE_BOOK_TYPE_SELECTOR == 'a.create-card.card-book[href*="typ=A"]'
    assert RULATE_CHINESE_CATEGORY_TITLE == "Китайские"
    assert RULATE_INFO_URL == "https://tl.rulate.ru/book/0/edit/info#general"


@pytest.mark.parametrize("start", ["type", "pubtype", "cat"])
def test_rulate_creation_walks_publication_step_before_category(monkeypatch, start):
    class Page:
        def __init__(self):
            self.stage = start
            self.calls = []
            self.translation = False

        def goto(self, url, **kwargs):
            assert url == RULATE_CATEGORY_URL

        def locator(self, selector):
            return Locator(self, selector)

        def get_by_role(self, role, *, name, exact):
            assert self.stage == "cat"
            assert role == "link" and name == "Китайские" and exact
            return Locator(self, "category")

    class Locator:
        def __init__(self, page, selector):
            self.page = page
            self.selector = selector

        @property
        def first(self):
            return self

        def count(self):
            if self.selector == RULATE_BOOK_TYPE_SELECTOR:
                return int(self.page.stage == "type")
            assert self.selector == 'input[name="copyright"][value="0"]'
            return int(self.page.stage == "pubtype")

        def check(self, **kwargs):
            self.page.translation = True
            self.page.calls.append("translation")

        def get_by_role(self, role, *, name, exact):
            assert self.selector == 'form[action="/book/0/edit/pubtype"]'
            assert role == "button" and name == "Продолжить" and exact
            return Locator(self.page, "continue")

        def click(self, **kwargs):
            if self.selector == RULATE_BOOK_TYPE_SELECTOR:
                self.page.stage = "pubtype"
            elif self.selector == "continue":
                assert self.page.translation
                self.page.stage = "cat"
            else:
                assert self.selector == "category" and self.page.stage == "cat"
                self.page.stage = "info"
            self.page.calls.append(self.selector)

        def wait_for(self, **kwargs):
            assert self.selector == "#form-edit" and self.page.stage == "info"

    page = Page()
    harness = _SocialLinksHarness()
    monkeypatch.setattr(workers, "_rulate_catalog_matches", lambda page, title: page.stage == "info")
    assert RulateFillWorker._select_catalog_category(harness, page)
    assert page.calls[-1] == "category"
    if start != "cat":
        assert page.calls.index("translation") < page.calls.index("continue") < page.calls.index("category")

    monkeypatch.setattr(workers, "_rulate_catalog_matches", lambda *args: False)
    page = Page()
    assert not RulateFillWorker._select_catalog_category(harness, page)


def test_qidian_creator_return_to_menu_closes_before_handler():
    handler_calls = []
    harness = _QidianCreatorHarness(handler=lambda: handler_calls.append("handler"))

    harness._return_to_menu()

    assert harness.calls == ["hide", "close"]
    assert handler_calls == ["handler"]


def test_qidian_creator_return_to_menu_without_handler_closes_then_reboots(monkeypatch):
    reboot_calls = []
    monkeypatch.setattr(creator_module, "return_to_main_menu", lambda: reboot_calls.append("menu"))
    harness = _QidianCreatorHarness()

    harness._return_to_menu()

    assert harness.calls == ["close"]
    assert reboot_calls == ["menu"]


def test_rulate_description_fill_does_not_insert_cover_url(monkeypatch):
    filled = []
    monkeypatch.setattr(workers, "_fill", lambda page, selector, value: filled.append((selector, value)))

    harness = _FillDescriptionHarness()
    page = _DescriptionPage()

    harness._fill_description(page)

    assert "#Book_new_img_url" not in [selector for selector, _value in filled]
    assert ('select[name="Book[status]"]', "1") in page.selected_options


def test_rulate_description_fill_uses_up_to_15_tags(monkeypatch):
    selected = []
    monkeypatch.setattr(
        workers,
        "_select_magic_value",
        lambda page, selector, value, allow_free: selected.append((selector, value)) or True,
    )
    harness = _FillDescriptionHarness()
    harness.draft.prepared.tags = [f"tag-{index}" for index in range(20)]
    page = _DescriptionPage()

    harness._fill_description(page)

    selected_tags = [value for selector, value in selected if selector == "#Book_tags"]
    assert selected_tags == [f"tag-{index}" for index in range(15)]


def test_rulate_description_fill_uses_up_to_7_genres(monkeypatch):
    selected = []
    monkeypatch.setattr(
        workers,
        "_select_magic_value",
        lambda page, selector, value, allow_free: selected.append((selector, value)) or True,
    )
    harness = _FillDescriptionHarness()
    harness.draft.prepared.genres = [f"genre-{index}" for index in range(10)]
    page = _DescriptionPage()

    harness._fill_description(page)

    selected_genres = [value for selector, value in selected if selector == "#Book_genres"]
    assert selected_genres == [f"genre-{index}" for index in range(7)]


def test_parse_prepared_metadata_strips_json_fence_and_normalizes_lists(monkeypatch):
    allowed_tags = [
        "sci-fi",
        "\u0442\u0430\u0439\u043d\u044b",
        "\u043c\u0438\u0441\u0442\u0438\u043a\u0430",
        "\u043f\u0443\u0442\u0435\u0448\u0435\u0441\u0442\u0432\u0438\u0435 \u0432 \u0434\u0440\u0443\u0433\u043e\u0439 \u043c\u0438\u0440",
    ]
    monkeypatch.setattr(workers, "load_rulate_tags", lambda: allowed_tags)
    payload = {
        "english_title": "Otherworldly Inn",
        "translated_title": "\u0418\u043d\u043e\u043c\u0435\u0440\u043d\u0430\u044f \u0433\u043e\u0441\u0442\u0438\u043d\u0438\u0446\u0430",
        "translated_description": "\u0422\u0435\u043a\u0441\u0442\n\n\n\u043e\u043f\u0438\u0441\u0430\u043d\u0438\u044f",
        "genres": [FANTASY.upper(), MYSTIC, "unknown"],
        "tags": [
            "SCI-FI",
            "\u0422\u0430\u0439\u043d\u044b",
            "\u043d\u0435\u0441\u0443\u0449\u0435\u0441\u0442\u0432\u0443\u044e\u0449\u0438\u0439 \u0442\u0435\u0433",
        ],
        "cover_prompt": "```text\nA cinematic cover. Typography: The text \"\u0418\u043d\u043e\u043c\u0435\u0440\u043d\u0430\u044f \u0433\u043e\u0441\u0442\u0438\u043d\u0438\u0446\u0430\" written in glowing serif letters. --ar 2:3\n```",
    }
    prepared = parse_prepared_metadata(f"```json\n{json.dumps(payload, ensure_ascii=False)}\n```")

    assert prepared.english_title == "Otherworldly Inn"
    assert prepared.translated_title
    assert prepared.translated_description == "\u0422\u0435\u043a\u0441\u0442\n\n\u043e\u043f\u0438\u0441\u0430\u043d\u0438\u044f"
    assert prepared.genres[:3] == [FANTASY, MYSTIC, ADVENTURE]
    assert prepared.tags[:3] == [
        "sci-fi",
        "\u0442\u0430\u0439\u043d\u044b",
        "\u043c\u0438\u0441\u0442\u0438\u043a\u0430",
    ]
    assert prepared.cover_prompt == (
        "A cinematic cover. Typography: The text \"\u0418\u043d\u043e\u043c\u0435\u0440\u043d\u0430\u044f \u0433\u043e\u0441\u0442\u0438\u043d\u0438\u0446\u0430\" written in glowing serif letters. --ar 2:3"
    )


def test_parse_translation_metadata_ignores_catalog_fields():
    payload = {
        "english_title": "Otherworldly Inn",
        "translated_title": "\u0418\u043d\u043e\u043c\u0438\u0440\u043d\u0430\u044f \u0433\u043e\u0441\u0442\u0438\u043d\u0438\u0446\u0430",
        "translated_description": "\u0422\u0435\u043a\u0441\u0442\n\n\n\u043e\u043f\u0438\u0441\u0430\u043d\u0438\u044f",
        "genres": [FANTASY],
        "tags": ["sci-fi"],
    }

    prepared = parse_translation_metadata(json.dumps(payload, ensure_ascii=False))

    assert prepared.english_title == "Otherworldly Inn"
    assert prepared.translated_title == "\u0418\u043d\u043e\u043c\u0438\u0440\u043d\u0430\u044f \u0433\u043e\u0441\u0442\u0438\u043d\u0438\u0446\u0430"
    assert prepared.translated_description == "\u0422\u0435\u043a\u0441\u0442\n\n\u043e\u043f\u0438\u0441\u0430\u043d\u0438\u044f"
    assert prepared.genres == []
    assert prepared.tags == []


def test_first_team_option_skips_rulate_no_team_placeholder():
    options = [
        {"index": 0, "value": "0", "label": "Нет", "disabled": False},
        {"index": 1, "value": "4120", "label": "Avalon", "disabled": False},
        {"index": 2, "value": "3546", "label": "SRS", "disabled": False},
    ]

    assert _first_meaningful_select_option(options) == 1


def test_social_links_use_defaults_for_legacy_draft(monkeypatch):
    filled = []
    monkeypatch.setattr(workers, "_fill", lambda _page, selector, value: filled.append((selector, value)))

    harness = _SocialLinksHarness()
    harness._fill_social_links(_SchedulePage())

    assert filled == [
        ('[name="Book[vk_link]"]', "https://vk.com/tldnd"),
        ('[name="Book[tg_url]"]', "https://t.me/tl_srs"),
    ]


def test_publication_schedule_uses_requested_counts_and_random_times(monkeypatch):
    random_values = iter((7, 42, 19))
    filled = []
    monkeypatch.setattr(workers.random, "randint", lambda _start, _end: next(random_values))
    monkeypatch.setattr(workers, "_fill", lambda _page, selector, value: filled.append((selector, value)))
    page = _SchedulePage()

    harness = _PublicationScheduleHarness()
    harness._fill_publication_schedule(page)

    assert filled == [
        ('[name="Book[unsub_count]"]', "1"),
        ('[name="Book[unsub_days]"]', "3"),
        ('[name="Book[unsub_limit]"]', "-1"),
        ('[name="Book[open_count]"]', "10"),
        ('[name="Book[open_days]"]', "1"),
        ('[name="Book[open_hours]"]', "19"),
    ]
    assert page.selected == [
        ('[name="Book[unsub_hours]"]', "7"),
        ('[name="Book[unsub_minutes]"]', "42"),
    ]
    assert page.checked == [
        '[name="Book[unsub_auto]"][type="checkbox"]',
        '[name="Book[open_auto]"][type="checkbox"]',
        '[name="Book[frequency]"][type="checkbox"]',
    ]


@pytest.mark.parametrize("state", ["missing", "hidden", "disabled", "readonly"])
def test_account_managed_rulate_fields_are_skipped_without_writes(monkeypatch, state):
    class AccountField(_CheckedField):
        def count(self):
            return 0 if state == "missing" else 1

        def is_visible(self):
            return state != "hidden"

        def is_enabled(self):
            return state != "disabled"

        def get_attribute(self, name):
            return "" if state == "readonly" and name == "readonly" else None

    page = _SchedulePage()
    page.locator = lambda selector: (
        _EmptyLocator() if "book_settings_mode" in selector else AccountField(selector, page.checked)
    )
    filled = []
    monkeypatch.setattr(workers, "_fill", lambda _page, selector, value: filled.append((selector, value)))
    social = _SocialLinksHarness()
    schedule = _PublicationScheduleHarness()

    social._fill_social_links(page)
    schedule._fill_publication_schedule(page)

    assert not filled
    assert not page.selected
    assert not page.checked
    assert social.logs and schedule.logs
    assert all(level == "INFO" for level, _ in social.logs)
    assert schedule.logs[0][0] == "WARNING"


def test_partially_locked_schedule_is_not_partially_overwritten(monkeypatch):
    page = _SchedulePage()
    original_locator = page.locator
    page.locator = lambda selector: (
        _EmptyLocator() if "frequency" in selector else original_locator(selector)
    )
    filled = []
    monkeypatch.setattr(workers, "_fill", lambda *args: filled.append(args))

    _PublicationScheduleHarness()._fill_publication_schedule(page)

    assert not filled
    assert not page.selected
    assert not page.checked


@pytest.mark.parametrize("switch_works", [True, False])
def test_schedule_enables_individual_settings_before_filling(monkeypatch, switch_works):
    enabled = set()
    filled = []

    class Switch(_CheckedField):
        def check(self, **kwargs):
            if switch_works:
                enabled.add(self.selector)

        def is_checked(self):
            return self.selector in enabled

    class Page(_SchedulePage):
        def locator(self, selector):
            if "book_settings_mode" in selector:
                return Switch(selector, self.checked)
            assert len(enabled) == 3
            return super().locator(selector)

    def fill(page, selector, value):
        assert len(enabled) == 3
        filled.append((selector, value))

    monkeypatch.setattr(workers, "_fill", fill)
    page = Page()
    harness = _PublicationScheduleHarness()
    harness._fill_publication_schedule(page)

    assert bool(filled) is switch_works
    assert bool(page.checked) is switch_works
    assert harness.logs[-1][0] == ("SUCCESS" if switch_works else "WARNING")


def test_parse_translation_metadata_repairs_unescaped_quotes_in_string():
    raw_response = r"""{
        "english_title": "Beast Taming Immortal Dynasty: I Can Design Evolutionary Forms",
        "translated_title": "\u0411\u0435\u0441\u0441\u043c\u0435\u0440\u0442\u043d\u0430\u044f \u0434\u0438\u043d\u0430\u0441\u0442\u0438\u044f \u0437\u0432\u0435\u0440\u0435\u0439",
        "translated_description": "\u0413\u0435\u0440\u043e\u0439 \u043f\u043e\u043b\u0443\u0447\u0430\u0435\u0442 \u043d\u0430\u0432\u044b\u043a "Evolution Design" \u0438 \u043c\u0435\u043d\u044f\u0435\u0442 \u0441\u0443\u0434\u044c\u0431\u0443."
    }"""

    prepared = parse_translation_metadata(raw_response)

    assert prepared.english_title == "Beast Taming Immortal Dynasty: I Can Design Evolutionary Forms"
    assert prepared.translated_title == "\u0411\u0435\u0441\u0441\u043c\u0435\u0440\u0442\u043d\u0430\u044f \u0434\u0438\u043d\u0430\u0441\u0442\u0438\u044f \u0437\u0432\u0435\u0440\u0435\u0439"
    assert prepared.translated_description == (
        "\u0413\u0435\u0440\u043e\u0439 \u043f\u043e\u043b\u0443\u0447\u0430\u0435\u0442 \u043d\u0430\u0432\u044b\u043a "
        '"Evolution Design" '
        "\u0438 \u043c\u0435\u043d\u044f\u0435\u0442 \u0441\u0443\u0434\u044c\u0431\u0443."
    )


def test_parse_catalog_metadata_returns_only_catalog_fields(monkeypatch):
    allowed_tags = ["sci-fi", "\u0442\u0430\u0439\u043d\u044b", "\u043c\u0438\u0441\u0442\u0438\u043a\u0430"]
    monkeypatch.setattr(workers, "load_rulate_tags", lambda: allowed_tags)
    payload = {
        "genres": [FANTASY.upper(), MYSTIC],
        "tags": ["SCI-FI"],
        "cover_prompt": "A cover. Typography: The text \"\u041d\u0430\u0437\u0432\u0430\u043d\u0438\u0435\" written in gold. --ar 2:3",
        "translated_description": "\u041d\u0435 \u0434\u043e\u043b\u0436\u043d\u043e \u043f\u043e\u043f\u0430\u0441\u0442\u044c \u0432 \u043f\u0435\u0440\u0435\u0432\u043e\u0434",
    }

    prepared = parse_catalog_metadata(json.dumps(payload, ensure_ascii=False))

    assert prepared.translated_description == ""
    assert prepared.genres[:3] == [FANTASY, MYSTIC, ADVENTURE]
    assert prepared.tags == ["sci-fi", "\u0442\u0430\u0439\u043d\u044b", "\u043c\u0438\u0441\u0442\u0438\u043a\u0430"]
    assert prepared.cover_prompt.startswith("A cover.")


def test_normalize_rulate_tags_requires_tags_from_allowed_file(monkeypatch):
    allowed_tags = ["sci-fi", "\u0442\u0430\u0439\u043d\u044b", "\u043c\u0438\u0441\u0442\u0438\u043a\u0430"]
    monkeypatch.setattr(workers, "load_rulate_tags", lambda: allowed_tags)

    tags = normalize_rulate_tags(["SCI-FI", "\u0447\u0443\u0436\u043e\u0439 \u0442\u0435\u0433"])

    assert tags == ["sci-fi", "\u0442\u0430\u0439\u043d\u044b", "\u043c\u0438\u0441\u0442\u0438\u043a\u0430"]


def test_clean_qidian_description_strips_seo_metadata():
    raw_description = (
        "盲候创作的奇幻小说《冒牌领主》，已更新227章，"
        "最新章节：第226章 瑟银要塞陷落。"
        "罗南穿越而来，成了贵族大少的背锅替身。"
        "此刻他正替那位刚凌辱了帝国名将夫人的本尊，被皇帝发配去往南境边陲的途中。"
        "旧神、尸鬼、灵能、义体，蒸汽与火枪...这是一个超凡世界。"
        "罗南从冒牌领主开始，一点点开拓荒地，发掘遗迹，航海探索。"
        "直到有一天，他登…本书的主要角色有罗南"
    )

    cleaned = _clean_qidian_description(raw_description, title="冒牌领主", author="盲候")

    assert cleaned.startswith("罗南穿越而来")
    assert "最新章节" not in cleaned
    assert "本书的主要角色" not in cleaned


def test_extract_qidian_description_from_body_removes_trailing_book_tag():
    body_text = (
        "作品简介\n\n"
        "罗南穿越而来，成了贵族大少的背锅替身。\n"
        "此刻他正替那位刚凌辱了帝国名将夫人的本尊，被皇帝发配去往南境边陲的途中。\n"
        "旧神、尸鬼、灵能、义体，蒸汽与火枪...\n"
        "这是一个超凡世界。\n"
        "罗南从冒牌领主开始，一点点开拓荒地，发掘遗迹，航海探索。\n"
        "直到有一天，他登通天塔而上。\n"
        "那些隐藏黑雾中的旧日主宰，尽皆匍匐，颤栗低语：“天灾之王”。\n"
        "我叫罗南，我即天灾。\n"
        "PS.《灾变卡皇》《机械炼金术士》相近题材，书荒可以看看两本300W+万定老书。\n\n"
        "龙\n\n"
        "月票\n推荐票"
    )

    description = _extract_qidian_description_from_body(body_text)

    assert "登通天塔而上" in description
    assert "龙" not in description.splitlines()[-1]
    assert "月票" not in description


def test_extract_qidian_description_accepts_alternate_headers_and_rank_stop():
    body_text = (
        "内容简介\n\n"
        "在日常之下，在理性尽头，在你所熟悉的世界之外——是你从未想象过的风景。\n"
        "当于生第一次打开那扇门的时候，他所熟悉的世界便轰然倒塌。\n\n"
        "男生月票榜No.10\n\n"
        "月票\n推荐票"
    )

    description = _extract_qidian_description_from_body(body_text)

    assert description == (
        "在日常之下，在理性尽头，在你所熟悉的世界之外——是你从未想象过的风景。\n"
        "当于生第一次打开那扇门的时候，他所熟悉的世界便轰然倒塌。"
    )


def test_select_qidian_description_prefers_full_body_over_truncated_meta():
    payload = {
        "body_text": (
            "作品简介\n\n"
            "罗南穿越而来，成了贵族大少的背锅替身。\n"
            "直到有一天，他登通天塔而上。\n"
            "我叫罗南，我即天灾。\n\n"
            "月票"
        ),
        "description": (
            "盲候创作的奇幻小说《冒牌领主》，已更新227章，"
            "最新章节：第226章 瑟银要塞陷落。罗南穿越而来，直到有一天，他登…"
        ),
        "meta_description": (
            "盲候创作的奇幻小说《冒牌领主》，已更新227章，"
            "最新章节：第226章 瑟银要塞陷落。罗南穿越而来，直到有一天，他登…"
        ),
    }

    description = _select_qidian_description(payload, title="冒牌领主", author="盲候")

    assert description == "罗南穿越而来，成了贵族大少的背锅替身。\n直到有一天，他登通天塔而上。\n我叫罗南，我即天灾。"


def test_select_qidian_description_uses_clean_partial_when_only_truncated_exists():
    payload = {
        "body_text": "",
        "description": "",
        "meta_description": (
            "盲候创作的奇幻小说《冒牌领主》，已更新227章，"
            "最新章节：第226章 瑟银要塞陷落。罗南穿越而来，直到有一天，他登…"
        ),
    }

    description = _select_qidian_description(payload, title="冒牌领主", author="盲候")

    assert description == "罗南穿越而来，直到有一天，他登…"


def test_build_ai_prompt_contains_only_translation_fields():
    metadata = QidianBookMetadata(
        source_url="https://www.qidian.com/book/1041604040/",
        title_original="\u5f02\u5ea6\u65c5\u793e",
        author_name="\u8fdc\u77b3",
        description="\u63cf\u8ff0",
    )

    prompt = build_ai_prompt(metadata, "Otherworldly Inn")

    assert "\u5f02\u5ea6\u65c5\u793e" in prompt
    assert "\u8fdc\u77b3" in prompt
    assert "Otherworldly Inn" in prompt
    assert "\u043d\u0435 \u0432\u0441\u0442\u0430\u0432\u043b\u044f\u0439 \u043d\u0430\u0437\u0432\u0430\u043d\u0438\u0435" in prompt
    assert "translated_description" in prompt
    assert "cover_prompt" not in prompt
    assert "\u0422\u0435\u043a\u0441\u0442 \u043f\u0435\u0440\u0432\u044b\u0445 \u0433\u043b\u0430\u0432" not in prompt


def test_build_catalog_prompt_contains_cover_context_and_no_translation_fields():
    metadata = QidianBookMetadata(
        source_url="https://www.qidian.com/book/1041604040/",
        title_original="\u5f02\u5ea6\u65c5\u793e",
        author_name="\u8fdc\u77b3",
        description="\u63cf\u8ff0",
    )
    prepared = PreparedRulateMetadata(
        english_title="Otherworldly Inn",
        translated_title="\u0418\u043d\u043e\u043c\u0438\u0440\u043d\u0430\u044f \u0433\u043e\u0441\u0442\u0438\u043d\u0438\u0446\u0430",
        translated_description="\u0420\u0443\u0441\u0441\u043a\u043e\u0435 \u043e\u043f\u0438\u0441\u0430\u043d\u0438\u0435.",
    )

    prompt = build_catalog_prompt(
        metadata,
        prepared,
        "\u7b2c1\u7ae0 \u96e8\n\u5947\u602a\u7684\u65c5\u793e\u5728\u96e8\u4e2d\u51fa\u73b0\u3002",
    )

    assert "cover_prompt" in prompt
    assert "genres" in prompt
    assert "от 3 до 7 жанров" in prompt
    assert "tags" in prompt
    assert "до 15-ти существующих тегов Rulate" in prompt
    assert "translated_description:" not in prompt
    assert "\u0422\u0435\u043a\u0441\u0442 \u043f\u0435\u0440\u0432\u044b\u0445 \u0433\u043b\u0430\u0432" in prompt
    assert "\u5947\u602a\u7684\u65c5\u793e\u5728\u96e8\u4e2d\u51fa\u73b0" in prompt
    assert 'The text "\u0418\u043d\u043e\u043c\u0438\u0440\u043d\u0430\u044f \u0433\u043e\u0441\u0442\u0438\u043d\u0438\u0446\u0430"' in prompt


def test_build_ai_prompt_does_not_include_hardcoded_tag_examples():
    metadata = QidianBookMetadata(
        source_url="https://www.qidian.com/book/1041604040/",
        title_original="\u5f02\u5ea6\u65c5\u793e",
        author_name="\u8fdc\u77b3",
        description="\u63cf\u8ff0",
    )

    prompt = build_ai_prompt(metadata, "Otherworldly Inn")

    assert "sci-fi, \u043c\u0438\u0441\u0442\u0438\u043a\u0430" not in prompt
    assert "\u043f\u0443\u0442\u0435\u0448\u0435\u0441\u0442\u0432\u0438\u0435 \u043c\u0435\u0436\u0434\u0443 \u043c\u0438\u0440\u0430\u043c\u0438" not in prompt
    assert "\u0441\u043e\u0432\u0440\u0435\u043c\u0435\u043d\u043d\u044b\u0439 \u043c\u0438\u0440, \u043a\u0438\u0442\u0430\u0439" not in prompt


def test_clean_qidian_chapter_text_removes_comment_counters():
    raw_text = "Первый абзац\n806\n\n\u3000\u3000Второй абзац\n109\n本章完"

    assert _clean_qidian_chapter_text(raw_text) == "Первый абзац\nВторой абзац"


def test_clean_ciweimao_chapter_text_removes_repeated_watermark_and_counters():
    raw_text = "第一段。3IANIx\n13\n第二段。3IANIx\n3\n第三段。3IANIx\n本章完"

    assert _clean_ciweimao_chapter_text(raw_text) == "第一段。\n第二段。\n第三段。"


def test_qidian_chapter_link_script_supports_chinese_chapter_numbers():
    assert "chineseNumber" in _QIDIAN_CHAPTER_LINKS_SCRIPT
    assert "[0-9零〇一二两三四五六七八九十百千万]+" in _QIDIAN_CHAPTER_LINKS_SCRIPT
    assert r"^第\s*\d+\s*章" not in _QIDIAN_CHAPTER_LINKS_SCRIPT


def test_qidian_chapter_link_script_supports_zero_padded_numeric_prefixes():
    assert "numericPrefix" in _QIDIAN_CHAPTER_LINKS_SCRIPT
    assert r"(\d{1,9})" in _QIDIAN_CHAPTER_LINKS_SCRIPT


def test_clean_fanqie_chapter_text_drops_obfuscated_private_use_text():
    obfuscated = "婚礼参。" * 4

    assert workers._clean_fanqie_chapter_text(obfuscated) == ""
    assert workers._clean_fanqie_chapter_text("<p>正常第一段。</p><p>正常第二段。</p>") == "正常第一段。\n正常第二段。"


def test_read_tomato_chapters_from_folder_prefers_resume_journal(tmp_path):
    folder = tmp_path / "7229603492648717324"
    folder.mkdir()
    records = [
        {"id": "1001", "title": "第一章", "content": "<p>第一段。</p><p>第二段。</p>"},
        {"id": "1002", "title": "第二章", "content": "婚礼参。" * 4},
        {"id": "1003", "title": "第三章", "content": "<p>第三段。</p>"},
    ]
    (folder / "downloaded_chapters.jsonl").write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records),
        encoding="utf-8",
    )
    (folder / "status.json").write_text(
        json.dumps(
            {
                "downloaded": {
                    "1004": ["第四章", "<p>第四段。</p>"],
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    text = _read_tomato_chapters_from_folder(folder, limit=3)

    assert "第一章" in text
    assert "第一段。\n第二段。" in text
    assert "第二章" not in text
    assert "第三章" in text
    assert "第四章" in text


def test_fanqie_scripts_use_initial_state_and_reader_links():
    assert "__INITIAL_STATE__" in _FANQIE_EXTRACT_SCRIPT
    assert "chapterListWithVolume" in _FANQIE_CHAPTER_LINKS_SCRIPT
    assert "/reader/" in _FANQIE_CHAPTER_LINKS_SCRIPT
    assert "reader.chapterData" in _FANQIE_CHAPTER_TEXT_SCRIPT


def test_ciweimao_scripts_use_public_book_metadata_and_chapter_links():
    assert "og:novel:book_name" in _CIWEIMAO_EXTRACT_SCRIPT
    assert ".book-intro-cnt" in _CIWEIMAO_EXTRACT_SCRIPT
    assert "data-original" in _CIWEIMAO_EXTRACT_SCRIPT
    assert "#J_book_chapter_list" in _CIWEIMAO_CHAPTER_LINKS_SCRIPT
    assert "/chapter/" in _CIWEIMAO_CHAPTER_LINKS_SCRIPT
    assert "验证码" in _CIWEIMAO_CHAPTER_TEXT_SCRIPT


def test_qimao_scripts_use_book_metadata_and_public_chapter_dom():
    assert ".book-information .title .txt" in _QIMAO_EXTRACT_SCRIPT
    assert ".book-introduction-item .intro" in _QIMAO_EXTRACT_SCRIPT
    assert ".book-information .wrap-pic img" in _QIMAO_EXTRACT_SCRIPT
    assert ".chapter-detail-article .article" in _QIMAO_CHAPTER_TEXT_SCRIPT
    assert ".book-introduction-item .article" in _QIMAO_CHAPTER_TEXT_SCRIPT


def test_source_cover_context_dispatches_qimao(monkeypatch):
    calls = []

    def fake_fetch(source_url, **kwargs):
        calls.append((source_url, kwargs))
        return "chapters", "description"

    monkeypatch.setattr(workers, "_fetch_qimao_cover_context", fake_fetch)

    result = workers._fetch_source_cover_context(
        "https://www.qimao.com/shuku/195958/",
        visible_browser=True,
        original_description="original",
        log_callback="logger",
    )

    assert result == ("chapters", "description")
    assert calls == [
        (
            "https://www.qimao.com/shuku/195958/",
            {
                "visible_browser": True,
                "original_description": "original",
                "log_callback": "logger",
            },
        )
    ]


def test_ciweimao_human_verification_waits_five_minutes_and_rechecks_page():
    class Page:
        def __init__(self):
            self.waited = []
            self.evaluated = []

        def wait_for_function(self, script, timeout):
            self.waited.append((script, timeout))

        def evaluate(self, script):
            self.evaluated.append(script)
            return {"blocked": False, "text": "正文"}

    page = Page()

    payload = _wait_for_ciweimao_human_verification(page)

    assert payload == {"blocked": False, "text": "正文"}
    assert page.waited[0][1] == 300_000
    assert "验证码" in page.waited[0][0]
    assert page.evaluated == [_CIWEIMAO_CHAPTER_TEXT_SCRIPT]


def test_build_cover_prompt_request_includes_ru_title_and_chapters():
    prompt = build_cover_prompt_request(
        "Иномирная гостиница",
        "第1章 雨\nГерой видит странную тень под фонарем.",
        original_description="Оригинальное описание про странный отель между мирами.",
    )

    assert "Название (RU): Иномирная гостиница" in prompt
    assert "Оригинальное описание источника:" in prompt
    assert "Оригинальное описание про странный отель между мирами." in prompt
    assert 'The text "Иномирная гостиница"' in prompt
    assert "Герой видит странную тень под фонарем." in prompt
    assert "--ar 2:3" in prompt


def test_clean_cover_prompt_response_strips_markdown_fence():
    response = "```text\nA hero in rain. Typography: The text \"Название\" written in neon font. --ar 2:3\n```"

    assert clean_cover_prompt_response(response) == (
        'A hero in rain. Typography: The text "Название" written in neon font. --ar 2:3'
    )


def test_upload_generated_cover_sets_rulate_file_input_and_confirms_cropper(tmp_path):
    cover_path = tmp_path / "translated.png"
    cover_path.write_bytes(b"image")
    harness = _UploadCoverHarness(cover_path)
    page = _UploadCoverPage()

    harness._upload_generated_cover(page)

    assert page.file_input.files == [str(cover_path)]
    assert page.ok_button.clicks
    assert page.evaluated[-1][1] == "general"
    assert any(level == "SUCCESS" and str(cover_path) in message for level, message in harness.logs)


def test_upload_generated_cover_skips_webp_because_rulate_input_does_not_accept_it(tmp_path):
    cover_path = tmp_path / "source.webp"
    cover_path.write_bytes(b"webp")
    harness = _UploadCoverHarness(cover_path)
    page = _UploadCoverPage()

    harness._upload_generated_cover(page)

    assert page.file_input.files == []
    assert page.ok_button.clicks == []


def test_codex_cover_translation_prompt_preserves_edit_requirements(tmp_path):
    target_path = tmp_path / "translated.png"
    prompt = _build_codex_cover_translation_prompt("Небесная башня", target_path)

    assert "Remove every existing visible text element" in prompt
    assert "ad banners" in prompt
    assert '"Небесная башня"' in prompt
    assert "2:3 aspect ratio" in prompt
    assert "4K-quality" in prompt
    assert str(target_path) in prompt


def test_load_cover_image_from_file_reads_local_source_cover(tmp_path):
    image_path = tmp_path / "source.png"
    image = QImage(2, 3, QImage.Format.Format_RGB32)
    image.fill(0xFF336699)
    assert image.save(str(image_path), "PNG")

    cover_image = _load_cover_image_from_file(image_path)

    assert cover_image is not None
    assert cover_image.width == 2
    assert cover_image.height == 3
    assert cover_image.content
    assert cover_image.url == str(image_path.resolve())


def test_load_cover_image_from_data_reuses_preview_bytes(tmp_path):
    image = QImage(2, 3, QImage.Format.Format_RGB32)
    image.fill(0xFF336699)
    image_path = tmp_path / "preview.png"
    assert image.save(str(image_path), "PNG")
    image_data = image_path.read_bytes()

    cover_image = _load_cover_image_from_data(image_data, source="https://example.com/cover.png")

    assert cover_image is not None
    assert cover_image.width == 2
    assert cover_image.height == 3
    assert cover_image.content == image_data
    assert cover_image.url == "https://example.com/cover.png"


def test_codex_cover_translation_uses_preview_bytes_without_redownloading(monkeypatch, tmp_path):
    image = QImage(2, 3, QImage.Format.Format_RGB32)
    image.fill(0xFF336699)
    image_path = tmp_path / "preview.png"
    assert image.save(str(image_path), "PNG")

    def fail_download(*_args, **_kwargs):
        raise AssertionError("The cover URL must not be downloaded when preview bytes are available")

    monkeypatch.setattr(workers, "_download_best_cover_image", fail_download)
    monkeypatch.setattr(workers, "_build_codex_cover_exec_command", lambda *_args, **_kwargs: ["codex"])
    monkeypatch.setattr(workers, "_append_codex_prompt", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        workers.subprocess,
        "run",
        lambda *_args, **_kwargs: workers.subprocess.CompletedProcess(["codex"], 0, "", ""),
    )
    monkeypatch.setattr(
        workers,
        "_find_generated_cover",
        lambda *_args, **_kwargs: tmp_path / "translated.png",
    )
    worker = CodexCoverTranslateWorker(
        "https://example.com/expired-cover.png",
        "Небесная башня",
        source_image_data=image_path.read_bytes(),
        output_dir=tmp_path,
    )
    logs = []
    worker.log = lambda level, message: logs.append((level, message))

    worker.run()

    assert list(tmp_path.glob("*_source_*.png"))
    assert any("из превью" in message for _level, message in logs)
    assert not any(level == "ERROR" for level, _message in logs)


def test_qidian_cover_url_candidates_try_larger_yuewen_sizes_first():
    candidates = _cover_url_candidates("https://bookcover.yuewen.com/qdbimg/349573/1041604040/180")

    assert candidates[:4] == [
        "https://bookcover.yuewen.com/qdbimg/349573/1041604040/600",
        "https://bookcover.yuewen.com/qdbimg/349573/1041604040/480",
        "https://bookcover.yuewen.com/qdbimg/349573/1041604040/300",
        "https://bookcover.yuewen.com/qdbimg/349573/1041604040/180",
    ]


def test_codex_cover_exec_command_ignores_user_config(tmp_path):
    target_path = tmp_path / "cover.png"
    command = _build_codex_cover_exec_command(target_path, tmp_path)

    assert "--ignore-user-config" in command
    assert command.index("--ignore-user-config") > command.index("exec")


def test_codex_cover_exec_command_uses_latest_recommended_model(tmp_path):
    target_path = tmp_path / "cover.png"
    command = _build_codex_cover_exec_command(target_path, tmp_path)
    model_index = command.index("--model")

    assert command[model_index + 1] == CODEX_COVER_MODEL == "gpt-5.5"
    assert model_index > command.index("exec")


def test_append_codex_prompt_separates_prompt_from_image_args(tmp_path):
    image_path = tmp_path / "source.png"
    target_path = tmp_path / "cover.png"
    command = _build_codex_cover_exec_command(target_path, tmp_path, extra_args=["--image", str(image_path)])

    _append_codex_prompt(command, "edit this cover")

    assert command[-2:] == ["--", "edit this cover"]
    assert command.index("--") > command.index(str(image_path))


def test_find_generated_cover_copies_codex_generated_image_from_stdout(tmp_path):
    codex_image_dir = tmp_path / ".codex" / "generated_images" / "abc"
    codex_image_dir.mkdir(parents=True)
    codex_image = codex_image_dir / "_image_id_.png"
    codex_image.write_bytes(b"image")
    target_path = tmp_path / "output" / "cover.png"
    stdout = f"saved under:\n`{codex_image}`"

    result = _find_generated_cover(tmp_path / "output", target_path, 0, codex_output=stdout)

    assert result == target_path.resolve()
    assert target_path.read_bytes() == b"image"


def test_find_generated_cover_copies_sibling_when_stdout_has_placeholder(tmp_path):
    codex_image_dir = tmp_path / ".codex" / "generated_images" / "abc"
    codex_image_dir.mkdir(parents=True)
    placeholder_path = codex_image_dir / "_image_id_.png"
    real_image = codex_image_dir / "ig_real.png"
    real_image.write_bytes(b"real-image")
    target_path = tmp_path / "output" / "cover.png"
    stdout = f"saved under:\n`{placeholder_path}`"

    result = _find_generated_cover(tmp_path / "output", target_path, 0, codex_output=stdout)

    assert result == target_path.resolve()
    assert target_path.read_bytes() == b"real-image"


def test_find_generated_cover_copies_from_codex_generated_image_directory(tmp_path):
    codex_image_dir = tmp_path / ".codex" / "generated_images" / "019f3783-2bb2-7571-8a42-022f6f4f97c3"
    codex_image_dir.mkdir(parents=True)
    real_image = codex_image_dir / "ig_real.png"
    real_image.write_bytes(b"real-image")
    target_path = tmp_path / "output" / "cover.png"
    stdout = f"generated under:\n`{codex_image_dir}`"

    result = _find_generated_cover(tmp_path / "output", target_path, 0, codex_output=stdout)

    assert result == target_path.resolve()
    assert target_path.read_bytes() == b"real-image"


def test_find_generated_cover_copies_from_codex_generated_image_wildcard(tmp_path):
    codex_image_dir = tmp_path / ".codex" / "generated_images" / "019f3783-2bb2-7571-8a42-022f6f4f97c3"
    codex_image_dir.mkdir(parents=True)
    real_image = codex_image_dir / "ig_real.png"
    real_image.write_bytes(b"real-image")
    target_path = tmp_path / "output" / "cover.png"
    stdout = f"Resolve-Path '{codex_image_dir}\\*.png'"

    result = _find_generated_cover(tmp_path / "output", target_path, 0, codex_output=stdout)

    assert result == target_path.resolve()
    assert target_path.read_bytes() == b"real-image"


def test_find_generated_cover_ignores_source_images(tmp_path):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    source_path = output_dir / "book_source_20260706.webp"
    source_path.write_bytes(b"source")

    result = _find_generated_cover(output_dir, output_dir / "missing.png", 0)

    assert result is None


def test_browser_missing_error_is_detected_for_playwright_install_message():
    error = RuntimeError(
        "BrowserType.launch: Executable doesn't exist at "
        "C:\\Users\\test\\AppData\\Local\\ms-playwright\\chromium_headless_shell-1223\\chrome.exe\n"
        "Looks like Playwright was just installed or updated. Please run: playwright install"
    )

    assert _is_browser_missing_error(error)
