#!/usr/bin/env python3
"""Image Size Checker (Python / PySide6)

Match an original-image folder against a mask-image folder by file name and
check their pixel dimensions in bulk.

 - Jump between size mismatches (previous / next)
 - Define the colors a mask is allowed to contain (palette), find masks that
   contain stray colors, and snap those colors to the nearest palette color
 - Downscale mask images to any size (no cropping, saved to a separate folder)
 - Export the results as CSV

Setup:
    pip install PySide6 Pillow numpy
Run:
    python image_check.py
"""
from __future__ import annotations

import csv
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image
from PySide6.QtCore import QSettings, QSize, Qt, QThread, Signal
from PySide6.QtGui import (
    QBrush, QColor, QIcon, QKeySequence, QPainter, QPalette, QPixmap, QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QColorDialog, QComboBox,
    QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QMessageBox, QProgressBar, QPushButton, QSizePolicy, QSpinBox, QSplitter,
    QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

Image.MAX_IMAGE_PIXELS = None  # do not warn / fail on very large images

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
STATE_LABEL = {"good": "Match", "bad": "Mismatch", "missing": "Missing"}
STATE_COLOR = {"bad": "#fde9e7", "missing": "#fff3d6"}
STRAY_COLOR = "#efe7fd"
ACCENT = {"total": "#5f6b7a", "good": "#16845b", "bad": "#d93025",
          "missing": "#b7791f", "stray": "#7e57c2"}
MAX_PALETTE = 64

STYLE = """
QMainWindow { background: #f5f6f8; }
QLabel { color: #202124; }
QLabel#title { font-size: 22px; font-weight: 700; }
QLabel#subtitle { color: #626a73; }
QLabel#hint { color: #68717c; font-size: 11px; }
QLabel#status { color: #59616c; font-size: 12px; }
QGroupBox {
    background: #ffffff; border: 1px solid #e0e3e8; border-radius: 12px;
    margin-top: 14px; padding: 18px 12px 12px 12px; font-weight: 600; color: #202124;
}
QGroupBox::title { subcontrol-origin: margin; left: 14px; padding: 0 6px; }
QPushButton {
    background: #ffffff; color: #202124; border: 1px solid #cbd1da;
    border-radius: 8px; padding: 7px 14px;
}
QPushButton:hover { background: #f0f2f5; }
QPushButton:pressed { background: #e6e9ee; }
QPushButton:disabled { color: #a0a6ae; background: #f3f4f6; border-color: #e3e6ea; }
QPushButton#primary { background: #202124; color: #ffffff; border-color: #202124; font-weight: 600; }
QPushButton#primary:hover { background: #3a3d42; }
QPushButton#primary:disabled { background: #9aa0a6; border-color: #9aa0a6; color: #ffffff; }
QLineEdit { background: #ffffff; border: 1px solid #cbd1da; border-radius: 8px; padding: 6px 8px; color: #202124; }
QTableWidget {
    background: #ffffff; border: 1px solid #e0e3e8; border-radius: 10px;
    gridline-color: #eef0f3; selection-background-color: #cfe0ff; selection-color: #202124;
}
QListWidget {
    background: #ffffff; border: 1px solid #e0e3e8; border-radius: 8px;
    selection-background-color: #cfe0ff; selection-color: #202124;
}
QListWidget::item { padding: 4px 6px; }
QHeaderView::section {
    background: #f8f9fb; color: #535c67; border: none; border-bottom: 1px solid #e0e3e8;
    padding: 8px; font-weight: 600;
}
QProgressBar { border: none; background: #e6e9ee; border-radius: 4px; height: 8px; }
QProgressBar::chunk { background: #202124; border-radius: 4px; }
QSplitter::handle { background: transparent; }
QTabWidget::pane { border: none; }
QTabBar::tab {
    padding: 8px 16px; background: transparent; color: #626a73;
    border: none; border-bottom: 2px solid transparent;
}
QTabBar::tab:selected { color: #202124; border-bottom: 2px solid #202124; font-weight: 600; }
"""


# ----------------------------------------------------------------------------
# Data handling
# ----------------------------------------------------------------------------
@dataclass
class Row:
    key: str
    original: Path | None
    mask: Path | None
    o_size: tuple | None = None
    m_size: tuple | None = None
    o_err: bool = False
    m_err: bool = False
    state: str = "good"
    message: str = ""
    # Color check results (filled by "Check Colors")
    c_checked: bool = False
    off_colors: int = 0
    off_pixels: int = 0
    total_px: int = 0

    @property
    def stray(self) -> bool:
        return self.c_checked and self.off_pixels > 0


def collect_images(folder: Path) -> dict[str, Path]:
    """Collect images (including subfolders) as {lowercased relative path: Path}."""
    found: dict[str, Path] = {}
    for dirpath, _dirs, files in os.walk(folder):
        for name in sorted(files):
            p = Path(dirpath) / name
            if p.suffix.lower() not in IMAGE_EXTS or name.startswith("."):
                continue
            key = p.relative_to(folder).as_posix().lower()
            found.setdefault(key, p)
    return found


def get_size(path: Path):
    try:
        with Image.open(path) as im:
            return im.size  # (width, height); pixel data is not decoded
    except Exception:
        return None


def build_row(key: str, o: Path | None, m: Path | None) -> Row:
    r = Row(key, o, m)
    if o:
        r.o_size = get_size(o)
        r.o_err = r.o_size is None
    if m:
        r.m_size = get_size(m)
        r.m_err = r.m_size is None
    if not o or not m:
        r.state = "missing"
        r.message = "Original image is missing" if not o else "Mask image is missing"
    elif r.o_err or r.m_err:
        r.state = "bad"
        r.message = "Cannot read image"
    elif r.o_size != r.m_size:
        r.state = "bad"
        r.message = "Width or height does not match"
    else:
        r.state = "good"
        r.message = "Width and height match"
    return r


def size_text(path, size, err) -> str:
    if path is None:
        return "—"
    if err or size is None:
        return "Load failed"
    return f"{size[0]} × {size[1]} px"


def color_text(r: Row) -> str:
    if r.mask is None or r.m_err:
        return "—"
    if not r.c_checked:
        return "—"
    if r.off_pixels == 0:
        return "OK"
    pct = 100.0 * r.off_pixels / max(1, r.total_px)
    return f"{r.off_colors} stray ({pct:.2f}% px)"


def target_size(r: Row, p: dict):
    """Return the resized (w, h), or None if this image should be skipped."""
    sw, sh = r.m_size
    if p["mode"] == "match":
        if not r.original or r.o_err or r.o_size is None:
            return None
        tw, th = r.o_size
    else:
        w, h = p["w"], p["h"]
        if w <= 0 and h <= 0:
            return None
        if p["keep"]:
            if w > 0 and h > 0:
                scale = min(w / sw, h / sh)
            elif w > 0:
                scale = w / sw
            else:
                scale = h / sh
            tw, th = max(1, round(sw * scale)), max(1, round(sh * scale))
        else:
            tw = w if w > 0 else sw
            th = h if h > 0 else sh
    if (tw, th) == (sw, sh):
        return None
    if p["shrink_only"] and (tw > sw or th > sh):
        return None
    return tw, th


def save_image(img: Image.Image, out: Path):
    out.parent.mkdir(parents=True, exist_ok=True)
    kwargs = {}
    if out.suffix.lower() in (".jpg", ".jpeg"):
        kwargs["quality"] = 95
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
    img.save(out, **kwargs)


# ---- color helpers ----------------------------------------------------------
def to_hex(c) -> str:
    return "#%02x%02x%02x" % tuple(int(v) for v in c)


def pack_rgb(arr: np.ndarray) -> np.ndarray:
    """(…,3) uint8 array -> flat uint32 array of 0xRRGGBB keys."""
    a = arr.reshape(-1, 3).astype(np.uint32)
    return (a[:, 0] << 16) | (a[:, 1] << 8) | a[:, 2]


def palette_keys(palette) -> np.ndarray:
    return np.array([(r << 16) | (g << 8) | b for r, g, b in palette], dtype=np.uint32)


def dominant_colors(path: Path, min_share: float, limit: int = 16):
    """Most frequent colors of an image (share >= min_share), most frequent first."""
    with Image.open(path) as im:
        arr = np.asarray(im.convert("RGB"))
    u, c = np.unique(pack_rgb(arr), return_counts=True)
    order = np.argsort(-c)
    total = c.sum()
    colors = []
    for i in order[:limit]:
        if c[i] / total < min_share:
            break
        k = int(u[i])
        colors.append(((k >> 16) & 255, (k >> 8) & 255, k & 255))
    return colors


def snap_image(im: Image.Image, palette) -> Image.Image:
    """Replace every pixel color by the nearest palette color (RGB distance)."""
    pal = np.array(palette, dtype=np.int32)
    has_alpha = "A" in im.getbands() or "transparency" in im.info
    gray_pal = bool(np.all(pal[:, 0] == pal[:, 1]) and np.all(pal[:, 1] == pal[:, 2]))
    was_gray = im.mode == "L"
    rgba = np.asarray(im.convert("RGBA"))
    h, w = rgba.shape[:2]
    keys = pack_rgb(rgba[..., :3])
    u, inv = np.unique(keys, return_inverse=True)
    ucol = np.stack([(u >> 16) & 255, (u >> 8) & 255, u & 255], axis=1).astype(np.int32)
    best = np.full(len(u), np.iinfo(np.int64).max, dtype=np.int64)
    idx = np.zeros(len(u), dtype=np.int64)
    for j, p in enumerate(pal):  # loop over palette to keep memory small
        d = ((ucol - p) ** 2).sum(axis=1).astype(np.int64)
        m = d < best
        best[m] = d[m]
        idx[m] = j
    out = pal.astype(np.uint8)[idx][inv.reshape(-1)].reshape(h, w, 3)
    if has_alpha:
        return Image.fromarray(np.dstack([out, rgba[..., 3]]))
    if was_gray and gray_pal:
        return Image.fromarray(out[..., 0])
    return Image.fromarray(out)


# ----------------------------------------------------------------------------
# Background workers
# ----------------------------------------------------------------------------
class ScanWorker(QThread):
    progress = Signal(int, int)
    done = Signal(list)

    def __init__(self, orig_dir: Path, mask_dir: Path):
        super().__init__()
        self.orig_dir, self.mask_dir = orig_dir, mask_dir

    def run(self):
        originals = collect_images(self.orig_dir)
        masks = collect_images(self.mask_dir)
        keys = sorted(set(originals) | set(masks))
        rows = []
        for i, k in enumerate(keys, 1):
            rows.append(build_row(k, originals.get(k), masks.get(k)))
            if i % 20 == 0 or i == len(keys):
                self.progress.emit(i, len(keys))
        self.done.emit(rows)


class ColorScanWorker(QThread):
    progress = Signal(int, int)
    done = Signal()

    def __init__(self, rows, palette):
        super().__init__()
        self.rows, self.palette = rows, palette

    def run(self):
        keys = palette_keys(self.palette)
        targets = [r for r in self.rows if r.mask and not r.m_err]
        for i, r in enumerate(targets, 1):
            try:
                with Image.open(r.mask) as im:
                    arr = np.asarray(im.convert("RGB"))
                u, c = np.unique(pack_rgb(arr), return_counts=True)
                off = ~np.isin(u, keys)
                r.off_colors = int(off.sum())
                r.off_pixels = int(c[off].sum())
                r.total_px = int(c.sum())
                r.c_checked = True
            except Exception:
                r.c_checked = False
            self.progress.emit(i, len(targets))
        self.done.emit()


class ColorFixWorker(QThread):
    progress = Signal(int, int)
    done = Signal(int, int, int)  # fixed, copied/skipped, errors

    def __init__(self, rows, mask_dir: Path, out_dir: Path, palette, copy_clean: bool):
        super().__init__()
        self.rows, self.mask_dir, self.out_dir = rows, mask_dir, out_dir
        self.palette, self.copy_clean = palette, copy_clean

    def run(self):
        fixed = other = errors = 0
        total = len(self.rows)
        for i, r in enumerate(self.rows, 1):
            self.progress.emit(i, total)
            try:
                out = self.out_dir / r.mask.relative_to(self.mask_dir)
                if r.stray:
                    with Image.open(r.mask) as im:
                        im.load()
                        save_image(snap_image(im, self.palette), out)
                    fixed += 1
                elif self.copy_clean:
                    out.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(r.mask, out)
                    other += 1
                else:
                    other += 1
            except Exception:
                errors += 1
        self.done.emit(fixed, other, errors)


class ResizeWorker(QThread):
    progress = Signal(int, int)
    done = Signal(int, int, int)  # resized, skipped, errors

    def __init__(self, rows, mask_dir: Path, out_dir: Path, params: dict):
        super().__init__()
        self.rows, self.mask_dir, self.out_dir, self.params = rows, mask_dir, out_dir, params

    def run(self):
        done = skipped = errors = 0
        smooth = self.params["interp"] == "smooth"
        resample = Image.LANCZOS if smooth else Image.NEAREST
        total = len(self.rows)
        for i, r in enumerate(self.rows, 1):
            self.progress.emit(i, total)
            t = target_size(r, self.params)
            if t is None:
                skipped += 1
                continue
            try:
                out = self.out_dir / r.mask.relative_to(self.mask_dir)
                with Image.open(r.mask) as im:
                    im.load()
                    try:
                        resized = im.resize(t, resample)  # scales the whole image (no cropping)
                    except ValueError:
                        resized = im.resize(t, Image.NEAREST)
                    if self.params["snap"]:
                        resized = snap_image(resized, self.params["palette"])
                    save_image(resized, out)
                done += 1
            except Exception:
                errors += 1
        self.done.emit(done, skipped, errors)


# ----------------------------------------------------------------------------
# UI components
# ----------------------------------------------------------------------------
class Chip(QFrame):
    """Small stat card. Emits `clicked` when pressed (if clickable)."""
    clicked = Signal()

    def __init__(self, title: str, accent: str, tooltip: str = ""):
        super().__init__()
        self.setObjectName("chip")
        self.setStyleSheet(
            f"QFrame#chip {{ background:#ffffff; border:1px solid #e0e3e8; "
            f"border-left:4px solid {accent}; border-radius:10px; }}"
            f"QFrame#chip QLabel {{ background:transparent; border:none; }}"
        )
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 8, 12, 8)
        v.setSpacing(0)
        t = QLabel(title)
        t.setStyleSheet("color:#626a73;font-size:11px;")
        self.value = QLabel("0")
        self.value.setStyleSheet("font-size:22px;font-weight:700;")
        v.addWidget(t)
        v.addWidget(self.value)
        self._clickable = bool(tooltip)
        if self._clickable:
            self.setCursor(Qt.PointingHandCursor)
            self.setToolTip(tooltip)

    def set_value(self, n):
        self.value.setText(str(n))

    def mousePressEvent(self, e):
        if self._clickable:
            self.clicked.emit()
        super().mousePressEvent(e)


class ImageView(QLabel):
    """Preview that scales to fit its area."""

    def __init__(self):
        super().__init__("No image")
        self._pm: QPixmap | None = None
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(120, 100)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.setStyleSheet("background:#ffffff;border:1px solid #e0e3e8;border-radius:10px;color:#8a919b;")

    def show_path(self, path: Path | None):
        pm = QPixmap(str(path)) if path else QPixmap()
        if path is None or pm.isNull():
            self._pm = None
            self.clear()
            self.setText("No image" if path is None else "Failed to load")
        else:
            self._pm = pm
            self._rescale()

    def _rescale(self):
        if self._pm:
            self.setPixmap(self._pm.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._rescale()


def swatch_icon(rgb) -> QIcon:
    pm = QPixmap(20, 20)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(*rgb))
    p.setPen(QColor("#9aa0a6"))
    p.drawRoundedRect(1, 1, 17, 17, 4, 4)
    p.end()
    return QIcon(pm)


# ----------------------------------------------------------------------------
# Main window
# ----------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Image Size Checker")
        self.resize(1400, 900)
        self.rows: list[Row] = []
        self.orig_dir: Path | None = None
        self.mask_dir: Path | None = None
        self.worker: QThread | None = None
        self.palette: list[tuple[int, int, int]] = []
        self.settings = QSettings("ImageCheck", "ImageCheck")
        self._build_ui()
        self._load_palette()

    # ---- UI construction ----
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(18, 14, 18, 14)
        hsplit = QSplitter(Qt.Horizontal)
        root.addWidget(hsplit)

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 8, 0)
        lv.setSpacing(10)

        title = QLabel("Image Size Checker")
        title.setObjectName("title")
        subtitle = QLabel("Match original and mask images by file name, verify their dimensions "
                          "and keep mask colors clean.")
        subtitle.setObjectName("subtitle")
        lv.addWidget(title)
        lv.addWidget(subtitle)

        folders = QGroupBox("Folders")
        fl = QFormLayout(folders)
        fl.setHorizontalSpacing(12)
        self.orig_edit, self.mask_edit = QLineEdit(), QLineEdit()
        self.orig_edit.setPlaceholderText("Folder with the original images")
        self.mask_edit.setPlaceholderText("Folder with the mask images")
        fl.addRow("1. Originals", self._folder_row(self.orig_edit))
        fl.addRow("2. Masks", self._folder_row(self.mask_edit))
        lv.addWidget(folders)

        ctl = QHBoxLayout()
        self.check_btn = QPushButton("Check Sizes")
        self.check_btn.setObjectName("primary")
        self.check_btn.setDefault(True)
        self.check_btn.clicked.connect(self.start_scan)
        self.csv_btn = QPushButton("Export CSV")
        self.csv_btn.setEnabled(False)
        self.csv_btn.clicked.connect(self.export_csv)
        self.filter_box = QComboBox()
        for label, data in [("All", "all"), ("Size mismatch", "bad"),
                            ("Missing counterpart", "missing"), ("Size match", "good"),
                            ("Stray colors", "stray")]:
            self.filter_box.addItem(label, data)
        self.filter_box.currentIndexChanged.connect(self.refill_table)
        ctl.addWidget(self.check_btn)
        ctl.addWidget(self.csv_btn)
        ctl.addStretch(1)
        ctl.addWidget(QLabel("Show:"))
        ctl.addWidget(self.filter_box)
        lv.addLayout(ctl)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(8)
        self.progress.setVisible(False)
        lv.addWidget(self.progress)

        chips = QHBoxLayout()
        self.chip_total = Chip("TOTAL", ACCENT["total"])
        self.chip_good = Chip("MATCH", ACCENT["good"])
        self.chip_bad = Chip("MISMATCH", ACCENT["bad"], "Click to jump to the next size mismatch")
        self.chip_missing = Chip("MISSING", ACCENT["missing"])
        self.chip_stray = Chip("STRAY COLORS", ACCENT["stray"], "Click to jump to the next mask with stray colors")
        self.chip_bad.clicked.connect(lambda: self.jump(1, "bad"))
        self.chip_stray.clicked.connect(lambda: self.jump(1, "stray"))
        for c in (self.chip_total, self.chip_good, self.chip_bad, self.chip_missing, self.chip_stray):
            chips.addWidget(c, 1)
        lv.addLayout(chips)

        self.status_label = QLabel("Select both folders, then press \"Check Sizes\".")
        self.status_label.setObjectName("status")
        lv.addWidget(self.status_label)

        # Navigation rows
        self.prev_btn, self.next_btn, self.jump_label = self._nav_row(lv, "bad", "mismatch", "Ctrl/⌘ + ↑ / ↓")
        self.sprev_btn, self.snext_btn, self.sjump_label = self._nav_row(lv, "stray", "stray colors", "Ctrl/⌘ + Shift + ↑ / ↓")
        QShortcut(QKeySequence("Ctrl+Down"), self, activated=lambda: self.jump(1, "bad"))
        QShortcut(QKeySequence("Ctrl+Up"), self, activated=lambda: self.jump(-1, "bad"))
        QShortcut(QKeySequence("Ctrl+Shift+Down"), self, activated=lambda: self.jump(1, "stray"))
        QShortcut(QKeySequence("Ctrl+Shift+Up"), self, activated=lambda: self.jump(-1, "stray"))

        # Results table + previews
        vsplit = QSplitter(Qt.Vertical)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["File", "Result", "Original", "Mask", "Colors", "Details"])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(30)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in (1, 2, 3, 4, 5):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.table.itemSelectionChanged.connect(self.on_select)
        vsplit.addWidget(self.table)

        prev = QWidget()
        pl = QHBoxLayout(prev)
        pl.setContentsMargins(0, 6, 0, 0)
        self.o_title, self.m_title = QLabel("Original"), QLabel("Mask")
        self.o_view, self.m_view = ImageView(), ImageView()
        for t, view in ((self.o_title, self.o_view), (self.m_title, self.m_view)):
            t.setStyleSheet("font-weight:600;")
            col = QVBoxLayout()
            col.addWidget(t)
            col.addWidget(view, 1)
            pl.addLayout(col, 1)
        vsplit.addWidget(prev)
        vsplit.setSizes([380, 300])
        lv.addWidget(vsplit, 1)
        hsplit.addWidget(left)

        # Right: tabs
        tabs = QTabWidget()
        tabs.addTab(self._build_colors_tab(), "Colors")
        tabs.addTab(self._build_resize_tab(), "Resize")
        tabs.setMinimumWidth(380)
        hsplit.addWidget(tabs)
        hsplit.setSizes([980, 400])
        hsplit.setStretchFactor(0, 1)

    def _nav_row(self, layout: QVBoxLayout, kind: str, noun: str, shortcut: str):
        row = QHBoxLayout()
        prev_btn = QPushButton(f"◀ Prev {noun}")
        next_btn = QPushButton(f"Next {noun} ▶")
        label = QLabel("")
        label.setAlignment(Qt.AlignCenter)
        label.setMinimumWidth(150)
        label.setStyleSheet("font-weight:600;")
        prev_btn.clicked.connect(lambda: self.jump(-1, kind))
        next_btn.clicked.connect(lambda: self.jump(1, kind))
        prev_btn.setEnabled(False)
        next_btn.setEnabled(False)
        hint = QLabel(shortcut)
        hint.setObjectName("hint")
        row.addWidget(prev_btn)
        row.addWidget(label)
        row.addWidget(next_btn)
        row.addSpacing(8)
        row.addWidget(hint)
        row.addStretch(1)
        layout.addLayout(row)
        return prev_btn, next_btn, label

    def _folder_row(self, edit: QLineEdit) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        btn = QPushButton("Browse…")
        btn.clicked.connect(lambda: self.pick_folder(edit))
        h.addWidget(edit, 1)
        h.addWidget(btn)
        return w

    # ---- Colors tab ----
    def _build_colors_tab(self) -> QWidget:
        page = QWidget()
        pv = QVBoxLayout(page)
        pv.setContentsMargins(0, 8, 0, 0)
        box = QGroupBox("Mask Palette & Color Cleanup")
        v = QVBoxLayout(box)
        v.setSpacing(10)

        note = QLabel("List the colors a mask is allowed to contain. Masks with any other color "
                      "are flagged, and their stray colors can be snapped to the nearest palette color.")
        note.setWordWrap(True)
        note.setObjectName("hint")
        v.addWidget(note)

        self.palette_list = QListWidget()
        self.palette_list.setIconSize(QSize(20, 20))
        self.palette_list.setMinimumHeight(150)
        self.palette_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        v.addWidget(self.palette_list)

        row1 = QHBoxLayout()
        add_btn = QPushButton("Add color…")
        add_btn.clicked.connect(self.add_palette_color)
        rm_btn = QPushButton("Remove")
        rm_btn.clicked.connect(self.remove_palette_color)
        clr_btn = QPushButton("Clear")
        clr_btn.clicked.connect(self.clear_palette)
        row1.addWidget(add_btn)
        row1.addWidget(rm_btn)
        row1.addWidget(clr_btn)
        v.addLayout(row1)

        row2 = QHBoxLayout()
        from_btn = QPushButton("Add from selected mask")
        from_btn.clicked.connect(self.add_from_selected)
        self.min_share = QDoubleSpinBox()
        self.min_share.setRange(0.01, 50.0)
        self.min_share.setValue(0.5)
        self.min_share.setDecimals(2)
        self.min_share.setSuffix(" % min")
        self.min_share.setToolTip("Only colors covering at least this share of the mask's pixels are added")
        row2.addWidget(from_btn, 1)
        row2.addWidget(self.min_share)
        v.addLayout(row2)

        self.color_check_btn = QPushButton("Check Colors")
        self.color_check_btn.setObjectName("primary")
        self.color_check_btn.setEnabled(False)
        self.color_check_btn.clicked.connect(self.start_color_scan)
        v.addWidget(self.color_check_btn)
        self.color_status = QLabel("")
        self.color_status.setWordWrap(True)
        self.color_status.setObjectName("status")
        v.addWidget(self.color_status)

        v.addWidget(QLabel("Fix stray colors (snap to palette)"))
        self.color_out_edit = QLineEdit()
        self.color_out_edit.setPlaceholderText("Where corrected masks are saved")
        v.addWidget(self._folder_row(self.color_out_edit))
        self.copy_clean = QCheckBox("Also copy masks that need no change")
        v.addWidget(self.copy_clean)
        self.color_fix_btn = QPushButton("Snap Stray Colors to Palette")
        self.color_fix_btn.setObjectName("primary")
        self.color_fix_btn.setEnabled(False)
        self.color_fix_btn.clicked.connect(self.start_color_fix)
        v.addWidget(self.color_fix_btn)
        self.fix_status = QLabel("")
        self.fix_status.setWordWrap(True)
        self.fix_status.setObjectName("status")
        v.addWidget(self.fix_status)

        tips = QLabel(
            "• Colors are compared exactly (RGB). Alpha is ignored when checking.\n"
            "• Each stray color is replaced by the closest palette color (RGB distance).\n"
            "• Saved with the same file names and folder structure. Use PNG — JPEG "
            "compression creates new stray colors.\n"
            "• Your palette is remembered between runs."
        )
        tips.setWordWrap(True)
        tips.setObjectName("hint")
        v.addWidget(tips)
        v.addStretch(1)
        pv.addWidget(box)
        return page

    # ---- Resize tab ----
    def _build_resize_tab(self) -> QWidget:
        page = QWidget()
        pv = QVBoxLayout(page)
        pv.setContentsMargins(0, 8, 0, 0)
        box = QGroupBox("Resize Masks (Downscale)")
        v = QVBoxLayout(box)
        v.setSpacing(10)
        note = QLabel("Scales the whole image — nothing is cropped. Results are saved to a "
                      "separate folder; your original files are never modified.")
        note.setWordWrap(True)
        note.setObjectName("hint")
        v.addWidget(note)

        form = QFormLayout()
        form.setHorizontalSpacing(12)
        self.rz_target = QComboBox()
        self.rz_target.addItem("All mask images", "all")
        self.rz_target.addItem("Mismatched masks only", "bad")
        self.rz_mode = QComboBox()
        self.rz_mode.addItem("Custom size", "custom")
        self.rz_mode.addItem("Same size as matching original", "match")
        self.rz_mode.currentIndexChanged.connect(self.sync_resize_ui)
        self.rz_w, self.rz_h = QSpinBox(), QSpinBox()
        for sp in (self.rz_w, self.rz_h):
            sp.setRange(0, 100000)
            sp.setSpecialValueText("Auto")
            sp.setSuffix(" px")
        size_row = QHBoxLayout()
        size_row.setContentsMargins(0, 0, 0, 0)
        size_row.addWidget(QLabel("W"))
        size_row.addWidget(self.rz_w, 1)
        size_row.addWidget(QLabel("H"))
        size_row.addWidget(self.rz_h, 1)
        self.size_widget = QWidget()
        self.size_widget.setLayout(size_row)
        self.rz_interp = QComboBox()
        self.rz_interp.addItem("Nearest neighbor (best for masks)", "nearest")
        self.rz_interp.addItem("Smooth (LANCZOS)", "smooth")
        form.addRow("Apply to", self.rz_target)
        form.addRow("Size mode", self.rz_mode)
        form.addRow("Size", self.size_widget)
        form.addRow("Interpolation", self.rz_interp)
        v.addLayout(form)

        self.rz_keep = QCheckBox("Keep aspect ratio (fit inside the given size)")
        self.rz_keep.setChecked(True)
        self.rz_shrink = QCheckBox("Downscale only (skip images that would grow)")
        self.rz_shrink.setChecked(True)
        self.rz_snap = QCheckBox("Snap colors to the palette after resizing")
        self.rz_snap.setToolTip("Uses the palette from the Colors tab to remove colors created by resizing")
        v.addWidget(self.rz_keep)
        v.addWidget(self.rz_shrink)
        v.addWidget(self.rz_snap)

        v.addWidget(QLabel("Output folder"))
        self.out_edit = QLineEdit()
        self.out_edit.setPlaceholderText("Where resized masks are saved")
        v.addWidget(self._folder_row(self.out_edit))

        self.rz_btn = QPushButton("Resize and Save")
        self.rz_btn.setObjectName("primary")
        self.rz_btn.setEnabled(False)
        self.rz_btn.clicked.connect(self.start_resize)
        v.addWidget(self.rz_btn)

        self.rz_status = QLabel("")
        self.rz_status.setWordWrap(True)
        self.rz_status.setObjectName("status")
        v.addWidget(self.rz_status)

        tips = QLabel(
            "• Keep aspect ratio: setting only W or only H also works.\n"
            "• Turn it off to stretch to the exact W × H (Auto = keep that side as is).\n"
            "• File names, formats and folder structure are preserved."
        )
        tips.setWordWrap(True)
        tips.setObjectName("hint")
        v.addWidget(tips)
        v.addStretch(1)
        pv.addWidget(box)
        self.sync_resize_ui()
        return page

    def sync_resize_ui(self):
        match = self.rz_mode.currentData() == "match"
        self.size_widget.setEnabled(not match)
        self.rz_keep.setEnabled(not match)

    # ---- Folder selection ----
    def pick_folder(self, edit: QLineEdit):
        d = QFileDialog.getExistingDirectory(self, "Select folder", edit.text() or str(Path.home()))
        if d:
            edit.setText(d)

    # ---- Palette ----
    def _load_palette(self):
        text = self.settings.value("palette", "", type=str) or ""
        for h in [t for t in text.split(",") if t]:
            c = QColor(h)
            if c.isValid() and (c.red(), c.green(), c.blue()) not in self.palette:
                self.palette.append((c.red(), c.green(), c.blue()))
        self._refresh_palette_list()

    def _refresh_palette_list(self):
        self.palette_list.clear()
        for rgb in self.palette:
            item = QListWidgetItem(swatch_icon(rgb), f"{to_hex(rgb)}   RGB {rgb[0]}, {rgb[1]}, {rgb[2]}")
            self.palette_list.addItem(item)
        self._update_color_buttons()

    def _palette_changed(self):
        self.settings.setValue("palette", ",".join(to_hex(c) for c in self.palette))
        self._refresh_palette_list()
        # Earlier color-check results no longer apply to the new palette
        for r in self.rows:
            r.c_checked = False
            r.off_colors = r.off_pixels = r.total_px = 0
        if self.rows:
            self.refill_table()
        self.color_status.setText("Palette changed — run \"Check Colors\" again.")

    def add_palette_color(self):
        start = QColor(*self.palette[-1]) if self.palette else QColor("#ffffff")
        c = QColorDialog.getColor(start, self, "Add mask color")
        if not c.isValid():
            return
        rgb = (c.red(), c.green(), c.blue())
        if rgb in self.palette:
            self.color_status.setText(f"{to_hex(rgb)} is already in the palette.")
            return
        if len(self.palette) >= MAX_PALETTE:
            self.color_status.setText(f"The palette is limited to {MAX_PALETTE} colors.")
            return
        self.palette.append(rgb)
        self._palette_changed()

    def remove_palette_color(self):
        rows = sorted({self.palette_list.row(i) for i in self.palette_list.selectedItems()}, reverse=True)
        if not rows:
            return
        for r in rows:
            del self.palette[r]
        self._palette_changed()

    def clear_palette(self):
        if self.palette:
            self.palette.clear()
            self._palette_changed()

    def add_from_selected(self):
        r = self.current_row()
        if r is None or r.mask is None or r.m_err:
            QMessageBox.information(self, "Palette", "Select a row that has a readable mask image first.")
            return
        try:
            colors = dominant_colors(r.mask, self.min_share.value() / 100.0, 16)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Palette", f"Could not read the mask: {e}")
            return
        added = 0
        for rgb in colors:
            if rgb not in self.palette and len(self.palette) < MAX_PALETTE:
                self.palette.append(rgb)
                added += 1
        self._palette_changed()
        self.color_status.setText(f"Added {added} color(s) from the selected mask. "
                                  "Remove any that should not be in the palette.")

    # ---- Scan ----
    def start_scan(self):
        o_text, m_text = self.orig_edit.text().strip(), self.mask_edit.text().strip()
        if not (o_text and m_text):
            QMessageBox.warning(self, "Check Sizes", "Please select both the originals and the masks folders.")
            return
        o, m = Path(o_text), Path(m_text)
        if not o.is_dir() or not m.is_dir():
            QMessageBox.warning(self, "Check Sizes", "One of the selected folders does not exist.")
            return
        self.orig_dir, self.mask_dir = o, m
        self.set_busy(True)
        self.progress.setRange(0, 0)
        self.status_label.setText("Checking images…")
        self.worker = ScanWorker(o, m)
        self.worker.progress.connect(self.on_progress)
        self.worker.done.connect(self.on_scan_done)
        self.worker.start()

    def on_progress(self, i, total):
        self.progress.setRange(0, total)
        self.progress.setValue(i)

    def on_scan_done(self, rows):
        self.rows = rows
        self.set_busy(False)
        self.csv_btn.setEnabled(bool(rows))
        self.refill_table()

    def _update_color_buttons(self):
        busy = self.worker is not None and self.worker.isRunning()
        has_rows = bool(self.rows)
        self.color_check_btn.setEnabled(not busy and has_rows and bool(self.palette))
        self.color_fix_btn.setEnabled(not busy and has_rows and bool(self.palette))

    def set_busy(self, busy: bool):
        self.progress.setVisible(busy)
        self.check_btn.setEnabled(not busy)
        self.rz_btn.setEnabled(not busy and bool(self.rows))
        if busy:
            self.color_check_btn.setEnabled(False)
            self.color_fix_btn.setEnabled(False)
        else:
            # worker may still report isRunning() for a moment; decide from state
            has_rows = bool(self.rows)
            self.color_check_btn.setEnabled(has_rows and bool(self.palette))
            self.color_fix_btn.setEnabled(has_rows and bool(self.palette))

    # ---- Results ----
    @staticmethod
    def row_visible(r: Row, flt: str) -> bool:
        if flt == "all":
            return True
        if flt == "stray":
            return r.stray
        return r.state == flt

    def refill_table(self):
        flt = self.filter_box.currentData()
        visible = [(i, r) for i, r in enumerate(self.rows) if self.row_visible(r, flt)]
        self.table.blockSignals(True)
        self.table.setRowCount(len(visible))
        for t, (i, r) in enumerate(visible):
            cells = [r.key, STATE_LABEL[r.state],
                     size_text(r.original, r.o_size, r.o_err),
                     size_text(r.mask, r.m_size, r.m_err),
                     color_text(r), r.message]
            row_bg = STATE_COLOR.get(r.state) or (STRAY_COLOR if r.stray else None)
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if c == 0:
                    item.setData(Qt.UserRole, i)
                bg = row_bg
                if c == 4 and r.stray:
                    bg = STRAY_COLOR
                if bg:
                    item.setBackground(QBrush(QColor(bg)))
                item.setForeground(QBrush(QColor("#202124")))
                self.table.setItem(t, c, item)
        self.table.blockSignals(False)
        good = sum(r.state == "good" for r in self.rows)
        bad = sum(r.state == "bad" for r in self.rows)
        miss = sum(r.state == "missing" for r in self.rows)
        stray = sum(r.stray for r in self.rows)
        self.chip_total.set_value(len(self.rows))
        self.chip_good.set_value(good)
        self.chip_bad.set_value(bad)
        self.chip_missing.set_value(miss)
        checked = any(r.c_checked for r in self.rows)
        self.chip_stray.set_value(stray if checked else "—")
        self.status_label.setText(
            f"Done — {len(visible)} of {len(self.rows)} shown. "
            "Files are matched by relative path, including the extension.")
        self._update_color_buttons()
        self.on_select()

    def current_row(self) -> Row | None:
        t = self.table.currentRow()
        if t < 0 or self.table.item(t, 0) is None:
            return None
        return self.rows[self.table.item(t, 0).data(Qt.UserRole)]

    def on_select(self):
        r = self.current_row()
        if r is None:
            self.o_view.show_path(None)
            self.m_view.show_path(None)
            self.o_title.setText("Original")
            self.m_title.setText("Mask")
        else:
            self.o_title.setText(f"Original · {size_text(r.original, r.o_size, r.o_err)}")
            extra = f" · {color_text(r)}" if r.c_checked else ""
            self.m_title.setText(f"Mask · {size_text(r.mask, r.m_size, r.m_err)}{extra}")
            self.o_view.show_path(r.original)
            self.m_view.show_path(r.mask)
        self.update_jump_labels()

    # ---- Navigation (mismatch / stray colors) ----
    @staticmethod
    def is_kind(r: Row, kind: str) -> bool:
        return r.state == "bad" if kind == "bad" else r.stray

    def kind_table_rows(self, kind: str) -> list[int]:
        return [t for t in range(self.table.rowCount())
                if self.is_kind(self.rows[self.table.item(t, 0).data(Qt.UserRole)], kind)]

    def _update_label(self, kind, label, prev_btn, next_btn, noun):
        total = sum(self.is_kind(r, kind) for r in self.rows)
        prev_btn.setEnabled(total > 0)
        next_btn.setEnabled(total > 0)
        cur = self.table.currentRow()
        rows = self.kind_table_rows(kind)
        if cur in rows:
            label.setText(f"{noun} {rows.index(cur) + 1} / {len(rows)}")
        else:
            label.setText(f"{noun}: {total}" if (kind == "bad" or any(r.c_checked for r in self.rows))
                          else f"{noun}: not checked")

    def update_jump_labels(self):
        self._update_label("bad", self.jump_label, self.prev_btn, self.next_btn, "Mismatch")
        self._update_label("stray", self.sjump_label, self.sprev_btn, self.snext_btn, "Stray")

    def jump(self, direction: int, kind: str = "bad"):
        if not any(self.is_kind(r, kind) for r in self.rows):
            return
        # If the current filter hides these rows, switch back to "All"
        if self.filter_box.currentData() not in ("all", kind):
            self.filter_box.setCurrentIndex(0)
        rows = self.kind_table_rows(kind)
        if not rows:
            return
        cur = self.table.currentRow()
        if direction > 0:
            nxt = next((t for t in rows if t > cur), rows[0])
        else:
            nxt = next((t for t in reversed(rows) if t < cur), rows[-1])
        self.table.selectRow(nxt)
        self.table.scrollToItem(self.table.item(nxt, 0), QAbstractItemView.PositionAtCenter)
        self.table.setFocus()

    # ---- CSV ----
    def export_csv(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export CSV", "image_check.csv", "CSV (*.csv)")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, quoting=csv.QUOTE_ALL)
            w.writerow(["File", "Result", "Original width", "Original height",
                        "Mask width", "Mask height", "Stray colors", "Stray pixels (%)", "Details"])
            for r in self.rows:
                ow, oh = (r.o_size if r.o_size else ("", ""))
                mw, mh = (r.m_size if r.m_size else ("", ""))
                sc = r.off_colors if r.c_checked else ""
                sp = f"{100.0 * r.off_pixels / max(1, r.total_px):.4f}" if r.c_checked else ""
                w.writerow([r.key, STATE_LABEL[r.state], ow, oh, mw, mh, sc, sp, r.message])
        self.status_label.setText(f"CSV exported: {path}")

    # ---- Color check / fix ----
    def start_color_scan(self):
        if not self.palette:
            QMessageBox.information(self, "Check Colors", "Add at least one color to the palette first.")
            return
        if not self.rows:
            QMessageBox.information(self, "Check Colors", "Run \"Check Sizes\" first.")
            return
        self.set_busy(True)
        self.progress.setRange(0, 0)
        self.color_status.setText("Checking mask colors…")
        self.worker = ColorScanWorker(self.rows, list(self.palette))
        self.worker.progress.connect(self.on_progress)
        self.worker.done.connect(self.on_color_scan_done)
        self.worker.start()

    def on_color_scan_done(self):
        self.set_busy(False)
        stray = sum(r.stray for r in self.rows)
        checked = sum(r.c_checked for r in self.rows)
        self.refill_table()
        self.color_status.setText(
            f"Checked {checked} mask(s): {stray} contain colors outside the palette. "
            "Use the \"Stray colors\" filter or the stray navigation to review them.")

    def _validate_out_dir(self, text: str, title: str) -> Path | None:
        if not text.strip():
            QMessageBox.warning(self, title, "Please choose an output folder.")
            return None
        out_dir = Path(text.strip())
        for d in (self.mask_dir, self.orig_dir):
            if d and d.resolve() == out_dir.resolve():
                QMessageBox.warning(self, title,
                                    "The output folder must differ from the originals and masks folders.")
                return None
        return out_dir

    def start_color_fix(self):
        if not self.palette:
            QMessageBox.information(self, "Snap Colors", "Add at least one color to the palette first.")
            return
        if not any(r.c_checked for r in self.rows):
            QMessageBox.information(self, "Snap Colors", "Run \"Check Colors\" first.")
            return
        targets = [r for r in self.rows if r.mask and not r.m_err and r.c_checked
                   and (r.stray or self.copy_clean.isChecked())]
        if not any(r.stray for r in targets):
            QMessageBox.information(self, "Snap Colors", "No mask contains colors outside the palette.")
            return
        out_dir = self._validate_out_dir(self.color_out_edit.text(), "Snap Colors")
        if out_dir is None:
            return
        out_dir.mkdir(parents=True, exist_ok=True)
        self.set_busy(True)
        self.progress.setRange(0, len(targets))
        self.fix_status.setText("Snapping colors…")
        self.worker = ColorFixWorker(targets, self.mask_dir, out_dir, list(self.palette),
                                     self.copy_clean.isChecked())
        self.worker.progress.connect(self.on_progress)
        self.worker.done.connect(lambda f, o, e: self.on_color_fix_done(f, o, e, out_dir))
        self.worker.start()

    def on_color_fix_done(self, fixed, other, errors, out_dir: Path):
        self.set_busy(False)
        msg = f"Done: {fixed} mask(s) corrected"
        msg += f", {other} copied" if self.copy_clean.isChecked() else ""
        msg += f", {errors} failed." if errors else "."
        msg += f"\nSaved to: {out_dir}\nTo verify, select that folder as the masks folder and run the checks again."
        self.fix_status.setText(msg)

    # ---- Resize ----
    def start_resize(self):
        if not self.rows or self.mask_dir is None:
            QMessageBox.information(self, "Resize", "Run \"Check Sizes\" first.")
            return
        params = {
            "mode": self.rz_mode.currentData(),
            "w": self.rz_w.value(),
            "h": self.rz_h.value(),
            "keep": self.rz_keep.isChecked(),
            "shrink_only": self.rz_shrink.isChecked(),
            "interp": self.rz_interp.currentData(),
            "snap": self.rz_snap.isChecked(),
            "palette": list(self.palette),
        }
        if params["mode"] == "custom" and params["w"] <= 0 and params["h"] <= 0:
            QMessageBox.warning(self, "Resize", "Set a width, a height, or both.")
            return
        if params["snap"] and not self.palette:
            QMessageBox.warning(self, "Resize", "Add colors to the palette (Colors tab) or turn off color snapping.")
            return
        out_dir = self._validate_out_dir(self.out_edit.text(), "Resize")
        if out_dir is None:
            return
        only_bad = self.rz_target.currentData() == "bad"
        targets = [r for r in self.rows
                   if r.mask and r.m_size and not r.m_err and (not only_bad or r.state == "bad")]
        if not targets:
            QMessageBox.information(self, "Resize", "There are no mask images to resize.")
            return
        out_dir.mkdir(parents=True, exist_ok=True)
        self.set_busy(True)
        self.progress.setRange(0, len(targets))
        self.rz_status.setText("Resizing…")
        self.worker = ResizeWorker(targets, self.mask_dir, out_dir, params)
        self.worker.progress.connect(self.on_progress)
        self.worker.done.connect(lambda d, s, e: self.on_resize_done(d, s, e, out_dir))
        self.worker.start()

    def on_resize_done(self, done, skipped, errors, out_dir: Path):
        self.set_busy(False)
        msg = f"Done: {done} resized, {skipped} skipped"
        msg += f", {errors} failed." if errors else "."
        msg += f"\nSaved to: {out_dir}\nTo verify, select that folder as the masks folder and check again."
        self.rz_status.setText(msg)


def apply_theme(app: QApplication):
    """Force a consistent light look regardless of the OS theme."""
    app.setStyle("Fusion")
    pal = QPalette()
    pal.setColor(QPalette.Window, QColor("#f5f6f8"))
    pal.setColor(QPalette.WindowText, QColor("#202124"))
    pal.setColor(QPalette.Base, QColor("#ffffff"))
    pal.setColor(QPalette.AlternateBase, QColor("#f8f9fb"))
    pal.setColor(QPalette.Text, QColor("#202124"))
    pal.setColor(QPalette.Button, QColor("#ffffff"))
    pal.setColor(QPalette.ButtonText, QColor("#202124"))
    pal.setColor(QPalette.Highlight, QColor("#cfe0ff"))
    pal.setColor(QPalette.HighlightedText, QColor("#202124"))
    pal.setColor(QPalette.PlaceholderText, QColor("#8a919b"))
    pal.setColor(QPalette.ToolTipBase, QColor("#ffffff"))
    pal.setColor(QPalette.ToolTipText, QColor("#202124"))
    app.setPalette(pal)
    app.setStyleSheet(STYLE)


def main():
    app = QApplication(sys.argv)
    apply_theme(app)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
