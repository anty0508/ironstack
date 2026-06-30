"""Pre-interview launcher (PySide6 QDialog), styled like the overlay.

Frameless dark window with a left sidebar (Resume / JD / Projects / References /
Preferences / Past meetings) and a content area. Manages the document library
(imported from .pdf/.docx/.txt), edits global preferences, picks what to use for
this interview, and reviews past meetings. On "Start interview" it creates a
meeting, builds the system prompt, and exposes (meeting_id, system_prompt).
"""

import html

from pathlib import Path

from PySide6 import QtCore, QtWidgets

Qt = QtCore.Qt

from storage import database
from storage import documents
from services.context import build_prompt_from_documents
from config import LANGUAGES, DEFAULT_LANGUAGE_CODE, language_name
from logsetup import get_logger
from ui.overlay import Overlay, apply_exclude_from_capture, get_window_visibility_controller  # for _format_answer in the transcript viewer

KIND_LABELS = {
    "resume": "Resume",
    "jd": "Job Description",
    "project": "Projects",
    "reference": "References",
}

NAV = [
    ("resume", "Resume"),
    ("jd", "Job Description"),
    ("project", "Projects"),
    ("reference", "References"),
    ("preferences", "Preferences"),
    ("history", "Past meetings"),
]


STYLE = """
* { font-family: 'Segoe UI', sans-serif; color: #e8eaf0; font-size: 13px; }
#panel { background: #14161c; border: 1px solid rgba(255,255,255,0.09); border-radius: 0; }

#header { background: transparent; }
#appDot { color: #f2a33c; font-size: 15px; }
#appTitle { font-size: 14px; font-weight: 600; }
#winbtn { background: transparent; border: none; color: #9aa0b2; font-size: 16px;
          font-weight: 600; padding: 0 10px; }
#winbtn:hover { color: #ffffff; }
#winbtn:pressed { background: transparent; }
#winbtn:focus { outline: none; }
QPushButton:pressed { background: transparent; }

#sidebar { background: #0f1116; border-right: 1px solid rgba(255,255,255,0.06); }
#nav { background: transparent; border: none; outline: none; }
#nav::item { padding: 11px 14px; border-radius: 9px; margin: 2px 10px; color: #b9bfce; }
#nav::item:hover { background: rgba(255,255,255,0.05); }
#nav::item:selected { background: rgba(242,163,60,0.16); color: #f6c98a; }

#pageTitle { font-size: 20px; font-weight: 700; }
#pageHint { font-size: 12px; color: #8a90a2; }
#fieldLabel { font-size: 11px; font-weight: 700; color: #7f8aa3; letter-spacing: 1px; }

QLineEdit, QPlainTextEdit, QTextBrowser, QListWidget {
    background: #1c2029; border: 1px solid rgba(255,255,255,0.08);
    border-radius: 10px; padding: 8px;
    selection-background-color: rgba(242,163,60,0.35);
}
QLineEdit:focus, QPlainTextEdit:focus { border: 1px solid rgba(242,163,60,0.55); }
QComboBox {
    background: #1c2029; border: 1px solid rgba(255,255,255,0.08);
    border-radius: 10px; padding: 7px 10px; color: #e8eaf0;
}
QComboBox:hover { border: 1px solid rgba(242,163,60,0.45); }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView {
    background: #1c2029; border: 1px solid rgba(255,255,255,0.10);
    selection-background-color: rgba(242,163,60,0.30); outline: none;
}
QListWidget#doclist::item, QListWidget#history::item { padding: 10px 10px; border-radius: 7px; }
QListWidget#doclist::item:selected, QListWidget#history::item:selected {
    background: rgba(242,163,60,0.18); color: #f6c98a; }
QListWidget#doclist::item:hover, QListWidget#history::item:hover {
    background: rgba(255,255,255,0.04); }

QPushButton { background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.10);
    border-radius: 9px; padding: 8px 16px; color: #e8eaf0; }
QPushButton:hover { background: rgba(255,255,255,0.12); }
#primary { background: #f2a33c; color: #1a1206; border: none; font-weight: 700; padding: 10px 22px; }
#primary:hover { background: #f4b257; }
#danger { background: #c0453a; color: #ffffff; border: none; font-weight: 700; padding: 10px 22px; }
#danger:hover { background: #d2554a; }
#dialogText { font-size: 13px; color: #cdd2de; }
#ghost { background: transparent; }
#ghost:hover { background: rgba(255,255,255,0.06); }
#footer { background: transparent; }

QScrollBar:vertical { background: rgba(255,255,255,0.05); width: 10px; margin: 2px; border-radius: 5px; }
QScrollBar::handle:vertical { background: rgba(242,163,60,0.5); border-radius: 5px; min-height: 28px; }
QScrollBar::handle:vertical:hover { background: rgba(242,163,60,0.8); }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

QSlider::groove:horizontal { height: 4px; background: rgba(255,255,255,0.12); border-radius: 2px; }
QSlider::handle:horizontal {
    width: 12px; height: 12px; margin: -5px 0;
    background: #f2a33c; border-radius: 6px;
}
"""


class _DragBar(QtWidgets.QFrame):
    """Header that drags the frameless window."""

    def __init__(self, window):
        super().__init__()
        self.setObjectName("header")
        self.setFixedHeight(48)
        self._window = window
        self._drag = None

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag = e.globalPosition().toPoint() - self._window.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag is not None and (e.buttons() & Qt.LeftButton):
            self._window.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, e):
        self._drag = None


# remembers the last folder browsed across imports
_LAST_DIR = None


class FilePicker(QtWidgets.QDialog):
    """Dark, self-contained file picker. Pure Qt widgets (no native OS dialog),
    so it matches the app and can't trigger the native-dialog crash/hang."""

    def __init__(self, title, start_dir=None, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        # Opaque (not WA_TranslucentBackground) with square corners, so the
        # window stays hidden when capture exclusion is on. A frameless
        # translucent window is invisible to WDA_EXCLUDEFROMCAPTURE on some GPUs.
        self.setMinimumSize(620, 460)
        self.resize(720, 540)

        self.selected = None
        self._current_file = None
        self._visibility_controller = get_window_visibility_controller()
        self._visibility_controller.register(self)
        start = Path(start_dir) if start_dir else Path.home()
        self.cwd = start if start.is_dir() else Path.home()

        style = self.style()
        self._dir_icon = style.standardIcon(QtWidgets.QStyle.StandardPixmap.SP_DirIcon)
        self._file_icon = style.standardIcon(QtWidgets.QStyle.StandardPixmap.SP_FileIcon)

        self._build(title)
        self.setStyleSheet(STYLE)
        self._populate()
        self._center()
        # Follow the app's current stealth state -- only hide from capture when
        # the app itself is in stealth mode (default off), not unconditionally.
        QtCore.QTimer.singleShot(0, lambda: apply_exclude_from_capture(
            self, enabled=self._visibility_controller.capture_excluded))

    def _center(self):
        screen = QtWidgets.QApplication.primaryScreen().availableGeometry()
        self.move(screen.center().x() - self.width() // 2,
                  screen.center().y() - self.height() // 2)

    def _build(self, title):
        shell = QtWidgets.QVBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        panel = QtWidgets.QFrame()
        panel.setObjectName("panel")
        shell.addWidget(panel)
        root = QtWidgets.QVBoxLayout(panel)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = _DragBar(self)
        hl = QtWidgets.QHBoxLayout(header)
        hl.setContentsMargins(18, 0, 10, 0)
        t = QtWidgets.QLabel(title)
        t.setObjectName("appTitle")
        hl.addWidget(t)
        hl.addStretch(1)
        # FilePicker follows the app's stealth state (applied in __init__ and via
        # the visibility controller); it has no stealth toggle of its own.
        self.opacity_slider = QtWidgets.QSlider(Qt.Horizontal)
        self.opacity_slider.setFixedWidth(90)
        self.opacity_slider.setRange(30, 100)
        self.opacity_slider.setValue(self._visibility_controller.opacity_percent)
        self.opacity_slider.setToolTip("Opacity")
        self.opacity_slider.valueChanged.connect(
            lambda v: self._visibility_controller.set_opacity_percent(v)
        )
        hl.addWidget(self.opacity_slider)
        hl.addSpacing(6)
        close = QtWidgets.QPushButton("✕")
        close.setObjectName("winbtn")
        close.setCursor(Qt.PointingHandCursor)
        close.clicked.connect(self.reject)
        hl.addWidget(close)
        root.addWidget(header)

        body = QtWidgets.QVBoxLayout()
        body.setContentsMargins(18, 8, 18, 16)
        body.setSpacing(10)
        root.addLayout(body, 1)

        pbar = QtWidgets.QHBoxLayout()
        up = QtWidgets.QPushButton("↑ Up")
        up.setObjectName("ghost")
        up.clicked.connect(self._go_up)
        self.path_edit = QtWidgets.QLineEdit()
        self.path_edit.returnPressed.connect(self._go_typed)
        pbar.addWidget(up)
        pbar.addWidget(self.path_edit, 1)
        body.addLayout(pbar)

        shortcuts = QtWidgets.QHBoxLayout()
        for label, path in self._shortcuts():
            b = QtWidgets.QPushButton(label)
            b.setObjectName("ghost")
            b.clicked.connect(lambda _=False, p=path: self._goto(p))
            shortcuts.addWidget(b)
        shortcuts.addStretch(1)
        body.addLayout(shortcuts)

        self.list = QtWidgets.QListWidget()
        self.list.setObjectName("doclist")
        self.list.itemDoubleClicked.connect(self._on_double)
        self.list.itemSelectionChanged.connect(self._on_select)
        body.addWidget(self.list, 1)

        footer = QtWidgets.QHBoxLayout()
        self.sel_label = QtWidgets.QLabel("")
        self.sel_label.setObjectName("pageHint")
        footer.addWidget(self.sel_label, 1)
        cancel = QtWidgets.QPushButton("Cancel")
        cancel.setObjectName("ghost")
        cancel.clicked.connect(self.reject)
        self.open_btn = QtWidgets.QPushButton("Open")
        self.open_btn.setObjectName("primary")
        self.open_btn.setEnabled(False)
        self.open_btn.clicked.connect(self._open)
        footer.addWidget(cancel)
        footer.addWidget(self.open_btn)
        footer.addWidget(QtWidgets.QSizeGrip(panel))
        body.addLayout(footer)

    def _shortcuts(self):
        home = Path.home()
        items = [("This PC", None)]
        for label, p in [("Home", home), ("Desktop", home / "Desktop"),
                         ("Documents", home / "Documents"), ("Downloads", home / "Downloads")]:
            if p.is_dir():
                items.append((label, p))
        return items

    def _drives(self):
        out = []
        for vol in QtCore.QStorageInfo.mountedVolumes():
            if not vol.isValid() or not vol.isReady():
                continue
            root = vol.rootPath()                 # e.g. "C:/"
            name = vol.displayName()
            label = f"{root}  ({name})" if name and name not in root else root
            out.append((root, label))
        if not out:                               # fallback: scan drive letters
            import os
            import string
            for letter in string.ascii_uppercase:
                d = f"{letter}:/"
                if os.path.exists(d):
                    out.append((d, d))
        return out

    def _populate(self):
        self.list.clear()
        self._current_file = None
        self.open_btn.setEnabled(False)
        self.sel_label.setText("")

        # "This PC" view: list all drives
        if self.cwd is None:
            self.path_edit.setText("This PC")
            for root, label in self._drives():
                item = QtWidgets.QListWidgetItem(self._dir_icon, label)
                item.setData(Qt.UserRole, root)
                item.setData(Qt.UserRole + 1, "drive")
                self.list.addItem(item)
            return

        self.path_edit.setText(str(self.cwd))
        try:
            entries = list(self.cwd.iterdir())
        except Exception as exc:
            self.sel_label.setText(f"Cannot open folder: {exc}")
            return

        def add(path, icon, kind):
            item = QtWidgets.QListWidgetItem(icon, path.name)
            item.setData(Qt.UserRole, str(path))
            item.setData(Qt.UserRole + 1, kind)
            self.list.addItem(item)

        dirs = sorted((p for p in entries if p.is_dir()), key=lambda p: p.name.lower())
        files = sorted(
            (p for p in entries if p.is_file()
             and p.suffix.lower() in documents.SUPPORTED_EXTENSIONS),
            key=lambda p: p.name.lower(),
        )
        for d in dirs:
            add(d, self._dir_icon, "dir")
        for f in files:
            add(f, self._file_icon, "file")

    def _on_double(self, item):
        kind = item.data(Qt.UserRole + 1)
        path = Path(item.data(Qt.UserRole))
        if kind in ("dir", "drive"):
            self.cwd = path
            self._populate()
        else:
            self.selected = str(path)
            self.accept()

    def _on_select(self):
        item = self.list.currentItem()
        if item and item.data(Qt.UserRole + 1) == "file":
            self._current_file = item.data(Qt.UserRole)
            self.sel_label.setText(Path(self._current_file).name)
            self.open_btn.setEnabled(True)
        else:
            self._current_file = None
            self.sel_label.setText("")
            self.open_btn.setEnabled(False)

    def _open(self):
        if self._current_file:
            self.selected = self._current_file
            self.accept()

    def _go_up(self):
        if self.cwd is None:
            return
        parent = self.cwd.parent
        if parent == self.cwd:
            self.cwd = None          # at a drive root -> show all drives
        else:
            self.cwd = parent
        self._populate()

    def _goto(self, path):
        if path is None:             # "This PC" shortcut
            self.cwd = None
            self._populate()
            return
        path = Path(path)
        if path.is_dir():
            self.cwd = path
            self._populate()

    def _go_typed(self):
        text = self.path_edit.text().strip()
        if text.lower() in ("this pc", ""):
            self.cwd = None
            self._populate()
            return
        path = Path(text)
        if path.is_dir():
            self.cwd = path
            self._populate()
        elif path.is_file():
            self.selected = str(path)
            self.accept()
        else:
            self.path_edit.setText("This PC" if self.cwd is None else str(self.cwd))


class MessageDialog(QtWidgets.QDialog):
    """Frameless dark dialog matching the app theme, used for confirmations and
    notices. The native QMessageBox ignores our dark stylesheet and renders
    near-invisible text on a white background, so we roll our own."""

    def __init__(self, title, message, parent=None,
                 confirm_text=None, cancel_text="OK", danger=False):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog | Qt.Tool)
        # Opaque (not WA_TranslucentBackground) with square corners so it stays
        # hidden from capture.
        self.setModal(True)

        shell = QtWidgets.QVBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        panel = QtWidgets.QFrame()
        panel.setObjectName("panel")
        shell.addWidget(panel)

        v = QtWidgets.QVBoxLayout(panel)
        v.setContentsMargins(26, 22, 26, 18)
        v.setSpacing(12)

        heading = QtWidgets.QLabel(title)
        heading.setObjectName("pageTitle")
        v.addWidget(heading)

        text = QtWidgets.QLabel(message)
        text.setObjectName("dialogText")
        text.setWordWrap(True)
        text.setMinimumWidth(340)
        v.addWidget(text, 1)

        row = QtWidgets.QHBoxLayout()
        row.setSpacing(10)
        row.addStretch(1)

        cancel = QtWidgets.QPushButton(cancel_text)
        cancel.setObjectName("ghost")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)

        if confirm_text:
            confirm = QtWidgets.QPushButton(confirm_text)
            confirm.setObjectName("danger" if danger else "primary")
            confirm.setCursor(Qt.PointingHandCursor)
            confirm.clicked.connect(self.accept)
            confirm.setDefault(True)
            row.addWidget(confirm)

        v.addLayout(row)
        self.setStyleSheet(STYLE)

    def showEvent(self, e):
        super().showEvent(e)
        # Center over the parent window, and keep this dialog out of screen
        # capture when stealth mode is on (consistent with the other windows).
        parent = self.parent()
        ref = (parent.frameGeometry() if parent is not None and parent.isVisible()
               else QtWidgets.QApplication.primaryScreen().availableGeometry())
        self.move(ref.center().x() - self.width() // 2,
                  ref.center().y() - self.height() // 2)
        apply_exclude_from_capture(
            self, enabled=get_window_visibility_controller().capture_excluded)


def _page(title, hint):
    """A content page with a styled header; returns (widget, body_layout)."""
    page = QtWidgets.QWidget()
    v = QtWidgets.QVBoxLayout(page)
    v.setContentsMargins(28, 24, 28, 16)
    v.setSpacing(6)
    t = QtWidgets.QLabel(title)
    t.setObjectName("pageTitle")
    v.addWidget(t)
    if hint:
        h = QtWidgets.QLabel(hint)
        h.setObjectName("pageHint")
        h.setWordWrap(True)
        v.addWidget(h)
    v.addSpacing(8)
    return page, v


class _DocSection(QtWidgets.QWidget):
    """A document library page for one kind, with Import / Delete + selection."""

    def __init__(self, kind, multi):
        super().__init__()
        self.kind = kind
        self.multi = multi

        hint = ("Tick any number to include in this interview."
                if multi else "Select one to use for this interview.")
        page, layout = _page(KIND_LABELS[kind], hint)
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(page)

        self.list = QtWidgets.QListWidget()
        self.list.setObjectName("doclist")
        if not multi:
            self.list.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        layout.addWidget(self.list, 1)

        buttons = QtWidgets.QHBoxLayout()
        import_btn = QtWidgets.QPushButton("Import…")
        import_btn.clicked.connect(self._import)
        delete_btn = QtWidgets.QPushButton("Delete")
        delete_btn.setObjectName("ghost")
        delete_btn.clicked.connect(self._delete)
        buttons.addWidget(import_btn)
        buttons.addWidget(delete_btn)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.refresh()

    def refresh(self):
        self.list.clear()
        for doc in database.list_documents(self.kind):
            # Show the original filename (with its extension) when we have it,
            # so .pdf / .docx / .txt is visible; fall back to the bare title.
            label = doc.get("source_filename") or doc["title"]
            item = QtWidgets.QListWidgetItem(label)
            item.setData(Qt.UserRole, doc["id"])
            if self.multi:
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Unchecked)
            self.list.addItem(item)
        if not self.multi and self.list.count():
            self.list.setCurrentRow(0)

    def _pick_file(self):
        """Open our own dark file picker (no native dialog -> no crash/hang)."""
        global _LAST_DIR
        dlg = FilePicker(f"Import {KIND_LABELS[self.kind]}", _LAST_DIR, parent=self)
        if dlg.exec() == QtWidgets.QDialog.Accepted and dlg.selected:
            _LAST_DIR = str(Path(dlg.selected).parent)
            return dlg.selected
        return None

    def _import(self):
        # Wrap the whole thing: in PySide6 an uncaught exception in a slot aborts
        # the app, so any failure here must be turned into a friendly message.
        try:
            path = self._pick_file()
            if not path:
                return

            text = documents.extract_text(path)
            if not text.strip():
                MessageDialog("Import failed",
                              "No text could be extracted from that file.",
                              parent=self).exec()
                return

            database.add_document(self.kind, Path(path).stem, text, Path(path).name)
            get_logger().info("imported %s document: %s", self.kind, Path(path).name)
            self.refresh()
        except Exception as exc:
            get_logger().exception("import failed")
            MessageDialog("Import failed", f"{type(exc).__name__}: {exc}",
                          parent=self).exec()

    def _delete(self):
        try:
            item = self.list.currentItem()
            if item is None:
                return
            database.delete_document(item.data(Qt.UserRole))
            get_logger().info("deleted %s document", self.kind)
            self.refresh()
        except Exception as exc:
            get_logger().exception("delete document failed")
            MessageDialog("Delete failed", str(exc), parent=self).exec()

    def selected_ids(self):
        if self.multi:
            return [
                self.list.item(i).data(Qt.UserRole)
                for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.Checked
            ]
        item = self.list.currentItem()
        return [item.data(Qt.UserRole)] if item else []


class Launcher(QtWidgets.QDialog):
    def __init__(self, hide_from_taskbar=True):
        super().__init__()
        self.setWindowTitle("IronStack — Setup")
        flags = Qt.FramelessWindowHint | Qt.Dialog
        if hide_from_taskbar:
            # Qt.Tool keeps the window off the taskbar (and alt-tab); the app is
            # reached through the system tray icon instead.
            flags |= Qt.Tool
        self.setWindowFlags(flags)
        # Opaque (not WA_TranslucentBackground) with square corners so the setup
        # window stays hidden from capture.
        self.setMinimumSize(820, 560)
        self.resize(940, 680)

        self.meeting_id = None
        self.system_prompt = None
        self.language_code = database.get_setting("language_code", DEFAULT_LANGUAGE_CODE)
        self.language_name = language_name(self.language_code)
        self._pinned = True            # the setup window starts pinned by default
        self._visibility_controller = get_window_visibility_controller()
        self._pinned = self._visibility_controller.pinned

        self._build_ui()
        self.setStyleSheet(STYLE)
        self._center()
        self._visibility_controller.register(self)
        QtCore.QTimer.singleShot(0, lambda: self._visibility_controller.register(self))

    def _apply_capture_exclusion(self):
        apply_exclude_from_capture(self, enabled=self._visibility_controller.capture_excluded)
        self._refresh_stealth_button()

    def _hide_window(self):
        self.hide()

    def closeEvent(self, event):
        event.ignore()
        self._hide_window()

    def _apply_pin_state(self):
        self._pinned = self._visibility_controller.pinned
        try:
            from ui.overlay import set_window_topmost
            set_window_topmost(self, self._pinned)
        except Exception:
            flags = self.windowFlags()
            if self._pinned:
                flags |= Qt.WindowStaysOnTopHint
            else:
                flags &= ~Qt.WindowStaysOnTopHint
            geo = self.geometry()
            self.setWindowFlags(flags)
            self.setGeometry(geo)
            self.show()
        try:
            self.pin_toggle.setChecked(self._pinned)
        except Exception:
            pass
        self._refresh_pin_button()

    def _toggle_pin(self):
        self._visibility_controller.set_pinned(not self._visibility_controller.pinned)

    def _refresh_pin_button(self):
        self.pin_toggle.setToolTip(
            "Unpin — currently always on top" if self._pinned
            else "Pin on top of other windows"
        )
        try:
            self.pin_toggle.setChecked(self._pinned)
        except Exception:
            pass
        if self._pinned:
            self.pin_toggle.setStyleSheet(
                "background: rgba(242,163,60,0.22); color: #f2a33c; border-radius:6px;"
            )
        else:
            self.pin_toggle.setStyleSheet(
                "background: transparent; color: #9aa0b2; border: none;"
            )

    # ---- layout ----

    def _build_ui(self):
        shell = QtWidgets.QVBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        panel = QtWidgets.QFrame()
        panel.setObjectName("panel")
        shell.addWidget(panel)

        root = QtWidgets.QVBoxLayout(panel)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # header
        header = _DragBar(self)
        hl = QtWidgets.QHBoxLayout(header)
        hl.setContentsMargins(18, 0, 10, 0)
        dot = QtWidgets.QLabel("●")
        dot.setObjectName("appDot")
        title = QtWidgets.QLabel("IronStack")
        title.setObjectName("appTitle")
        hl.addWidget(dot)
        hl.addSpacing(8)
        hl.addWidget(title)
        hl.addStretch(1)

        self.stealth_toggle = QtWidgets.QPushButton("Stealth Off")
        self.stealth_toggle.setObjectName("ghost")
        self.stealth_toggle.setCursor(Qt.PointingHandCursor)
        self.stealth_toggle.setFlat(True)
        self.stealth_toggle.setFocusPolicy(Qt.NoFocus)
        self.stealth_toggle.setStyleSheet("background: transparent; border: none;")
        self.stealth_toggle.setToolTip("Stealth mode: hidden from remote viewers")
        self.stealth_toggle.clicked.connect(self._toggle_stealth_mode)
        self._refresh_stealth_button()
        hl.addWidget(self.stealth_toggle)
        hl.addSpacing(6)
        self.opacity_slider = QtWidgets.QSlider(Qt.Horizontal)
        self.opacity_slider.setFixedWidth(90)
        self.opacity_slider.setRange(30, 100)
        self.opacity_slider.setValue(self._visibility_controller.opacity_percent)
        self.opacity_slider.setToolTip("Opacity")
        self.opacity_slider.valueChanged.connect(
            lambda v: self._visibility_controller.set_opacity_percent(v)
        )
        hl.addWidget(self.opacity_slider)
        hl.addSpacing(6)

        self.pin_toggle = QtWidgets.QPushButton("📌")
        self.pin_toggle.setObjectName("winbtn")
        self.pin_toggle.setCursor(Qt.PointingHandCursor)
        self.pin_toggle.setFlat(True)
        self.pin_toggle.setCheckable(True)
        self.pin_toggle.setFocusPolicy(Qt.NoFocus)
        self.pin_toggle.setStyleSheet("background: transparent; border: none;")
        self.pin_toggle.clicked.connect(self._toggle_pin)
        self._refresh_pin_button()
        hl.addWidget(self.pin_toggle)
        hl.addSpacing(6)

        close = QtWidgets.QPushButton("✕")
        close.setObjectName("winbtn")
        close.setCursor(Qt.PointingHandCursor)
        close.clicked.connect(self._hide_window)
        hl.addWidget(close)
        root.addWidget(header)

        # body: sidebar + stacked pages
        body = QtWidgets.QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        root.addLayout(body, 1)

        sidebar = QtWidgets.QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(196)
        sv = QtWidgets.QVBoxLayout(sidebar)
        sv.setContentsMargins(0, 8, 0, 8)
        self.nav = QtWidgets.QListWidget()
        self.nav.setObjectName("nav")
        for _, label in NAV:
            self.nav.addItem(QtWidgets.QListWidgetItem(label))
        self.nav.currentRowChanged.connect(self._on_nav)
        sv.addWidget(self.nav, 1)
        body.addWidget(sidebar)

        self.stack = QtWidgets.QStackedWidget()
        self.resume = _DocSection("resume", multi=False)
        self.jd = _DocSection("jd", multi=False)
        self.projects = _DocSection("project", multi=True)
        self.references = _DocSection("reference", multi=True)
        for w in (self.resume, self.jd, self.projects, self.references):
            self.stack.addWidget(w)
        self.stack.addWidget(self._build_preferences_page())
        self.stack.addWidget(self._build_history_page())
        body.addWidget(self.stack, 1)

        # footer: meeting fields + actions
        footer = QtWidgets.QFrame()
        footer.setObjectName("footer")
        fl = QtWidgets.QHBoxLayout(footer)
        fl.setContentsMargins(24, 12, 18, 16)
        fl.setSpacing(16)

        fl.addLayout(self._field("INTERVIEW", "title_edit",
                                 "e.g. OX Security — Backend Engineer", 260))
        fl.addLayout(self._field("COMPANY", "company_edit", "e.g. OX Security", 180))
        fl.addLayout(self._language_field())
        fl.addStretch(1)

        grip = QtWidgets.QSizeGrip(footer)
        quit_btn = QtWidgets.QPushButton("Quit")
        quit_btn.setObjectName("ghost")
        quit_btn.clicked.connect(self.reject)
        start = QtWidgets.QPushButton("Start interview")
        start.setObjectName("primary")
        start.setCursor(Qt.PointingHandCursor)
        start.clicked.connect(self._start)
        fl.addWidget(quit_btn, 0, Qt.AlignBottom)
        fl.addWidget(start, 0, Qt.AlignBottom)
        fl.addWidget(grip, 0, Qt.AlignBottom)
        root.addWidget(footer)

        self.nav.setCurrentRow(0)

    def _toggle_stealth_mode(self):
        self._visibility_controller.set_capture_excluded(
            not self._visibility_controller.capture_excluded
        )
        self._refresh_stealth_button()
        self._apply_capture_exclusion()

    def _refresh_stealth_button(self):
        on = self._visibility_controller.capture_excluded
        self.stealth_toggle.setText("Stealth")
        self.stealth_toggle.setToolTip(
            "Stealth ON — hidden from screen capture (click to disable)" if on
            else "Stealth OFF — visible in screen capture (click to enable)"
        )
        self.stealth_toggle.setStyleSheet(
            "background: rgba(242,163,60,0.18); color: #f6c98a; border-radius:7px;"
            if on else "background: transparent; color: #b9bfce; border: none;"
        )

    def _field(self, label, attr, placeholder, width):
        col = QtWidgets.QVBoxLayout()
        col.setSpacing(3)
        lbl = QtWidgets.QLabel(label)
        lbl.setObjectName("fieldLabel")
        edit = QtWidgets.QLineEdit()
        edit.setPlaceholderText(placeholder)
        edit.setFixedWidth(width)
        setattr(self, attr, edit)
        col.addWidget(lbl)
        col.addWidget(edit)
        return col

    def _language_field(self):
        """Language picker for transcription + AI answers (remembered for next
        time). It can also be changed live from the interview overlay."""
        col = QtWidgets.QVBoxLayout()
        col.setSpacing(3)
        lbl = QtWidgets.QLabel("LANGUAGE")
        lbl.setObjectName("fieldLabel")
        self.language_combo = QtWidgets.QComboBox()
        self.language_combo.setFixedWidth(170)
        for label, code in LANGUAGES:
            self.language_combo.addItem(label, code)
        idx = self.language_combo.findData(self.language_code)
        self.language_combo.setCurrentIndex(idx if idx >= 0 else 0)
        col.addWidget(lbl)
        col.addWidget(self.language_combo)
        return col

    def _on_nav(self, row):
        if row >= 0:
            self.stack.setCurrentIndex(row)

    def _center(self):
        screen = QtWidgets.QApplication.primaryScreen().availableGeometry()
        self.move(screen.center().x() - self.width() // 2,
                  screen.center().y() - self.height() // 2)

    # ---- preferences page ----

    def _build_preferences_page(self):
        page, layout = _page(
            "Preferences",
            "Logistics used for quick answers (salary, notice, work authorization…). "
            "Saved globally and reused for every interview.",
        )
        self.prefs_edit = QtWidgets.QPlainTextEdit()
        self.prefs_edit.setPlainText(database.get_setting("preferences", ""))
        layout.addWidget(self.prefs_edit, 1)
        save = QtWidgets.QPushButton("Save preferences")
        save.clicked.connect(self._save_preferences)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(save)
        row.addStretch(1)
        layout.addLayout(row)
        return page

    def _save_preferences(self):
        database.set_setting("preferences", self.prefs_edit.toPlainText().strip())

    # ---- history page ----

    def _build_history_page(self):
        page, layout = _page("Past meetings",
                             "Review the transcript and answers from earlier interviews.")
        split = QtWidgets.QHBoxLayout()
        split.setSpacing(14)

        left = QtWidgets.QVBoxLayout()
        left.setSpacing(8)
        self.history_list = QtWidgets.QListWidget()
        self.history_list.setObjectName("history")
        self.history_list.setFixedWidth(240)
        self.history_list.currentItemChanged.connect(self._show_transcript)
        left.addWidget(self.history_list, 1)

        self.delete_meeting_btn = QtWidgets.QPushButton("Delete meeting")
        self.delete_meeting_btn.setObjectName("ghost")
        self.delete_meeting_btn.setEnabled(False)
        self.delete_meeting_btn.clicked.connect(self._delete_meeting)
        left.addWidget(self.delete_meeting_btn)
        split.addLayout(left)

        self.transcript = QtWidgets.QTextBrowser()
        split.addWidget(self.transcript, 1)
        layout.addLayout(split, 1)
        self._refresh_history()
        return page

    def _delete_meeting(self):
        item = self.history_list.currentItem()
        if item is None:
            return
        if not self._confirm(
            "Delete meeting",
            "Delete this meeting and its transcript? This cannot be undone.",
            confirm_text="Delete",
        ):
            return
        try:
            database.delete_meeting(item.data(Qt.UserRole))
            get_logger().info("deleted meeting %s", item.data(Qt.UserRole))
        except Exception as exc:
            get_logger().exception("delete meeting failed")
            self._warn(f"Delete failed: {exc}")
            return
        self._refresh_history()
        self.transcript.clear()

    def _confirm(self, title, message, confirm_text="OK"):
        dlg = MessageDialog(title, message, parent=self,
                            confirm_text=confirm_text, cancel_text="Cancel",
                            danger=True)
        return dlg.exec() == QtWidgets.QDialog.Accepted

    def _refresh_history(self):
        self.history_list.clear()
        for m in database.list_meetings():
            label = m["title"]
            if m["company"]:
                label += f" — {m['company']}"
            label += f"\n{m['created_at']}"
            item = QtWidgets.QListWidgetItem(label)
            item.setData(Qt.UserRole, m["id"])
            self.history_list.addItem(item)

    def _show_transcript(self, current, _previous=None):
        self.delete_meeting_btn.setEnabled(current is not None)
        if current is None:
            self.transcript.clear()
            return
        meeting_id = current.data(Qt.UserRole)
        parts = []
        for msg in database.list_messages(meeting_id):
            if msg["role"] == "interviewer":
                parts.append(
                    f"<p style='color:#9aa0b2'><b>Interviewer:</b> "
                    f"{html.escape(msg['content'])}</p>"
                )
            else:
                parts.append("<p style='color:#f2a33c'><b>AI</b></p>"
                             + Overlay._format_answer(msg["content"]))
        self.transcript.setHtml("".join(parts) or "<p><i>No transcript.</i></p>")

    # ---- start ----

    def _start(self):
        resume_ids = self.resume.selected_ids()
        jd_ids = self.jd.selected_ids()
        if not resume_ids:
            self._warn("Pick a Resume (import one first if the list is empty).")
            self.nav.setCurrentRow(0)
            return
        if not jd_ids:
            self._warn("Pick a Job Description (import one first if the list is empty).")
            self.nav.setCurrentRow(1)
            return

        self._save_preferences()

        self.language_code = self.language_combo.currentData()
        self.language_name = self.language_combo.currentText()
        database.set_setting("language_code", self.language_code)

        doc_ids = resume_ids + jd_ids + self.projects.selected_ids() + \
            self.references.selected_ids()

        title = self.title_edit.text().strip() or \
            self.company_edit.text().strip() or "Interview"
        company = self.company_edit.text().strip()

        self.meeting_id = database.create_meeting(title, company, doc_ids)
        docs = database.get_meeting_documents(self.meeting_id)
        preferences = database.get_setting("preferences", "")
        self.system_prompt = build_prompt_from_documents(docs, preferences)
        get_logger().info("created meeting %s (title=%r, company=%r, %d documents)",
                          self.meeting_id, title, company, len(doc_ids))
        self.accept()

    def _warn(self, text):
        MessageDialog("Setup", text, parent=self, cancel_text="OK").exec()
