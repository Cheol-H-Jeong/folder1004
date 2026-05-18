"""Organize / Search / History / Settings views."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from ..config import (
    CLASSIFICATION_GUIDANCE_PRESETS,
    Config,
    ORGANIZE_MODE_BUNDLE_REBUILD,
    ORGANIZE_MODE_FULL_REBUILD,
    ORGANIZE_MODE_PRESERVE_EXISTING,
    ORGANIZE_MODE_PRESERVE_FOLDER1004,
    get_api_key,
    normalize_organize_mode,
    save_config,
    set_api_key,
)
from ..folder_profile import analyze_folder_profile
from ..metadata import collect
from ..index import IndexDB
from ..models import Assignment, OperationResult, Plan
from .widgets import Card, PathDropBar, StageIndicator, StatsRow


def _is_live_status(text: str) -> bool:
    """True for messages that update in place rather than spawn new rows.

    Currently: streaming-token lines (``"… 토큰 수신 중 …"``) and the
    per-second heartbeat (``"… 응답 대기 … Ns 경과"``).  These all
    represent the same logical event and shouldn't push a new row each
    time.  Anything else (file moves, stage transitions, warnings,
    errors) is a real new event and gets its own row.
    """
    return ("토큰 수신" in text) or ("응답 대기" in text and "경과" in text)


def _live_group(text: str) -> str:
    """Group key for in-place-updating log lines.

    Within a single planning stage we get *both* heartbeat lines
    (``"plan: LLM 응답 대기 중 (5 파일) … 1s 경과"``) and token-stream
    lines (``"plan 토큰 수신 (5 파일): 96자 수신 중 — …"``).  They
    describe the same in-flight call, so they must collapse onto the
    same row — but the two strings have different prefixes before the
    first colon, which used to put them in different groups.

    Fix: take the *very first stage word* (until the first whitespace,
    colon, or '…') as the key — both ``"plan: LLM 응답 대기"`` and
    ``"plan 토큰 수신"`` map to ``"plan"`` so they overwrite each other.
    For chunked stages we also keep an ``[idx/total]`` suffix so
    ``"stage-a [1/5]"`` and ``"stage-a [2/5]"`` stay distinct.
    """
    import re as _re

    head = text.lstrip()
    # First whitespace-delimited stage token, lowercased.
    m = _re.match(r"\S+", head)
    if not m:
        return "live"
    base = m.group(0).rstrip(":").casefold()
    # Optional bracketed chunk index (e.g. "[2/5]") so chunk
    # transitions append a fresh row.
    rest = head[m.end():].lstrip()
    chunk = ""
    cm = _re.match(r"\[\s*\d+\s*/\s*\d+\s*\]", rest)
    if cm:
        chunk = " " + cm.group(0)
    return (base + chunk)[:64] or "live"


class FlowLayout(QtWidgets.QLayout):
    """Wrap child widgets to the next line and report height-for-width.

    Used for preset tag chips so they never require scrollbars and never
    overlap the custom instruction textbox below.
    """

    def __init__(self, parent=None, margin: int = 0, hspacing: int = 8, vspacing: int = 8):
        super().__init__(parent)
        self.setContentsMargins(margin, margin, margin, margin)
        self._hspace = hspacing
        self._vspace = vspacing
        self._items: list[QtWidgets.QLayoutItem] = []

    def __del__(self):
        while self.count():
            self.takeAt(0)

    def _safe_items(self) -> list[QtWidgets.QLayoutItem]:
        # Qt can call virtual layout methods during construction/destruction
        # on Windows/PySide before Python attributes are fully initialized.
        return getattr(self, "_items", [])

    def addItem(self, item: QtWidgets.QLayoutItem) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._safe_items())

    def itemAt(self, index: int) -> QtWidgets.QLayoutItem | None:
        items = self._safe_items()
        return items[index] if 0 <= index < len(items) else None

    def takeAt(self, index: int) -> QtWidgets.QLayoutItem | None:
        items = self._safe_items()
        return items.pop(index) if 0 <= index < len(items) else None

    def expandingDirections(self) -> QtCore.Qt.Orientations:
        return QtCore.Qt.Orientations(QtCore.Qt.Orientation(0))

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QtCore.QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QtCore.QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QtCore.QSize:
        return self.minimumSize()

    def minimumSize(self) -> QtCore.QSize:
        size = QtCore.QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QtCore.QSize(
            margins.left() + margins.right(),
            margins.top() + margins.bottom(),
        )
        return size

    def _do_layout(self, rect: QtCore.QRect, *, test_only: bool) -> int:
        margins = self.contentsMargins()
        effective = rect.adjusted(
            margins.left(), margins.top(), -margins.right(), -margins.bottom()
        )
        x = effective.x()
        y = effective.y()
        line_height = 0
        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + self._hspace
            if next_x - self._hspace > effective.right() and line_height > 0:
                x = effective.x()
                y += line_height + self._vspace
                next_x = x + hint.width() + self._hspace
                line_height = 0
            if not test_only:
                item.setGeometry(QtCore.QRect(QtCore.QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + margins.bottom()


class ToastDialog(QtWidgets.QDialog):
    """Apple-style modal toast.

    Drop-in replacement for ``QMessageBox.information`` whose default
    layout looks cramped on short messages — title and OK button
    floated awkwardly to opposite corners, ellipsised on small windows.
    This dialog uses an explicit vertical box with generous padding,
    a centred title and body, and a single right-aligned OK button.
    Sized to comfortably fit the message at any parent-window size.
    """

    def __init__(
        self,
        parent: QtWidgets.QWidget,
        title: str,
        body: str = "",
        *,
        kind: str = "info",  # info | warn | error
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(360)
        self.setMaximumWidth(520)
        # No system frame icon — keep it visually quiet.
        self.setWindowFlag(QtCore.Qt.WindowContextHelpButtonHint, False)

        accent = {
            "info":  "#0a8a3a",
            "warn":  "#c37200",
            "error": "#b3261e",
        }.get(kind, "#0a8a3a")

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(24, 22, 24, 18)
        outer.setSpacing(12)

        title_label = QtWidgets.QLabel(title)
        title_label.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        title_label.setStyleSheet(
            f"font-size:15px; font-weight:600; color:{accent};"
        )
        title_label.setWordWrap(True)
        outer.addWidget(title_label)

        if body:
            body_label = QtWidgets.QLabel(body)
            body_label.setWordWrap(True)
            body_label.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignTop)
            body_label.setStyleSheet("font-size:13px; color:#1d1d1f;")
            outer.addWidget(body_label)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.setContentsMargins(0, 4, 0, 0)
        btn_row.addStretch(1)
        ok = QtWidgets.QPushButton("확인")
        ok.setObjectName("Primary")
        ok.setMinimumWidth(96)
        ok.setDefault(True)
        ok.setAutoDefault(True)
        ok.clicked.connect(self.accept)
        btn_row.addWidget(ok)
        outer.addLayout(btn_row)


def show_toast_dialog(
    parent: QtWidgets.QWidget, title: str, body: str = "", *, kind: str = "info"
) -> None:
    ToastDialog(parent, title, body, kind=kind).exec()


def provider_label_for_ui(provider: str, base_url: str) -> str:
    """Convenience wrapper used by the Settings card; isolates the UI
    layer from the config module's dataclass requirements."""
    from ..config import provider_label, Config

    proxy = Config()
    proxy.llm_provider = provider
    proxy.llm_base_url = base_url or ""
    return provider_label(proxy)


def _open_in_explorer(path: Path):
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:
        pass


class PreviewPlanDialog(QtWidgets.QDialog):
    """Draggable classification preview before mutating the filesystem."""

    def __init__(self, op: OperationResult, parent=None):
        super().__init__(parent)
        self.op = op
        self.setWindowTitle("분류 미리보기")
        self.resize(980, 680)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 14)
        layout.setSpacing(12)

        intro = QtWidgets.QLabel(
            "분석 결과입니다. 파일을 다른 폴더 항목으로 드래그앤드롭해 수정한 뒤 "
            "“그대로 분류 실행”을 누르면 실제 파일 이동을 시작합니다."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["분류 폴더 / 파일", "예정 위치"])
        self.tree.setAlternatingRowColors(True)
        self.tree.setDragDropMode(QtWidgets.QAbstractItemView.InternalMove)
        self.tree.setDefaultDropAction(QtCore.Qt.MoveAction)
        self.tree.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.tree.setRootIsDecorated(True)
        self.tree.header().setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        self.tree.header().setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        layout.addWidget(self.tree, 1)

        self._populate()

        buttons = QtWidgets.QHBoxLayout()
        buttons.addStretch(1)
        cancel = QtWidgets.QPushButton("취소")
        cancel.clicked.connect(self.reject)
        run = QtWidgets.QPushButton("그대로 분류 실행")
        run.setObjectName("Primary")
        run.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(run)
        layout.addLayout(buttons)

    def _populate(self) -> None:
        by_cat = {c.id: [] for c in self.op.categories}
        for mf in self.op.moved:
            by_cat.setdefault(mf.category_id, []).append(mf)
        for cat in self.op.categories:
            parent = QtWidgets.QTreeWidgetItem([cat.name or cat.id, cat.id])
            parent.setData(0, QtCore.Qt.UserRole, {"type": "category", "category_id": cat.id})
            parent.setFlags(
                QtCore.Qt.ItemIsEnabled
                | QtCore.Qt.ItemIsSelectable
                | QtCore.Qt.ItemIsDropEnabled
            )
            parent.setExpanded(True)
            self.tree.addTopLevelItem(parent)
            for mf in by_cat.get(cat.id, []):
                child = QtWidgets.QTreeWidgetItem([Path(mf.original_path).name, str(mf.new_path.parent)])
                child.setToolTip(0, str(mf.original_path))
                child.setToolTip(1, str(mf.new_path))
                child.setData(0, QtCore.Qt.UserRole, {
                    "type": "file",
                    "original_path": str(mf.original_path),
                    "score": float(mf.score or 0.0),
                    "reason": mf.reason or "",
                })
                child.setFlags(
                    QtCore.Qt.ItemIsEnabled
                    | QtCore.Qt.ItemIsSelectable
                    | QtCore.Qt.ItemIsDragEnabled
                )
                parent.addChild(child)
        self.tree.expandAll()

    def to_plan(self) -> Plan:
        categories = []
        assignments: list[Assignment] = []
        assigned_paths: set[str] = set()
        for i in range(self.tree.topLevelItemCount()):
            parent = self.tree.topLevelItem(i)
            pdata = parent.data(0, QtCore.Qt.UserRole) or {}
            cid = pdata.get("category_id") or ""
            cat = next((c for c in self.op.categories if c.id == cid), None)
            if cat is None:
                continue
            if parent.childCount() == 0:
                continue
            categories.append(cat)
            for j in range(parent.childCount()):
                child = parent.child(j)
                data = child.data(0, QtCore.Qt.UserRole) or {}
                if data.get("type") != "file":
                    continue
                assignments.append(Assignment(
                    file_path=Path(data["original_path"]),
                    primary_category_id=cat.id,
                    primary_score=float(data.get("score") or 1.0),
                    reason=(data.get("reason") or "미리보기에서 확정"),
                ))
                assigned_paths.add(str(data["original_path"]))
        # QTreeWidget's built-in InternalMove can leave a file at top-level
        # after an awkward drop.  Never let that make the confirmed plan lose a
        # file: anything not under a category falls back to its original preview
        # assignment.
        by_cat = {c.id: c for c in self.op.categories}
        for mf in self.op.moved:
            original = str(mf.original_path)
            if original in assigned_paths:
                continue
            cat = by_cat.get(mf.category_id)
            if cat is not None and cat not in categories:
                categories.append(cat)
            assignments.append(Assignment(
                file_path=Path(original),
                primary_category_id=mf.category_id,
                primary_score=float(mf.score or 1.0),
                reason=mf.reason or "미리보기에서 확정",
            ))
        return Plan(categories=categories, assignments=assignments)


class OrganizeView(QtWidgets.QWidget):
    # path, recursive, dry_run, mode, action ("preview" | "run")
    start_requested = QtCore.Signal(str, bool, bool, str, str)
    cancel_requested = QtCore.Signal()
    rollback_requested = QtCore.Signal()

    def __init__(self, config: Config, parent=None):
        super().__init__(parent)
        self.config = config
        self._last_op: OperationResult | None = None
        self._build()

    def _build(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.scroll_area = QtWidgets.QScrollArea()
        self.scroll_area.setObjectName("PageScroll")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        root.addWidget(self.scroll_area)

        content = QtWidgets.QWidget()
        self.scroll_area.setWidget(content)
        outer = QtWidgets.QVBoxLayout(content)
        outer.setContentsMargins(28, 28, 28, 20)
        outer.setSpacing(18)

        title = QtWidgets.QLabel("폴더 정리")
        title.setObjectName("Title")
        subtitle = QtWidgets.QLabel("폴더를 고르면 파일을 읽고 의미에 맞게 자동 분류합니다.")
        subtitle.setObjectName("Subtitle")
        outer.addWidget(title)
        outer.addWidget(subtitle)

        self.path_bar = PathDropBar()
        outer.addWidget(self.path_bar)

        # Inline toast banner (취소 / 안내 / 오류).
        self._toast = QtWidgets.QFrame()
        self._toast.setObjectName("Toast")
        self._toast.setVisible(False)
        toast_layout = QtWidgets.QHBoxLayout(self._toast)
        toast_layout.setContentsMargins(14, 12, 14, 12)
        toast_layout.setSpacing(12)
        self._toast_title = QtWidgets.QLabel()
        self._toast_body = QtWidgets.QLabel()
        toast_text = QtWidgets.QVBoxLayout()
        toast_text.setSpacing(2)
        toast_text.addWidget(self._toast_title)
        toast_text.addWidget(self._toast_body)
        toast_layout.addLayout(toast_text, 1)
        outer.addWidget(self._toast)

        options = Card()
        opt_row = QtWidgets.QHBoxLayout(options)
        opt_row.setContentsMargins(18, 14, 18, 14)
        opt_row.setSpacing(20)

        self.chk_recursive = QtWidgets.QCheckBox("하위 폴더 포함 (항상 적용)")
        self.chk_recursive.setChecked(True)
        self.chk_recursive.setEnabled(False)
        self.chk_recursive.setToolTip(
            "Folder1004는 기본적으로 하위 폴더까지 읽어 현재 구조를 이해합니다. "
            "다만 아래 정리 방식 1~3은 현재 1-depth 폴더 체계만 재배치하고 내부는 해체하지 않습니다."
        )
        opt_row.addWidget(self.chk_recursive)
        opt_row.addStretch(1)

        # 정리 방식 카드 — 최상위 폴더 체계와 하위 폴더 해체 여부를 명확히 고른다.
        mode_card = Card()
        mc = QtWidgets.QVBoxLayout(mode_card)
        mc.setContentsMargins(18, 14, 18, 14)
        mc.setSpacing(10)
        mode_head = QtWidgets.QHBoxLayout()
        mode_head.setSpacing(18)
        mode_lbl = QtWidgets.QLabel("정리 방식")
        mode_lbl.setObjectName("ModeTitle")
        mode_lbl.setStyleSheet("font-weight:600;")
        mode_lbl.setMinimumWidth(112)
        mode_hint = QtWidgets.QLabel(
            "기본 추천은 Folder1004가 알아서 판단합니다. 아래 1~3번 방식은 현재 폴더 바로 아래의 1-depth 폴더 체계에만 적용됩니다. "
            "하위 폴더 내용은 읽어서 참고하지만 해체하지 않습니다. 마지막 방식만 모든 하위 폴더까지 해체합니다."
        )
        mode_hint.setObjectName("ModeHint")
        mode_hint.setWordWrap(True)
        mode_hint.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        mode_hint.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred)
        mode_hint.setStyleSheet("color:#6e6e73;font-size:12px;")
        mode_head.addWidget(mode_lbl)
        mode_head.addWidget(mode_hint, 1)
        mc.addLayout(mode_head)
        mode_options_wrap = QtWidgets.QWidget()
        mode_options = FlowLayout(mode_options_wrap, margin=0, hspacing=20, vspacing=8)

        self.rad_bundle = QtWidgets.QRadioButton("새 폴더 체계로 정리 (자동 판단 기본값)")
        self.rad_bundle.setToolTip(
            "현재 폴더 바로 아래의 파일/폴더 묶음만 새 Folder1004 체계로 옮깁니다.\n"
            "하위 폴더 내부는 해체하지 않습니다."
        )
        self.rad_existing = QtWidgets.QRadioButton("기존 폴더 체계 유지")
        self.rad_existing.setToolTip(
            "현재 1-depth 기존 폴더는 그대로 두고, 루트에 흩어진 파일만 기존 폴더나\n"
            "새 Folder1004 폴더에 넣습니다. 기존 폴더 내부는 건드리지 않습니다."
        )
        self.rad_folder1004 = QtWidgets.QRadioButton("Folder1004 폴더만 유지")
        self.rad_folder1004.setToolTip(
            "현재 1-depth의 Folder1004 서명 폴더는 그대로 두고, 그 밖의 파일과 일반 폴더만\n"
            "묶음 단위로 기존/새 Folder1004 폴더에 정리합니다."
        )
        self.rad_full = QtWidgets.QRadioButton("모든 폴더 해체 후 재정리 (주의)")
        self.rad_full.setToolTip(
            "모든 하위 폴더 안의 파일까지 모두 꺼내 파일 단위로 다시 분류합니다.\n"
            "기존 폴더명은 참고 힌트로만 사용합니다."
        )

        # Backwards-compatible attribute names used by older tests/plugins.
        self.rad_new = self.rad_bundle
        self.rad_inc = self.rad_existing
        self.rad_add = self.rad_folder1004

        current_mode = normalize_organize_mode(getattr(self.config, "organize_mode", ""))
        self.rad_bundle.setChecked(current_mode == ORGANIZE_MODE_BUNDLE_REBUILD)
        self.rad_existing.setChecked(current_mode == ORGANIZE_MODE_PRESERVE_EXISTING)
        self.rad_folder1004.setChecked(current_mode == ORGANIZE_MODE_PRESERVE_FOLDER1004)
        self.rad_full.setChecked(current_mode == ORGANIZE_MODE_FULL_REBUILD)
        if not (self.rad_bundle.isChecked() or self.rad_existing.isChecked() or self.rad_folder1004.isChecked() or self.rad_full.isChecked()):
            self.rad_bundle.setChecked(True)
        mode_grp = QtWidgets.QButtonGroup(self)
        for rb in (self.rad_bundle, self.rad_existing, self.rad_folder1004, self.rad_full):
            mode_grp.addButton(rb)
            mode_options.addWidget(rb)
        mc.addWidget(mode_options_wrap)
        outer.addWidget(mode_card)

        style_card = Card()
        sc = QtWidgets.QVBoxLayout(style_card)
        sc.setContentsMargins(18, 16, 18, 16)
        sc.setSpacing(10)
        style_title = QtWidgets.QLabel("추천 분류 스타일")
        style_title.setStyleSheet("font-size:16px;font-weight:600;")
        sc.addWidget(style_title)
        style_sub = QtWidgets.QLabel(
            "폴더를 고르면 Folder1004가 정리 방향을 추천해 자동으로 켭니다. "
            "프리셋의 실제 프롬프트는 숨기고, 필요한 추가 요청만 직접 적으세요."
        )
        style_sub.setWordWrap(True)
        style_sub.setStyleSheet("color:#6e6e73;font-size:12px;")
        sc.addWidget(style_sub)

        self.lbl_classification_recommendation = QtWidgets.QLabel("폴더를 선택하면 추천 스타일이 자동으로 적용됩니다.")
        self.lbl_classification_recommendation.setWordWrap(True)
        self.lbl_classification_recommendation.setStyleSheet("color:#0a8a3a;font-size:12px;font-weight:600;")
        sc.addWidget(self.lbl_classification_recommendation)

        preset_wrap = QtWidgets.QWidget()
        preset_wrap.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Preferred,
        )
        preset_flow = FlowLayout(preset_wrap, margin=0, hspacing=8, vspacing=8)
        self.classification_preset_buttons: dict[str, QtWidgets.QPushButton] = {}
        selected_presets = set(getattr(self.config, "classification_guidance_preset_names", []) or [])
        for preset in CLASSIFICATION_GUIDANCE_PRESETS:
            label = str(preset.get("label") or "").strip()
            if not label:
                continue
            btn = QtWidgets.QPushButton(label)
            btn.setObjectName("PresetTag")
            btn.setCheckable(True)
            btn.setProperty("preset_label", label)
            btn.setChecked(label in selected_presets)
            btn.setCursor(QtCore.Qt.PointingHandCursor)
            btn.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)
            btn.toggled.connect(self._sync_classification_guidance_config)
            self.classification_preset_buttons[label] = btn
            preset_flow.addWidget(btn)
        preset_wrap.setMinimumHeight(160)
        sc.addWidget(preset_wrap)

        self.edit_custom_classification_guidance = QtWidgets.QTextEdit()
        self.edit_custom_classification_guidance.setAcceptRichText(False)
        self.edit_custom_classification_guidance.setMinimumHeight(72)
        self.edit_custom_classification_guidance.setPlaceholderText(
            "추가로 원하는 점만 입력하세요. 예: 고객명과 촬영일을 특히 우선해줘."
        )
        self.edit_custom_classification_guidance.setPlainText(
            (getattr(self.config, "classification_guidance", "") or "").strip()
        )
        self.edit_custom_classification_guidance.textChanged.connect(
            self._sync_classification_guidance_config
        )
        sc.addWidget(self.edit_custom_classification_guidance)

        outer.addWidget(style_card)
        self.path_bar.path_changed.connect(self._recommend_classification_style)
        self._sync_classification_guidance_config()

        self.badge_api = QtWidgets.QLabel("API 키 확인 중…")
        self.badge_api.setObjectName("Badge")
        opt_row.addWidget(self.badge_api)

        outer.addWidget(options)

        # Progress card
        self.progress_card = Card()
        pc = QtWidgets.QVBoxLayout(self.progress_card)
        pc.setContentsMargins(18, 16, 18, 16)
        pc.setSpacing(12)
        self.stage_ind = StageIndicator()
        pc.addWidget(self.stage_ind)
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        pc.addWidget(self.progress_bar)
        self.progress_label = QtWidgets.QLabel("대기 중")
        self.progress_label.setStyleSheet("color:#6e6e73;")
        pc.addWidget(self.progress_label)

        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        self.log_view.setMinimumHeight(140)
        self.log_view.setStyleSheet(
            "QPlainTextEdit { background:#0f1115; color:#d6d6dc; border-radius:10px;"
            " padding:10px; font-family:'JetBrains Mono','SF Mono',Menlo,Consolas,monospace; font-size:12px; }"
        )
        self.log_view.setPlaceholderText("진행 로그가 여기에 한 줄씩 표시됩니다.")
        pc.addWidget(self.log_view, 1)
        outer.addWidget(self.progress_card, 1)

        # Action row
        actions = QtWidgets.QHBoxLayout()
        actions.setSpacing(10)
        self.btn_preview = QtWidgets.QPushButton("분석 후 미리보기")
        self.btn_preview.setObjectName("Ghost")
        self.btn_preview.setToolTip("분석이 끝나면 드래그앤드롭으로 분류를 조정한 뒤 실제 정리를 실행합니다.")
        self.btn_preview.clicked.connect(lambda: self._on_start("preview"))
        self.btn_primary = QtWidgets.QPushButton("바로 끝까지 정리")
        self.btn_primary.setObjectName("Primary")
        self.btn_primary.setToolTip("분석과 실제 파일 이동을 한 번에 끝까지 실행합니다.")
        self.btn_primary.clicked.connect(lambda: self._on_start("run"))
        self.btn_cancel = QtWidgets.QPushButton("취소")
        self.btn_cancel.setObjectName("Ghost")
        self.btn_cancel.setVisible(False)
        self.btn_cancel.clicked.connect(self.cancel_requested)
        actions.addStretch(1)
        self.btn_open_log = QtWidgets.QPushButton("로그 폴더 열기")
        self.btn_open_log.setObjectName("Ghost")
        self.btn_open_log.clicked.connect(self._open_log_dir)
        actions.addWidget(self.btn_open_log)
        actions.addWidget(self.btn_cancel)
        actions.addWidget(self.btn_preview)
        actions.addWidget(self.btn_primary)
        outer.addLayout(actions)

        # Report card
        self.report_card = Card()
        self.report_card.setVisible(False)
        rc = QtWidgets.QVBoxLayout(self.report_card)
        rc.setContentsMargins(18, 16, 18, 16)
        rc.setSpacing(14)

        top = QtWidgets.QHBoxLayout()
        rt = QtWidgets.QLabel("최근 정리 결과")
        rt.setStyleSheet("font-size:18px;font-weight:600;")
        top.addWidget(rt)
        top.addStretch(1)
        self.btn_open_folder = QtWidgets.QPushButton("폴더 열기")
        self.btn_open_folder.setObjectName("Ghost")
        self.btn_open_folder.clicked.connect(self._open_target)
        self.btn_open_report = QtWidgets.QPushButton("리포트 열기")
        self.btn_open_report.setObjectName("Ghost")
        self.btn_open_report.clicked.connect(self._open_report)
        self.btn_rollback = QtWidgets.QPushButton("방금 정리 롤백")
        self.btn_rollback.setObjectName("Ghost")
        self.btn_rollback.setToolTip("마지막 정리 작업에서 옮긴 파일을 가능한 한 원래 위치로 되돌립니다.")
        self.btn_rollback.clicked.connect(self.rollback_requested)
        top.addWidget(self.btn_open_folder)
        top.addWidget(self.btn_open_report)
        top.addWidget(self.btn_rollback)
        rc.addLayout(top)

        self.stats_row = StatsRow()
        rc.addWidget(self.stats_row)

        self.cat_table = QtWidgets.QTableWidget(0, 3)
        self.cat_table.setHorizontalHeaderLabels(["카테고리", "폴더명", "파일 수"])
        self.cat_table.horizontalHeader().setStretchLastSection(False)
        self.cat_table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        self.cat_table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        self.cat_table.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
        self.cat_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.cat_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.cat_table.setAlternatingRowColors(True)
        rc.addWidget(self.cat_table, 1)

        outer.addWidget(self.report_card, 1)
        self.refresh_api_badge()

    # ------------------------------------------------------------------
    def _selected_classification_preset_names(self) -> list[str]:
        buttons = getattr(self, "classification_preset_buttons", {})
        return [label for label, btn in buttons.items() if btn.isChecked()]

    def _sync_classification_guidance_config(self) -> None:
        if not hasattr(self, "edit_custom_classification_guidance"):
            return
        self.config.classification_guidance = (
            self.edit_custom_classification_guidance.toPlainText().strip()
        )
        self.config.classification_guidance_preset_names = (
            self._selected_classification_preset_names()
        )

    def _set_classification_presets(self, labels: list[str]) -> None:
        wanted = set(labels)
        for label, btn in getattr(self, "classification_preset_buttons", {}).items():
            btn.blockSignals(True)
            btn.setChecked(label in wanted)
            btn.blockSignals(False)
        self._sync_classification_guidance_config()

    def _recommend_classification_style(self, path_text: str) -> None:
        path = Path(path_text) if path_text else None
        labels, reason = self._classification_style_recommendation(path)
        if labels:
            self._set_classification_presets(labels)
            self.lbl_classification_recommendation.setText(
                f"추천 적용: {', '.join(labels)} · {reason}"
            )
        else:
            self.lbl_classification_recommendation.setText(
                "폴더를 선택하면 추천 스타일이 자동으로 적용됩니다."
            )

    def _classification_style_recommendation(self, path: Path | None) -> tuple[list[str], str]:
        if path is None or not path.exists():
            return [], ""
        entries = []
        try:
            for child in list(path.iterdir())[:80]:
                if child.is_file() and not child.is_symlink():
                    try:
                        entries.append(collect(child))
                    except OSError:
                        continue
        except OSError:
            pass
        summary = analyze_folder_profile(path, entries, recursive=False)
        reason = (
            f"{summary.label} · 건강 점수 {summary.health_score}/100"
            if summary.file_count
            else "일반 폴더에는 안전한 기본 스타일을 사용해요"
        )
        return summary.recommended_preset_names, reason

    # ------------------------------------------------------------------
    def refresh_api_badge(self):
        from ..config import provider_label

        key = get_api_key(self.config)
        if key:
            self.badge_api.setText(f"{provider_label(self.config)} 연결됨")
            self.badge_api.setObjectName("Badge")
        else:
            self.badge_api.setText("Mock 모드 (API 키 없음)")
            self.badge_api.setObjectName("BadgeWarn")
        self.badge_api.setStyle(self.badge_api.style())

    # ------------------------------------------------------------------
    def _on_start(self, action: str = "run"):
        path = self.path_bar.path()
        if not path:
            QtWidgets.QMessageBox.warning(self, "경로 필요", "먼저 정리할 폴더를 선택하세요.")
            return
        if not Path(path).is_dir():
            QtWidgets.QMessageBox.warning(self, "폴더 아님", "선택한 경로가 폴더가 아닙니다.")
            return
        self._sync_classification_guidance_config()
        try:
            save_config(self.config)
        except Exception:
            pass
        self.set_running(True)
        if self.rad_full.isChecked():
            mode = ORGANIZE_MODE_FULL_REBUILD
            QtWidgets.QMessageBox.warning(
                self,
                "모든 폴더 해체 후 재정리",
                "주의: 이 방식은 하위 폴더 안의 파일까지 모두 꺼내 파일 단위로 다시 분류합니다.\n"
                "기존 폴더명은 참고만 하며, 현재 폴더 구조는 유지되지 않습니다.",
            )
        elif self.rad_folder1004.isChecked():
            mode = ORGANIZE_MODE_PRESERVE_FOLDER1004
        elif self.rad_existing.isChecked():
            mode = ORGANIZE_MODE_PRESERVE_EXISTING
        else:
            mode = ORGANIZE_MODE_BUNDLE_REBUILD
        self.start_requested.emit(
            path, True,
            action == "preview", mode, action,
        )

    def set_running(self, running: bool):
        self.btn_primary.setDisabled(running)
        self.btn_preview.setDisabled(running)
        self.btn_cancel.setVisible(running)
        if running:
            self._frozen_after_cancel = False
            self._live_group = None
            self.report_card.setVisible(False)
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(0)
            self.stage_ind.reset()
            self.progress_label.setText("시작 중…")
            self.log_view.clear()
            self._set_toast(None)

    def show_canceling(self):
        # The user clicked Cancel.  Freeze the visible progress *now* —
        # don't wait for the worker thread to wind itself down.  We:
        #   (a) flip the bar back to determinate 0 so the marquee stops,
        #   (b) reset stage indicator pills to neutral,
        #   (c) show the cancellation toast,
        #   (d) ignore any subsequent stage/status events so they can't
        #       re-start the marquee or push another "토큰 수신 중…" line.
        self._frozen_after_cancel = True
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.stage_ind.reset()
        self.progress_label.setText("취소 요청됨 — 진행 중인 호출을 정리하는 중…")
        self._set_toast(("warn", "취소 요청됨", "현재 단계가 안전하게 멈출 때까지 잠시만요…"))

    def show_canceled(self):
        self.set_running(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.stage_ind.reset()
        self.progress_label.setText("취소되었습니다.")
        self._set_toast(("info", "정리를 취소했습니다", "이미 옮긴 파일은 그대로 유지됩니다. 다시 시작할 수 있습니다."))

    def _set_toast(self, payload):
        """Inline toast banner.  payload = (kind, title, body) or None."""
        if not hasattr(self, "_toast"):
            return
        if payload is None:
            self._toast.setVisible(False)
            return
        kind, title, body = payload
        palette = {
            "warn":  ("#FFF4E5", "#C37200", "#FFE2B2"),
            "info":  ("#EAF4FF", "#0B66C2", "#C7DEF8"),
            "error": ("#FFECEC", "#B3261E", "#F4C7C5"),
        }
        bg, fg, border = palette.get(kind, palette["info"])
        self._toast.setStyleSheet(
            f"QFrame#Toast {{ background:{bg}; border:1px solid {border}; "
            f"border-radius:12px; }}"
        )
        self._toast_title.setText(title)
        self._toast_title.setStyleSheet(f"color:{fg};font-weight:700;font-size:14px;")
        self._toast_body.setText(body)
        self._toast_body.setStyleSheet(f"color:{fg};font-size:12px;")
        self._toast.setVisible(True)

    def on_stage(self, stage: str, pct: float):
        # After cancel, drop any in-flight stage events so the marquee
        # bar / stage pills don't keep moving while the worker thread
        # finishes unwinding.
        if getattr(self, "_frozen_after_cancel", False):
            return
        self.stage_ind.set_active(stage)
        if pct < 0:
            self.progress_bar.setRange(0, 0)
        else:
            if self.progress_bar.maximum() == 0:
                self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(max(0, min(100, int(pct * 100))))

    def on_status(self, text: str):
        if getattr(self, "_frozen_after_cancel", False):
            return
        head = text if len(text) <= 90 else text[:87] + "…"
        self.progress_label.setText(head)
        from datetime import datetime as _dt

        # The streaming-token line and the per-second heartbeat both update
        # the *same* visual idea (count + tail).  Don't push a new log row
        # for each one — overwrite the previous row in place when the
        # status belongs to the same "live" group.  A new row is appended
        # only when the message kind changes (different stage, or a
        # non-live event like "move [3/14] foo.pptx → …").
        ts = _dt.now().strftime("%H:%M:%S")
        formatted = f"[{ts}] {text}"
        if _is_live_status(text):
            self._replace_or_append_live_line(formatted, group=_live_group(text))
        else:
            self._live_group = None
            self.log_view.appendPlainText(formatted)
        sb = self.log_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _replace_or_append_live_line(self, formatted: str, *, group: str) -> None:
        """Update the trailing line of the log view in place when this
        status belongs to the same live group as the previous one;
        otherwise append a new line.  This keeps the streaming-token
        feed on a single growing row instead of one row per second.
        """
        prev_group = getattr(self, "_live_group", None)
        doc = self.log_view.document()
        if prev_group != group or doc.blockCount() == 0:
            self._live_group = group
            self.log_view.appendPlainText(formatted)
            return
        # Replace the last block's content with the new formatted line.
        cursor = self.log_view.textCursor()
        cursor.movePosition(QtGui.QTextCursor.End)
        cursor.movePosition(QtGui.QTextCursor.StartOfBlock)
        cursor.movePosition(
            QtGui.QTextCursor.EndOfBlock, QtGui.QTextCursor.KeepAnchor
        )
        cursor.removeSelectedText()
        cursor.insertText(formatted)

    def on_finished(self, op: OperationResult):
        self._last_op = op
        self.set_running(False)
        self.progress_bar.setValue(100)
        self.progress_label.setText(
            f"완료 — 이동 {op.total_moved}, 바로가기 {op.total_shortcuts}, 스킵 {op.total_skipped}"
        )
        usage = op.llm_usage
        if usage is None or usage.model == "mock" or usage.request_count == 0:
            llm_label = "0 (Mock)"
            cost_label = "₩0"
            speed_label = "—"
        else:
            llm_label = f"{usage.request_count}회"
            krw = usage.estimate_cost_krw()
            usd = usage.estimate_cost_usd()
            if krw < 1.0:
                cost_label = f"≈ ₩{krw:.2f}\n(${usd:.5f})"
            else:
                cost_label = f"≈ ₩{krw:,.1f}\n(${usd:.4f})"
            tps = usage.avg_tokens_per_second()
            speed_label = f"{tps:.1f} tok/s\n총 {usage.total_duration_s:.1f}s"
        self.stats_row.update_items(
            [
                ("스캔 파일", str(op.total_scanned)),
                ("이동", str(op.total_moved)),
                ("바로가기", str(op.total_shortcuts)),
                ("스킵", str(op.total_skipped)),
                ("LLM 호출", llm_label),
                ("예상 비용", cost_label),
                ("LLM 속도", speed_label),
            ]
        )
        from collections import Counter

        counter = Counter(m.category_id for m in op.moved)
        self.cat_table.setRowCount(len(op.categories))
        for row, cat in enumerate(op.categories):
            self.cat_table.setItem(row, 0, QtWidgets.QTableWidgetItem(cat.id))
            self.cat_table.setItem(row, 1, QtWidgets.QTableWidgetItem(cat.name))
            self.cat_table.setItem(row, 2, QtWidgets.QTableWidgetItem(str(counter.get(cat.id, 0))))
        self.report_card.setVisible(True)
        self.btn_rollback.setEnabled(not op.dry_run and op.operation_id is not None)

    def confirm_preview_plan(self, op: OperationResult) -> Plan | None:
        dialog = PreviewPlanDialog(op, self)
        if dialog.exec() != QtWidgets.QDialog.Accepted:
            self.set_running(False)
            self.progress_label.setText("미리보기를 취소했습니다.")
            self._set_toast(("info", "미리보기 취소", "실제 파일 이동은 실행되지 않았습니다."))
            return None
        return dialog.to_plan()

    def on_failed(self, msg: str):
        self.set_running(False)
        # Detect the user-cancel path and surface it gently rather than as
        # a scary "Critical Error" modal.
        low = (msg or "").lower()
        if "cancel" in low or "취소" in (msg or ""):
            self.show_canceled()
            return
        # Friendlier copy for common transport failures.
        friendly = msg or ""
        if "read timeout" in low or "timed out" in low:
            friendly = "LLM 응답이 시간 안에 도착하지 못했어요. 잠시 후 다시 시도해 주세요."
        elif "connectionerror" in low or "connection refused" in low:
            friendly = "LLM 서버에 연결하지 못했어요. 엔드포인트 URL과 서버 상태를 확인해 주세요."
        elif "invalid api key" in low or "unauthorized" in low or "401" in low:
            friendly = "API 키가 인증되지 않았어요. 설정에서 키를 다시 확인해 주세요."
        else:
            friendly = f"문제가 발생했어요: {msg}"
        try:
            from ..runlog import current_log_path

            lp = current_log_path()
            if lp is not None:
                friendly += f"  ·  자세한 내용은 로그를 확인하세요: {lp}"
        except Exception:
            pass
        self._set_toast(("error", "정리를 끝내지 못했어요", friendly))

    def _open_log_dir(self):
        from ..config import default_paths

        d = default_paths().logs_dir
        d.mkdir(parents=True, exist_ok=True)
        _open_in_explorer(d)

    def _open_target(self):
        if self._last_op:
            _open_in_explorer(self._last_op.target_root)

    def _open_report(self):
        if self._last_op:
            explicit = getattr(self._last_op, "report_path", None)
            if explicit:
                p = Path(explicit)
                if p.exists():
                    _open_in_explorer(p)
                    return
            # Fallback for older in-memory OperationResult objects: the
            # reporter writes to target_root/Folder1004_Report_*.md.  Use a
            # glob instead of reconstructing from timestamps so preview→apply
            # runs still open the report even if the timestamp differs by a
            # second or the path was not persisted in a legacy object.
            try:
                candidates = sorted(
                    self._last_op.target_root.glob("Folder1004_Report_*.md"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                if candidates:
                    _open_in_explorer(candidates[0])
            except Exception:
                pass


class SearchView(QtWidgets.QWidget):
    def __init__(self, index_db: IndexDB, parent=None):
        super().__init__(parent)
        self.index_db = index_db
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(28, 28, 28, 20)
        v.setSpacing(14)
        t = QtWidgets.QLabel("검색")
        t.setObjectName("Title")
        sub = QtWidgets.QLabel("정리된 파일을 이름, 카테고리, 원본 경로로 찾습니다.")
        sub.setObjectName("Subtitle")
        v.addWidget(t)
        v.addWidget(sub)

        row = QtWidgets.QHBoxLayout()
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("입력하는 즉시 검색됩니다 — 예: 2025 계약, 보고서, receipt…")
        self.search.setClearButtonEnabled(True)
        v.addLayout(row)
        row.addWidget(self.search, 1)

        # Reindex action — let the user push the current contents of any
        # folder into the search index without going through a full LLM
        # organise pass.  Useful when the existing index is stale (older
        # runs / manual moves / a folder that was never organised).
        self.btn_reindex = QtWidgets.QPushButton("폴더 다시 인덱싱…")
        self.btn_reindex.setObjectName("Ghost")
        self.btn_reindex.setToolTip(
            "선택한 폴더의 현재 파일 트리를 검색 인덱스에 갱신합니다.\n"
            "정리 작업 없이도 그 폴더 안 모든 파일을 검색할 수 있습니다."
        )
        self.btn_reindex.clicked.connect(self._do_reindex)
        row.addWidget(self.btn_reindex)

        # Live-search: every keystroke triggers a fresh query, debounced
        # by 120 ms so a fast typist doesn't fire a SQL hit per character.
        # Pressing Enter still works for users who expect explicit submit.
        self._search_timer = QtCore.QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(120)
        self._search_timer.timeout.connect(self._do_search)
        self.search.textChanged.connect(lambda _t: self._search_timer.start())
        self.search.returnPressed.connect(self._do_search)

        self.table = QtWidgets.QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["파일", "매치", "카테고리", "현재 위치", "스니펫", "정리 시각"]
        )
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(QtWidgets.QHeaderView.Interactive)
        hdr.setStretchLastSection(False)
        # Sensible default column widths so the filename never starts as
        # an ellipsis; user can still drag any column wider.
        self.table.setColumnWidth(0, 320)   # filename
        self.table.setColumnWidth(1, 70)    # match field
        self.table.setColumnWidth(2, 200)   # category
        self.table.setColumnWidth(3, 320)   # current location
        self.table.setColumnWidth(4, 380)   # snippet
        self.table.setColumnWidth(5, 130)   # timestamp
        hdr.setSectionResizeMode(4, QtWidgets.QHeaderView.Stretch)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.table.doubleClicked.connect(self._open_selected)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(30)
        v.addWidget(self.table, 1)

    def focus_search(self):
        self.search.setFocus(QtCore.Qt.ShortcutFocusReason)
        self.search.selectAll()

    def _do_search(self):
        q = self.search.text().strip()
        if not q:
            # Empty query: clear the results so the table doesn't look
            # stale; keep the column layout intact.
            self.table.setRowCount(0)
            return
        hits = self.index_db.search(q, limit=300)
        self.table.setRowCount(len(hits))
        for row, h in enumerate(hits):
            filename = Path(h.new_path).name
            cells = [
                (filename,           f"{filename}\n원본: {h.original_path}"),
                (h.matched_in or "", f"매치 위치: {h.matched_in or '미상'}"),
                (h.category,         h.category),
                (h.new_path,         h.new_path),
                (h.snippet or "",    h.snippet or "(미리보기 없음)"),
                (h.created_at,       h.created_at),
            ]
            for col, (text, tip) in enumerate(cells):
                item = QtWidgets.QTableWidgetItem(text)
                item.setToolTip(tip)
                self.table.setItem(row, col, item)
        self.table.resizeRowsToContents()

    def _open_selected(self, idx: QtCore.QModelIndex):
        row = idx.row()
        # "Current location" is col 3 in the new layout; fall back to
        # the filename cell if a row is sparse.
        item = self.table.item(row, 3) or self.table.item(row, 0)
        if item:
            p = Path(item.text())
            _open_in_explorer(p if p.exists() else p.parent)

    def _do_reindex(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "다시 인덱싱할 폴더 선택",
            "",
        )
        if not d:
            return
        n = self.index_db.reindex_folder(Path(d), recursive=True)
        show_toast_dialog(
            self,
            "인덱싱 완료",
            f"{n}개 파일을 인덱스에 추가하거나 갱신했습니다. 이제 검색이 즉시 가능합니다.",
            kind="info",
        )
        self._do_search()


class HistoryView(QtWidgets.QWidget):
    def __init__(self, index_db: IndexDB, parent=None):
        super().__init__(parent)
        self.index_db = index_db
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(28, 28, 28, 20)
        v.setSpacing(14)
        t = QtWidgets.QLabel("히스토리")
        t.setObjectName("Title")
        sub = QtWidgets.QLabel(
            "지난 정리 결과들. 행을 더블클릭하면 그 실행의 리포트를 엽니다."
        )
        sub.setObjectName("Subtitle")
        v.addWidget(t)
        v.addWidget(sub)

        self.table = QtWidgets.QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["ID", "대상 폴더", "시작", "파일 수", "모드", "리포트"]
        )
        self.table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.cellDoubleClicked.connect(self._on_double_click)
        v.addWidget(self.table, 1)

        row = QtWidgets.QHBoxLayout()
        row.addStretch(1)
        btn_refresh = QtWidgets.QPushButton("새로고침")
        btn_refresh.setObjectName("Ghost")
        btn_refresh.clicked.connect(self.refresh)
        row.addWidget(btn_refresh)
        v.addLayout(row)

        self._report_paths: dict[int, str] = {}

    def refresh(self):
        ops = self.index_db.list_operations(limit=100)
        self.table.setRowCount(len(ops))
        self._report_paths.clear()
        for row, op in enumerate(ops):
            self.table.setItem(row, 0, QtWidgets.QTableWidgetItem(str(op.op_id)))
            self.table.setItem(row, 1, QtWidgets.QTableWidgetItem(op.target_root))
            self.table.setItem(row, 2, QtWidgets.QTableWidgetItem(op.started_at))
            self.table.setItem(row, 3, QtWidgets.QTableWidgetItem(str(op.moved_count)))
            self.table.setItem(row, 4, QtWidgets.QTableWidgetItem("Dry" if op.dry_run else "실행"))
            rp = (op.report_path or "").strip()
            if rp and Path(rp).exists():
                report_label = "📄 더블클릭"
                self._report_paths[op.op_id] = rp
            elif rp:
                report_label = "(파일 없음)"
            else:
                report_label = "(미생성)"
            self.table.setItem(row, 5, QtWidgets.QTableWidgetItem(report_label))

    def _on_double_click(self, row: int, _col: int):
        try:
            op_id = int(self.table.item(row, 0).text())
        except (ValueError, AttributeError):
            return
        rp = self._report_paths.get(op_id)
        if rp:
            _open_in_explorer(Path(rp))
            return
        # Fallback: try to find a report under the target_root that
        # was saved by an older build that didn't yet persist the path.
        target = self.table.item(row, 1).text() if self.table.item(row, 1) else ""
        if target:
            try:
                candidates = sorted(Path(target).glob("Folder1004_Report_*.md"))
                if candidates:
                    _open_in_explorer(candidates[-1])
                    return
            except Exception:
                pass
        QtWidgets.QMessageBox.information(
            self, "리포트 없음",
            "이 실행의 리포트 파일을 찾지 못했습니다."
            " 보고서 파일이 삭제됐거나 대상 폴더가 이동됐을 수 있습니다.",
        )


class SettingsView(QtWidgets.QWidget):
    config_changed = QtCore.Signal()

    def __init__(self, config: Config, parent=None):
        super().__init__(parent)
        self.config = config
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.scroll_area = QtWidgets.QScrollArea()
        self.scroll_area.setObjectName("PageScroll")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        root.addWidget(self.scroll_area)

        content = QtWidgets.QWidget()
        self.scroll_area.setWidget(content)
        v = QtWidgets.QVBoxLayout(content)
        v.setContentsMargins(28, 28, 28, 20)
        v.setSpacing(14)

        t = QtWidgets.QLabel("설정")
        t.setObjectName("Title")
        sub = QtWidgets.QLabel("LLM 연결과 앱 동작을 조정합니다. 분류 스타일은 폴더 정리 화면에서 폴더별로 추천됩니다.")
        sub.setObjectName("Subtitle")
        v.addWidget(t)
        v.addWidget(sub)

        # ────────────────────────────────────────────────────────────
        # Card 1 — LLM 연결.  Two fields the user actually has to fill in:
        #   API endpoint URL + API key.  Model is presetted but editable.
        #   Provider type is inferred from the URL — never asked.
        #   Reasoning mode is decided automatically per model name.
        # ────────────────────────────────────────────────────────────
        conn_card = Card()
        c1 = QtWidgets.QVBoxLayout(conn_card)
        c1.setContentsMargins(18, 16, 18, 16)
        c1.setSpacing(12)
        c1_title = QtWidgets.QLabel("LLM 연결")
        c1_title.setStyleSheet("font-size:16px;font-weight:600;")
        c1.addWidget(c1_title)
        c1_sub = QtWidgets.QLabel(
            "API 엔드포인트와 API 키만 채우면 됩니다. "
            "Gemini · OpenAI · OpenRouter · Ollama · vLLM 등 어떤 호환 서비스든 같은 화면을 사용합니다."
        )
        c1_sub.setWordWrap(True)
        c1_sub.setStyleSheet("color:#6e6e73;font-size:12px;")
        c1.addWidget(c1_sub)

        # 0) Preset selector — register multiple endpoint setups
        # ("회사 Gemini", "로컬 Ollama", "OpenRouter Claude" …) and
        # switch between them.  Selecting a preset replaces the URL /
        # model fields below in one click.
        preset_row = QtWidgets.QHBoxLayout()
        preset_row.setContentsMargins(0, 0, 0, 0)
        preset_row.setSpacing(6)
        self.cmb_preset = QtWidgets.QComboBox()
        self.cmb_preset.setMinimumWidth(200)
        self._populate_preset_combo()
        self.cmb_preset.currentIndexChanged.connect(self._on_preset_chosen)
        preset_row.addWidget(self.cmb_preset, 1)
        btn_preset_add = QtWidgets.QPushButton("＋ 추가")
        btn_preset_add.setObjectName("Ghost")
        btn_preset_add.clicked.connect(self._preset_add)
        btn_preset_rename = QtWidgets.QPushButton("이름 변경")
        btn_preset_rename.setObjectName("Ghost")
        btn_preset_rename.clicked.connect(self._preset_rename)
        btn_preset_delete = QtWidgets.QPushButton("삭제")
        btn_preset_delete.setObjectName("Ghost")
        btn_preset_delete.clicked.connect(self._preset_delete)
        preset_row.addWidget(btn_preset_add)
        preset_row.addWidget(btn_preset_rename)
        preset_row.addWidget(btn_preset_delete)

        preset_wrap = QtWidgets.QWidget()
        preset_wrap.setLayout(preset_row)

        f1 = QtWidgets.QFormLayout()
        f1.setSpacing(10)
        f1.addRow("프리셋", preset_wrap)

        # 1) Endpoint URL
        self.edit_base_url = QtWidgets.QLineEdit()
        self.edit_base_url.setPlaceholderText(
            "예: https://generativelanguage.googleapis.com/v1beta · "
            "https://api.openai.com/v1 · http://localhost:11434/v1"
        )
        self.edit_base_url.setText((self.config.llm_base_url or "").strip())
        self.edit_base_url.textChanged.connect(self._on_endpoint_changed)
        f1.addRow("API 엔드포인트", self.edit_base_url)

        # 2) Model — auto-detected from the endpoint when possible.
        # Most non-Gemini providers serve exactly one model per endpoint
        # (Ollama runs one tag, vLLM serves one --model, llama-server
        # serves one --alias, etc.) so the user shouldn't have to type
        # it in.  We probe ``GET {url}/models``; if the endpoint lists
        # one model we lock the field to it; if several, show them as
        # a drop-down; if zero, fall back to a free-text editor.
        self.cmb_model = QtWidgets.QComboBox()
        self.cmb_model.setEditable(True)
        if self.config.model:
            self.cmb_model.addItem(self.config.model)
            self.cmb_model.setCurrentText(self.config.model)
        self.cmb_model.editTextChanged.connect(lambda _t: self._refresh_status())
        self.lbl_model_help = QtWidgets.QLabel("")
        self.lbl_model_help.setStyleSheet("color:#6e6e73;font-size:11px;")
        model_wrap = QtWidgets.QWidget()
        mv = QtWidgets.QVBoxLayout(model_wrap)
        mv.setContentsMargins(0, 0, 0, 0)
        mv.setSpacing(2)
        mv.addWidget(self.cmb_model)
        mv.addWidget(self.lbl_model_help)
        f1.addRow("모델", model_wrap)
        # debounce model auto-detect on URL / key edits
        self._probe_timer = QtCore.QTimer(self)
        self._probe_timer.setSingleShot(True)
        self._probe_timer.setInterval(400)
        self._probe_timer.timeout.connect(self._auto_detect_models)

        # 3) API key — masked, with inline save / delete.
        self.edit_key = QtWidgets.QLineEdit()
        self.edit_key.setEchoMode(QtWidgets.QLineEdit.Password)
        self.edit_key.setMinimumWidth(280)
        key_row = QtWidgets.QHBoxLayout()
        key_row.setContentsMargins(0, 0, 0, 0)
        key_row.setSpacing(8)
        key_row.addWidget(self.edit_key, 1)
        btn_save_key = QtWidgets.QPushButton("저장")
        btn_save_key.setObjectName("Primary")
        btn_save_key.clicked.connect(self._save_key)
        btn_clear_key = QtWidgets.QPushButton("삭제")
        btn_clear_key.setObjectName("Ghost")
        btn_clear_key.clicked.connect(self._clear_key)
        key_row.addWidget(btn_save_key)
        key_row.addWidget(btn_clear_key)
        wrap_key = QtWidgets.QWidget()
        wrap_key.setLayout(key_row)
        f1.addRow("API 키", wrap_key)

        c1.addLayout(f1)

        # 4) Connection status — single line under the card.
        self.lbl_status = QtWidgets.QLabel("")
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setStyleSheet("color:#6e6e73;font-size:12px;")
        c1.addWidget(self.lbl_status)

        v.addWidget(conn_card)

        # ────────────────────────────────────────────────────────────
        # Card 3 — 분류 동작
        # 평소엔 LLM이 기존 폴더명을 단서로 활용해 사용자가 이미
        # 정리해 둔 구조를 존중합니다.  하지만 한번 잘못 분류된
        # 하위 폴더 체계를 다시 입력해 *기존 폴더 기준 정리*를 하려는 경우에는,
        # 그 폴더명이 오히려 잘못된 그룹을 그대로 잠가 버립니다.
        # 이 토글은 LLM에 보내는 경로에서 부모 폴더명을 익명화해
        # 파일명 + 본문만으로 다시 판단하도록 강제합니다.
        # ────────────────────────────────────────────────────────────
        beh_card = Card()
        c3 = QtWidgets.QVBoxLayout(beh_card)
        c3.setContentsMargins(18, 16, 18, 16)
        c3.setSpacing(10)
        c3_title = QtWidgets.QLabel("분류 동작")
        c3_title.setStyleSheet("font-size:16px;font-weight:600;")
        c3.addWidget(c3_title)

        # 신규 / 기존 폴더 기준 모드는 *시작 화면*에서 매번 선택합니다 — 여기엔
        # 보조 옵션(중복 파일 dedup 임계값)만 둡니다.
        f3 = QtWidgets.QFormLayout()
        f3.setSpacing(8)
        self.spin_dedup_mb = QtWidgets.QSpinBox()
        self.spin_dedup_mb.setRange(0, 4096)
        self.spin_dedup_mb.setSuffix(" MB")
        cur_mb = max(0, int(getattr(self.config, "dedup_min_bytes", 1_048_576) // (1 << 20)))
        self.spin_dedup_mb.setValue(cur_mb)
        f3.addRow("중복 파일 자동 삭제 (이 크기 이상)", self.spin_dedup_mb)
        c3.addLayout(f3)
        c3_hint = QtWidgets.QLabel(
            "동일 내용 파일이 여러 곳에 있으면 가장 짧은 경로에 1개만 분류하고 "
            "나머지는 삭제해 용량을 회수합니다.  0 = 모든 크기 dedup, "
            "큰 값일수록 보수적."
        )
        c3_hint.setWordWrap(True)
        c3_hint.setStyleSheet("color:#6e6e73;font-size:12px;")
        c3.addWidget(c3_hint)
        v.addWidget(beh_card)


        # ────────────────────────────────────────────────────────────
        # Card 4 — 외관
        # ────────────────────────────────────────────────────────────
        look_card = Card()
        c4 = QtWidgets.QVBoxLayout(look_card)
        c4.setContentsMargins(18, 16, 18, 16)
        c4.setSpacing(12)
        c4_title = QtWidgets.QLabel("외관")
        c4_title.setStyleSheet("font-size:16px;font-weight:600;")
        c4.addWidget(c4_title)
        f4 = QtWidgets.QFormLayout()
        f4.setSpacing(10)
        self.cmb_appearance = QtWidgets.QComboBox()
        self.cmb_appearance.addItems(["auto", "light", "dark"])
        idx = self.cmb_appearance.findText(self.config.appearance)
        if idx >= 0:
            self.cmb_appearance.setCurrentIndex(idx)
        f4.addRow("테마", self.cmb_appearance)
        c4.addLayout(f4)
        v.addWidget(look_card)

        # Save button
        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch(1)
        btn_save = QtWidgets.QPushButton("설정 저장")
        btn_save.setObjectName("Primary")
        btn_save.clicked.connect(self._save)
        btn_row.addWidget(btn_save)
        v.addLayout(btn_row)
        v.addStretch(1)

        # Initial render: populate the API-key placeholder with what we
        # currently have, update the status line, and probe the
        # endpoint once to fill in the model list automatically.
        self._refresh_status()
        QtCore.QTimer.singleShot(0, self._auto_detect_models)

    # ------------------------------------------------------------------
    # Single connection card — provider type is inferred from the URL,
    # never asked.  Reasoning is decided automatically per model name.
    # ------------------------------------------------------------------
    def _current_provider(self) -> str:
        from ..llm.client import infer_provider_from_url
        return infer_provider_from_url(
            self.edit_base_url.text().strip(),
            self.cmb_model.currentText().strip(),
        )

    def _on_endpoint_changed(self, _t: str) -> None:
        self._refresh_status()
        self._probe_timer.start()

    def _auto_detect_models(self) -> None:
        """Ask the configured endpoint what models it serves and adapt
        the model widget accordingly.

        Most non-Gemini endpoints serve exactly one model — in that
        case we lock the field to that id and tell the user.  If the
        endpoint advertises several, show them as a drop-down.  If the
        probe fails or returns nothing, fall back to free-text entry
        (Gemini is treated this way too: it serves many models so the
        editable preset list is the right UX).
        """
        from ..llm.client import list_models, infer_provider_from_url
        from ..config import get_api_key

        url = self.edit_base_url.text().strip()
        provider = infer_provider_from_url(url, self.cmb_model.currentText().strip())
        # Gemini's /models lists ~30 entries — too noisy.  Keep the
        # preset behaviour for Gemini and only auto-detect for
        # OpenAI-compat endpoints (where single-model setups are
        # common: Ollama, vLLM, llama-server, ...).
        if provider == "gemini":
            self._set_gemini_presets()
            return

        key = get_api_key(self.config, provider=provider) or ""
        models = list_models(url, key) if url else []

        self.cmb_model.blockSignals(True)
        current = self.cmb_model.currentText().strip()
        self.cmb_model.clear()
        if len(models) == 1:
            only = models[0]
            self.cmb_model.addItem(only)
            self.cmb_model.setCurrentText(only)
            self.cmb_model.setEditable(False)
            self.lbl_model_help.setText(
                f"엔드포인트가 단일 모델 ‘{only}’ 만 제공해서 자동 선택했습니다."
            )
        elif len(models) > 1:
            self.cmb_model.setEditable(False)
            self.cmb_model.addItems(models)
            if current in models:
                self.cmb_model.setCurrentText(current)
            self.lbl_model_help.setText(
                f"엔드포인트가 제공하는 {len(models)}개 모델 중에서 선택하세요."
            )
        else:
            self.cmb_model.setEditable(True)
            if current:
                self.cmb_model.addItem(current)
            self.cmb_model.setCurrentText(current)
            if url:
                self.lbl_model_help.setText(
                    "엔드포인트에서 모델 목록을 받지 못했습니다. 모델 ID를 직접 입력해 주세요."
                )
            else:
                self.lbl_model_help.setText("")
        self.cmb_model.blockSignals(False)
        self._refresh_status()

    def _set_gemini_presets(self) -> None:
        presets = [
            "gemini-2.5-flash",
            "gemini-2.5-pro",
            "gemini-2.5-flash-lite",
            "gemini-1.5-flash",
            "gemini-1.5-pro",
        ]
        current = self.cmb_model.currentText().strip() or presets[0]
        self.cmb_model.blockSignals(True)
        self.cmb_model.setEditable(True)
        self.cmb_model.clear()
        self.cmb_model.addItems(presets)
        self.cmb_model.setCurrentText(current)
        self.cmb_model.blockSignals(False)
        self.lbl_model_help.setText("Gemini는 한 엔드포인트에서 여러 모델을 선택할 수 있습니다.")

    def _refresh_status(self) -> None:
        from ..config import get_api_key, provider_label, Config

        provider = self._current_provider()
        url = self.edit_base_url.text().strip()
        model = self.cmb_model.currentText().strip()

        proxy = Config()
        proxy.llm_provider = provider
        proxy.llm_base_url = url
        pname = provider_label(proxy)

        existing = get_api_key(self.config, provider=provider)
        if existing:
            self.edit_key.setPlaceholderText(f"{pname} 키 저장됨 — 덮어쓰려면 새 키 입력")
        else:
            self.edit_key.setPlaceholderText("비워두면 Mock 모드 — 키 없이 휴리스틱 분류")

        if existing:
            target = url or ("Google AI Studio" if provider == "gemini" else "(기본 OpenAI 호환)")
            self.lbl_status.setText(f"● 연결 준비 — {pname} · {target} · 모델 {model}")
            self.lbl_status.setStyleSheet("color:#0a8a3a;font-size:12px;")
        else:
            self.lbl_status.setText("○ Mock 모드 — API 키가 없으면 휴리스틱 분류로 동작합니다.")
            self.lbl_status.setStyleSheet("color:#a07000;font-size:12px;")

    # ------------------------------------------------------------------
    def _save_key(self):
        key = self.edit_key.text().strip()
        if not key:
            return
        provider = self._current_provider()
        secure = set_api_key(key, self.config, provider=provider)
        self.edit_key.clear()
        if not secure:
            self.lbl_status.setText("키가 저장됐지만 keyring을 못 찾아 config.json 평문에 들어갔습니다.")
            self.lbl_status.setStyleSheet("color:#a07000;font-size:12px;")
        else:
            self._refresh_status()
        # New key may unlock the /models probe — re-detect.
        QtCore.QTimer.singleShot(0, self._auto_detect_models)
        self.config_changed.emit()

    def _clear_key(self):
        provider = self._current_provider()
        set_api_key("", self.config, provider=provider)
        self.config.api_key_fallback = ""
        save_config(self.config)
        self._refresh_status()
        self.config_changed.emit()

    # ------------------------------------------------------------------
    # Preset management
    # ------------------------------------------------------------------
    def _populate_preset_combo(self):
        self.cmb_preset.blockSignals(True)
        self.cmb_preset.clear()
        self.cmb_preset.addItem("(저장되지 않음)", userData="")
        active = (self.config.active_preset or "").strip()
        for p in (self.config.llm_presets or []):
            name = p.get("name") if isinstance(p, dict) else None
            if not name:
                continue
            self.cmb_preset.addItem(name, userData=name)
        # restore selection
        idx = self.cmb_preset.findData(active)
        self.cmb_preset.setCurrentIndex(idx if idx >= 0 else 0)
        self.cmb_preset.blockSignals(False)

    def _preset_by_name(self, name: str):
        for p in (self.config.llm_presets or []):
            if isinstance(p, dict) and p.get("name") == name:
                return p
        return None

    def _on_preset_chosen(self, _idx: int):
        name = self.cmb_preset.currentData()
        if name is None:
            return
        # User picked the placeholder "(저장되지 않음)" entry — clear
        # the active preset but leave the form values alone.
        if not name:
            self.config.active_preset = ""
            save_config(self.config)
            self.config_changed.emit()
            self._refresh_status()
            return
        p = self._preset_by_name(name)
        if not p:
            return
        # Replace flat fields with preset values …
        new_url = (p.get("base_url") or "").strip()
        new_model = (p.get("model") or "").strip()
        new_provider = (p.get("llm_provider") or "").strip().lower()
        new_reasoning = (p.get("reasoning_mode") or "off").strip().lower()
        self.edit_base_url.setText(new_url)
        if new_model:
            if self.cmb_model.findText(new_model) < 0:
                self.cmb_model.addItem(new_model)
            self.cmb_model.setCurrentText(new_model)
        # … and immediately persist them to the live Config + disk so
        # the next pipeline run uses the new endpoint without the user
        # having to also click "저장".  Past pain: switching from
        # Gemini → Qwen via the dropdown changed the form fields but
        # the next run still used Gemini because save_config wasn't
        # called.
        self.config.active_preset = name
        if new_provider in ("gemini", "openai_compat"):
            self.config.llm_provider = new_provider
        else:
            # Fall back to inference from URL (existing helper).
            try:
                self.config.llm_provider = self._current_provider()
            except Exception:
                pass
        self.config.llm_base_url = new_url
        self.config.model = new_model
        self.config.reasoning_mode = new_reasoning or "off"
        save_config(self.config)
        # Don't write the actual API key into the masked input (would
        # leak its length to onlookers).  The status line under the
        # form already says "{provider} 키 저장됨" if a key exists for
        # this preset's provider, so the user knows it'll be used.
        self.edit_key.clear()
        self._refresh_status()
        self.config_changed.emit()

    def _preset_add(self):
        name, ok = QtWidgets.QInputDialog.getText(
            self, "프리셋 추가",
            "이름:\n(현재 화면의 URL · 모델 · 키가 이 이름으로 저장됩니다)"
        )
        if not ok or not (name := name.strip()):
            return
        if self._preset_by_name(name) is not None:
            QtWidgets.QMessageBox.warning(
                self, "이미 있음", f"'{name}' 프리셋이 이미 존재합니다."
            )
            return
        self.config.llm_presets = list(self.config.llm_presets or [])
        self.config.llm_presets.append({
            "name": name,
            "llm_provider": self._current_provider(),
            "base_url": self.edit_base_url.text().strip(),
            "model": self.cmb_model.currentText().strip(),
            "reasoning_mode": "off",
        })
        self.config.active_preset = name
        save_config(self.config)
        self._populate_preset_combo()

    def _preset_rename(self):
        name = self.cmb_preset.currentData()
        if not name:
            QtWidgets.QMessageBox.information(
                self, "프리셋 없음", "이름을 변경할 프리셋을 먼저 선택하세요."
            )
            return
        new_name, ok = QtWidgets.QInputDialog.getText(
            self, "이름 변경", "새 이름:", text=name
        )
        if not ok or not (new_name := new_name.strip()) or new_name == name:
            return
        if self._preset_by_name(new_name) is not None:
            QtWidgets.QMessageBox.warning(
                self, "이미 있음", f"'{new_name}' 프리셋이 이미 존재합니다."
            )
            return
        for p in (self.config.llm_presets or []):
            if isinstance(p, dict) and p.get("name") == name:
                p["name"] = new_name
                break
        if self.config.active_preset == name:
            self.config.active_preset = new_name
        save_config(self.config)
        self._populate_preset_combo()

    def _preset_delete(self):
        name = self.cmb_preset.currentData()
        if not name:
            return
        resp = QtWidgets.QMessageBox.question(
            self, "프리셋 삭제",
            f"프리셋 '{name}' 을 삭제할까요?\n"
            "(URL · 모델 설정만 사라지고 API 키는 keyring 에 그대로 남습니다.)",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        )
        if resp != QtWidgets.QMessageBox.Yes:
            return
        self.config.llm_presets = [
            p for p in (self.config.llm_presets or [])
            if not (isinstance(p, dict) and p.get("name") == name)
        ]
        if self.config.active_preset == name:
            self.config.active_preset = ""
        save_config(self.config)
        self._populate_preset_combo()

    def _save_active_preset_snapshot(self):
        """Update the currently-active preset to match the freshly-saved
        flat fields, so editing & re-saving a preset isn't lost."""
        name = (self.config.active_preset or "").strip()
        if not name:
            return
        for p in (self.config.llm_presets or []):
            if isinstance(p, dict) and p.get("name") == name:
                p["llm_provider"] = self.config.llm_provider
                p["base_url"] = self.config.llm_base_url
                p["model"] = self.config.model
                p["reasoning_mode"] = self.config.reasoning_mode
                return

    # ------------------------------------------------------------------
    def _save(self):
        provider = self._current_provider()
        self.config.llm_provider = provider
        self.config.llm_base_url = self.edit_base_url.text().strip()
        self.config.model = self.cmb_model.currentText().strip()
        # Auto-tuned values: never user-editable.  Always force the
        # behaviour to "single call when it fits, micro-batch otherwise"
        # and a sensible ambiguity threshold so users don't have to
        # think about it.
        self.config.economy_mode = True
        self.config.local_microbatch_mode = "auto"
        self.config.batch_size = 30          # legacy fallback only
        self.config.ambiguity_threshold = 0.15
        self.config.max_excerpt_chars = 1800
        # economy_max_files is kept as a soft cap; the planner now uses
        # the model's real context window when available.
        self.config.economy_max_files = max(self.config.economy_max_files or 120, 60)
        # organize_mode is set on the start-screen radio (per-run), not
        # here.  Keep the legacy reclassify_mode bool aligned with the
        # latest run so old code paths don't desync.
        self.config.dedup_min_bytes = int(self.spin_dedup_mb.value()) * (1 << 20)
        self.config.appearance = self.cmb_appearance.currentText()
        # Reasoning mode is decided automatically from the model name in
        # OpenAICompatClient — no user knob.  Keep the saved value at
        # "off" so any persisted older state can't surprise.
        self.config.reasoning_mode = "off"
        # Mirror the saved flat fields back into the active preset so a
        # round-trip "select preset → edit → save" updates that preset.
        self._save_active_preset_snapshot()
        save_config(self.config)
        self.config_changed.emit()
        show_toast_dialog(
            self,
            "설정 저장됨",
            "변경한 설정이 적용되었습니다.",
            kind="info",
        )
