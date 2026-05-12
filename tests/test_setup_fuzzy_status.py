import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from gemini_translator.ui.dialogs.setup import InitialSetupDialog


class _LabelStub:
    def __init__(self):
        self.text = ""
        self.tooltip = ""
        self.style = ""

    def setText(self, text):
        self.text = text

    def setToolTip(self, tooltip):
        self.tooltip = tooltip

    def setStyleSheet(self, style):
        self.style = style


class _ValueStub:
    def __init__(self, value):
        self._value = value

    def value(self):
        return self._value


class _CheckStub:
    def __init__(self, checked):
        self._checked = checked

    def isChecked(self):
        return self._checked


class _GlossaryStub:
    def __init__(self, size):
        self._items = [object()] * size

    def get_glossary(self):
        return list(self._items)


class _ModelSettingsStub:
    def __init__(self, *, dynamic_glossary=True, fuzzy_threshold=100, rpm=5):
        self.fuzzy_status_label = _LabelStub()
        self.dynamic_glossary_checkbox = _CheckStub(dynamic_glossary)
        self.fuzzy_threshold_spin = _ValueStub(fuzzy_threshold)
        self.rpm_spin = _ValueStub(rpm)


class _TranslationOptionsStub:
    def __init__(self, *, task_size=30000, batching=True, chunking=False):
        self.batch_checkbox = _CheckStub(batching)
        self.chunking_checkbox = _CheckStub(chunking)
        self.task_size_spin = _ValueStub(task_size)
        self.chapter_compositions = {}


class _FuzzyStatusHarness:
    _update_fuzzy_status_display = InitialSetupDialog._update_fuzzy_status_display

    def __init__(self):
        self.cpu_performance_index = 1000
        self.model_settings_widget = _ModelSettingsStub(
            dynamic_glossary=True,
            fuzzy_threshold=100,
            rpm=5,
        )
        self.glossary_widget = _GlossaryStub(2300)
        self.instances_spin = _ValueStub(1)
        self.translation_options_widget = _TranslationOptionsStub(task_size=30000)
        self.html_files = []


class FuzzyStatusTests(unittest.TestCase):
    def test_fuzzy_status_does_not_show_red_estimate_when_threshold_is_100(self):
        harness = _FuzzyStatusHarness()

        harness._update_fuzzy_status_display()

        label = harness.model_settings_widget.fuzzy_status_label
        self.assertIn("выключен", label.text.lower())
        self.assertNotIn("Дольше", label.text)
        self.assertNotIn("red", label.style.lower())


if __name__ == "__main__":
    unittest.main()
