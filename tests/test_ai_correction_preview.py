import os
import importlib
import sys
import types
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from gemini_translator.utils.glossary_review import (
    classify_translation_review_change,
    normalize_translation_review_key,
)


def _import_correction_preview_dialog():
    """Import ai_correction without tripping the widgets/glossary circular import."""
    module_name = "gemini_translator.ui.widgets.glossary_widget"
    previous_module = sys.modules.get(module_name)
    fake_module = types.ModuleType(module_name)
    fake_glossary_widget = type("_FakeGlossaryWidget", (), {})
    fake_module.GlossaryWidget = fake_glossary_widget
    sys.modules[module_name] = fake_module
    try:
        module = importlib.import_module(
            "gemini_translator.ui.dialogs.glossary_dialogs.ai_correction"
        )
    finally:
        if previous_module is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous_module

        widgets_package = sys.modules.get("gemini_translator.ui.widgets")
        if widgets_package and getattr(widgets_package, "GlossaryWidget", None) is fake_glossary_widget:
            if previous_module is not None:
                widgets_package.GlossaryWidget = previous_module.GlossaryWidget
            else:
                try:
                    real_module = importlib.import_module(module_name)
                    widgets_package.GlossaryWidget = real_module.GlossaryWidget
                except Exception:
                    delattr(widgets_package, "GlossaryWidget")

    return module.CorrectionPreviewDialog


CorrectionPreviewDialog = _import_correction_preview_dialog()


class CorrectionPreviewDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def test_normalized_equivalent_translation_key_ignores_wrappers_case_and_yo(self):
        normalized = normalize_translation_review_key(
            ' [\u00ab\u041f\u043e\u043b\u0443\u0434\u0401\u043c\u043e\u043d\u00bb] '
        )
        self.assertEqual(normalized, "\u043f\u043e\u043b\u0443\u0434\u0435\u043c\u043e\u043d")

    def test_classify_translation_change_treats_case_only_change_as_cosmetic(self):
        meaningful, cosmetic = classify_translation_review_change(
            ["\u041f\u043e\u043b\u0443\u0434\u0435\u043c\u043e\u043d"],
            "\u043f\u043e\u043b\u0443\u0434\u0435\u043c\u043e\u043d",
        )
        self.assertFalse(meaningful)
        self.assertTrue(cosmetic)

    def test_classify_translation_change_treats_wrappers_as_same_translation(self):
        meaningful, cosmetic = classify_translation_review_change(
            ["\u041f\u043e\u043b\u0443\u0434\u0435\u043c\u043e\u043d"],
            '["\u041f\u043e\u043b\u0443\u0434\u0435\u043c\u043e\u043d"]',
        )
        self.assertFalse(meaningful)
        self.assertTrue(cosmetic)

    def test_classify_translation_change_keeps_real_translation_change_visible(self):
        meaningful, cosmetic = classify_translation_review_change(
            ["\u041f\u043e\u043b\u0443\u0434\u0435\u043c\u043e\u043d"],
            "\u0412\u044b\u0441\u0448\u0438\u0439 \u0434\u0435\u043c\u043e\u043d",
        )
        self.assertTrue(meaningful)
        self.assertFalse(cosmetic)

    def test_manual_save_does_not_prompt_during_table_rebuild(self):
        dialog = CorrectionPreviewDialog(
            original_glossary_list=[
                {"original": "Wei", "rus": "Vey", "note": "Character"}
            ],
            patch_dict={
                "Wei": {"rus": "Senior Vey", "note": "Character"}
            },
            direct_conflicts={},
        )
        self.addCleanup(dialog.close)

        pending_prompts = []
        dialog._resolve_pending_translation_edit = (
            lambda *args, **kwargs: pending_prompts.append((args, kwargs)) or True
        )

        dialog._select_row(0)
        dialog.translation_editor.setPlainText("Vey the Elder")
        self.assertTrue(dialog._translation_edit_dirty)

        self.assertTrue(dialog._save_current_translation_edit())
        self.assertEqual(pending_prompts, [])


if __name__ == "__main__":
    unittest.main()
