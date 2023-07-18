from remedy import *

from PyQt5.QtGui import QColor, QPainter
from PyQt5.QtWidgets import QWidget, QColorDialog, QHBoxLayout, QApplication, QComboBox
from PyQt5.QtCore import pyqtSignal, pyqtSlot, Qt, QSize, QRect, QPoint

from remedy.remarkable.constants import COLORS
from remedy.remarkable.palette import Palette

from remedy.utils import log

PALETTE_ORDER = [
    ["black", "gray", "white"],
    ["red", "blue"],
    ["highlight", "yellow", "green", "pink", "blend_gray"],
]

COLOR_TITLES = {
    "highlight": "Old-style Highlighter",
    "yellow": "Highlighter Yellow",
    "green": "Highlighter Green",
    "pink": "Highlighter Pink",
    "blend_gray": "Highlighter Gray",
}


class ColorButton(QWidget):
    changed = pyqtSignal(str, QColor)

    def __init__(
        self,
        name,
        *args,
        color=None,
        editable=True,
        options=QColorDialog.ShowAlphaChannel,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.options = options
        self.name = name
        self.setColor(color)
        self.setToolTip(COLOR_TITLES.get(name, name.capitalize()))
        # self.setContentsMargins(0,3,0,3)
        self.setEditable(editable)
        # self.setSizePolicy(QSizePolicy.Fixed,QSizePolicy.Fixed)

    @pyqtSlot(bool)
    def selectColor(self, *args):
        color = QColorDialog.getColor(
            self._color or Qt.GlobalColor.black, self.window(), options=self.options
        )
        if color.isValid():
            self.setColor(color)
            self.changed.emit(self.name, color)

    def sizeHint(self):
        return QSize(16, 16).grownBy(self.contentsMargins())

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        p = self.contentsRect()
        painter.setPen(QColor(120, 120, 120))
        painter.setBrush(QColor(self._color))
        # painter.drawRect(QRect(QPoint(p.left(), p.center().y()-8),QSize(16,16)))
        painter.drawChord(
            QRect(QPoint(p.left() + 1, p.center().y() - 7), QSize(14, 14)), 0, 5760
        )
        # painter.drawRoundedRect(QRect(QPoint(p.left()+1, p.center().y()-7),QSize(14,14)),3,3)

    def setEditable(self, editable):
        self._editable = editable
        if editable:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)

    def editable(self):
        return self._editable

    def mouseReleaseEvent(self, event):
        if self._editable:
            self.selectColor()

    def setColor(self, color):
        self._color = color
        self.repaint(self.rect())

    def color(self):
        return self._color


class PaletteBar(QWidget):
    changed = pyqtSignal(str, QColor)

    def __init__(self, palette, *args, editable=True, **kwargs):
        super().__init__(*args, **kwargs)
        colorsel = QHBoxLayout(self)
        self._btn = {}
        colorsel.setSpacing(0)
        colorsel.setContentsMargins(0, 0, 0, 0)
        colorsel.addStrut(18)
        for group in PALETTE_ORDER:
            for col in group:
                self._btn[col] = ColorButton(col, editable=editable)
                self._btn[col].setColor(QColor(palette.get(col)))
                self._btn[col].changed.connect(self.changed)
                colorsel.addWidget(self._btn[col])
            colorsel.addSpacing(3)

    def setPalette(self, palette):
        if palette:
            for col in self._btn:
                self._btn[col].setColor(palette.get(col))

    def setColors(self, palette):
        for col in palette:
            if col in self._btn:
                self._btn[col].setColor(QColor(palette.get(col)))

    def getColors(self):
        return {col: b.color() for col, b in self._btn.items()}


class PaletteSelector(QWidget):
    def __init__(
        self, *args, palettes=None, palette="default", editable=True, **kwargs
    ):
        super().__init__(*args, **kwargs)
        if palettes is None:
            palettes = QApplication.instance().config.palettes
        if not isinstance(palette, Palette):
            palette = palettes.get(palette)

        self.selector = QComboBox()
        for i, (name, pal) in enumerate(palettes.items()):
            self.selector.addItem(pal.title(), pal)
            if name == palette.name():
                self.selector.setCurrentIndex(i)

        self.bar = PaletteBar(palette, editable=editable)

        if editable:
            self.custom = Palette()
            self.selector.addItem("Custom...", self.custom)
            if name is None:
                self.selector.setCurrentIndex(self.selector.count() - 1)
            self.selector.activated.connect(self._onSelect)
            self.bar.changed.connect(self._onChange)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.bar)
        layout.addWidget(self.selector)

    def _onSelect(self, i):
        self.bar.setPalette(self.selector.itemData(i))

    def _onChange(self, name, color):
        self.custom.setColors(self.bar.getColors())
        self.selector.setCurrentIndex(self.selector.count() - 1)
        # self.bar.setPalette(self.custom)

    def getPalette(self):
        return Palette(self.bar.getColors())
