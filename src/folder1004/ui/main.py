"""Main application window — Apple-inspired side-nav + pages layout."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from ..config import Config, normalize_organize_mode, default_paths, load_config
from ..index import IndexDB
from ..models import Plan
from ..worker import ApplyPlanWorker, OrganizeWorker
from .styles import resolve_qss
from .views import HistoryView, OrganizeView, SearchView, SettingsView
from .widgets import NavButton

log = logging.getLogger(__name__)

_qt_message_handler = None


def _install_qt_message_logging() -> None:
    """Route Qt warnings/fatal messages into the active run log.

    PyInstaller GUI builds run without a console on Windows, so Qt/plugin
    diagnostics otherwise disappear when the window closes unexpectedly.
    """
    global _qt_message_handler
    if _qt_message_handler is not None:
        return

    def _handler(mode, context, message):  # type: ignore[no-untyped-def]
        if mode == QtCore.QtMsgType.QtFatalMsg:
            level = logging.CRITICAL
        elif mode == QtCore.QtMsgType.QtCriticalMsg:
            level = logging.ERROR
        elif mode == QtCore.QtMsgType.QtWarningMsg:
            level = logging.WARNING
        else:
            level = logging.DEBUG
        where = ""
        try:
            if context and context.file:
                where = f" ({context.file}:{context.line})"
        except Exception:
            where = ""
        logging.getLogger("folder1004.qt").log(level, "%s%s", message, where)

    _qt_message_handler = _handler
    try:
        QtCore.qInstallMessageHandler(_handler)
    except Exception:
        log.exception("failed to install Qt message handler")


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, config: Config, paths):
        super().__init__()
        self.config = config
        self.paths = paths
        self.index_db = IndexDB(paths.index_db)
        self.setWindowTitle("Folder1004")
        self.resize(1180, 760)

        self._thread: QtCore.QThread | None = None
        self._worker: OrganizeWorker | ApplyPlanWorker | None = None
        self._closing_after_worker = False
        self._current_action = "run"
        self._current_target: Path | None = None

        self._build()
        self._apply_style()

    # ------------------------------------------------------------------
    def _build(self):
        root = QtWidgets.QWidget()
        root.setObjectName("MainRoot")
        self.setCentralWidget(root)
        row = QtWidgets.QHBoxLayout(root)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        # Sidebar ------------------------------------------------------
        sidebar = QtWidgets.QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(220)
        sb = QtWidgets.QVBoxLayout(sidebar)
        sb.setContentsMargins(16, 18, 16, 16)
        sb.setSpacing(6)

        logo = QtWidgets.QLabel("Folder1004")
        logo.setStyleSheet("font-size:19px;font-weight:700;padding:6px 10px 18px 10px;")
        sb.addWidget(logo)

        self.nav_buttons: list[NavButton] = []
        for idx, (key, name, icon) in enumerate(
            [
                ("organize", "정리", "✨"),
                ("search", "검색", "🔎"),
                ("history", "히스토리", "🕒"),
                ("settings", "설정", "⚙"),
            ]
        ):
            btn = NavButton(name, icon)
            btn.clicked.connect(lambda _=False, i=idx: self._goto(i))
            sb.addWidget(btn)
            self.nav_buttons.append(btn)

        sb.addStretch(1)
        self.status_badge = QtWidgets.QLabel()
        self.status_badge.setWordWrap(True)
        self.status_badge.setStyleSheet("color:#6e6e73;font-size:12px;padding:4px 10px;")
        sb.addWidget(self.status_badge)

        row.addWidget(sidebar)

        # Pages --------------------------------------------------------
        self.stack = QtWidgets.QStackedWidget()
        self.organize_view = OrganizeView(self.config)
        self.search_view = SearchView(self.index_db)
        self.history_view = HistoryView(self.index_db)
        self.settings_view = SettingsView(self.config)

        self.stack.addWidget(self.organize_view)
        self.stack.addWidget(self.search_view)
        self.stack.addWidget(self.history_view)
        self.stack.addWidget(self.settings_view)
        row.addWidget(self.stack, 1)

        self.organize_view.start_requested.connect(self._start)
        self.organize_view.cancel_requested.connect(self._cancel)
        self.organize_view.rollback_requested.connect(self._rollback_latest)
        self.settings_view.config_changed.connect(self._on_config_changed)

        # Menu / shortcuts --------------------------------------------
        self._build_menu()
        QtGui.QShortcut(QtGui.QKeySequence.Find, self, self._focus_search)
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+,"), self, lambda: self._goto(3))
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+1"), self, lambda: self._goto(0))
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+2"), self, lambda: self._goto(1))
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+3"), self, lambda: self._goto(2))

        self._goto(0)
        self._update_status_badge()

    def _apply_style(self):
        self.setStyleSheet(resolve_qss(self.config.appearance))

    def _build_menu(self):
        self.diagnostics_menu = QtWidgets.QMenu("진단", self)
        self.menuBar().addMenu(self.diagnostics_menu)

        show_errors = QtGui.QAction("최근 오류 기록 보기/복사…", self)
        show_errors.setShortcut(QtGui.QKeySequence("Ctrl+Shift+E"))
        show_errors.triggered.connect(self._show_recent_error_report)
        self.diagnostics_menu.addAction(show_errors)

        copy_errors = QtGui.QAction("최근 오류 기록 바로 복사", self)
        copy_errors.triggered.connect(self._copy_recent_error_report)
        self.diagnostics_menu.addAction(copy_errors)

        self.diagnostics_menu.addSeparator()

        open_logs = QtGui.QAction("로그 폴더 열기", self)
        open_logs.triggered.connect(self._open_log_dir)
        self.diagnostics_menu.addAction(open_logs)

        open_current = QtGui.QAction("현재 로그 파일 열기", self)
        open_current.triggered.connect(self._open_current_log)
        self.diagnostics_menu.addAction(open_current)

        self.diagnostics_menu.addSeparator()

        rollback = QtGui.QAction("최근 정리 롤백", self)
        rollback.triggered.connect(self._rollback_latest)
        self.diagnostics_menu.addAction(rollback)

    def _goto(self, idx: int):
        self.stack.setCurrentIndex(idx)
        for i, btn in enumerate(self.nav_buttons):
            btn.setChecked(i == idx)
        if idx == 1:
            self.search_view.focus_search()
        elif idx == 2:
            self.history_view.refresh()

    def _focus_search(self):
        self._goto(1)

    def _open_log_dir(self):
        from ..config import default_paths
        from .views import _open_in_explorer

        d = default_paths().logs_dir
        d.mkdir(parents=True, exist_ok=True)
        _open_in_explorer(d)

    def _open_current_log(self):
        from ..runlog import current_log_path
        from .views import _open_in_explorer

        p = current_log_path()
        if p is not None and p.exists():
            _open_in_explorer(p)
        else:
            self.statusBar().showMessage("현재 로그 파일을 찾지 못했습니다.", 4000)

    def _copy_recent_error_report(self):
        from ..runlog import recent_error_report

        report = recent_error_report()
        QtWidgets.QApplication.clipboard().setText(report)
        self.statusBar().showMessage("최근 오류 기록을 클립보드에 복사했습니다.", 5000)

    def _show_recent_error_report(self):
        from ..runlog import recent_error_report

        report = recent_error_report()
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("최근 오류 기록")
        dialog.resize(860, 620)
        layout = QtWidgets.QVBoxLayout(dialog)
        layout.setContentsMargins(18, 18, 18, 14)
        layout.setSpacing(10)

        intro = QtWidgets.QLabel(
            "최근 실행/정리 로그에서 오류·Traceback·강제 종료 단서를 모았습니다. "
            "아래 내용을 복사해서 전달하면 원인 분석에 사용할 수 있습니다."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        text = QtWidgets.QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText(report)
        text.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        text.setMinimumHeight(420)
        text.setStyleSheet(
            "QPlainTextEdit { font-family:'JetBrains Mono','SF Mono',Consolas,monospace; "
            "font-size:12px; }"
        )
        layout.addWidget(text, 1)

        buttons = QtWidgets.QHBoxLayout()
        open_logs = QtWidgets.QPushButton("로그 폴더 열기")
        open_logs.setObjectName("Ghost")
        open_logs.clicked.connect(self._open_log_dir)
        copy = QtWidgets.QPushButton("클립보드에 복사")
        copy.setObjectName("Primary")
        copy.clicked.connect(lambda: QtWidgets.QApplication.clipboard().setText(text.toPlainText()))
        copy.clicked.connect(lambda: self.statusBar().showMessage("최근 오류 기록을 복사했습니다.", 5000))
        close = QtWidgets.QPushButton("닫기")
        close.clicked.connect(dialog.accept)
        buttons.addWidget(open_logs)
        buttons.addStretch(1)
        buttons.addWidget(copy)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        dialog.exec()

    def _update_status_badge(self):
        from ..config import get_api_key, provider_label

        key = get_api_key(self.config)
        if key:
            self.status_badge.setText(
                f"{provider_label(self.config)}: 연결됨\n모델: {self.config.model}"
            )
        else:
            self.status_badge.setText("Mock 모드\n설정에서 API 키 등록")

    def _on_config_changed(self):
        self._apply_style()
        self.organize_view.refresh_api_badge()
        self._update_status_badge()

    # ------------------------------------------------------------------
    def _start(self, path: str, recursive: bool, dry_run: bool, mode: str = "new", action: str = "run"):
        if self._thread is not None:
            return
        self._current_action = action or ("preview" if dry_run else "run")
        self._current_target = Path(path)
        # Persist the chosen mode onto the live config so the pipeline
        # picks it up.  Legacy mode ids are normalized for old configs.
        self.config.organize_mode = normalize_organize_mode(mode)
        # Open a fresh per-run log file so every Organize run is captured
        # with full INFO/DEBUG and tracebacks.
        from ..runlog import start_session

        try:
            start_session("organize")
        except Exception:
            pass
        self._thread = QtCore.QThread()
        self._worker = OrganizeWorker(
            Path(path), self.config, recursive, dry_run, self.index_db
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.stage_changed.connect(self.organize_view.on_stage)
        self._worker.status.connect(self.organize_view.on_status)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._thread.start()

    def _start_preplanned(self, target: Path, plan: Plan):
        if self._thread is not None:
            return
        self._current_action = "run"
        self._current_target = Path(target)
        from ..runlog import start_session

        try:
            start_session("organize")
        except Exception:
            pass
        self.organize_view.set_running(True)
        self._thread = QtCore.QThread()
        self._worker = ApplyPlanWorker(Path(target), self.config, plan, self.index_db)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.stage_changed.connect(self.organize_view.on_stage)
        self._worker.status.connect(self.organize_view.on_status)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._thread.start()

    def _cancel(self):
        if self._worker:
            self._worker.cancel()
        # Update the UI immediately so the user gets an instant
        # acknowledgement, regardless of how quickly the worker thread
        # actually unwinds.
        if hasattr(self, "organize_view"):
            self.organize_view.show_canceling()
        # Give the worker a brief grace window to stop on its own
        # (next safe checkpoint).  Do NOT force-terminate QThread:
        # on Windows that can kill a thread while pypdf/openpyxl/Qt is
        # holding native resources and make the app disappear.
        QtCore.QTimer.singleShot(800, self._force_teardown_after_cancel)

    def _force_teardown_after_cancel(self):
        if self._thread is None:
            return
        if self._thread.isRunning():
            # Ask the thread event loop to quit, but never force-kill it.
            # The worker checks the cancel flag between safe stages.
            self._thread.quit()
            if not self._thread.wait(400):
                log.warning("worker still unwinding after cancel; waiting safely")
                QtCore.QTimer.singleShot(1000, self._force_teardown_after_cancel)
                return
        self._teardown_worker()
        self.organize_view.show_canceled()

    def _on_finished(self, op):
        self._teardown_worker()
        if self._current_action == "preview" or op.dry_run:
            plan = self.organize_view.confirm_preview_plan(op)
            self._current_action = "run"
            if plan is not None and self._current_target is not None:
                self._start_preplanned(self._current_target, plan)
            return
        self.organize_view.on_finished(op)
        # refresh history list since we added a record
        if self.stack.currentWidget() is self.history_view:
            self.history_view.refresh()
        if getattr(self, "_closing_after_worker", False):
            self.close()

    def _rollback_latest(self):
        try:
            from ..rollback import rollback_latest

            result = rollback_latest(self.index_db)
        except Exception as exc:
            log.exception("rollback failed")
            QtWidgets.QMessageBox.warning(self, "롤백 실패", str(exc) or type(exc).__name__)
            return
        if result.moved == 0 and result.deleted_shortcuts == 0:
            QtWidgets.QMessageBox.information(self, "롤백할 작업 없음", "되돌릴 최근 정리 작업을 찾지 못했습니다.")
            return
        self.organize_view.on_status(
            f"rollback: 파일 {result.moved}개 복구 / 바로가기 {result.deleted_shortcuts}개 제거 / 스킵 {len(result.skipped)}개"
        )
        self.organize_view._set_toast((
            "info",
            "최근 정리를 롤백했습니다",
            f"파일 {result.moved}개를 원래 위치로 되돌렸습니다. 스킵 {len(result.skipped)}개.",
        ))
        self.history_view.refresh()

    def _on_failed(self, msg: str):
        log.error("organize failed: %s", msg)
        self.organize_view.on_failed(msg)
        self._teardown_worker()
        if getattr(self, "_closing_after_worker", False):
            self.close()

    def _teardown_worker(self):
        if self._thread:
            self._thread.quit()
            self._thread.wait(2000)
        self._thread = None
        self._worker = None

    def closeEvent(self, event: QtGui.QCloseEvent):
        if self._thread is not None and self._thread.isRunning():
            log.info("close requested while worker is running; canceling safely")
            self._closing_after_worker = True
            if self._worker:
                self._worker.cancel()
            if hasattr(self, "organize_view"):
                self.organize_view.show_canceling()
            self.hide()
            event.ignore()
            return
        try:
            self._teardown_worker()
        finally:
            self.index_db.close()
        super().closeEvent(event)


def launch(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from ..runlog import start_session

    try:
        start_session("gui")
    except Exception:
        pass
    try:
        from ..app_icon import set_windows_app_user_model_id

        set_windows_app_user_model_id()
    except Exception:
        pass
    _install_qt_message_logging()

    QtWidgets.QApplication.setHighDpiScaleFactorRoundingPolicy(
        QtCore.Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    # macOS / Windows: ensure crisp icons + native title-bar window
    # appearance.  No-op on Linux.
    if sys.platform == "darwin":
        # Use macOS unified title bar tone
        os.environ.setdefault("QT_MAC_WANTS_LAYER", "1")
    app = QtWidgets.QApplication(argv)
    app.setApplicationName("Folder1004")
    app.setApplicationDisplayName("Folder1004")
    app.setOrganizationName("Folder1004")
    app.setOrganizationDomain("folder1004.app")
    icon = QtGui.QIcon()
    try:
        from ..app_icon import app_icon

        icon = app_icon()
        app.setWindowIcon(icon)
    except Exception:
        log.debug("app icon load skipped", exc_info=True)

    paths = default_paths()
    config = load_config(paths)
    window = MainWindow(config, paths)
    try:
        window.setWindowIcon(icon)
    except Exception:
        pass
    window.show()
    return app.exec()
