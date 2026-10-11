#!/usr/bin/env python3
"""
Daily Checklist — PySide6 desktop app
Time-tracked checklists with unlimited nesting depth: per-item timers with
idle auto-pause, a prominent "now tracking" banner, a daily time goal,
daily/weekday repeats, custom-colored tags (drag chips to tag, quick-tag
menu), multi-select bulk ops, a tags+presets sidebar, time-by-tag stats,
copy-as-markdown, copy-from-previous-day, tray notifications, debounced
autosave, backups and undo/redo.
"""

import copy
import csv
import json
import math
import os
import re
import shutil
import sys
import time
import zlib
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from pathlib import Path
from string import Template
from typing import Optional

from PySide6.QtCore import (Qt, QTimer, QDate, QEvent, Signal, QRect, QRectF,
                            QSettings, QUrl)
from PySide6.QtGui import (QKeySequence, QShortcut, QPainter, QPen, QColor,
                           QBrush, QDrag, QMimeData, QAction, QActionGroup,
                           QIcon, QPixmap, QDesktopServices)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QCheckBox, QFrame, QDateEdit,
    QScrollArea, QFileDialog, QMessageBox, QProgressBar, QInputDialog,
    QDialog, QComboBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QMenu, QListWidget, QListWidgetItem, QTabWidget, QSystemTrayIcon,
    QAbstractSpinBox, QDockWidget, QColorDialog,
)

APP_DIR = Path.home() / ".daily_checklist_timer"
DATA_FILE = APP_DIR / "data.json"
BACKUP_DIR = APP_DIR / "backups"
TEMPLATE_KEY = "__templates"
FILTER_KEY = "__filters"
TAGCOLOR_KEY = "__tagcolors"
MIME_ROW = "application/x-dct-row"
MIME_TAG = "application/x-dct-tag"
MAX_DEPTH = 8
SAVE_DELAY_MS = 1200
MAX_CONTENT_W = 1000          # task list is centered / capped on wide windows


# ---------------------------------------------------------------- helpers
def fmt_hms(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def fmt_dur(seconds: float) -> str:
    m = int(round(seconds / 60))
    if m < 60:
        return f"{m}m"
    h, rem = divmod(m, 60)
    return f"{h}h" + (f"{rem:02d}" if rem else "")


def fmt_axis(seconds: float) -> str:
    if seconds >= 3600:
        return f"{seconds / 3600:g}h"
    return f"{int(seconds // 60)}m"


EST_RE = re.compile(
    r"[~≈]\s*(?:"
    r"(\d{1,3}[.,]?\d?)\s*h(?:rs?|ours?)?(?:\s*(\d{1,2})\s*m(?:in)?)?"
    r"|(\d{1,4})\s*m(?:in)?s?"
    r"|(\d{1,3})"
    r")\s*$", re.IGNORECASE)


def parse_estimate(text: str) -> tuple[str, float]:
    m = EST_RE.search(text)
    if not m:
        return text.strip(), 0.0
    h, h_extra_m, m_only, bare = m.groups()
    if h:
        secs = round(float(h.replace(",", ".")) * 3600) \
               + (int(h_extra_m) if h_extra_m else 0) * 60
    elif m_only:
        secs = int(m_only) * 60
    else:
        secs = int(bare) * 60
    clean = EST_RE.sub("", text).strip(" \t-–—:;,")
    return clean, float(secs)


TAG_TOK_RE = re.compile(r"(?:^|(?<=\s))#([A-Za-z0-9_\-]+)")


def extract_tags(text: str) -> tuple[str, list[str]]:
    tags: list[str] = []

    def repl(m):
        t = m.group(1).lower()
        if t not in tags:
            tags.append(t)
        return " "

    clean = TAG_TOK_RE.sub(repl, text)
    return re.sub(r"\s{2,}", " ", clean).strip(), tags


def shift_month(d: date, n: int) -> date:
    idx = d.month - 1 + n
    return date(d.year + idx // 12, idx % 12 + 1, 1)


def polish(widget: QWidget):
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def fresh_copy(t: "Task") -> "Task":
    c = copy.deepcopy(t)
    for it in c.iter_tree():
        it.done = False
        it.completed_at = None
        it.seconds_spent = 0.0
        it._running_since = None
        it.expanded = False
    c.created = datetime.now().isoformat(timespec="seconds")
    return c


# ---------------------------------------------------------------- theme
THEMES: dict[str, dict[str, str]] = {
    "light": {
        "BG": "#eef1f6", "CARD": "#ffffff", "BORDER": "#e0e4ec",
        "TEXT": "#1f2430", "SUB": "#6b7280", "EST": "#8a93a2",
        "ACCENT": "#2f6fed", "ACCENT_H": "#2a63d4",
        "GREEN": "#34a853", "GREEN_BG": "#e7f4ec", "GREEN_TXT": "#0e8a4c",
        "RED": "#e5484d", "RED_BG": "#fdecec",
        "AMBER": "#f0c27a", "AMBER_BG": "#fff4e0", "AMBER_TXT": "#d97706",
        "HOVER": "#f2f4f8", "PRESSED": "#e8ebf1", "INPUT": "#ffffff",
        "GRID": "#e6e9f0", "ALT": "#fafbfd", "ROWDONE": "#f6f7f9",
        "MUTED": "#b3b9c4",
    },
    "dark": {
        "BG": "#1b1e27", "CARD": "#242836", "BORDER": "#333949",
        "TEXT": "#e8eaf0", "SUB": "#9aa3b5", "EST": "#7c8698",
        "ACCENT": "#5b8def", "ACCENT_H": "#6e9bf5",
        "GREEN": "#4cc38a", "GREEN_BG": "#1e2f28", "GREEN_TXT": "#4cc38a",
        "RED": "#f16a6f", "RED_BG": "#38262b",
        "AMBER": "#c99a3f", "AMBER_BG": "#33291a", "AMBER_TXT": "#e2b341",
        "HOVER": "#2b3040", "PRESSED": "#323848", "INPUT": "#1f2330",
        "GRID": "#2e3444", "ALT": "#272c3a", "ROWDONE": "#20242f",
        "MUTED": "#5d6577",
    },
}

QSS_TEMPLATE = Template("""
* { font-family: 'Segoe UI', 'Helvetica Neue', Arial, sans-serif;
    font-size: 13px; color: $TEXT; }
QWidget#central, QDialog { background: $BG; }
QFrame#header { background: $CARD; border: 1px solid $BORDER; border-radius: 10px; }
QFrame#bulkBar { background: $CARD; border: 1px solid $ACCENT; border-radius: 8px; }
QFrame#nowBar { background: $GREEN_BG; border: 1px solid $GREEN; border-radius: 10px; }
QFrame#vsep { background: $BORDER; max-width: 1px; border: none; }
QLabel#dayTitle { font-size: 17px; font-weight: 600; color: $TEXT; }
QLabel#summaryLbl { color: $SUB; }
QLabel#bulkLbl { color: $ACCENT; font-weight: 600; }
QLabel#nowPath { color: $SUB; }
QLabel#nowTime { font-size: 21px; font-weight: 700; color: $GREEN_TXT; }
QLabel#periodLbl { font-size: 15px; font-weight: 600; color: $TEXT; }
QLabel#statsSummary { color: $SUB; font-size: 14px; }
QLabel#sideTitle { font-weight: 600; color: $TEXT; }
QPushButton { background: $CARD; border: 1px solid $BORDER; border-radius: 6px;
              padding: 5px 12px; color: $TEXT; }
QPushButton:hover { background: $HOVER; border-color: $MUTED; }
QPushButton:pressed { background: $PRESSED; }
QPushButton:disabled { color: $MUTED; border-color: $BORDER; }
QPushButton#addBtn { background: $ACCENT; color: #ffffff; border: none;
                     font-weight: 600; padding: 6px 16px; }
QPushButton#addBtn:hover { background: $ACCENT_H; }
QPushButton#dangerBtn { background: $RED; color: #ffffff; border: none;
                        font-weight: 600; }
QPushButton#dangerBtn:hover { background: $RED; border-color: $RED; }
QPushButton#nowPause { font-weight: 700; padding: 5px 14px; }
QLineEdit { background: $INPUT; border: 1px solid $BORDER; border-radius: 6px;
            padding: 7px 10px; selection-background-color: $ACCENT; }
QLineEdit:focus { border-color: $ACCENT; }
QDateEdit, QComboBox { background: $INPUT; border: 1px solid $BORDER;
                       border-radius: 6px; padding: 5px 8px; }
QScrollArea { background: transparent; border: none; }
QDockWidget { color: $TEXT; }
QDockWidget::title { background: $CARD; border: 1px solid $BORDER;
                     border-radius: 6px; padding: 5px 8px; margin: 4px; }
QFrame#taskRow { background: $CARD; border: 1px solid $BORDER; border-radius: 8px; }
QFrame#taskRow[nested="true"] { background: $BG; }
QFrame#taskRow[running="true"] { border: 1px solid $GREEN; background: $GREEN_BG; }
QFrame#taskRow[done="true"] { background: $ROWDONE; }
QFrame#taskRow[selected="true"] { border: 1px solid $ACCENT; }
QFrame#taskRow[dropEdge="top"] { border-top: 3px solid $ACCENT; }
QFrame#taskRow[dropEdge="bottom"] { border-bottom: 3px solid $ACCENT; }
QFrame#taskRow[dropInto="true"] { border: 2px solid $ACCENT; background: $GREEN_BG; }
QFrame#taskRow[tagTarget="true"] { border: 2px solid $GREEN; background: $GREEN_BG; }
QLabel#taskTitle { color: $TEXT; }
QLabel#timeLbl { color: $SUB; }
QLabel#estLbl { color: $EST; }
QLabel#noteLbl { color: $AMBER_TXT; }
QLabel#grip { color: $MUTED; font-size: 11px; }
QLabel#grip:hover { color: $SUB; }
QLabel#subCount { color: $MUTED; font-size: 11px; }
QLabel#tagChip { background: $ALT; border: 1px solid $BORDER; border-radius: 9px;
                 padding: 1px 7px; color: $SUB; font-size: 11px; }
QPushButton#timerBtn { font-size: 13px; padding: 2px; color: $ACCENT;
                       font-weight: 700; }
QPushButton#timerBtn:checked { background: $GREEN_BG; color: $GREEN_TXT;
                               border-color: $GREEN; }
QPushButton#expBtn { border: none; color: $SUB; font-size: 11px; padding: 0; }
QPushButton#expBtn:hover { color: $ACCENT; background: $HOVER; border-radius: 4px; }
QLineEdit#subAdd { background: transparent; border: 1px dashed $BORDER;
                   border-radius: 6px; padding: 5px 8px; color: $SUB; }
QLineEdit#subAdd:focus { border-color: $ACCENT; color: $TEXT; background: $INPUT; }
QPushButton#repBtn { border: none; font-size: 13px; color: $MUTED; }
QPushButton#repBtn:hover { background: $HOVER; color: $SUB; border-radius: 6px; }
QPushButton#repBtn:checked { background: $AMBER_BG; color: $AMBER_TXT;
                             border: 1px solid $AMBER; border-radius: 6px; }
QPushButton#delBtn { color: $MUTED; border: none; font-weight: 700; }
QPushButton#delBtn:hover { color: $RED; background: $RED_BG; border-radius: 6px; }
QLabel#emptyLbl { color: $MUTED; font-size: 14px; padding: 30px; }
QProgressBar { background: $GRID; border: none; border-radius: 4px; }
QProgressBar::chunk { background: $GREEN; border-radius: 4px; }
QProgressBar#goalBar::chunk { background: $ACCENT; border-radius: 3px; }
QCheckBox::indicator { width: 18px; height: 18px; border: 2px solid $MUTED;
                       border-radius: 5px; background: $INPUT; }
QCheckBox::indicator:hover { border-color: $ACCENT; }
QCheckBox::indicator:checked { background: $ACCENT; border-color: $ACCENT; }
QTableWidget { background: $CARD; border: 1px solid $BORDER; border-radius: 8px;
               gridline-color: $GRID; alternate-background-color: $ALT; }
QHeaderView::section { background: $HOVER; color: $SUB; border: none;
                       border-bottom: 1px solid $BORDER; padding: 6px 8px;
                       font-weight: 600; }
QTabWidget::pane { border: 1px solid $BORDER; border-radius: 8px; background: $CARD; }
QTabBar::tab { background: transparent; color: $SUB; padding: 7px 16px;
               border: none; }
QTabBar::tab:selected { color: $ACCENT; font-weight: 600;
                        border-bottom: 2px solid $ACCENT; }
QListWidget { background: $INPUT; border: 1px solid $BORDER; border-radius: 6px; }
QMenu { background: $CARD; border: 1px solid $BORDER; border-radius: 8px;
        padding: 4px; }
QMenu::item { padding: 6px 22px; border-radius: 5px; }
QMenu::item:selected { background: $ACCENT; color: white; }
QMenu::separator { height: 1px; background: $BORDER; margin: 4px 6px; }
QMenuBar { background: $CARD; border-bottom: 1px solid $BORDER; }
QMenuBar::item { padding: 4px 10px; background: transparent; border-radius: 4px; }
QMenuBar::item:selected { background: $HOVER; }
QStatusBar { background: $CARD; color: $SUB; }
QToolTip { background: $CARD; color: $TEXT; border: 1px solid $BORDER;
           padding: 4px; }
""")

_current_theme: dict[str, str] = THEMES["light"]
_is_dark: bool = False
_tag_overrides: dict[str, str] = {}


def theme_color(key: str) -> QColor:
    return QColor(_current_theme.get(key, "#888888"))


def apply_theme(app: QApplication, name: str):
    global _current_theme, _is_dark
    _current_theme = THEMES.get(name, THEMES["light"])
    _is_dark = name == "dark"
    app.setStyleSheet(QSS_TEMPLATE.substitute(_current_theme))


def tag_colors(tag: str) -> tuple[str, str, str]:
    ov = _tag_overrides.get(tag)
    if ov:
        base = QColor(ov)
        if not base.isValid():
            base = QColor("#888888")
        if _is_dark:
            bg = base.darker(200)
            fg = base.lighter(140)
        else:
            bg = base.lighter(175)
            fg = base.darker(160)
        return bg.name(), fg.name(), base.name()
    hue = (zlib.crc32(tag.encode("utf-8")) % 360) / 360.0
    bg = QColor(); bg.setHslF(hue, 0.72 if not _is_dark else 0.45,
                              0.90 if not _is_dark else 0.26)
    fg = QColor(); fg.setHslF(hue, 0.60 if not _is_dark else 0.55,
                              0.28 if not _is_dark else 0.80)
    bd = QColor(); bd.setHslF(hue, 0.55 if not _is_dark else 0.45,
                              0.74 if not _is_dark else 0.40)
    return bg.name(), fg.name(), bd.name()


def chip_stylesheet(tag: str, selected_remove: bool = False) -> str:
    if selected_remove:
        return ("QLabel#tagChip { background:%s; color:#ffffff; "
                "border:1px solid %s; border-radius:9px; padding:1px 7px; "
                "font-size:11px; text-decoration: line-through; }"
                % (_current_theme["RED"], _current_theme["RED"]))
    bg, fg, bd = tag_colors(tag)
    return ("QLabel#tagChip { background:%s; color:%s; border:1px solid %s; "
            "border-radius:9px; padding:1px 7px; font-size:11px; }"
            % (bg, fg, bd))


# ---------------------------------------------------------------- model
@dataclass
class Task:
    """Recursive item — top-level instances are tasks, nested ones subtasks."""
    text: str
    done: bool = False
    seconds_spent: float = 0.0
    estimated_seconds: float = 0.0
    repeat: str = "none"            # "none" | "daily" | "weekdays"
    note: str = ""
    tags: list[str] = field(default_factory=list)
    children: list["Task"] = field(default_factory=list)
    expanded: bool = False
    created: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    completed_at: Optional[str] = None
    _running_since: Optional[datetime] = field(default=None, repr=False, compare=False)

    @property
    def is_running(self) -> bool:
        return self._running_since is not None

    def start(self):
        if self._running_since is None:
            self._running_since = datetime.now()

    def stop(self):
        if self._running_since is not None:
            self.seconds_spent += (datetime.now() - self._running_since).total_seconds()
            self._running_since = None

    def current_seconds(self) -> float:
        total = self.seconds_spent
        if self._running_since is not None:
            total += (datetime.now() - self._running_since).total_seconds()
        return total

    def iter_tree(self):
        yield self
        for c in self.children:
            yield from c.iter_tree()

    def subtree_seconds(self) -> float:
        return sum(i.current_seconds() for i in self.iter_tree())

    def children_done(self) -> tuple[int, int]:
        return sum(1 for c in self.children if c.done), len(self.children)

    def repeats_on(self, target: date) -> bool:
        if self.repeat == "daily":
            return True
        if self.repeat == "weekdays":
            return target.weekday() < 5
        return False

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "done": self.done,
            "seconds_spent": round(self.seconds_spent, 1),
            "estimated_seconds": round(self.estimated_seconds, 1),
            "repeat": self.repeat,
            "repeat_daily": self.repeat != "none",   # legacy readers
            "note": self.note,
            "tags": list(self.tags),
            "children": [c.to_dict() for c in self.children],
            "expanded": self.expanded,
            "created": self.created,
            "completed_at": self.completed_at,
            "running_since": self._running_since.isoformat(timespec="seconds")
                             if self._running_since else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        raw_children = d.get("children")
        if raw_children is None:                       # legacy "subtasks" key
            raw_children = d.get("subtasks") or []
        children = [cls.from_dict(c) for c in raw_children if isinstance(c, dict)]
        rep = str(d.get("repeat") or
                  ("daily" if d.get("repeat_daily") else "none")).lower()
        if rep not in ("none", "daily", "weekdays"):
            rep = "none"
        tags: list[str] = []
        for x in (d.get("tags") or []):
            s = str(x).strip().lstrip("#").lower()
            if s and s not in tags:
                tags.append(s)
        t = cls(
            text=d.get("text", ""),
            done=bool(d.get("done", False)),
            seconds_spent=float(d.get("seconds_spent", 0.0)),
            estimated_seconds=float(d.get("estimated_seconds", 0.0)),
            repeat=rep,
            note=d.get("note", ""),
            tags=tags,
            children=children,
            expanded=bool(d.get("expanded", bool(children))),
            created=d.get("created") or datetime.now().isoformat(timespec="seconds"),
            completed_at=d.get("completed_at"),
        )
        rs = d.get("running_since")
        if rs:
            try:
                t._running_since = datetime.fromisoformat(rs)
            except ValueError:
                pass
        return t


# ---------------------------------------------------------------- small widgets
class ClickableLabel(QLabel):
    clicked = Signal()
    doubleClicked = Signal()

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._pressed_inside = False

    def mousePressEvent(self, e):
        self._pressed_inside = e.button() == Qt.MouseButton.LeftButton
        e.accept()

    def mouseReleaseEvent(self, e):
        if (self._pressed_inside
                and e.button() == Qt.MouseButton.LeftButton
                and self.rect().contains(e.position().toPoint())):
            self.clicked.emit()
        self._pressed_inside = False
        e.accept()

    def mouseDoubleClickEvent(self, e):
        self.doubleClicked.emit()
        e.accept()


class TagChip(ClickableLabel):
    """Colored tag chip: click = filter, drag onto a task = tag it,
    right-click = color & more."""

    def __init__(self, tag: str, main: Optional["MainWindow"] = None,
                 draggable: bool = True):
        super().__init__("#" + tag)
        self.tag = tag
        self.main = main
        self.setObjectName("tagChip")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._press_pos = None
        if draggable:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setToolTip("Click: filter by this tag • drag onto a task to "
                            "tag it • right-click: color & more")

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._press_pos = e.position()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if (self._press_pos is not None and self.main is not None
                and (e.buttons() & Qt.MouseButton.LeftButton)
                and (e.position() - self._press_pos).manhattanLength() > 8):
            drag = QDrag(self)
            mime = QMimeData()
            mime.setData(MIME_TAG, self.tag.encode("utf-8"))
            drag.setMimeData(mime)
            self._press_pos = None
            self._pressed_inside = False
            drag.exec(Qt.DropAction.CopyAction | Qt.DropAction.MoveAction)
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._press_pos = None
        super().mouseReleaseEvent(e)

    def contextMenuEvent(self, e):
        if self.main is None:
            return
        m = QMenu(self)
        m.addAction(f"🔍  Filter by #{self.tag}",
                    lambda: self.main._filter_by_tag(self.tag))
        m.addAction("🎨  Edit color…",
                    lambda: self.main._edit_tag_color(self.tag))
        if self.tag in self.main._tag_color_overrides():
            m.addAction("↺  Reset to auto color",
                        lambda: self.main._reset_tag_color(self.tag))
        m.exec(e.globalPos())


class DragGrip(QLabel):
    def __init__(self, row: "ItemRow"):
        super().__init__("⋮⋮")
        self.row = row
        self.setObjectName("grip")
        self.setFixedWidth(16)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.setToolTip("Drag to reorder / re-nest")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            drag = QDrag(self.row)
            mime = QMimeData()
            mime.setData(MIME_ROW, str(id(self.row)).encode())
            drag.setMimeData(mime)
            drag.exec(Qt.DropAction.MoveAction)
        e.accept()


class CenteredList(QWidget):
    """Scroll content that caps its own width on very wide windows,
    keeping task rows at a comfortable reading width."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(6)

    def addLayout(self, lay):
        self._lay.addLayout(lay)

    def addWidget(self, w):
        self._lay.addWidget(w)

    def addStretch(self):
        self._lay.addStretch()

    def resizeEvent(self, e):
        extra = max(0, self.width() - MAX_CONTENT_W)
        m = extra // 2
        self._lay.setContentsMargins(m, 0, m, 0)
        super().resizeEvent(e)


# ---------------------------------------------------------------- item row
class ItemRow(QFrame):
    changed = Signal()
    deleteRequested = Signal(object)
    duplicateRequested = Signal(object)
    moveRequested = Signal(object)
    exclusiveStart = Signal(object)
    dropRequested = Signal(object, object, str)
    estimateExceeded = Signal(object)
    doneToggled = Signal(object)

    def __init__(self, task: Task, main: "MainWindow", depth: int = 0, parent=None):
        super().__init__(parent)
        self.item = task
        self.main = main
        self.depth = depth
        self.parent_row: Optional["ItemRow"] = None
        self.child_rows: list["ItemRow"] = []
        self._editor: Optional[QLineEdit] = None
        self._exceeded_notified = False
        self.setObjectName("taskRow")
        self.setAcceptDrops(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)

        self.outer = QVBoxLayout(self)
        self.outer.setContentsMargins(1 + depth * 16, 0, 1, 1)
        self.outer.setSpacing(0)
        self.setProperty("nested", depth > 0)

        self.header_w = QWidget()
        self.lay = QHBoxLayout(self.header_w)
        self.lay.setContentsMargins(8, 6, 8, 6)
        self.lay.setSpacing(8)
        self.outer.addWidget(self.header_w)

        self.est_bar = QProgressBar()
        self.est_bar.setFixedHeight(4)
        self.est_bar.setTextVisible(False)
        self.est_bar.setRange(0, 100)
        self.est_bar.hide()
        self.outer.addWidget(self.est_bar)

        self.child_box = QWidget()
        c_outer = QVBoxLayout(self.child_box)
        c_outer.setContentsMargins(10, 0, 2, 4)
        c_outer.setSpacing(4)
        self.child_lay = QVBoxLayout()
        self.child_lay.setSpacing(4)
        c_outer.addLayout(self.child_lay)

        self.child_add = QLineEdit()
        self.child_add.setObjectName("subAdd")
        self.child_add.setPlaceholderText("＋ Add subtask…  (#tag ~1h30 work, Esc collapses)")
        self.child_add.installEventFilter(self)
        self.child_add.returnPressed.connect(self._add_child_from_input)
        c_outer.addWidget(self.child_add)
        self.outer.addWidget(self.child_box)

        self.grip = DragGrip(self)
        self.lay.addWidget(self.grip)

        self.exp_btn = QPushButton("▸")
        self.exp_btn.setObjectName("expBtn")
        self.exp_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.exp_btn.setFixedSize(22, 22)
        self.exp_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.exp_btn.setToolTip("Show / hide subtasks")
        self.exp_btn.clicked.connect(self._toggle_expanded)
        self.lay.addWidget(self.exp_btn)

        self.done_cb = QCheckBox()
        self.done_cb.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.done_cb.setCursor(Qt.CursorShape.PointingHandCursor)
        self.done_cb.setToolTip("Mark as done  (Space)")
        self.done_cb.toggled.connect(self._on_done)
        self.lay.addWidget(self.done_cb)

        self.note_lbl = QLabel("🗒")
        self.note_lbl.setObjectName("noteLbl")
        self.note_lbl.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.note_lbl.hide()
        self.lay.addWidget(self.note_lbl)

        self.title_lbl = ClickableLabel(task.text)
        self.title_lbl.setObjectName("taskTitle")
        self.title_lbl.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.title_lbl.setToolTip(
            "Double-click to edit • right-click for more • "
            "Ctrl+click to multi-select")
        self.title_lbl.doubleClicked.connect(self._begin_edit)
        self.lay.addWidget(self.title_lbl, 1)

        self.tag_box = QWidget()
        self.tag_lay = QHBoxLayout(self.tag_box)
        self.tag_lay.setContentsMargins(0, 0, 0, 0)
        self.tag_lay.setSpacing(3)
        self.lay.addWidget(self.tag_box)

        self.count_lbl = QLabel("")
        self.count_lbl.setObjectName("subCount")
        self.count_lbl.hide()
        self.lay.addWidget(self.count_lbl)

        self.est_lbl = ClickableLabel("–")
        self.est_lbl.setObjectName("estLbl")
        self.est_lbl.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.est_lbl.setMinimumWidth(48)
        self.est_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.est_lbl.setToolTip("Click to set a time estimate")
        self.est_lbl.setCursor(Qt.CursorShape.PointingHandCursor)
        self.est_lbl.clicked.connect(self._edit_estimate)
        self.lay.addWidget(self.est_lbl)

        self.time_lbl = ClickableLabel("0:00")
        self.time_lbl.setObjectName("timeLbl")
        self.time_lbl.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.time_lbl.setMinimumWidth(70)
        self.time_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.time_lbl.setToolTip("Time spent on this item")
        self.time_lbl.doubleClicked.connect(self._adjust_time)
        self.lay.addWidget(self.time_lbl)

        self.timer_btn = QPushButton("▶")
        self.timer_btn.setObjectName("timerBtn")
        self.timer_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.timer_btn.setCheckable(True)
        self.timer_btn.setFixedSize(36, 28)
        self.timer_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.timer_btn.setToolTip("Start / pause timer  (Ctrl+Space)")
        self.timer_btn.toggled.connect(self._on_timer_toggled)
        self.lay.addWidget(self.timer_btn)

        self.rep_btn = QPushButton("🔁")
        self.rep_btn.setObjectName("repBtn")
        self.rep_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.rep_btn.setCheckable(True)
        self.rep_btn.setFixedSize(28, 28)
        self.rep_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.rep_btn.clicked.connect(self._cycle_repeat)
        self.lay.addWidget(self.rep_btn)

        self.del_btn = QPushButton("✕")
        self.del_btn.setObjectName("delBtn")
        self.del_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.del_btn.setFixedSize(28, 28)
        self.del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.del_btn.setToolTip("Delete item (and its subtasks)  (Del)")
        self.del_btn.clicked.connect(lambda: self.deleteRequested.emit(self))
        self.lay.addWidget(self.del_btn)

        self.timer_btn.blockSignals(True)
        self.timer_btn.setChecked(task.is_running)
        self.timer_btn.blockSignals(False)

        self.child_box.setVisible(task.expanded)
        self.exp_btn.setText("▾" if task.expanded else "▸")
        self.child_add.setVisible(self.depth < MAX_DEPTH - 1)

        self._update_note()
        self.refresh()

    # -- identity -----------------------------------------------------------
    def path_key(self) -> str:
        parts = [self.item.text]
        p = self.parent_row
        while p is not None:
            parts.append(p.item.text)
            p = p.parent_row
        return " ▸ ".join(reversed(parts))

    # -- children ----------------------------------------------------------
    def _add_child_from_input(self):
        text = self.child_add.text().strip()
        if not text:
            return
        text, tags = extract_tags(text)
        text, est = parse_estimate(text)
        if not text:
            return
        self.main._create_row(Task(text=text, estimated_seconds=est, tags=tags),
                              parent_row=self)
        self.child_add.clear()
        self.child_add.setFocus()
        self.refresh()
        self.changed.emit()

    def _on_child_done(self, _child_row):
        if not self.main._auto_complete_parents():
            return
        if not self.item.children or self.item.done:
            return
        if all(c.done for c in self.item.children):
            self.done_cb.setChecked(True)

    def eventFilter(self, obj, event):
        if obj is self.child_add and event.type() == QEvent.Type.KeyPress \
                and event.key() == Qt.Key.Key_Escape:
            self.main._user_set_expansion(self, False)
            return True
        return super().eventFilter(obj, event)

    def _toggle_expanded(self):
        self.main._user_set_expansion(self, not self.item.expanded)

    def set_expanded(self, expanded: bool, emit: bool = True):
        self.item.expanded = expanded
        self.child_box.setVisible(expanded)
        self.exp_btn.setText("▾" if expanded else "▸")
        if emit:
            self.changed.emit()

    def set_depth(self, d: int):
        self.depth = d
        self.setProperty("nested", d > 0)
        self.outer.setContentsMargins(1 + d * 16, 0, 1, 1)
        self.child_add.setVisible(d < MAX_DEPTH - 1)
        for c in self.child_rows:
            c.set_depth(d + 1)
        polish(self)

    # -- slots ------------------------------------------------------------
    def _on_timer_toggled(self, checked: bool):
        if checked:
            if self.item.done:
                self.done_cb.blockSignals(True)
                self.done_cb.setChecked(False)
                self.done_cb.blockSignals(False)
                self.item.done = False
            self.item.start()
            self._exceeded_notified = False
            self.exclusiveStart.emit(self)
        else:
            self.item.stop()
        self.refresh()
        self.changed.emit()

    def _on_done(self, checked: bool):
        self.item.done = checked
        if checked:
            self.item.completed_at = datetime.now().isoformat(timespec="seconds")
            if self.item.is_running:
                self.stop_timer()
        self.refresh()
        self.doneToggled.emit(self)
        self.changed.emit()

    def _on_repeat(self, checked: bool):
        self.item.repeat_daily = checked
        self.changed.emit()

    def _cycle_repeat(self):
        order = ["none", "daily", "weekdays"]
        self.item.repeat = order[(order.index(self.item.repeat) + 1) % 3]
        self.refresh()
        self.changed.emit()

    def _set_repeat(self, mode: str):
        if mode == self.item.repeat:
            return
        self.item.repeat = mode
        self.refresh()
        self.changed.emit()

    def _edit_estimate(self):
        cur = int(self.item.estimated_seconds // 60)
        minutes, ok = QInputDialog.getInt(
            self.window(), "Time estimate",
            f"Estimated minutes for:\n\"{self.item.text[:60]}\"\n(0 = no estimate)",
            cur, 0, 100000, 5)
        if ok:
            self.item.estimated_seconds = float(minutes) * 60
            self._exceeded_notified = False
            self.refresh()
            self.changed.emit()

    def _edit_note(self):
        note, ok = QInputDialog.getMultiLineText(
            self.window(), "Task note",
            f"Note for:\n\"{self.item.text[:60]}\"", self.item.note)
        if ok:
            self.item.note = note.strip()
            self._update_note()
            self.changed.emit()

    def _adjust_time(self):
        minutes, ok = QInputDialog.getInt(
            self.window(), "Adjust time",
            f"Adjust tracked time for:\n\"{self.item.text[:60]}\"\n"
            f"Current: {fmt_hms(self.item.current_seconds())}\n"
            f"Enter minutes to add (negative to subtract):",
            0, -100000, 100000, 5)
        if ok and minutes:
            self.item.seconds_spent = max(0.0, self.item.seconds_spent + minutes * 60)
            self.refresh()
            self.changed.emit()

    def _reset_time(self):
        self.item.seconds_spent = 0.0
        self._exceeded_notified = False
        self.refresh()
        self.changed.emit()

    def _begin_edit(self):
        if self._editor is not None:
            return
        self._editor = QLineEdit(self.item.text)
        self._editor.setObjectName("taskEditor")
        self._editor.editingFinished.connect(self._commit_edit)
        self.title_lbl.hide()
        self.lay.insertWidget(self.lay.indexOf(self.title_lbl), self._editor, 1)
        self._editor.setFocus()
        self._editor.selectAll()

    def _commit_edit(self):
        if self._editor is None:
            return
        editor, self._editor = self._editor, None
        text = editor.text().strip()
        if text:
            self.item.text = text
        self.title_lbl.setText(self.item.text)
        self.lay.removeWidget(editor)
        editor.deleteLater()
        self.title_lbl.show()
        self._update_note()
        self.changed.emit()

    def _update_note(self):
        has = bool(self.item.note)
        self.note_lbl.setVisible(has)
        self.note_lbl.setToolTip(self.item.note)
        tip = "Double-click to edit • right-click for more"
        n_done, n_total = self.item.children_done()
        if n_total:
            tip += f"\n\nSubtasks: {n_done}/{n_total}"
        if self.item.tags:
            tip += "\n\n🏷 " + " ".join("#" + t for t in self.item.tags)
        if has:
            tip += "\n\n🗒 " + self.item.note
        self.title_lbl.setToolTip(tip)

    # -- context menu -------------------------------------------------------
    def contextMenuEvent(self, e):
        menu = QMenu(self)
        menu.addAction("✏  Edit title  (Enter)", self._begin_edit)
        menu.addAction("⏱  Set estimate…", self._edit_estimate)
        menu.addAction("🗒  Note…", self._edit_note)
        menu.addSeparator()
        menu.addAction("📋  Copy title",
                       lambda: QApplication.clipboard().setText(self.item.text))
        menu.addAction("📋  Copy as Markdown (subtree)",
                       lambda: self.main._copy_row_markdown(self))
        menu.addSeparator()
        tag_txt = "🏷  Tags…"
        if self.item.tags:
            tag_txt += f"   ({', '.join('#' + t for t in self.item.tags[:4])})"
        menu.addAction(tag_txt, lambda: self.main._edit_tags(self))
        known = self.main._known_tags()
        if known:
            sm = menu.addMenu("🏷  Quick tag ▸")
            for t in known[:12]:
                a = sm.addAction("#" + t)
                a.setCheckable(True)
                a.setChecked(t in self.item.tags)
                a.triggered.connect(
                    lambda _on=False, tt=t: self.main._quick_toggle_tag(self, tt))
        menu.addAction("➕  Add subtask…", self._focus_child_add)
        menu.addSeparator()
        rm = menu.addMenu("🔁  Repeat ▸")
        for mode, label in (("none", "Off"),
                            ("daily", "Every day"),
                            ("weekdays", "Weekdays (Mon–Fri)")):
            a = rm.addAction(label)
            a.setCheckable(True)
            a.setChecked(self.item.repeat == mode)
            a.triggered.connect(lambda _=False, m=mode: self._set_repeat(m))
        menu.addSeparator()
        menu.addAction(f"＋/－ Adjust time…  ({fmt_hms(self.item.current_seconds())})",
                       self._adjust_time)
        menu.addAction("↺  Reset time", self._reset_time)
        menu.addSeparator()
        menu.addAction("⧉  Duplicate", lambda: self.duplicateRequested.emit(self))
        menu.addAction("📅  Move to date…", lambda: self.main._move_to_date(self))
        menu.addAction("➡  Move to tomorrow", lambda: self.moveRequested.emit(self))
        menu.addSeparator()
        menu.addAction("✕  Delete  (Del)", lambda: self.deleteRequested.emit(self))
        menu.exec(e.globalPos())

    def _focus_child_add(self):
        self.main._user_set_expansion(self, True)
        self.child_add.setFocus()

    # -- drag & drop ----------------------------------------------------------
    def _drag_id(self, e) -> Optional[int]:
        if not e.mimeData().hasFormat(MIME_ROW):
            return None
        try:
            return int(bytes(e.mimeData().data(MIME_ROW)).decode())
        except ValueError:
            return None

    def _drop_zone(self, e) -> str:
        y = e.position().y()
        h = max(1.0, float(self.header_w.height()))
        if y < h:
            if y < h * 0.25:
                return "above"
            if y > h * 0.75:
                return "below"
            return "child"
        return "child"

    def _set_drop_props(self, zone: str):
        edge = "top" if zone == "above" else "bottom" if zone == "below" else ""
        into = "true" if zone == "child" else ""
        changed = False
        if self.property("dropEdge") != edge:
            self.setProperty("dropEdge", edge)
            changed = True
        if self.property("dropInto") != into:
            self.setProperty("dropInto", into)
            changed = True
        if changed:
            polish(self)

    def _clear_tag_target(self):
        if self.property("tagTarget"):
            self.setProperty("tagTarget", "")
            polish(self)

    def dragEnterEvent(self, e):
        md = e.mimeData()
        if md.hasFormat(MIME_ROW) or md.hasFormat(MIME_TAG):
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        md = e.mimeData()
        if md.hasFormat(MIME_TAG):
            self._set_drop_props("")
            if self.property("tagTarget") != "true":
                self.setProperty("tagTarget", "true")
                polish(self)
            e.acceptProposedAction()
            return
        self._clear_tag_target()
        src = self.main._find_row(self._drag_id(e) or 0)
        zone = self._drop_zone(e)
        if src is None or not self.main._drop_allowed(src, self, zone):
            self._set_drop_props("")
            e.ignore()
            return
        self._set_drop_props(zone)
        e.acceptProposedAction()

    def dragLeaveEvent(self, e):
        self._set_drop_props("")
        self._clear_tag_target()

    def dropEvent(self, e):
        self._set_drop_props("")
        self._clear_tag_target()
        md = e.mimeData()
        if md.hasFormat(MIME_TAG):
            tag = bytes(md.data(MIME_TAG)).decode("utf-8", "ignore").strip().lower()
            if tag:
                self.main._apply_dropped_tag(self, tag)
            e.acceptProposedAction()
            return
        src = self.main._find_row(self._drag_id(e) or 0)
        zone = self._drop_zone(e)
        if src is not None and self.main._drop_allowed(src, self, zone):
            self.dropRequested.emit(src, self, zone)
            e.acceptProposedAction()

    # -- public -----------------------------------------------------------
    def stop_timer(self):
        self.timer_btn.blockSignals(True)
        self.timer_btn.setChecked(False)
        self.timer_btn.blockSignals(False)
        self.item.stop()
        self.refresh()

    def refresh_time(self):
        secs = self.item.current_seconds()
        self.time_lbl.setText(fmt_hms(secs))
        est = self.item.estimated_seconds
        if est > 0:
            ratio = secs / est
            if secs > est:
                color = _current_theme["RED"]
                if ratio - 1.0 < 0.02 and self.item.is_running \
                        and not self._exceeded_notified:
                    self._exceeded_notified = True
                    self.estimateExceeded.emit(self)
            elif ratio >= 0.8:
                color = _current_theme["AMBER_TXT"]
            else:
                color = ""
            self.time_lbl.setStyleSheet(
                f"color:{color}; font-weight:600;" if color else "")
            note = " • over estimate!" if secs > est else ""
            self.time_lbl.setToolTip(
                f"Actual {fmt_hms(secs)} of ~{fmt_dur(est)} ({int(ratio * 100)}%){note}"
                "\nDouble-click to adjust")

            bar_color = (_current_theme["RED"] if secs > est else
                         _current_theme["AMBER"] if ratio >= 0.8 else
                         _current_theme["GREEN"])
            self.est_bar.show()
            self.est_bar.setValue(min(100, int(ratio * 100)))
            self.est_bar.setStyleSheet(
                "QProgressBar { background: transparent; border: none; }"
                f"QProgressBar::chunk {{ background: {bar_color}; border-radius: 2px; }}")
        else:
            self.time_lbl.setStyleSheet(
                f"color:{_current_theme['GREEN_TXT']}; font-weight:600;"
                if self.item.is_running else "")
            self.time_lbl.setToolTip("Time spent on this item"
                                     "\nDouble-click to adjust")
            self.est_bar.hide()
            self.est_bar.setStyleSheet("")

        if self.item.children:
            sub = self.item.subtree_seconds() - secs
            if sub > 0:
                self.time_lbl.setToolTip(
                    self.time_lbl.toolTip()
                    + f"\nIncl. subtasks: {fmt_hms(self.item.subtree_seconds())}")

    def refresh(self):
        running, done = self.item.is_running, self.item.done
        self.setProperty("running", running)
        self.setProperty("done", done)
        self.timer_btn.setText("⏸" if running else "▶")

        mode = self.item.repeat
        self.rep_btn.blockSignals(True)
        self.rep_btn.setChecked(mode != "none")
        self.rep_btn.blockSignals(False)
        if mode == "weekdays":
            self.rep_btn.setText("📅")
            self.rep_btn.setToolTip(
                "Repeats on weekdays (Mon–Fri) — click: every day, again: off")
        elif mode == "daily":
            self.rep_btn.setText("🔁")
            self.rep_btn.setToolTip(
                "Repeats every day — click: weekdays only, again: off")
        else:
            self.rep_btn.setText("🔁")
            self.rep_btn.setToolTip(
                "Repeat: off — click for every day, again for weekdays only")

        n_done, n_total = self.item.children_done()
        self.exp_btn.setText("▾" if self.item.expanded else "▸")
        self.exp_btn.setToolTip(
            (f"Subtasks: {n_done}/{n_total} — " if n_total else "")
            + ("click to collapse" if self.item.expanded else "click to expand"))
        if n_total:
            self.count_lbl.setVisible(True)
            self.count_lbl.setText(f"☑ {n_done}/{n_total}")
            self.count_lbl.setStyleSheet(
                f"color:{_current_theme['GREEN_TXT']}; font-weight:600;"
                if n_done == n_total else "")
        else:
            self.count_lbl.setVisible(False)

        while self.tag_lay.count():
            it = self.tag_lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        for t in self.item.tags[:6]:
            chip = TagChip(t, main=self.main)
            chip.setStyleSheet(chip_stylesheet(t))
            chip.clicked.connect(lambda tt=t: self.main._filter_by_tag(tt))
            self.tag_lay.addWidget(chip)
        if len(self.item.tags) > 6:
            more = QLabel(f"+{len(self.item.tags) - 6}")
            more.setObjectName("tagChip")
            self.tag_lay.addWidget(more)
        self.tag_box.setVisible(bool(self.item.tags))

        est = self.item.estimated_seconds
        if est > 0:
            self.est_lbl.setText("~" + fmt_dur(est))
            self.est_lbl.setStyleSheet("")
        else:
            self.est_lbl.setText("–")
            self.est_lbl.setStyleSheet("color:" + _current_theme["MUTED"] + ";")

        font = self.title_lbl.font()
        font.setStrikeOut(done)
        self.title_lbl.setFont(font)
        self.title_lbl.setStyleSheet("color:" + _current_theme["MUTED"] + ";" if done else "")
        polish(self)
        self.refresh_time()
        self._update_note()


# ---------------------------------------------------------------- dialogs
class MoveToDateDialog(QDialog):
    def __init__(self, current: date, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Move to date")
        self.setFixedWidth(320)

        v = QVBoxLayout(self)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(10)
        v.addWidget(QLabel("Move the selected item (with its subtasks) to:"))
        self.date_edit = QDateEdit()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        self.date_edit.setDate(QDate(current.year, current.month, current.day))
        v.addWidget(self.date_edit)

        h = QHBoxLayout()
        cancel_b = QPushButton("Cancel")
        ok_b = QPushButton("Move")
        ok_b.setObjectName("addBtn")
        h.addStretch()
        h.addWidget(cancel_b)
        h.addWidget(ok_b)
        v.addLayout(h)
        ok_b.clicked.connect(self.accept)
        cancel_b.clicked.connect(self.reject)

    def selected_date(self) -> date:
        qd = self.date_edit.date()
        return date(qd.year(), qd.month(), qd.day())


class TagsDialog(QDialog):
    def __init__(self, item: Task, known_tags: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Tags")
        self.resize(400, 240)

        v = QVBoxLayout(self)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(10)
        v.addWidget(QLabel(f"Tags for:\n\"{item.text[:70]}\""))

        self.edit = QLineEdit(" ".join("#" + t for t in item.tags))
        self.edit.setPlaceholderText("#work #deep   (space or comma separated)")
        v.addWidget(self.edit)

        if known_tags:
            v.addWidget(QLabel("Existing tags — click to add:"))
            sug = QWidget()
            flow = QHBoxLayout(sug)
            flow.setContentsMargins(0, 0, 0, 0)
            flow.setSpacing(4)
            for t in known_tags[:14]:
                chip = TagChip(t, main=None, draggable=False)
                chip.setStyleSheet(chip_stylesheet(t))
                chip.clicked.connect(lambda tt=t: self._add_tag(tt))
                flow.addWidget(chip)
            flow.addStretch()
            v.addWidget(sug)

        v.addStretch()
        h = QHBoxLayout()
        cancel_b = QPushButton("Cancel")
        ok_b = QPushButton("Save tags")
        ok_b.setObjectName("addBtn")
        h.addStretch()
        h.addWidget(cancel_b)
        h.addWidget(ok_b)
        v.addLayout(h)
        ok_b.clicked.connect(self.accept)
        cancel_b.clicked.connect(self.reject)

    def _add_tag(self, t: str):
        cur = self.tags()
        if t in cur:
            return
        self.edit.setText(" ".join("#" + x for x in cur + [t]))

    def tags(self) -> list[str]:
        out: list[str] = []
        for tok in self.edit.text().replace(",", " ").split():
            t = tok.strip().lstrip("#").lower()
            if t and t not in out:
                out.append(t)
        return out


class BulkTagsDialog(QDialog):
    def __init__(self, count: int, union: list[str], known: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Bulk tags")
        self.resize(430, 280)
        self._remove: set[str] = set()
        self._remove_chips: dict[str, ClickableLabel] = {}

        v = QVBoxLayout(self)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(10)
        v.addWidget(QLabel(f"Edit tags on {count} selected item(s):"))

        v.addWidget(QLabel("Tags to ADD:"))
        self.add_edit = QLineEdit()
        self.add_edit.setPlaceholderText("#urgent #deep   (space or comma separated)")
        v.addWidget(self.add_edit)

        if known:
            sug = QWidget()
            flow = QHBoxLayout(sug)
            flow.setContentsMargins(0, 0, 0, 0)
            flow.setSpacing(4)
            for t in [x for x in known if x not in union][:12]:
                chip = TagChip(t, main=None, draggable=False)
                chip.setStyleSheet(chip_stylesheet(t))
                chip.clicked.connect(lambda tt=t: self._add_to_edit(tt))
                flow.addWidget(chip)
            flow.addStretch()
            v.addWidget(sug)

        if union:
            v.addWidget(QLabel("Tags on selection — click to REMOVE:"))
            rem = QWidget()
            rflow = QHBoxLayout(rem)
            rflow.setContentsMargins(0, 0, 0, 0)
            rflow.setSpacing(4)
            for t in union[:14]:
                chip = TagChip(t, main=None, draggable=False)
                chip.setStyleSheet(chip_stylesheet(t))
                chip.clicked.connect(lambda c=chip, tt=t: self._toggle_remove(c, tt))
                self._remove_chips[t] = chip
                rflow.addWidget(chip)
            rflow.addStretch()
            v.addWidget(rem)

        v.addStretch()
        h = QHBoxLayout()
        cancel_b = QPushButton("Cancel")
        ok_b = QPushButton("Apply to selection")
        ok_b.setObjectName("addBtn")
        h.addStretch()
        h.addWidget(cancel_b)
        h.addWidget(ok_b)
        v.addLayout(h)
        ok_b.clicked.connect(self.accept)
        cancel_b.clicked.connect(self.reject)

    def _add_to_edit(self, t: str):
        cur = self.add_tags()
        if t in cur:
            return
        self.add_edit.setText(" ".join("#" + x for x in cur + [t]))

    def _toggle_remove(self, chip: ClickableLabel, t: str):
        if t in self._remove:
            self._remove.discard(t)
            chip.setStyleSheet(chip_stylesheet(t))
        else:
            self._remove.add(t)
            chip.setStyleSheet(chip_stylesheet(t, selected_remove=True))

    def add_tags(self) -> list[str]:
        out: list[str] = []
        for tok in self.add_edit.text().replace(",", " ").split():
            t = tok.strip().lstrip("#").lower()
            if t and t not in out:
                out.append(t)
        return out

    def remove_tags(self) -> set[str]:
        return set(self._remove)


class PresetsDialog(QDialog):
    def __init__(self, main: "MainWindow", parent=None):
        super().__init__(parent)
        self.main = main
        self.setWindowTitle("Filter presets")
        self.resize(480, 380)

        v = QVBoxLayout(self)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(10)
        v.addWidget(QLabel("Saved searches, with live match counts for the "
                           "day you're viewing. Double-click to apply."))
        self.list_w = QListWidget()
        v.addWidget(self.list_w, 1)

        self.preview = QLabel("")
        self.preview.setObjectName("statsSummary")
        self.preview.setWordWrap(True)
        v.addWidget(self.preview)

        h = QHBoxLayout()
        close_b = QPushButton("Close")
        del_b = QPushButton("🗑 Delete")
        ren_b = QPushButton("✏ Rename")
        apply_b = QPushButton("Apply")
        apply_b.setObjectName("addBtn")
        h.addWidget(close_b)
        h.addStretch()
        h.addWidget(del_b)
        h.addWidget(ren_b)
        h.addWidget(apply_b)
        v.addLayout(h)

        close_b.clicked.connect(self.reject)
        del_b.clicked.connect(self._delete)
        ren_b.clicked.connect(self._rename)
        apply_b.clicked.connect(self._apply)
        self.list_w.currentItemChanged.connect(lambda *_: self._update_preview())
        self.list_w.itemDoubleClicked.connect(lambda *_: self._apply())
        self._reload()

    def _presets(self) -> dict:
        return self.main.data.get(FILTER_KEY, {})

    def _selected_name(self) -> Optional[str]:
        it = self.list_w.currentItem()
        if not it or it.data(Qt.ItemDataRole.UserRole) is None:
            return None
        return it.data(Qt.ItemDataRole.UserRole)

    def _reload(self):
        self.list_w.clear()
        pres = self._presets()
        for name in sorted(pres):
            q = pres[name].get("q", "")
            n = self.main._preset_count(q)
            it = QListWidgetItem(f"{name}   ·   {n} match(es)")
            it.setData(Qt.ItemDataRole.UserRole, name)
            self.list_w.addItem(it)
        if self.list_w.count():
            self.list_w.setCurrentRow(0)

    def _update_preview(self):
        name = self._selected_name()
        q = self._presets().get(name, {}).get("q", "") if name else ""
        extra = ""
        if q:
            extra = f"  •  {self.main._preset_count(q)} match(es) today"
        self.preview.setText("Query: " + (q or "–") + extra)

    def _apply(self):
        name = self._selected_name()
        if name:
            self.main._apply_preset(name)
            self.accept()

    def _rename(self):
        name = self._selected_name()
        if not name:
            return
        new, ok = QInputDialog.getText(self, "Rename preset",
                                       "New name:", text=name)
        new = new.strip()
        if not ok or not new or new == name:
            return
        pres = self.main.data.setdefault(FILTER_KEY, {})
        if new in pres:
            QMessageBox.warning(self, "Rename", "A preset with that name exists.")
            return
        pres[new] = pres.pop(name)
        self.main._write_data()
        self.main._refresh_sidebar()
        self._reload()

    def _delete(self):
        name = self._selected_name()
        if not name:
            return
        if QMessageBox.question(self, "Delete preset",
                                f"Delete preset \"{name}\"?") \
                != QMessageBox.StandardButton.Yes:
            return
        self.main.data.get(FILTER_KEY, {}).pop(name, None)
        self.main._write_data()
        self.main._refresh_sidebar()
        self._reload()


# ---------------------------------------------------------------- bar chart
class BarChart(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: list[tuple[str, float, bool, str]] = []
        self.setMinimumHeight(220)
        self.setMouseTracking(True)

    def set_data(self, items):
        self._items = items
        self.update()

    @staticmethod
    def _nice_max(v: float) -> float:
        steps = [1800, 3600, 2 * 3600, 3 * 3600, 4 * 3600, 6 * 3600, 8 * 3600,
                 12 * 3600, 18 * 3600, 24 * 3600, 36 * 3600, 48 * 3600, 72 * 3600]
        for s in steps:
            if v <= s:
                return float(s)
        return math.ceil(v / 86400) * 86400

    def paintEvent(self, event):
        if not self._items:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        left, right, top, bottom = 46, 12, 20, 26
        pw = self.width() - left - right
        ph = self.height() - top - bottom
        if pw <= 10 or ph <= 10:
            return
        base_y = top + ph

        vmax = max(v for _, v, _, _ in self._items)
        nice = self._nice_max(vmax if vmax > 0 else 3600)

        small = p.font()
        small.setPointSize(8)
        p.setFont(small)

        for i in range(1, 5):
            y = top + ph * (1 - i / 4)
            p.setPen(QPen(theme_color("GRID"), 1))
            p.drawLine(left, int(y), left + pw, int(y))
            p.setPen(QPen(theme_color("SUB"), 1))
            p.drawText(QRect(0, int(y) - 8, left - 8, 16),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       fmt_axis(nice * i / 4))

        p.setPen(QPen(theme_color("BORDER"), 1))
        p.drawLine(left, base_y, left + pw, base_y)

        n = len(self._items)
        slot = pw / n
        bar_w = max(6.0, min(slot * 0.6, 40.0))

        for i, (label, v, hl, _tip) in enumerate(self._items):
            cx = left + slot * i + slot / 2
            x = cx - bar_w / 2
            if v > 0:
                bh = max(3.0, ph * (v / nice))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(theme_color("GREEN") if hl else theme_color("ACCENT"))
                p.drawRoundedRect(QRectF(x, base_y - bh, bar_w, bh), 3, 3)
                p.setFont(small)
                p.setPen(QPen(theme_color("TEXT"), 1))
                p.drawText(QRectF(cx - slot / 2, base_y - bh - 16, slot, 14),
                           Qt.AlignmentFlag.AlignCenter, fmt_dur(v))
            else:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(theme_color("GRID"))
                p.drawRoundedRect(QRectF(x, base_y - 2, bar_w, 2), 1, 1)

            f = p.font()
            f.setPointSize(8)
            f.setBold(hl)
            p.setFont(f)
            p.setPen(QPen(theme_color("TEXT") if hl else theme_color("SUB"), 1))
            p.drawText(QRectF(cx - slot / 2, base_y + 4, slot, 16),
                       Qt.AlignmentFlag.AlignCenter, label)
        p.end()

    def mouseMoveEvent(self, e):
        if self._items:
            pw = self.width() - 46 - 12
            slot = pw / len(self._items)
            i = int((e.position().x() - 46) / slot)
            if 0 <= i < len(self._items):
                self.setToolTip(self._items[i][3])
        super().mouseMoveEvent(e)


# ---------------------------------------------------------------- stats dialog
class StatsDialog(QDialog):
    def __init__(self, data: dict, parent=None):
        super().__init__(parent)
        self.data = data
        self._period_rows: list[tuple] = []
        self.setWindowTitle("Statistics")
        self.resize(720, 690)
        self.mode = "week"
        self.anchor = date.today()

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        tabs = QTabWidget()
        root.addWidget(tabs, 1)

        days_tab = QWidget()
        dv = QVBoxLayout(days_tab)
        dv.setContentsMargins(10, 10, 10, 10)
        dv.setSpacing(10)

        nav = QHBoxLayout()
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Week", "Month"])
        self.prev_btn = QPushButton("◀")
        self.prev_btn.setFixedWidth(34)
        self.next_btn = QPushButton("▶")
        self.next_btn.setFixedWidth(34)
        self.period_lbl = QLabel("")
        self.period_lbl.setObjectName("periodLbl")
        self.current_btn = QPushButton("Today")
        nav.addWidget(self.mode_combo)
        nav.addWidget(self.prev_btn)
        nav.addWidget(self.period_lbl, 1)
        nav.addWidget(self.next_btn)
        nav.addWidget(self.current_btn)
        dv.addLayout(nav)

        card = QFrame()
        card.setObjectName("header")
        cv = QVBoxLayout(card)
        cv.setContentsMargins(12, 10, 12, 8)
        cv.setSpacing(6)
        self.summary_lbl = QLabel("")
        self.summary_lbl.setObjectName("statsSummary")
        self.summary_lbl.setWordWrap(True)
        cv.addWidget(self.summary_lbl)
        self.chart = BarChart()
        cv.addWidget(self.chart, 1)
        dv.addWidget(card, 1)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["Date", "Done", "Tasks", "Subtasks", "Tracked", "Estimated",
             "vs. estimate"])
        self._style_table(self.table)
        dv.addWidget(self.table, 1)
        tabs.addTab(days_tab, "Days")

        tasks_tab = QWidget()
        tv = QVBoxLayout(tasks_tab)
        tv.setContentsMargins(10, 10, 10, 10)
        self.tasks_summary = QLabel("")
        self.tasks_summary.setObjectName("statsSummary")
        tv.addWidget(self.tasks_summary)
        self.task_table = QTableWidget(0, 5)
        self.task_table.setHorizontalHeaderLabels(
            ["Item", "Total", "Estimated", "vs. estimate", "Days active"])
        self._style_table(self.task_table)
        tv.addWidget(self.task_table)
        tabs.addTab(tasks_tab, "Top items")

        tags_tab = QWidget()
        gv = QVBoxLayout(tags_tab)
        gv.setContentsMargins(10, 10, 10, 10)
        self.tags_summary = QLabel("")
        self.tags_summary.setObjectName("statsSummary")
        self.tags_summary.setWordWrap(True)
        gv.addWidget(self.tags_summary)
        self.tag_table = QTableWidget(0, 4)
        self.tag_table.setHorizontalHeaderLabels(
            ["Tag", "Items", "Time", "Share"])
        self._style_table(self.tag_table)
        gv.addWidget(self.tag_table)
        tabs.addTab(tags_tab, "Time by tag")

        foot = QHBoxLayout()
        foot.addStretch()
        export_b = QPushButton("⬇  Export period CSV…")
        export_b.clicked.connect(self._export_csv)
        foot.addWidget(export_b)
        root.addLayout(foot)

        self.mode_combo.currentTextChanged.connect(self._on_mode)
        self.prev_btn.clicked.connect(lambda: self._nav(-1))
        self.next_btn.clicked.connect(lambda: self._nav(1))
        self.current_btn.clicked.connect(self._go_current)

        self.refresh()

    @staticmethod
    def _style_table(t: QTableWidget):
        t.verticalHeader().setVisible(False)
        t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        t.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        t.setAlternatingRowColors(True)
        hh = t.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)

    def _period(self):
        if self.mode == "week":
            start = self.anchor - timedelta(days=self.anchor.weekday())
            days = [start + timedelta(days=i) for i in range(7)]
            iso = self.anchor.isocalendar()
            title = (f"Week {iso.week} • {days[0].strftime('%b %d')}"
                     f" – {days[-1].strftime('%b %d, %Y')}")
            labels = [d.strftime("%a") for d in days]
        else:
            start = self.anchor.replace(day=1)
            nxt = shift_month(start, 1)
            days = [start + timedelta(days=i) for i in range((nxt - start).days)]
            title = start.strftime("%B %Y")
            labels = [str(d.day) for d in days]
        return days, title, labels

    def _day_stats(self, d: date):
        top = [Task.from_dict(td)
               for td in self.data.get(d.isoformat(), []) if isinstance(td, dict)]
        all_items = [i for t in top for i in t.iter_tree()]
        n_top = len(top)
        done_top = sum(1 for t in top if t.done)
        tracked = sum(i.current_seconds() for i in all_items)
        est = sum(i.estimated_seconds for i in all_items)
        sub_total = len(all_items) - n_top
        sub_done = sum(1 for i in all_items if i.done) - done_top
        return top, all_items, n_top, done_top, sub_done, sub_total, tracked, est

    @staticmethod
    def _diff_text(tracked: float, est: float):
        if est <= 0:
            return "–", _current_theme["SUB"]
        diff = tracked - est
        if diff > 60:
            return f"over {fmt_hms(diff)}", _current_theme["RED"]
        if diff < -60:
            return f"under {fmt_hms(-diff)}", _current_theme["GREEN_TXT"]
        return "on target", _current_theme["SUB"]

    def refresh(self):
        days, title, labels = self._period()
        self.period_lbl.setText(title)

        items, rows, all_period_items = [], [], []
        tt = te = 0.0
        td = tn = tsd = tst = 0
        for d, lab in zip(days, labels):
            top, all_items, n, done, sd, st, tracked, est = self._day_stats(d)
            all_period_items.extend(all_items)
            tt += tracked
            te += est
            td += done
            tn += n
            tsd += sd
            tst += st
            tip = (f"{d.strftime('%A, %B %d')}\n"
                   f"Tracked {fmt_hms(tracked)} • {done}/{n} done"
                   + (f" • subtasks {sd}/{st}" if st else ""))
            items.append((lab, tracked, d == date.today(), tip))
            if n or tracked:
                rows.append((d, done, n, sd, st, tracked, est))
        self.chart.set_data(items)
        self._period_rows = rows

        if te > 0:
            diff = te - tt
            if diff > 60:
                dtxt = (f"<span style='color:{_current_theme['GREEN_TXT']}'>"
                        f"under by {fmt_hms(diff)}</span>")
            elif diff < -60:
                dtxt = (f"<span style='color:{_current_theme['RED']}'>"
                        f"over by {fmt_hms(-diff)}</span>")
            else:
                dtxt = "on target"
            est_part = f" &nbsp;•&nbsp; Estimated {fmt_dur(te)} ({dtxt})"
        else:
            est_part = ""
        sub_part = f" &nbsp;•&nbsp; Subtasks {tsd}/{tst}" if tst else ""
        self.summary_lbl.setText(
            f"<b>Tracked {fmt_hms(tt)}</b> &nbsp;•&nbsp; Completed {td}/{tn}"
            f"{sub_part}{est_part}")

        self.table.setRowCount(len(rows))
        for r, (d, done, n, sd, st, tracked, est) in enumerate(rows):
            dtxt, color = self._diff_text(tracked, est)
            cells = [
                d.strftime("%a, %b %d") + ("  •" if d == date.today() else ""),
                f"{done}/{n}", str(n),
                f"{sd}/{st}" if st else "–",
                fmt_hms(tracked),
                fmt_dur(est) if est else "–",
                dtxt,
            ]
            for c, txt in enumerate(cells):
                it = QTableWidgetItem(txt)
                it.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
                if c in (1, 2, 3, 4, 5, 6):
                    it.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                        | Qt.AlignmentFlag.AlignVCenter)
                if c == 6:
                    it.setForeground(QBrush(QColor(color)))
                self.table.setItem(r, c, it)

        self._refresh_top_items(all_period_items)
        self._refresh_tag_times(all_period_items)

    def _refresh_tag_times(self, items: list[Task]):
        agg: dict[str, dict] = {}
        untagged = 0.0
        for t in items:
            secs = t.current_seconds()
            if secs < 1:
                continue
            if t.tags:
                for tg in t.tags:
                    a = agg.setdefault(tg, {"secs": 0.0, "n": 0})
                    a["secs"] += secs
                    a["n"] += 1
            else:
                untagged += secs

        total_tagged = sum(a["secs"] for a in agg.values())
        grand = total_tagged + untagged
        rows = sorted(agg.items(), key=lambda kv: -kv[1]["secs"])[:40]

        self.tags_summary.setText(
            f"Tracked time per tag across the period"
            + (f" • {fmt_hms(grand)} total, {fmt_hms(total_tagged)} tagged"
               if grand else "")
            + "  (items with several tags count toward each)")

        self.tag_table.setRowCount(len(rows) + (1 if untagged >= 1 else 0))
        for r, (tg, a) in enumerate(rows):
            share = (a["secs"] / total_tagged * 100) if total_tagged else 0
            cells = [f"#{tg}", str(a["n"]), fmt_hms(a["secs"]), f"{share:.0f}%"]
            base = QColor(tag_colors(tg)[2])
            for c, txt in enumerate(cells):
                it = QTableWidgetItem(txt)
                it.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
                if c > 0:
                    it.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                        | Qt.AlignmentFlag.AlignVCenter)
                if c == 0:
                    it.setForeground(QBrush(base))
                self.tag_table.setItem(r, c, it)
        if untagged >= 1:
            r = len(rows)
            share = (untagged / grand * 100) if grand else 0
            cells = ["(untagged)", "–", fmt_hms(untagged), f"{share:.0f}%"]
            for c, txt in enumerate(cells):
                it = QTableWidgetItem(txt)
                it.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
                if c > 0:
                    it.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                        | Qt.AlignmentFlag.AlignVCenter)
                it.setForeground(QBrush(QColor(_current_theme["SUB"])))
                self.tag_table.setItem(r, c, it)

    def _export_csv(self):
        default = f"stats_{self.mode}_{self.anchor.isoformat()}.csv"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export period as CSV", default, "CSV files (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["Date", "Done", "Tasks", "Subtasks done",
                            "Subtasks", "Tracked (h:mm:ss)", "Estimated",
                            "vs. estimate"])
                for (d, done, n, sd, st, tracked, est) in self._period_rows:
                    dtxt, _c = self._diff_text(tracked, est)
                    w.writerow([d.isoformat(), f"{done}/{n}", n,
                                sd, st, fmt_hms(tracked),
                                fmt_dur(est) if est else "", dtxt])
            self.accept()
        except OSError as e:
            QMessageBox.warning(self, "Export failed", str(e))

    def _refresh_top_items(self, items: list[Task]):
        agg: dict[str, dict] = {}
        for t in items:
            a = agg.setdefault(t.text, {"secs": 0.0, "est": 0.0, "days": set()})
            a["secs"] += t.current_seconds()
            a["est"] += t.estimated_seconds
            a["days"].add(t.created[:10])
        top = sorted(agg.items(), key=lambda kv: -kv[1]["secs"])[:25]

        total_secs = sum(a["secs"] for a in agg.values())
        self.tasks_summary.setText(
            f"{len(agg)} unique items in period • total {fmt_hms(total_secs)}"
            + ("  (per-item totals, all nesting levels)" if total_secs else ""))

        self.task_table.setRowCount(len(top))
        for r, (name, a) in enumerate(top):
            dtxt, color = self._diff_text(a["secs"], a["est"])
            cells = [name, fmt_hms(a["secs"]),
                     fmt_dur(a["est"]) if a["est"] else "–",
                     dtxt, str(len(a["days"]))]
            for c, txt in enumerate(cells):
                it = QTableWidgetItem(txt)
                it.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
                if c > 0:
                    it.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                        | Qt.AlignmentFlag.AlignVCenter)
                if c == 3:
                    it.setForeground(QBrush(QColor(color)))
                self.task_table.setItem(r, c, it)

    def _on_mode(self, text: str):
        self.mode = text.lower()
        if self.mode == "month":
            self.anchor = self.anchor.replace(day=1)
        self.refresh()

    def _nav(self, step: int):
        if self.mode == "week":
            self.anchor += timedelta(days=7 * step)
        else:
            self.anchor = shift_month(self.anchor, step)
        self.refresh()

    def _go_current(self):
        self.anchor = date.today()
        self.refresh()


# ---------------------------------------------------------------- templates dialog
class TemplatesDialog(QDialog):
    def __init__(self, data: dict, parent=None):
        super().__init__(parent)
        self.data = data
        self.setWindowTitle("Templates")
        self.resize(420, 380)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)
        root.addWidget(QLabel("Save a day's list (including subtasks and tags) "
                              "as a reusable routine, then apply it to any date."))
        self.list_w = QListWidget()
        root.addWidget(self.list_w, 1)

        btns = QHBoxLayout()
        save_b = QPushButton("💾 Save current day…")
        apply_b = QPushButton("Apply to current day")
        apply_b.setObjectName("addBtn")
        del_b = QPushButton("Delete")
        close_b = QPushButton("Close")
        for b in (save_b, del_b, close_b):
            btns.addWidget(b)
        btns.addStretch()
        btns.addWidget(apply_b)
        root.addLayout(btns)

        save_b.clicked.connect(self._save)
        apply_b.clicked.connect(self._apply)
        del_b.clicked.connect(self._delete)
        close_b.clicked.connect(self.accept)
        self.list_w.itemDoubleClicked.connect(lambda _: self._apply())
        self._reload()

    def _templates(self) -> dict:
        return self.data.get(TEMPLATE_KEY, {})

    def _reload(self):
        self.list_w.clear()
        for name in sorted(self._templates()):
            n = len(self._templates()[name])
            self.list_w.addItem(f"{name}   ({n} tasks)")
        if self.list_w.count():
            self.list_w.setCurrentRow(0)

    def _selected_name(self) -> Optional[str]:
        it = self.list_w.currentItem()
        if not it:
            return None
        return it.text().rsplit("   (", 1)[0]

    def _save(self):
        main = self.parent()
        rows = getattr(main, "rows", [])
        if not rows:
            QMessageBox.information(self, "Save template",
                                    "The current day has no tasks to save.")
            return
        name, ok = QInputDialog.getText(self, "Save template",
                                        "Template name:", text="")
        name = name.strip()
        if not ok or not name:
            return
        if name in self._templates():
            if QMessageBox.question(
                    self, "Overwrite",
                    f"Template \"{name}\" exists. Overwrite?") \
                    != QMessageBox.StandardButton.Yes:
                return
        self.data.setdefault(TEMPLATE_KEY, {})[name] = [
            r.item.to_dict() for r in rows]
        if hasattr(main, "_write_data"):
            main._write_data()
        self._reload()

    def _apply(self):
        name = self._selected_name()
        if not name:
            return
        main = self.parent()
        if hasattr(main, "_apply_template"):
            main._apply_template(name)
        self.accept()

    def _delete(self):
        name = self._selected_name()
        if not name:
            return
        if QMessageBox.question(self, "Delete template",
                                f"Delete template \"{name}\"?") \
                != QMessageBox.StandardButton.Yes:
            return
        self.data.get(TEMPLATE_KEY, {}).pop(name, None)
        if hasattr(self.parent(), "_write_data"):
            self.parent()._write_data()
        self._reload()


# ---------------------------------------------------------------- backup restore
class BackupDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Restore backup")
        self.resize(520, 420)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)
        root.addWidget(QLabel(
            "Backups are taken at every launch (last 10 kept).\n"
            "Restoring replaces ALL current data (incl. presets & tag colors)."))

        self.list_w = QListWidget()
        root.addWidget(self.list_w, 1)

        self.preview = QLabel("")
        self.preview.setObjectName("statsSummary")
        self.preview.setWordWrap(True)
        root.addWidget(self.preview)

        btns = QHBoxLayout()
        close_b = QPushButton("Close")
        restore_b = QPushButton("↺ Restore selected")
        restore_b.setObjectName("addBtn")
        btns.addStretch()
        btns.addWidget(close_b)
        btns.addWidget(restore_b)
        root.addLayout(btns)

        close_b.clicked.connect(self.reject)
        restore_b.clicked.connect(self._restore)
        self.list_w.currentItemChanged.connect(lambda *_: self._update_preview())

        self._files = sorted(BACKUP_DIR.glob("data_*.json"), reverse=True)
        for f in self._files:
            stamp = f.stem.replace("data_", "")
            try:
                dt = datetime.strptime(stamp, "%Y%m%d_%H%M%S")
                label = dt.strftime("%a, %b %d — %H:%M")
            except ValueError:
                label = stamp
            self.list_w.addItem(label)
        if self.list_w.count():
            self.list_w.setCurrentRow(0)

    def _selected_path(self) -> Optional[Path]:
        it = self.list_w.currentItem()
        if not it:
            return None
        return self._files[self.list_w.row(it)]

    def _update_preview(self):
        path = self._selected_path()
        if not path:
            self.preview.setText("")
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            days = [k for k in data if not k.startswith("__")]
            n_top = n_sub = 0
            secs = 0.0
            for k in days:
                for td in data.get(k, []):
                    if not isinstance(td, dict):
                        continue
                    t = Task.from_dict(td)
                    all_items = list(t.iter_tree())
                    n_top += 1
                    n_sub += len(all_items) - 1
                    secs += sum(i.current_seconds() for i in all_items)
            latest = max(days) if days else "–"
            subtxt = f" • {n_sub} subtasks" if n_sub else ""
            self.preview.setText(
                f"{len(days)} days • {n_top} tasks{subtxt} • "
                f"{fmt_hms(secs)} tracked • latest day {latest}")
        except (OSError, json.JSONDecodeError):
            self.preview.setText("⚠ Could not read this backup file.")

    def _restore(self):
        path = self._selected_path()
        if path and hasattr(self.parent(), "_restore_backup"):
            self.parent()._restore_backup(path)


# ---------------------------------------------------------------- main window
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("DailyChecklist", "DailyChecklist")
        self.setWindowTitle("Daily Checklist")
        self.resize(980, 700)
        self.setMinimumSize(760, 520)
        geo = self.settings.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)

        self._goal_hours = float(self.settings.value("goal_hours", 0.0) or 0.0)
        self._idle_min = int(self.settings.value("idle_pause_min", 5) or 0)
        self._last_activity = time.monotonic()
        self._idle_paused = False

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(SAVE_DELAY_MS)
        self._save_timer.timeout.connect(self._write_data)

        self.data = self._read_data()
        self._sync_tag_overrides()
        self._backup_data()
        self.rows: list[ItemRow] = []
        self.current_date = date.today()
        self._known_today = date.today()
        self._day_loaded = False
        self._undo_stack: list[tuple[str, str, list[dict]]] = []
        self._redo_stack: list[tuple[str, str, list[dict]]] = []
        self._filter_text = ""
        self._hide_done = False
        self._selected_row: Optional[ItemRow] = None
        self._selected_rows: list[ItemRow] = []
        self._sel_anchor: Optional[ItemRow] = None
        self._title_running = False
        self._expansion_memory: dict[str, bool] = {}
        self._prefilter_state: Optional[tuple[str, dict[str, bool]]] = None
        self.tray: Optional[QSystemTrayIcon] = None

        self._build_ui()
        self._build_sidebar()
        self._init_tray()

        start = self.current_date
        try:
            last = self.settings.value("last_date", "")
            if last:
                start = date.fromisoformat(str(last))
        except (ValueError, TypeError):
            pass
        self._show_day(start)

        QApplication.instance().installEventFilter(self)

        self._tick = QTimer(self)
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self._on_tick)
        self._tick.start()

    # -- tree traversal --------------------------------------------------------
    def iter_rows(self, rows=None):
        if rows is None:
            rows = self.rows
        for r in rows:
            yield r
            yield from self.iter_rows(r.child_rows)

    def _find_row(self, row_id: int) -> Optional[ItemRow]:
        for r in self.iter_rows():
            if id(r) == row_id:
                return r
        return None

    def _subtree_height(self, row: ItemRow) -> int:
        if not row.child_rows:
            return 1
        return 1 + max(self._subtree_height(c) for c in row.child_rows)

    def _running_row(self) -> Optional[ItemRow]:
        return next((r for r in self.iter_rows() if r.item.is_running), None)

    # -- UI ----------------------------------------------------------------
    def _build_ui(self):
        central = QWidget()
        central.setObjectName("central")
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 10, 14, 12)
        root.setSpacing(8)

        mb = self.menuBar()
        m_file = mb.addMenu("&File")
        self.act_export = QAction("⬇  Export CSV…", self)
        self.act_export.triggered.connect(self._export_csv)
        self.act_paste = QAction("📋  Paste task list from clipboard", self)
        self.act_paste.setShortcut("Ctrl+Shift+V")
        self.act_paste.triggered.connect(self._paste_tasks)
        self.act_folder = QAction("📁  Open data folder", self)
        self.act_folder.triggered.connect(self._open_data_folder)
        self.act_restore = QAction("↺  Restore backup…", self)
        self.act_restore.triggered.connect(self._open_backups)
        act_quit = QAction("✕  Quit", self)
        act_quit.triggered.connect(self.close)
        for a in (self.act_export, self.act_paste, None,
                  self.act_folder, self.act_restore, None, act_quit):
            m_file.addAction(a) if a else m_file.addSeparator()

        m_view = mb.addMenu("&View")
        self.act_theme = QAction("🌙  Toggle dark / light theme", self)
        self.act_theme.triggered.connect(self._toggle_theme)
        self.act_today = QAction("↯  Jump to today", self)
        self.act_today.triggered.connect(lambda: self._show_day(date.today()))
        self.act_prev_day = QAction("◀  Previous day", self)
        self.act_prev_day.triggered.connect(lambda: self._shift_day(-1))
        self.act_next_day = QAction("▶  Next day", self)
        self.act_next_day.triggered.connect(lambda: self._shift_day(1))
        self._sidebar_visible = self.settings.value("sidebar", False, type=bool)
        self.act_sidebar = QAction("★  Tags & presets sidebar", self)
        self.act_sidebar.setCheckable(True)
        self.act_sidebar.setChecked(self._sidebar_visible)
        self.act_sidebar.setShortcut("Ctrl+B")
        self.act_sidebar.toggled.connect(self._toggle_sidebar)
        self.act_autosort = QAction("↧  Auto-move done tasks to bottom", self)
        self.act_autosort.setCheckable(True)
        self.act_autosort.setChecked(self.settings.value("autosort", True, type=bool))
        self.act_autosort.toggled.connect(self._on_autosort_toggled)
        self.act_hide_done = QAction("👁  Hide completed tasks", self)
        self.act_hide_done.setCheckable(True)
        self.act_hide_done.toggled.connect(self._on_hide_done)
        self.act_autocomplete = QAction(
            "☑  Auto-complete parent when all subtasks done", self)
        self.act_autocomplete.setCheckable(True)
        self.act_autocomplete.setChecked(
            self.settings.value("autocomplete_parent", True, type=bool))
        self.act_autocomplete.toggled.connect(self._on_autocomplete_toggled)
        self.act_remember_exp = QAction(
            "🧠 Remember expand/collapse between days (this session)", self)
        self.act_remember_exp.setCheckable(True)
        self.act_remember_exp.setChecked(
            self.settings.value("remember_expansion", False, type=bool))
        self.act_remember_exp.toggled.connect(self._on_remember_expansion)
        self.act_expand_all = QAction("▾  Expand all subtasks", self)
        self.act_expand_all.triggered.connect(lambda: self._set_all_expanded(True))
        self.act_collapse_all = QAction("▸  Collapse all subtasks", self)
        self.act_collapse_all.triggered.connect(lambda: self._set_all_expanded(False))
        for a in (self.act_theme, None, self.act_today, self.act_prev_day,
                  self.act_next_day, None, self.act_sidebar, self.act_autosort,
                  self.act_hide_done, self.act_autocomplete,
                  self.act_remember_exp, None,
                  self.act_expand_all, self.act_collapse_all):
            m_view.addAction(a) if a else m_view.addSeparator()

        m_tasks = mb.addMenu("&Tasks")
        act_add = QAction("＋  New task", self)
        act_add.triggered.connect(self._focus_input)
        act_carry = QAction("↩  Carry repeating tasks…", self)
        act_carry.triggered.connect(
            lambda: self._carry_over(self.current_date, quiet=False))
        act_prev_copy = QAction("⧉  Copy tasks from previous day", self)
        act_prev_copy.triggered.connect(self._copy_from_previous_day)
        act_goal = QAction("🎯  Set daily time goal…", self)
        act_goal.triggered.connect(self._set_goal)
        act_clear = QAction("🧹  Clear completed…", self)
        act_clear.triggered.connect(self._clear_completed)
        m_tasks.addAction(act_add)
        m_tasks.addAction(act_carry)
        m_tasks.addAction(act_prev_copy)
        m_tasks.addSeparator()
        m_tasks.addAction(act_goal)
        m_tasks.addMenu(self._build_idle_menu())
        m_tasks.addSeparator()
        m_tasks.addAction(act_clear)

        m_help = mb.addMenu("&Help")
        act_help = QAction("⌨  Keyboard shortcuts", self)
        act_help.triggered.connect(self._show_help)
        act_about = QAction("ℹ  About", self)
        act_about.triggered.connect(self._show_about)
        m_help.addAction(act_help)
        m_help.addAction(act_about)

        # ================= header card =================
        header = QFrame()
        header.setObjectName("header")
        hv = QVBoxLayout(header)
        hv.setContentsMargins(12, 8, 12, 8)
        hv.setSpacing(5)

        def vsep():
            s = QFrame()
            s.setObjectName("vsep")
            s.setFixedSize(1, 20)
            return s

        def tool(text: str, tip: str, cb) -> QPushButton:
            b = QPushButton(text)
            b.setFixedSize(34, 28)
            b.setToolTip(tip)
            b.clicked.connect(cb)
            return b

        h1 = QHBoxLayout()
        h1.setSpacing(5)
        self.prev_btn = QPushButton("◀")
        self.prev_btn.setFixedSize(34, 28)
        self.prev_btn.setToolTip("Previous day  (Alt+←)")
        self.next_btn = QPushButton("▶")
        self.next_btn.setFixedSize(34, 28)
        self.next_btn.setToolTip("Next day  (Alt+→)")
        self.date_edit = QDateEdit()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        self.date_edit.setKeyboardTracking(False)
        self.today_btn = QPushButton("Today")
        self.today_btn.setToolTip("Jump to today  (Ctrl+T)")
        h1.addWidget(self.prev_btn)
        h1.addWidget(self.date_edit)
        h1.addWidget(self.next_btn)
        h1.addWidget(self.today_btn)
        h1.addSpacing(6)
        h1.addWidget(vsep())
        h1.addSpacing(4)
        self.undo_btn = QPushButton("↶")
        self.undo_btn.setFixedSize(34, 28)
        self.undo_btn.setToolTip("Undo  (Ctrl+Z)")
        self.undo_btn.setEnabled(False)
        self.redo_btn = QPushButton("↷")
        self.redo_btn.setFixedSize(34, 28)
        self.redo_btn.setToolTip("Redo  (Ctrl+Shift+Z)")
        self.redo_btn.setEnabled(False)
        h1.addWidget(self.undo_btn)
        h1.addWidget(self.redo_btn)
        h1.addStretch()

        # compact icon tools (tooltips explain each)
        self.theme_btn = tool("🌙", "Toggle light / dark theme", self._toggle_theme)
        self.stats_btn = tool("📊", "Statistics  (Ctrl+D)", self._open_stats)
        self.templates_btn = tool("📝", "Templates — save/apply routines",
                                  self._open_templates)
        self.carry_btn = tool("↩", "Carry repeating tasks into this day",
                              lambda: self._carry_over(self.current_date,
                                                       quiet=False))
        self.export_btn = tool("⬇", "Export this day as CSV", self._export_csv)
        self.clear_btn = tool("🧹", "Clear completed tasks",
                              self._clear_completed)
        for b in (self.theme_btn, self.stats_btn, self.templates_btn,
                  self.carry_btn, self.export_btn, self.clear_btn):
            h1.addWidget(b)
        hv.addLayout(h1)

        h2 = QHBoxLayout()
        h2.setSpacing(10)
        self.day_title = QLabel("")
        self.day_title.setObjectName("dayTitle")
        self.summary_lbl = QLabel("")
        self.summary_lbl.setObjectName("summaryLbl")
        h2.addWidget(self.day_title)
        h2.addStretch()
        h2.addWidget(self.summary_lbl)
        hv.addLayout(h2)

        self.progress = QProgressBar()
        self.progress.setFixedHeight(7)
        self.progress.setTextVisible(False)
        hv.addWidget(self.progress)

        self.goal_bar = QProgressBar()
        self.goal_bar.setObjectName("goalBar")
        self.goal_bar.setFixedHeight(5)
        self.goal_bar.setTextVisible(False)
        self.goal_bar.hide()
        hv.addWidget(self.goal_bar)
        root.addWidget(header)

        # ================= now-tracking banner =================
        self.now_bar = QFrame()
        self.now_bar.setObjectName("nowBar")
        nb = QHBoxLayout(self.now_bar)
        nb.setContentsMargins(14, 7, 10, 7)
        nb.setSpacing(10)
        self.now_icon = QLabel("▶")
        self.now_icon.setStyleSheet("color:" + _current_theme["GREEN_TXT"] +
                                    "; font-weight:700;")
        self.now_path = ClickableLabel("")
        self.now_path.setObjectName("nowPath")
        self.now_path.setToolTip("Click to scroll to the running task")
        self.now_path.clicked.connect(self._locate_running)
        self.now_time = QLabel("0:00")
        self.now_time.setObjectName("nowTime")
        self.now_pause = QPushButton("⏸ Pause")
        self.now_pause.setObjectName("nowPause")
        self.now_pause.setCursor(Qt.CursorShape.PointingHandCursor)
        self.now_pause.clicked.connect(self._pause_running_timer)
        nb.addWidget(self.now_icon)
        nb.addWidget(self.now_path, 1)
        nb.addWidget(self.now_time)
        nb.addWidget(self.now_pause)
        self.now_bar.hide()
        root.addWidget(self.now_bar)

        # ================= combined add / filter row =================
        inputbar = QHBoxLayout()
        inputbar.setSpacing(6)
        self.input = QLineEdit()
        self.input.setPlaceholderText(
            "＋ Add a task…  e.g.  Write report #work #deep ~1h30   (Enter)")
        self.add_btn = QPushButton("Add")
        self.add_btn.setObjectName("addBtn")
        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText(
            "🔍 Filter…  text / #tags   (Ctrl+F)")
        self.filter_input.setClearButtonEnabled(True)
        self.filter_input.textChanged.connect(self._on_filter_changed)
        self.preset_btn = QPushButton("★")
        self.preset_btn.setFixedSize(34, 30)
        self.preset_btn.setToolTip("Tags & presets sidebar  (Ctrl+B)")
        self.preset_btn.clicked.connect(self._toggle_sidebar_from_button)
        self.hide_done_cb = QCheckBox("Hide done")
        self.hide_done_cb.toggled.connect(self._on_hide_done)
        self.match_lbl = QLabel("")
        self.match_lbl.setObjectName("summaryLbl")
        inputbar.addWidget(self.input, 3)
        inputbar.addWidget(self.add_btn)
        inputbar.addSpacing(6)
        inputbar.addWidget(self.filter_input, 2)
        inputbar.addWidget(self.preset_btn)
        inputbar.addWidget(self.hide_done_cb)
        inputbar.addWidget(self.match_lbl)
        root.addLayout(inputbar)

        # ================= bulk selection bar =================
        self.bulk_bar = QFrame()
        self.bulk_bar.setObjectName("bulkBar")
        bb = QHBoxLayout(self.bulk_bar)
        bb.setContentsMargins(12, 6, 10, 6)
        bb.setSpacing(6)
        self.bulk_lbl = QLabel("0 selected")
        self.bulk_lbl.setObjectName("bulkLbl")
        bb.addWidget(self.bulk_lbl)
        bb.addStretch()
        self.bulk_done_btn = QPushButton("✓ Done")
        self.bulk_undone_btn = QPushButton("↺ Undone")
        self.bulk_tags_btn = QPushButton("🏷 Tags…")
        self.bulk_del_btn = QPushButton("🗑 Delete")
        self.bulk_del_btn.setObjectName("dangerBtn")
        self.bulk_clear_btn = QPushButton("✕")
        self.bulk_clear_btn.setFixedWidth(30)
        self.bulk_clear_btn.setToolTip("Clear selection  (Esc)")
        for b in (self.bulk_done_btn, self.bulk_undone_btn, self.bulk_tags_btn,
                  self.bulk_del_btn, self.bulk_clear_btn):
            bb.addWidget(b)
        self.bulk_bar.hide()
        root.addWidget(self.bulk_bar)

        # ================= task list =================
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        container = CenteredList()
        self.rows_lay = QVBoxLayout()
        self.rows_lay.setSpacing(6)
        container.addLayout(self.rows_lay)
        self.empty_lbl = QLabel("Nothing planned yet — add your first task above ✨")
        self.empty_lbl.setObjectName("emptyLbl")
        self.empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        container.addWidget(self.empty_lbl)
        self.nomatch_lbl = QLabel("No tasks match your filter")
        self.nomatch_lbl.setObjectName("emptyLbl")
        self.nomatch_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.nomatch_lbl.hide()
        container.addWidget(self.nomatch_lbl)
        container.addStretch()
        self.scroll.setWidget(container)
        root.addWidget(self.scroll, 1)

        self.setCentralWidget(central)
        idle_txt = (f"idle auto-pause: {self._idle_min} min"
                    if self._idle_min else "idle auto-pause: off")
        self.statusBar().showMessage(
            f"Ctrl+click multi-select • Ctrl+A select all • {idle_txt} • F1 help")

        self.prev_btn.clicked.connect(lambda: self._shift_day(-1))
        self.next_btn.clicked.connect(lambda: self._shift_day(1))
        self.today_btn.clicked.connect(lambda: self._show_day(date.today()))
        self.date_edit.dateChanged.connect(
            lambda qd: self._show_day(date(qd.year(), qd.month(), qd.day())))
        self.input.returnPressed.connect(self._add_task)
        self.add_btn.clicked.connect(self._add_task)
        self.undo_btn.clicked.connect(self._undo)
        self.redo_btn.clicked.connect(self._redo)

        self.bulk_done_btn.clicked.connect(lambda: self._bulk_set_done(True))
        self.bulk_undone_btn.clicked.connect(lambda: self._bulk_set_done(False))
        self.bulk_tags_btn.clicked.connect(self._bulk_tags)
        self.bulk_del_btn.clicked.connect(self._bulk_delete)
        self.bulk_clear_btn.clicked.connect(self._clear_selection)

        QShortcut(QKeySequence("Ctrl+N"), self).activated.connect(self._focus_input)
        QShortcut(QKeySequence("Alt+Left"), self).activated.connect(
            lambda: self._shift_day(-1))
        QShortcut(QKeySequence("Alt+Right"), self).activated.connect(
            lambda: self._shift_day(1))
        QShortcut(QKeySequence("Ctrl+T"), self).activated.connect(
            lambda: self._show_day(date.today()))
        QShortcut(QKeySequence("Ctrl+D"), self).activated.connect(self._open_stats)
        QShortcut(QKeySequence("Ctrl+F"), self).activated.connect(
            lambda: self.filter_input.setFocus())
        QShortcut(QKeySequence("Ctrl+Z"), self).activated.connect(self._undo)
        QShortcut(QKeySequence("Ctrl+Shift+Z"), self).activated.connect(self._redo)
        QShortcut(QKeySequence("Ctrl+Y"), self).activated.connect(self._redo)
        QShortcut(QKeySequence("F1"), self).activated.connect(self._show_help)

    def _build_idle_menu(self) -> QMenu:
        m = QMenu("⏸  Idle auto-pause ▸", self)
        grp = QActionGroup(self)
        grp.setExclusive(True)
        cur = self._idle_min
        for label, mins in (("Off", 0), ("1 min", 1), ("2 min", 2),
                            ("5 min", 5), ("10 min", 10), ("15 min", 15)):
            a = m.addAction(label)
            a.setCheckable(True)
            a.setChecked(cur == mins)
            a.triggered.connect(lambda _=False, mm=mins: self._set_idle_pause(mm))
            grp.addAction(a)
        return m

    def _locate_running(self):
        r = self._running_row()
        if r is not None:
            self.scroll.ensureWidgetVisible(r, 0, 0)
            self._select_only(r)

    # -- notifications ---------------------------------------------------------
    def _notify(self, title: str, body: str, msecs: int = 6000):
        self.statusBar().showMessage(f"{title} — {body}", msecs + 2000)
        if self.tray:
            self.tray.showMessage(title, body,
                                  QSystemTrayIcon.MessageIcon.Information, msecs)

    # -- idle detection ----------------------------------------------------------
    def _set_idle_pause(self, minutes: int):
        self._idle_min = minutes
        self.settings.setValue("idle_pause_min", minutes)
        msg = ("Timers keep running while you're away"
               if minutes == 0 else
               f"Running timers pause after {minutes} min without activity")
        self.statusBar().showMessage(f"⏸ Idle auto-pause: {msg}", 6000)

    def _check_idle(self, running: list[ItemRow]):
        if self._idle_paused or not running or self._idle_min <= 0:
            return
        idle_secs = time.monotonic() - self._last_activity
        if idle_secs >= self._idle_min * 60:
            self._idle_paused = True
            r = running[0]
            r.stop_timer()
            self._notify(
                "⏸ Timer paused — idle",
                f"\"{r.item.text[:40]}\" paused after {self._idle_min} min "
                "without activity")

    # -- daily goal -------------------------------------------------------------
    def _set_goal(self):
        val, ok = QInputDialog.getDouble(
            self, "Daily time goal",
            "Hours of tracked time to aim for each day\n(0 = no goal):",
            self._goal_hours, 0.0, 24.0, 1, 0.5)
        if not ok:
            return
        self._goal_hours = float(val)
        self.settings.setValue("goal_hours", self._goal_hours)
        self._refresh_summary()
        msg = (f"Daily goal set to {fmt_dur(self._goal_hours * 3600)}"
               if self._goal_hours > 0 else "Daily goal turned off")
        self.statusBar().showMessage("🎯 " + msg, 5000)

    # -- sidebar: tags + presets ---------------------------------------------------
    def _build_sidebar(self):
        self.side_dock = QDockWidget("★ Tags & presets", self)
        self.side_dock.setObjectName("presetDock")
        self.side_dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea)

        panel = QWidget()
        v = QVBoxLayout(panel)
        v.setContentsMargins(8, 6, 8, 8)
        v.setSpacing(6)

        hdr = QHBoxLayout()
        title = QLabel("🏷 Tags · ★ Presets")
        title.setObjectName("sideTitle")
        plus_b = QPushButton("＋")
        plus_b.setFixedWidth(28)
        plus_b.setToolTip("Save the current filter as a preset")
        manage_b = QPushButton("🗂")
        manage_b.setFixedWidth(28)
        manage_b.setToolTip("Manage presets (rename / delete)")
        hdr.addWidget(title)
        hdr.addStretch()
        hdr.addWidget(plus_b)
        hdr.addWidget(manage_b)
        v.addLayout(hdr)

        self.side_list = QListWidget()
        self.side_list.setToolTip("Click a tag or preset to filter • "
                                  "right-click for more")
        self.side_list.itemClicked.connect(self._sidebar_apply)
        self.side_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.side_list.customContextMenuRequested.connect(self._sidebar_menu)
        v.addWidget(self.side_list, 1)

        hint = QLabel("Tags are auto-collected. Type a filter above, then "
                      "press ＋ to save it as a preset.")
        hint.setObjectName("summaryLbl")
        hint.setWordWrap(True)
        v.addWidget(hint)

        self.side_dock.setWidget(panel)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.side_dock)
        self.side_dock.setVisible(self._sidebar_visible)
        self.side_dock.visibilityChanged.connect(self._on_sidebar_visibility)

        plus_b.clicked.connect(self._save_preset)
        manage_b.clicked.connect(self._manage_presets)

    def _sidebar_header_item(self, text: str) -> QListWidgetItem:
        it = QListWidgetItem(text)
        it.setFlags(Qt.ItemFlag.ItemIsEnabled)          # not selectable
        f = it.font()
        f.setBold(True)
        it.setFont(f)
        it.setForeground(QColor(_current_theme["MUTED"]))
        return it

    def _toggle_sidebar(self, checked: bool):
        self.side_dock.setVisible(checked)

    def _toggle_sidebar_from_button(self):
        self.side_dock.setVisible(not self.side_dock.isVisible())

    def _on_sidebar_visibility(self, visible: bool):
        self.settings.setValue("sidebar", visible)
        if self.act_sidebar.isChecked() != visible:
            self.act_sidebar.blockSignals(True)
            self.act_sidebar.setChecked(visible)
            self.act_sidebar.blockSignals(False)

    def _sidebar_apply(self, item: QListWidgetItem):
        data = item.data(Qt.ItemDataRole.UserRole)
        if not data:
            return
        kind, val = data
        if kind == "tag":
            self._filter_by_tag(val)
        else:
            self._apply_preset(val)

    def _sidebar_menu(self, pos):
        m = QMenu(self)
        it = self.side_list.itemAt(pos)
        data = it.data(Qt.ItemDataRole.UserRole) if it else None
        if data:
            kind, val = data
            if kind == "tag":
                m.addAction(f"🔍  Filter by #{val}",
                            lambda: self._filter_by_tag(val))
                m.addAction("🎨  Edit color…",
                            lambda: self._edit_tag_color(val))
                if val in self._tag_color_overrides():
                    m.addAction("↺  Reset to auto color",
                                lambda: self._reset_tag_color(val))
            else:
                m.addAction("🔍  Apply", lambda: self._apply_preset(val))
                m.addSeparator()
                m.addAction("✏  Rename…",
                            lambda: self._rename_preset_interactive(val))
                m.addAction("🗑  Delete…",
                            lambda: self._delete_preset_interactive(val))
            m.addSeparator()
        m.addAction("＋  Save current filter…", self._save_preset)
        m.addAction("🗂  Manage presets…", self._manage_presets)
        m.exec(self.side_list.viewport().mapToGlobal(pos))

    def _rename_preset_interactive(self, name: str):
        new, ok = QInputDialog.getText(self, "Rename preset",
                                       "New name:", text=name)
        new = new.strip()
        if not ok or not new or new == name:
            return
        pres = self.data.setdefault(FILTER_KEY, {})
        if new in pres:
            QMessageBox.warning(self, "Rename", "A preset with that name exists.")
            return
        pres[new] = pres.pop(name)
        self._write_data()
        self._refresh_sidebar()

    def _delete_preset_interactive(self, name: str):
        if QMessageBox.question(self, "Delete preset",
                                f"Delete preset \"{name}\"?") \
                != QMessageBox.StandardButton.Yes:
            return
        self.data.get(FILTER_KEY, {}).pop(name, None)
        self._write_data()
        self._refresh_sidebar()

    def _refresh_sidebar(self):
        if not hasattr(self, "side_list"):
            return
        self.side_list.blockSignals(True)
        self.side_list.clear()

        tags = self._known_tags()
        self.side_list.addItem(self._sidebar_header_item("🏷  TAGS"))
        for t in tags[:30]:
            n = sum(1 for r in self.iter_rows() if t in r.item.tags)
            it = QListWidgetItem(f"#{t}    {n}")
            it.setData(Qt.ItemDataRole.UserRole, ("tag", t))
            it.setForeground(QColor(tag_colors(t)[2]))
            it.setToolTip(f"Filter by #{t} • right-click to edit its color")
            self.side_list.addItem(it)
        if not tags:
            e = QListWidgetItem("   (no tags yet)")
            e.setFlags(Qt.ItemFlag.ItemIsEnabled)
            e.setForeground(QColor(_current_theme["MUTED"]))
            self.side_list.addItem(e)

        self.side_list.addItem(self._sidebar_header_item("★  PRESETS"))
        pres = self._presets()
        cur_q = self.filter_input.text().strip().lower()
        for name in sorted(pres):
            q = pres[name].get("q", "")
            n = self._preset_count(q)
            it = QListWidgetItem(f"{name}    {n}")
            it.setData(Qt.ItemDataRole.UserRole, ("preset", name))
            it.setToolTip(f"{q}\n{n} match(es) on the day you're viewing")
            if q and q.strip().lower() == cur_q:
                f = it.font()
                f.setBold(True)
                it.setFont(f)
            self.side_list.addItem(it)
        if not pres:
            e = QListWidgetItem("   (no presets yet)")
            e.setFlags(Qt.ItemFlag.ItemIsEnabled)
            e.setForeground(QColor(_current_theme["MUTED"]))
            self.side_list.addItem(e)

        self.side_list.blockSignals(False)

    def _preset_count(self, q: str) -> int:
        toks = q.lower().split()
        tags = [t[1:] for t in toks if t.startswith("#") and len(t) > 1]
        free = " ".join(t for t in toks
                        if not (t.startswith("#") and len(t) > 1))
        n = 0
        for r in self.iter_rows():
            it = r.item
            if free and free not in it.text.lower():
                continue
            if any(t not in it.tags for t in tags):
                continue
            n += 1
        return n

    # -- tag colors ---------------------------------------------------------------
    def _sync_tag_overrides(self):
        global _tag_overrides
        _tag_overrides = dict(self.data.get(TAGCOLOR_KEY, {}))

    def _tag_color_overrides(self) -> dict:
        return self.data.get(TAGCOLOR_KEY, {})

    def _edit_tag_color(self, tag: str):
        cur = QColor(self._tag_color_overrides().get(tag)
                     or tag_colors(tag)[0])
        c = QColorDialog.getColor(cur, self, f"Pick a color for #{tag}")
        if not c.isValid():
            return
        self.data.setdefault(TAGCOLOR_KEY, {})[tag] = c.name()
        self._sync_tag_overrides()
        self._write_data()
        for r in self.iter_rows():
            r.refresh()
        self._refresh_sidebar()
        self.statusBar().showMessage(f"Custom color saved for #{tag}", 3000)

    def _reset_tag_color(self, tag: str):
        ov = self.data.get(TAGCOLOR_KEY, {})
        if tag not in ov:
            return
        del ov[tag]
        self._sync_tag_overrides()
        self._write_data()
        for r in self.iter_rows():
            r.refresh()
        self._refresh_sidebar()
        self.statusBar().showMessage(f"#{tag} is back to its auto color", 3000)

    def _apply_dropped_tag(self, row: ItemRow, tag: str):
        if tag in row.item.tags:
            self.statusBar().showMessage(
                f"\"{row.item.text[:30]}\" already has #{tag}", 3000)
            return
        self._push_undo(f"Add tag #{tag}")
        row.item.tags.append(tag)
        row.refresh()
        self._on_rows_changed()
        self.statusBar().showMessage(
            f"🏷 Tagged \"{row.item.text[:30]}\" with #{tag}", 4000)

    def _quick_toggle_tag(self, row: ItemRow, tag: str):
        pr = row.parent_row
        if pr is None:
            if row not in self.rows:
                return
        elif row not in pr.child_rows:
            return
        self._push_undo("Toggle tag")
        if tag in row.item.tags:
            row.item.tags.remove(tag)
            msg = f"Removed #{tag} from \"{row.item.text[:30]}\""
        else:
            row.item.tags.append(tag)
            msg = f"🏷 Tagged \"{row.item.text[:30]}\" with #{tag}"
        row.refresh()
        self._on_rows_changed()
        self.statusBar().showMessage(msg, 3000)

    def _copy_row_markdown(self, row: ItemRow):
        lines: list[str] = []

        def rec(t: Task, depth: int):
            mark = "x" if t.done else " "
            extra = []
            if t.estimated_seconds:
                extra.append("~" + fmt_dur(t.estimated_seconds))
            secs = t.current_seconds()
            if secs >= 1:
                extra.append(fmt_hms(secs))
            suffix = ("  `" + " · ".join(extra) + "`") if extra else ""
            tags = "".join(" #" + x for x in t.tags)
            lines.append("  " * depth + f"- [{mark}] {t.text}{tags}{suffix}")
            for c in t.children:
                rec(c, depth + 1)

        rec(row.item, 0)
        QApplication.clipboard().setText("\n".join(lines))
        self.statusBar().showMessage("Copied subtree as Markdown", 3000)

    # -- tray -----------------------------------------------------------------
    def _init_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(make_icon(), self)
        menu = QMenu()
        self._tray_toggle_act = QAction("Show / Hide window", self)
        self._tray_toggle_act.triggered.connect(self._toggle_window_visible)
        self._tray_pause_act = QAction("⏸  No timer running", self)
        self._tray_pause_act.setEnabled(False)
        self._tray_pause_act.triggered.connect(self._pause_running_timer)
        act_today = QAction("↯  Jump to today", self)
        act_today.triggered.connect(lambda: self._show_day(date.today()))
        act_quit = QAction("✕  Quit", self)
        act_quit.triggered.connect(self.close)
        for a in (self._tray_toggle_act, self._tray_pause_act, act_today,
                  None, act_quit):
            menu.addAction(a) if a else menu.addSeparator()
        self.tray.setContextMenu(menu)
        self.tray.setToolTip("Daily Checklist")
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._toggle_window_visible()

    def _toggle_window_visible(self):
        if self.isVisible() and not self.isMinimized():
            self.hide()
        else:
            self.showNormal()
            self.activateWindow()
            self._focus_input()

    def _pause_running_timer(self):
        for r in self.iter_rows():
            if r.item.is_running:
                r.timer_btn.setChecked(False)
                break

    # -- selection (single + multi) ---------------------------------------------
    def _sync_selection(self):
        sel = {id(r) for r in self._selected_rows}
        for r in self.iter_rows():
            want = id(r) in sel
            if bool(r.property("selected")) != want:
                r.setProperty("selected", want)
                polish(r)
        n = len(self._selected_rows)
        self.bulk_bar.setVisible(n > 1)
        self.bulk_lbl.setText(f"{n} selected")

    def _select_only(self, row: Optional[ItemRow]):
        self._selected_rows = [row] if row is not None else []
        self._selected_row = row
        self._sel_anchor = row
        self._sync_selection()

    def _set_selected(self, row: Optional[ItemRow]):
        self._select_only(row)

    def _clear_selection(self):
        self._selected_rows = []
        self._selected_row = None
        self._sel_anchor = None
        self._sync_selection()

    def _toggle_sel(self, row: ItemRow):
        if row in self._selected_rows:
            self._selected_rows.remove(row)
        else:
            self._selected_rows.append(row)
        self._selected_row = row
        self._sel_anchor = row
        self._sync_selection()

    def _range_sel(self, row: ItemRow):
        vis = [r for r in self.iter_rows() if r.isVisible()]
        if self._sel_anchor not in vis:
            self._toggle_sel(row)
            return
        i1, i2 = vis.index(self._sel_anchor), vis.index(row)
        lo, hi = min(i1, i2), max(i1, i2)
        self._selected_rows = vis[lo:hi + 1]
        self._selected_row = row
        self._sync_selection()

    def _effective_selection(self) -> list[ItemRow]:
        out = []
        for r in self._selected_rows:
            anc, covered = r.parent_row, False
            while anc is not None:
                if anc in self._selected_rows:
                    covered = True
                    break
                anc = anc.parent_row
            if not covered:
                out.append(r)
        return out

    def _forget_row(self, row: ItemRow):
        if row in self._selected_rows:
            self._selected_rows.remove(row)
        if self._sel_anchor is row:
            self._sel_anchor = None
        if self._selected_row is row:
            self._selected_row = None

    # -- expansion memory (session) ---------------------------------------------
    def _remember_expansion_enabled(self) -> bool:
        return self.settings.value("remember_expansion", False, type=bool)

    def _on_remember_expansion(self, checked: bool):
        self.settings.setValue("remember_expansion", checked)
        if checked:
            for r in self.iter_rows():
                self._expansion_memory[r.path_key()] = r.item.expanded
            self.statusBar().showMessage(
                "🧠 Expansion states are now remembered between days "
                "for this session", 6000)
        else:
            self._expansion_memory.clear()

    def _user_set_expansion(self, row: ItemRow, expanded: bool):
        row.set_expanded(expanded,
                         emit=not self._remember_expansion_enabled())
        self._record_expansion(row, expanded)

    def _record_expansion(self, row: ItemRow, state: bool):
        if self._remember_expansion_enabled():
            self._expansion_memory[row.path_key()] = state

    def _apply_expansion_memory(self):
        if not self._expansion_memory:
            return
        for r in self.iter_rows():
            st = self._expansion_memory.get(r.path_key())
            if st is not None and st != r.item.expanded:
                r.set_expanded(st, emit=False)

    # -- row factory --------------------------------------------------------
    def _create_row(self, task: Task, index: Optional[int] = None,
                    parent_row: Optional[ItemRow] = None,
                    register: bool = True) -> ItemRow:
        depth = 0 if parent_row is None else parent_row.depth + 1
        row = ItemRow(task, self, depth)
        row.parent_row = parent_row
        row.changed.connect(self._on_rows_changed)
        row.deleteRequested.connect(self._delete_row)
        row.duplicateRequested.connect(self._duplicate_row)
        row.moveRequested.connect(self._move_to_tomorrow)
        row.exclusiveStart.connect(self._enforce_single_timer)
        row.dropRequested.connect(self._move_row)
        row.estimateExceeded.connect(self._on_estimate_exceeded)
        row.doneToggled.connect(self._reposition_done_row)
        if parent_row is not None:
            row.doneToggled.connect(parent_row._on_child_done)

        if parent_row is None:
            sibs, lay = self.rows, self.rows_lay
        else:
            sibs, lay = parent_row.child_rows, parent_row.child_lay
            if register:
                if index is None or index >= len(sibs):
                    parent_row.item.children.append(task)
                else:
                    parent_row.item.children.insert(index, task)
        if index is None or index >= len(sibs):
            sibs.append(row)
            lay.addWidget(row)
        else:
            sibs.insert(index, row)
            lay.insertWidget(index, row)
        return row

    # -- detach / insert (drag & drop plumbing) -------------------------------
    def _detach_row(self, row: ItemRow):
        pr = row.parent_row
        if pr is None:
            if row in self.rows:
                self.rows.remove(row)
            self.rows_lay.removeWidget(row)
        else:
            if row in pr.child_rows:
                pr.child_rows.remove(row)
            pr.child_lay.removeWidget(row)
            if row.item in pr.item.children:
                pr.item.children.remove(row.item)
        row.parent_row = None

    def _insert_row(self, row: ItemRow, parent_row: Optional[ItemRow],
                    index: int, register: bool = True):
        row.parent_row = parent_row
        if parent_row is None:
            self.rows.insert(index, row)
            self.rows_lay.insertWidget(index, row)
        else:
            parent_row.child_rows.insert(index, row)
            parent_row.child_lay.insertWidget(index, row)
            if register:
                parent_row.item.children.insert(index, row.item)

    def _drop_allowed(self, src: ItemRow, dst: ItemRow, mode: str) -> bool:
        if src is dst:
            return False
        for r in self.iter_rows([src]):
            if r is dst:
                return False
        base = dst.depth + (1 if mode == "child" else 0)
        return base + self._subtree_height(src) <= MAX_DEPTH

    # -- day handling --------------------------------------------------------
    def _show_day(self, d: date, auto_carry: bool = True):
        if self._day_loaded:
            for r in self.iter_rows():
                if r.item.is_running:
                    r.stop_timer()
            self._flush_now()
        self._day_loaded = True
        self.current_date = d
        self._idle_paused = False

        self.date_edit.blockSignals(True)
        self.date_edit.setDate(QDate(d.year, d.month, d.day))
        self.date_edit.blockSignals(False)

        self._reload_rows()
        self.filter_input.blockSignals(True)
        self.filter_input.clear()
        self.filter_input.blockSignals(False)
        self._sync_selection()
        self._apply_filter()
        self._refresh_sidebar()

        if auto_carry and d == date.today() and not self.rows:
            self._carry_over(d, quiet=True)

        self.day_title.setText(d.strftime("%A, %B %d, %Y"))
        self.setWindowTitle(f"Daily Checklist — {d.isoformat()}")
        self._refresh_summary()
        self._focus_input()

    def _reload_rows(self):
        self._remove_rows(list(self.rows))
        self._selected_rows = []
        self._selected_row = None
        self._sel_anchor = None
        self._prefilter_state = None
        for td in self.data.get(self.current_date.isoformat(), []):
            if not isinstance(td, dict):
                continue
            self._build_tree(Task.from_dict(td), None)
        self._apply_expansion_memory()

    def _build_tree(self, item: Task, parent_row: Optional[ItemRow]):
        row = self._create_row(item, parent_row=parent_row, register=False)
        for c in item.children:
            self._build_tree(c, row)

    def _remove_rows(self, rows: list[ItemRow]):
        for r in rows:
            for rr in list(self.iter_rows([r])):
                if rr.item.is_running:
                    rr.stop_timer()
                self._forget_row(rr)
            self._detach_row(r)
            r.setParent(None)
            r.deleteLater()

    def _shift_day(self, days: int):
        self._show_day(self.current_date + timedelta(days=days))

    # -- undo / redo ------------------------------------------------------------
    def _push_undo(self, label: str):
        snap = (self.current_date.isoformat(), label,
                copy.deepcopy([r.item.to_dict() for r in self.rows]))
        self._undo_stack.append(snap)
        if len(self._undo_stack) > 50:
            self._undo_stack.pop(0)
        self._redo_stack.clear()
        self._update_undo_buttons()

    def _update_undo_buttons(self):
        self.undo_btn.setEnabled(bool(self._undo_stack))
        self.redo_btn.setEnabled(bool(self._redo_stack))

    def _restore_snapshot(self, entry: tuple[str, str, list[dict]]):
        date_iso, _label, tasks = entry
        if date_iso != self.current_date.isoformat():
            self._show_day(datetime.fromisoformat(date_iso).date(),
                           auto_carry=False)
        self.data[date_iso] = copy.deepcopy(tasks)
        self._reload_rows()
        self._on_rows_changed()

    def _undo(self):
        if not self._undo_stack:
            self.statusBar().showMessage("Nothing to undo", 3000)
            return
        date_iso, label, tasks = self._undo_stack.pop()
        self._redo_stack.append(
            (self.current_date.isoformat(), label,
             copy.deepcopy([r.item.to_dict() for r in self.rows])))
        self._restore_snapshot((date_iso, label, tasks))
        self._update_undo_buttons()
        self.statusBar().showMessage(f"Undid: {label}", 4000)

    def _redo(self):
        if not self._redo_stack:
            self.statusBar().showMessage("Nothing to redo", 3000)
            return
        date_iso, label, tasks = self._redo_stack.pop()
        self._undo_stack.append(
            (self.current_date.isoformat(), label,
             copy.deepcopy([r.item.to_dict() for r in self.rows])))
        self._restore_snapshot((date_iso, label, tasks))
        self._update_undo_buttons()
        self.statusBar().showMessage(f"Redid: {label}", 4000)

    # -- repeating tasks -------------------------------------------------------
    def _unfinished_repeating_before(self, target: date,
                                     limit_days: int = 60) -> list[Task]:
        seen: set[str] = set()
        out: list[Task] = []
        for i in range(1, limit_days + 1):
            d = target - timedelta(days=i)
            for td in self.data.get(d.isoformat(), []):
                if not isinstance(td, dict):
                    continue
                t = Task.from_dict(td)
                for it in t.iter_tree():
                    key = it.text.strip().lower()
                    if (it.repeat != "none" and not it.done
                            and it.repeats_on(target) and key not in seen):
                        seen.add(key)
                        out.append(it)
        out.reverse()
        return out

    def _carry_over(self, target: date, quiet: bool) -> int:
        found = self._unfinished_repeating_before(target)
        existing = {r.item.text.strip().lower() for r in self.rows}
        to_add = [t for t in found if t.text.strip().lower() not in existing]
        if not to_add:
            if not quiet:
                QMessageBox.information(
                    self, "Nothing to carry",
                    "No unfinished repeating items in the last 60 days.")
            return 0
        self._push_undo("Carry repeating items")
        for t in to_add:
            self._create_row(fresh_copy(t))
        self._on_rows_changed()
        self.statusBar().showMessage(
            f"Carried {len(to_add)} unfinished repeating item(s)", 6000)
        return len(to_add)

    # -- copy from previous day ---------------------------------------------------
    def _most_recent_day_before(self, target: date, limit: int = 14) -> Optional[date]:
        for i in range(1, limit + 1):
            d = target - timedelta(days=i)
            if any(isinstance(td, dict)
                   for td in self.data.get(d.isoformat(), [])):
                return d
        return None

    def _copy_from_previous_day(self):
        src = self._most_recent_day_before(self.current_date)
        if src is None:
            QMessageBox.information(
                self, "Copy from previous day",
                "No previous day with tasks found (searched 14 days back).")
            return
        existing = {r.item.text.strip().lower() for r in self.rows}
        to_add = []
        for td in self.data[src.isoformat()]:
            t = Task.from_dict(td)
            if t.text.strip().lower() not in existing:
                to_add.append(t)
                existing.add(t.text.strip().lower())
        if not to_add:
            QMessageBox.information(
                self, "Copy from previous day",
                f"Everything from {src.isoformat()} is already on this day.")
            return
        self._push_undo(f"Copy from {src.isoformat()}")
        for t in to_add:
            self._create_row(fresh_copy(t))
        self._on_rows_changed()
        self.statusBar().showMessage(
            f"⧉ Copied {len(to_add)} task(s) from {src.isoformat()} "
            "(fresh timers, unchecked)", 6000)

    # -- templates / backups ------------------------------------------------------
    def _open_templates(self):
        TemplatesDialog(self.data, self).exec()

    def _apply_template(self, name: str):
        tds = self.data.get(TEMPLATE_KEY, {}).get(name)
        if not tds:
            return
        self._push_undo(f"Apply template \"{name}\"")
        for td in tds:
            self._create_row(fresh_copy(Task.from_dict(td)))
        self._on_rows_changed()
        self.statusBar().showMessage(
            f"Applied template \"{name}\" ({len(tds)} tasks)", 5000)

    def _open_backups(self):
        BackupDialog(self).exec()

    def _restore_backup(self, path: Path):
        try:
            newdata = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(newdata, dict):
                raise ValueError("not a valid data file")
        except (OSError, json.JSONDecodeError, ValueError) as e:
            QMessageBox.warning(self, "Restore failed", f"Bad backup:\n{e}")
            return
        answer = QMessageBox.question(
            self, "Restore backup",
            f"Replace ALL current data with the backup\n\"{path.name}\"?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.data = newdata
        self._sync_tag_overrides()
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._update_undo_buttons()
        self._flush_now()
        self._show_day(date.today())
        self.statusBar().showMessage(f"Restored backup {path.name}", 6000)

    def _open_data_folder(self):
        try:
            APP_DIR.mkdir(parents=True, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(APP_DIR)))
        except OSError as e:
            QMessageBox.warning(self, "Daily Checklist", str(e))

    # -- filter presets ---------------------------------------------------------
    def _presets(self) -> dict:
        return self.data.get(FILTER_KEY, {})

    def _save_preset(self):
        q = self.filter_input.text().strip()
        if not q:
            QMessageBox.information(self, "Save preset",
                                    "Type a filter first, then save it.")
            return
        name, ok = QInputDialog.getText(
            self, "Save filter preset", "Preset name:", text="")
        name = name.strip()
        if not ok or not name:
            return
        if name in self._presets():
            if QMessageBox.question(
                    self, "Overwrite",
                    f"Preset \"{name}\" exists. Overwrite?") \
                    != QMessageBox.StandardButton.Yes:
                return
        self.data.setdefault(FILTER_KEY, {})[name] = {"q": q}
        self._write_data()
        self._refresh_sidebar()
        self.statusBar().showMessage(f"Saved preset \"{name}\"", 4000)

    def _apply_preset(self, name: str):
        q = self._presets().get(name, {}).get("q", "")
        self.filter_input.setText(q)
        self._refresh_sidebar()
        self.statusBar().showMessage(f"Applied preset \"{name}\"", 3000)

    def _manage_presets(self):
        PresetsDialog(self, self).exec()

    # -- tags ---------------------------------------------------------------
    def _known_tags(self) -> list[str]:
        counts: dict[str, int] = {}
        for r in self.iter_rows():
            for t in r.item.tags:
                counts[t] = counts.get(t, 0) + 1
        return sorted(counts, key=lambda t: -counts[t])

    def _filter_by_tag(self, tag: str):
        self.filter_input.setText("#" + tag)
        self.filter_input.setFocus()

    def _edit_tags(self, row: ItemRow):
        dlg = TagsDialog(row.item, self._known_tags(), self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        new = dlg.tags()
        if new == row.item.tags:
            return
        self._push_undo("Edit tags")
        row.item.tags = new
        row.refresh()
        self._on_rows_changed()
        self.statusBar().showMessage("Tags updated", 3000)

    # -- bulk operations ---------------------------------------------------------
    def _bulk_set_done(self, done: bool, rows: Optional[list[ItemRow]] = None):
        rows = rows if rows is not None else self._effective_selection()
        if not rows:
            return
        self._push_undo(f"Mark {len(rows)} done" if done
                        else f"Mark {len(rows)} undone")
        now = datetime.now().isoformat(timespec="seconds")
        for r in rows:
            r.item.done = done
            if done:
                r.item.completed_at = now
                if r.item.is_running:
                    r.stop_timer()
        groups: list[tuple[list[ItemRow], QVBoxLayout]] = []
        for r in rows:
            pr = r.parent_row
            g = (self.rows if pr is None else pr.child_rows,
                 self.rows_lay if pr is None else pr.child_lay)
            if g not in groups:
                groups.append(g)
        if self._autosort_enabled():
            for sibs, lay in groups:
                order = ([s for s in sibs if not s.item.done]
                         + [s for s in sibs if s.item.done])
                if order != list(sibs):
                    sibs[:] = order
                    for s in order:
                        lay.removeWidget(s)
                    for s in order:
                        lay.addWidget(s)
        for r in rows:
            r.refresh()
        self._on_rows_changed()
        self.statusBar().showMessage(
            f"{'Marked' if done else 'Unmarked'} {len(rows)} item(s)", 4000)

    def _bulk_delete(self):
        rows = self._effective_selection()
        if not rows:
            return
        answer = QMessageBox.question(
            self, "Delete selected",
            f"Delete {len(rows)} item(s) (and their subtasks)?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._push_undo("Bulk delete")
        self._remove_rows(rows)
        self._on_rows_changed()
        self.statusBar().showMessage(f"Deleted {len(rows)} item(s)", 4000)

    def _bulk_tags(self):
        rows = self._effective_selection()
        if not rows:
            return
        union: list[str] = []
        for r in rows:
            for t in r.item.tags:
                if t not in union:
                    union.append(t)
        dlg = BulkTagsDialog(len(rows), union, self._known_tags(), self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        add, remove = dlg.add_tags(), dlg.remove_tags()
        if not add and not remove:
            return
        self._push_undo("Bulk tags")
        changed = 0
        for r in rows:
            new = [t for t in r.item.tags if t not in remove]
            for t in add:
                if t not in new:
                    new.append(t)
            if new != r.item.tags:
                r.item.tags = new
                r.refresh()
                changed += 1
        if changed:
            self._on_rows_changed()
            self.statusBar().showMessage(
                f"Updated tags on {changed} item(s)", 4000)

    # -- move to date ---------------------------------------------------------
    def _move_to_date(self, row: ItemRow):
        dlg = MoveToDateDialog(self.current_date, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        target = dlg.selected_date()
        if target == self.current_date:
            self.statusBar().showMessage("Item is already on that date", 4000)
            return
        pr = row.parent_row
        if pr is None:
            if row not in self.rows:
                return
        elif row not in pr.child_rows:
            return
        self._push_undo("Move to date")
        if row.item.is_running:
            row.stop_timer()
        self.data.setdefault(target.isoformat(), []).insert(0, row.item.to_dict())
        self._remove_rows([row])
        if pr is not None:
            pr.refresh()
        self._on_rows_changed()
        self.statusBar().showMessage(
            f"Item moved to {target.isoformat()} — use the date picker to view it",
            6000)

    # -- subtask helpers ------------------------------------------------------
    def _auto_complete_parents(self) -> bool:
        return self.settings.value("autocomplete_parent", True, type=bool)

    def _on_autocomplete_toggled(self, checked: bool):
        self.settings.setValue("autocomplete_parent", checked)

    def _set_all_expanded(self, expanded: bool):
        for r in self.iter_rows():
            r.set_expanded(expanded, emit=False)
            self._record_expansion(r, expanded)
        self._on_rows_changed()

    # -- drag & drop reorder / re-nest --------------------------------------------
    def _move_row(self, src: ItemRow, dst: ItemRow, mode: str):
        if not self._drop_allowed(src, dst, mode):
            self.statusBar().showMessage(
                "Can't move an item into its own subtasks (or past max depth)",
                5000)
            return
        self._push_undo("Reorder / re-nest")
        self._detach_row(src)
        if mode == "child":
            self._insert_row(src, dst, len(dst.child_rows))
            dst.set_expanded(True, emit=False)
            self._record_expansion(dst, True)
            src.set_depth(dst.depth + 1)
        else:
            idx = (self.rows if dst.parent_row is None
                   else dst.parent_row.child_rows).index(dst) \
                + (0 if mode == "above" else 1)
            self._insert_row(src, dst.parent_row, idx)
            src.set_depth(dst.depth)
        self._on_rows_changed()

    # -- auto-sort done to bottom ---------------------------------------------------
    def _autosort_enabled(self) -> bool:
        return self.settings.value("autosort", True, type=bool)

    def _reposition_done_row(self, row: ItemRow):
        if not self._autosort_enabled():
            return
        pr = row.parent_row
        sibs = self.rows if pr is None else pr.child_rows
        lay = self.rows_lay if pr is None else pr.child_lay
        if row not in sibs:
            return
        idx = sibs.index(row)
        if row.item.done:
            if any(not s.item.done for s in sibs[idx + 1:]):
                sibs.remove(row)
                lay.removeWidget(row)
                sibs.append(row)
                lay.addWidget(row)
        else:
            first_done = next((s for s in sibs if s.item.done), None)
            if first_done is not None and sibs.index(first_done) < sibs.index(row):
                sibs.remove(row)
                lay.removeWidget(row)
                i = sibs.index(first_done)
                sibs.insert(i, row)
                lay.insertWidget(i, row)

    def _sort_done_to_bottom(self):
        self.rows = ([r for r in self.rows if not r.item.done]
                     + [r for r in self.rows if r.item.done])
        for r in self.rows:
            self.rows_lay.removeWidget(r)
        for r in self.rows:
            self.rows_lay.addWidget(r)
        self._apply_filter()

    def _on_autosort_toggled(self, checked: bool):
        self.settings.setValue("autosort", checked)
        if checked:
            self._sort_done_to_bottom()

    # -- filtering -------------------------------------------------------------------
    def _on_filter_changed(self, text: str):
        self._filter_text = text.strip().lower()
        if self._filter_text:
            if self._prefilter_state is None:
                self._prefilter_state = (
                    self.current_date.isoformat(),
                    {r.path_key(): r.item.expanded for r in self.iter_rows()})
        else:
            if self._prefilter_state \
                    and self._prefilter_state[0] == self.current_date.isoformat():
                m = self._prefilter_state[1]
                for r in self.iter_rows():
                    st = m.get(r.path_key())
                    if st is not None and st != r.item.expanded:
                        r.set_expanded(st, emit=False)
            self._prefilter_state = None
        self._apply_filter()
        self._refresh_sidebar()

    def _on_hide_done(self, checked: bool):
        self._hide_done = checked
        for w in (self.hide_done_cb, self.act_hide_done):
            if w.isChecked() != checked:
                w.blockSignals(True)
                w.setChecked(checked)
                w.blockSignals(False)
        self._apply_filter()

    def _filter_tokens(self) -> tuple[str, list[str]]:
        toks = self._filter_text.split()
        tags = [t[1:] for t in toks if t.startswith("#") and len(t) > 1]
        free = " ".join(t for t in toks
                        if not (t.startswith("#") and len(t) > 1))
        return free, tags

    def _filter_match(self, item: Task) -> bool:
        if self._hide_done and item.done:
            return False
        if not self._filter_text:
            return True
        free, tags = self._filter_tokens()
        if free and free not in item.text.lower():
            return False
        for t in tags:
            if t not in item.tags:
                return False
        return True

    def _apply_filter_tree(self, rows: list[ItemRow]) -> int:
        visible = 0
        for r in rows:
            kids = self._apply_filter_tree(r.child_rows)
            own = self._filter_match(r.item)
            vis = own or kids > 0
            r.setVisible(vis)
            if vis and kids > 0 and self._filter_text \
                    and not r.item.expanded:
                r.set_expanded(True, emit=False)
            visible += 1 if vis else 0
        return visible

    def _apply_filter(self):
        visible = self._apply_filter_tree(self.rows)
        total = sum(1 for _ in self.iter_rows())
        if total and visible == 0:
            self.empty_lbl.hide()
            self.nomatch_lbl.show()
        else:
            self.nomatch_lbl.hide()
            self.empty_lbl.setVisible(len(self.rows) == 0)
        if self._filter_text or self._hide_done:
            self.match_lbl.setText(f"{visible}/{total} shown")
        else:
            self.match_lbl.setText("")

    # -- keyboard / mouse navigation ------------------------------------------------
    def eventFilter(self, obj, event):
        et = event.type()
        if et in (QEvent.Type.MouseMove, QEvent.Type.MouseButtonPress,
                  QEvent.Type.KeyPress, QEvent.Type.Wheel):
            self._last_activity = time.monotonic()

        if et == QEvent.Type.FocusIn and isinstance(obj, ItemRow):
            self._set_selected(obj)
            return super().eventFilter(obj, event)

        if et == QEvent.Type.MouseButtonPress \
                and event.button() == Qt.MouseButton.LeftButton \
                and isinstance(obj, QWidget):
            w, row = obj, None
            while w is not None:
                if isinstance(w, ItemRow):
                    row = w
                    break
                w = w.parentWidget()
            if row is not None and row.window() is self \
                    and not isinstance(obj, (QCheckBox, QPushButton,
                                             QLineEdit, QComboBox,
                                             QListWidget, QTableWidget,
                                             QAbstractSpinBox)):
                mods = event.modifiers()
                if mods & Qt.KeyboardModifier.ControlModifier:
                    self._toggle_sel(row)
                    return True
                if mods & Qt.KeyboardModifier.ShiftModifier:
                    self._range_sel(row)
                    return True

        if et != QEvent.Type.KeyPress or not self.isActiveWindow():
            return super().eventFilter(obj, event)

        fw = QApplication.focusWidget()
        if fw is None or fw.window() is not self:
            return super().eventFilter(obj, event)
        if isinstance(fw, (QLineEdit, QAbstractSpinBox, QComboBox,
                           QListWidget, QTableWidget)):
            return super().eventFilter(obj, event)

        key, mods = event.key(), event.modifiers()
        target = fw if isinstance(fw, ItemRow) else self._selected_row

        if key in (Qt.Key.Key_Up, Qt.Key.Key_Down) \
                and mods == Qt.KeyboardModifier.NoModifier:
            self._move_selection(-1 if key == Qt.Key.Key_Up else 1)
            return True
        if key == Qt.Key.Key_A and (mods & Qt.KeyboardModifier.ControlModifier):
            vis = [r for r in self.iter_rows() if r.isVisible()]
            self._selected_rows = vis
            self._selected_row = vis[-1] if vis else None
            self._sel_anchor = None
            self._sync_selection()
            return True
        if key == Qt.Key.Key_Space:
            if mods == Qt.KeyboardModifier.NoModifier and target:
                sel = self._effective_selection()
                if len(sel) > 1:
                    self._bulk_set_done(any(not r.item.done for r in sel))
                else:
                    target.done_cb.setChecked(not target.item.done)
                return True
            if mods & Qt.KeyboardModifier.ControlModifier and target:
                target.timer_btn.setChecked(not target.item.is_running)
                return True
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) \
                and mods == Qt.KeyboardModifier.NoModifier and target:
            target._begin_edit()
            return True
        if key == Qt.Key.Key_Delete \
                and mods == Qt.KeyboardModifier.NoModifier and target:
            if len(self._selected_rows) > 1:
                self._bulk_delete()
            else:
                self._delete_row(target)
            return True
        if key == Qt.Key.Key_Escape:
            if self.filter_input.text():
                self.filter_input.clear()
            self._clear_selection()
            return True
        return super().eventFilter(obj, event)

    def _move_selection(self, step: int):
        vis = [r for r in self.iter_rows() if r.isVisible()]
        if not vis:
            return
        try:
            i = vis.index(self._selected_row)
        except ValueError:
            i = -1 if step > 0 else 0
        i = max(0, min(len(vis) - 1, i + step))
        self._set_selected(vis[i])

    # -- actions ---------------------------------------------------------------
    def _add_task(self):
        text = self.input.text().strip()
        if not text:
            return
        text, tags = extract_tags(text)
        text, est = parse_estimate(text)
        if not text:
            return
        self._create_row(Task(text=text, estimated_seconds=est, tags=tags))
        self.input.clear()
        self._on_rows_changed()

    def _paste_tasks(self):
        text = QApplication.clipboard().text()
        lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
        if not lines:
            self.statusBar().showMessage("Clipboard is empty", 3000)
            return
        self._push_undo("Paste task list")
        stack: list[tuple[int, ItemRow]] = []
        added = 0
        for raw in lines:
            indent = 0
            for ch in raw:
                if ch == " ":
                    indent += 1
                elif ch == "\t":
                    indent += 2
                else:
                    break
            body, tags = extract_tags(raw[indent:])
            body, est = parse_estimate(body)
            if not body:
                continue
            level = indent // 2
            while stack and stack[-1][0] >= level:
                stack.pop()
            parent = stack[-1][1] if stack else None
            row = self._create_row(Task(text=body, estimated_seconds=est,
                                        tags=tags),
                                   parent_row=parent)
            if parent is not None:
                self._user_set_expansion(parent, True)
                parent.refresh()
            stack.append((level, row))
            added += 1
        if added:
            self._on_rows_changed()
            self.statusBar().showMessage(
                f"Added {added} item(s) from clipboard "
                "(indentation preserved as nesting, #tags picked up)", 5000)

    def _delete_row(self, row: ItemRow):
        pr = row.parent_row
        if pr is None:
            if row not in self.rows:
                return
        elif row not in pr.child_rows:
            return
        self._push_undo("Delete item")
        self._remove_rows([row])
        if pr is not None:
            pr.refresh()
        self._on_rows_changed()

    def _duplicate_row(self, row: ItemRow):
        pr = row.parent_row
        if pr is None:
            if row not in self.rows:
                return
        elif row not in pr.child_rows:
            return
        self._push_undo("Duplicate item")
        dup = fresh_copy(row.item)
        dup.text += " (copy)"
        dup.expanded = row.item.expanded
        if pr is None:
            idx = self.rows.index(row) + 1
        else:
            idx = pr.child_rows.index(row) + 1
            pr.refresh()
        self._create_row(dup, index=idx, parent_row=pr)
        self._on_rows_changed()

    def _move_to_tomorrow(self, row: ItemRow):
        pr = row.parent_row
        if pr is None:
            if row not in self.rows:
                return
        elif row not in pr.child_rows:
            return
        self._push_undo("Move item to tomorrow")
        if row.item.is_running:
            row.stop_timer()
        nxt = (self.current_date + timedelta(days=1)).isoformat()
        self.data.setdefault(nxt, []).insert(0, row.item.to_dict())
        self._remove_rows([row])
        if pr is not None:
            pr.refresh()
        self._on_rows_changed()
        self.statusBar().showMessage("Item moved to tomorrow", 4000)

    def _enforce_single_timer(self, row: ItemRow):
        self._idle_paused = False
        for r in self.iter_rows():
            if r is not row and r.item.is_running:
                r.stop_timer()
        self._flush_current_day()
        self._refresh_summary()

    def _on_estimate_exceeded(self, row: ItemRow):
        QApplication.beep()
        QApplication.alert(self)
        self._notify("⏱ Estimate exceeded",
                     f"\"{row.item.text[:40]}\" has gone over its estimate",
                     8000)

    def _clear_completed(self):
        done_rows = [r for r in self.rows if r.item.done]
        if not done_rows:
            return
        answer = QMessageBox.question(
            self, "Clear completed",
            f"Delete {len(done_rows)} completed task(s) "
            f"(including their subtasks)?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._push_undo("Clear completed")
        self._remove_rows(done_rows)
        self._on_rows_changed()

    def _toggle_theme(self):
        cur = self.settings.value("theme", "light")
        new = "dark" if cur == "light" else "light"
        self.settings.setValue("theme", new)
        apply_theme(QApplication.instance(), new)
        self.theme_btn.setText("☀️" if new == "dark" else "🌙")
        if self.tray:
            self.tray.setIcon(make_icon())
        for r in self.iter_rows():
            r.refresh()
        self._refresh_sidebar()

    def _open_stats(self):
        StatsDialog(self.data, self).exec()

    def _show_help(self):
        QMessageBox.information(
            self, "Shortcuts",
            "<b>Add task</b> — Enter &nbsp;(attach an estimate with ~1h30 and "
            "tags with #work)<br>"
            "<b>Paste list from clipboard</b> — Ctrl+Shift+V "
            "(indentation → nesting, #tags picked up)<br>"
            "<b>New-task focus</b> — Ctrl+N &nbsp;•&nbsp; "
            "<b>Filter</b> — Ctrl+F &nbsp;(text and/or #tags)<br>"
            "<b>Tags &amp; presets sidebar</b> — Ctrl+B or the ★ button<br>"
            "<b>Select all visible</b> — Ctrl+A<br>"
            "<b>Previous / next day</b> — Alt+← / Alt+→ &nbsp;•&nbsp; "
            "<b>Today</b> — Ctrl+T<br>"
            "<b>Statistics</b> — Ctrl+D &nbsp;•&nbsp; "
            "<b>Undo / redo</b> — Ctrl+Z / Ctrl+Shift+Z<br><br>"
            "<b>Now-tracking banner:</b> when a timer runs, a green card "
            "above the list shows the item and its live time — click the "
            "path to jump to the row, ⏸ to pause.<br><br>"
            "<b>Timers:</b> one at a time, any depth. <b>Idle auto-pause</b> "
            "(Tasks menu) pauses after N min without input. Set a "
            "<b>daily goal</b> for 🎯 progress in the header.<br><br>"
            "<b>Repeats:</b> 🔁 cycles off → daily → weekdays (📅)."
            "<br><br>"
            "<b>Right-click an item:</b> copy title / Markdown subtree, "
            "quick-tag, repeat, adjust time (or double-click the time), "
            "duplicate, move to date/tomorrow, delete.<br><br>"
            "<b>Nesting:</b> drop dragged items on a row's middle to nest "
            "(edges = above/below). Max depth 8.<br><br>"
            "<b>Saving:</b> autosaves ~1s after your last edit, always on exit.")

    def _show_about(self):
        QMessageBox.about(
            self, "About Daily Checklist",
            "<b>Daily Checklist</b><br>Time-tracked daily checklists with "
            "unlimited-depth sub-checklists, idle-aware timers, goals, "
            "repeats, tags and presets.<br><br>"
            f"Data folder:<br><code>{APP_DIR}</code>")

    def _export_csv(self):
        default = f"checklist_{self.current_date.isoformat()}.csv"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export day as CSV", default, "CSV files (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["Item", "Done", "Estimated",
                            "Time spent (h:mm:ss)", "Repeat", "Note",
                            "Tags", "Created", "Completed at"])

                def write_item(t: Task, depth: int):
                    prefix = "    " * depth + ("└ " if depth else "")
                    w.writerow([
                        prefix + t.text,
                        "yes" if t.done else "no",
                        fmt_dur(t.estimated_seconds) if t.estimated_seconds else "",
                        fmt_hms(t.current_seconds()),
                        t.repeat if t.repeat != "none" else "",
                        t.note,
                        ", ".join(t.tags),
                        t.created, t.completed_at or ""])
                    for c in t.children:
                        write_item(c, depth + 1)

                for r in self.rows:
                    write_item(r.item, 0)
            self.statusBar().showMessage(f"Exported to {path}", 5000)
        except OSError as e:
            QMessageBox.warning(self, "Export failed", str(e))

    # -- persistence & summary -------------------------------------------------
    def _on_rows_changed(self):
        self._flush_current_day()
        self._refresh_summary()
        self._apply_filter()
        self._refresh_sidebar()

    def _flush_current_day(self):
        if not self._day_loaded:
            return
        self.data[self.current_date.isoformat()] = [
            r.item.to_dict() for r in self.rows]
        self._save_timer.start()

    def _flush_now(self):
        self._save_timer.stop()
        self._write_data()

    def _refresh_summary(self):
        all_items = [r.item for r in self.iter_rows()]
        total = len(all_items)
        done = sum(1 for i in all_items if i.done)
        secs = sum(i.current_seconds() for i in all_items)
        day_est = sum(i.estimated_seconds for i in all_items)
        txt = f"{done}/{total} done   •   {fmt_hms(secs)} tracked"
        if day_est > 0:
            txt += f"   •   est {fmt_dur(day_est)}"

        if self._goal_hours > 0:
            goal_secs = self._goal_hours * 3600
            pct = int(secs / goal_secs * 100)
            txt += (f"   •   🎯 {fmt_hms(secs)} / {fmt_dur(goal_secs)} "
                    f"({pct}%)" + ("  ✓" if secs >= goal_secs else ""))
            self.goal_bar.show()
            self.goal_bar.setValue(min(100, pct))
        else:
            self.goal_bar.hide()

        self.summary_lbl.setText(txt)
        self.progress.setRange(0, 100)
        self.progress.setValue(int(done * 100 / total) if total else 0)

    def _on_tick(self):
        self._check_rollover()

        running = [r for r in self.iter_rows() if r.item.is_running]
        self._check_idle(running)
        running = [r for r in self.iter_rows() if r.item.is_running]
        for r in running:
            r.refresh_time()

        # now-tracking banner
        if running:
            r0 = running[0]
            self.now_bar.setVisible(True)
            self.now_path.setText("▶  " + r0.path_key())
            self.now_time.setText(fmt_hms(r0.item.current_seconds()))
        else:
            self.now_bar.setVisible(False)

        if running:
            self._refresh_summary()
            r0 = running[0]
            path = r0.path_key()
            self.setWindowTitle(
                f"▶ {path[:60]} — {fmt_hms(r0.item.current_seconds())}"
                f" — Daily Checklist")
            if self.tray:
                self.tray.setToolTip(
                    f"▶ {path[:60]} — {fmt_hms(r0.item.current_seconds())}")
                self._tray_pause_act.setText(f"⏸  Pause \"{r0.item.text[:30]}\"")
                self._tray_pause_act.setEnabled(True)
            self._title_running = True
        elif self._title_running:
            self._title_running = False
            self.setWindowTitle(f"Daily Checklist — {self.current_date.isoformat()}")
            if self.tray:
                self.tray.setToolTip("Daily Checklist")
                self._tray_pause_act.setText("⏸  No timer running")
                self._tray_pause_act.setEnabled(False)

    def _check_rollover(self):
        today = date.today()
        if today == self._known_today:
            return
        prev, self._known_today = self._known_today, today
        if self.current_date == prev:
            self._show_day(today, auto_carry=False)
            n = self._carry_over(today, quiet=True)
            msg = "New day started"
            if n:
                msg += f" — {n} repeating item(s) carried over"
            self._notify("🌅 New day", msg)

    def _focus_input(self):
        self.input.setFocus()
        self.input.selectAll()

    def _read_data(self) -> dict:
        try:
            loaded = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            return loaded if isinstance(loaded, dict) else {}
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, OSError) as e:
            QMessageBox.warning(self, "Daily Checklist",
                                f"Could not read data:\n{e}")
            return {}

    def _write_data(self):
        try:
            APP_DIR.mkdir(parents=True, exist_ok=True)
            tmp = DATA_FILE.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self.data, indent=2, ensure_ascii=False),
                encoding="utf-8")
            os.replace(tmp, DATA_FILE)
        except OSError as e:
            QMessageBox.warning(self, "Daily Checklist",
                                f"Could not save data:\n{e}")

    @staticmethod
    def _backup_data():
        try:
            if DATA_FILE.exists() and DATA_FILE.stat().st_size > 0:
                BACKUP_DIR.mkdir(parents=True, exist_ok=True)
                name = f"data_{datetime.now():%Y%m%d_%H%M%S}.json"
                shutil.copy2(DATA_FILE, BACKUP_DIR / name)
                backups = sorted(BACKUP_DIR.glob("data_*.json"))
                for old in backups[:-10]:
                    old.unlink()
        except OSError:
            pass

    def closeEvent(self, event):
        for r in self.iter_rows():
            if r.item.is_running:
                r.stop_timer()
        self._flush_now()
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("last_date", self.current_date.isoformat())
        if self.tray:
            self.tray.hide()
        super().closeEvent(event)


# ---------------------------------------------------------------- app icon
def make_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(_current_theme.get("ACCENT", "#2f6fed")))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(2, 2, 60, 60, 14, 14)
    pen = QPen(QColor("white"), 7)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.drawLine(18, 34, 28, 44)
    p.drawLine(28, 44, 46, 22)
    p.end()
    return QIcon(pm)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Daily Checklist")
    app.setOrganizationName("DailyChecklist")
    app.setStyle("Fusion")
    app.setWindowIcon(make_icon())

    settings = QSettings("DailyChecklist", "DailyChecklist")
    apply_theme(app, settings.value("theme", "light"))

    win = MainWindow()
    if settings.value("theme", "light") == "dark":
        win.theme_btn.setText("☀️")
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()