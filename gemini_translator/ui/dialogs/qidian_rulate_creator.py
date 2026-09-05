# -*- coding: utf-8 -*-

from __future__ import annotations

from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QMainWindow, QToolBar

from .menu_utils import return_to_main_menu


class QidianRulateCreatorWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Qidian/Fanqie/Ciweimao/Qimao -> Rulate")
        self.resize(1180, 920)
        self._return_to_menu_handler = None

        from gemini_translator.ui.pages.qidian_creator_page import QidianCreatorPage

        self.page = QidianCreatorPage(self)
        self.setCentralWidget(self.page)

        toolbar = QToolBar()
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        act_return = QAction("Вернуться в меню", self)
        act_return.triggered.connect(self._return_to_menu)
        toolbar.addAction(act_return)

    def set_return_to_menu_handler(self, handler):
        self._return_to_menu_handler = handler

    def _return_to_menu(self) -> None:
        if callable(self._return_to_menu_handler):
            self.hide()
            self.close()
            self._return_to_menu_handler()
            return
        self.close()
        return_to_main_menu()


def _split_csv(text: str) -> list[str]:
    result = []
    for part in (text or "").replace("\n", ",").split(","):
        item = part.strip()
        if item and item not in result:
            result.append(item)
    return result
