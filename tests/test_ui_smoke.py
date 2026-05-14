"""UI instantiation smoke test under the offscreen Qt platform."""
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")
from PySide6 import QtCore, QtWidgets  # noqa: E402

from folder1004.config import default_paths, load_config  # noqa: E402
from folder1004.ui.main import MainWindow  # noqa: E402


def test_every_stream_label_in_planner_uses_live_status_phrase():
    """Every ``stream_label=...`` literal in the planner must contain
    "토큰 수신" so the UI's ``_is_live_status`` detector treats the
    streamed-token lines as in-place updates rather than spawning a
    fresh row per token tick.  Past regression: filename-first pass
    used "토큰" without "수신" and the user saw rows pile up.
    """
    src = (Path(__file__).resolve().parents[1]
           / "src" / "folder1004" / "planner.py").read_text(encoding="utf-8")
    import re
    # Match either f-strings or plain strings.
    bad: list[str] = []
    for m in re.finditer(r'stream_label\s*=\s*([fF]?"[^"]+")', src):
        lit = m.group(1)
        if "토큰 수신" not in lit:
            bad.append(lit)
    assert not bad, (
        "stream_label literals missing '토큰 수신' phrase — UI will spawn "
        f"a new row per token tick: {bad}"
    )


def test_live_status_collapses_heartbeat_and_token_stream(tmp_path, monkeypatch):
    """Heartbeat ("…N s 경과") and token-stream ("토큰 수신") lines for
    the same planning stage must overwrite each other on the same row,
    not pile up one new row per second.  Stage transitions still
    append a fresh row.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    from PySide6 import QtWidgets
    from folder1004.config import default_paths, load_config
    from folder1004.ui.views import OrganizeView

    paths = default_paths()
    paths.ensure()
    cfg = load_config(paths)
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    v = OrganizeView(cfg)
    v.set_running(True)

    seq = [
        "plan: LLM 호출 중 (5 파일)…",
        "plan: LLM 응답 대기 중 (5 파일) … 0s 경과",
        "plan: LLM 응답 대기 중 (5 파일) … 1s 경과",
        "plan 토큰 수신 (5 파일): 12자 수신 중 — …",
        "plan: LLM 응답 대기 중 (5 파일) … 2s 경과",
        "plan 토큰 수신 (5 파일): 48자 수신 중 — …",
        "plan 토큰 수신 (5 파일): 96자 수신 중 — …",
        "plan: 응답 수신 — 카테고리 1",
        "organize: 파일 이동 시작",
    ]
    for line in seq:
        v.on_status(line)

    text = v.log_view.toPlainText()
    rows = [line for line in text.splitlines() if line.strip()]
    # 9 status events arrived but 6 of them are heartbeat / token-stream
    # for the same plan stage and must collapse onto a single in-place
    # row.  The other 3 rows are real stage transitions
    # (호출 중, 응답 수신, organize 시작).  So we expect ≤ 4 rows total.
    assert len(rows) <= 4, f"too many rows ({len(rows)}); rows={rows}"
    # The collapsed plan-stream row must show the latest progress.
    assert any("96자" in line for line in rows)
    # And no row counts the heartbeat-second appearing twice.
    assert sum(1 for line in rows if "1s 경과" in line) == 0
    assert sum(1 for line in rows if "2s 경과" in line) == 0


def test_token_stream_preview_strips_json_noise(tmp_path, monkeypatch):
    """Streaming preview must show readable Korean + meaningful tokens,
    not JSON syntax (\\", {, [, : , ,) — that's what the user saw as
    'looks like an error message'."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    from PySide6 import QtWidgets
    from folder1004.config import default_paths, load_config
    from folder1004.planner import Planner

    paths = default_paths()
    paths.ensure()
    cfg = load_config(paths)
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    p = Planner(cfg, gemini=None)
    seen: list[str] = []

    # Synthesise an _on_stream by going through a fake LLMCall: easier
    # to reach the helper by calling the closure builder used by
    # _llm_call.  We instead re-import the closure's helper directly.
    # The behaviour is exposed via a real Planner stream via _on_stream
    # which is created inside _llm_call.  Simulate that flow by
    # invoking the public API with a fake client.
    class _Fake:
        def generate_json(self, prompt, *, heartbeat=None,
                          cancel_check=None, stream_text=None):
            if stream_text:
                stream_text(
                    'partial: {"categories":[{"id":"alpha"}],"reason":"…"}',
                    52,
                )
            return {"categories": [{"id": "alpha", "name": "Alpha", "group": 1}],
                    "assignments": []}

    p.gemini = _Fake()
    def progress(msg, _pct):
        seen.append(msg)
    # Drive a single _llm_call via the planner.
    p._llm_call("hi",
                heartbeat=None,
                stream_label="plan 토큰 수신 (5 파일)",
                progress=progress)

    stream_lines = [m for m in seen if "토큰 수신" in m]
    assert stream_lines, "no streaming preview emitted"
    last = stream_lines[-1]
    # Split off the header ("plan … 52자 수신 중 — ") and inspect only
    # the body portion that came from the LLM stream.
    body = last.split("—", 1)[1]
    for ch in ['{', '}', '[', ']', '\\"']:
        assert ch not in body, f"JSON noise {ch!r} leaked into preview: {last!r}"
    # Korean / Latin words from the stream should still be visible.
    assert "alpha" in body or "categories" in body


def test_mainwindow_builds(tmp_path, monkeypatch):
    # Point XDG/HOME-like paths at tmp
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    paths = default_paths()
    paths.ensure()
    cfg = load_config(paths)

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    w = MainWindow(cfg, paths)
    w.show()
    app.processEvents()
    # Switch through each tab
    for idx in range(4):
        w._goto(idx)
        app.processEvents()
    w.close()
    w.index_db.close()


def test_cancel_path_never_force_terminates_qthread():
    src = (Path(__file__).resolve().parents[1]
           / "src" / "folder1004" / "ui" / "main.py").read_text(encoding="utf-8")
    assert ".terminate(" not in src


def test_organize_guidance_presets_are_hidden_toggles(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    from folder1004.config import (
        CLASSIFICATION_GUIDANCE_PRESETS,
        Config,
        combined_classification_guidance,
    )
    from folder1004.ui.views import OrganizeView

    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    view = OrganizeView(Config())
    preset = CLASSIFICATION_GUIDANCE_PRESETS[0]
    label = preset["label"]
    button = view.classification_preset_buttons[label]

    assert not button.toolTip()
    button.setChecked(True)
    assert button.text() == label
    assert button.objectName() == "PresetTag"
    assert button.styleSheet() == ""
    assert preset["text"] not in view.edit_custom_classification_guidance.toPlainText()

    view.edit_custom_classification_guidance.setPlainText("고객명과 기간을 우선해줘.")
    view._sync_classification_guidance_config()
    assert view.config.classification_guidance == "고객명과 기간을 우선해줘."
    assert view.config.classification_guidance_preset_names == [label]
    combined = combined_classification_guidance(view.config)
    assert preset["text"] in combined
    assert "고객명과 기간" in combined
    view.close()


def test_organize_recommends_classification_style_from_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    from folder1004.config import Config
    from folder1004.ui.views import OrganizeView

    folder = tmp_path / "Downloads"
    folder.mkdir()
    (folder / "setup.exe").write_text("fake", encoding="utf-8")
    (folder / "archive.zip").write_text("fake", encoding="utf-8")

    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    view = OrganizeView(Config())
    view.path_bar.set_path(str(folder))

    assert view.classification_preset_buttons["업무/용도 중심"].isChecked()
    assert view.classification_preset_buttons["버림 후보 분리"].isChecked()
    assert "업무/용도 중심" in view.config.classification_guidance_preset_names
    assert "버림 후보 분리" in view.config.classification_guidance_preset_names
    assert "추천 적용" in view.lbl_classification_recommendation.text()
    view.close()


def test_prompt_builders_include_classification_guidance():
    from folder1004.llm import prompts

    prompt = prompts.build_single_call(
        [{"path": "/tmp/a.pdf", "name": "a.pdf"}],
        3,
        12,
        0.15,
        classification_guidance="고객명 중심으로 묶어줘.",
    )
    assert "사용자 분류 원칙" in prompt
    assert "고객명 중심" in prompt


def test_guidance_toggle_checked_state_has_visible_style():
    from folder1004.ui.styles import DARK_QSS, LIGHT_QSS

    for qss in (LIGHT_QSS, DARK_QSS):
        assert "QPushButton#PresetTag:checked" in qss
        checked_block = qss.split("QPushButton#PresetTag:checked", 1)[1].split("}", 1)[0]
        assert "background" in checked_block
        assert "border" in checked_block
        assert "font-weight" in checked_block


def test_default_mode_explains_auto_decision():
    from folder1004.config import Config
    from folder1004.ui.views import OrganizeView

    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    view = OrganizeView(Config())
    labels = [child.text() for child in view.findChildren(QtWidgets.QLabel)]
    radios = [child.text() for child in view.findChildren(QtWidgets.QRadioButton)]
    assert any("알아서 판단" in text for text in labels)
    assert any("자동 판단 기본값" in text for text in radios)
    view.close()


def test_folder_mode_hint_sits_to_the_right_of_heading():
    from folder1004.config import Config
    from folder1004.ui.views import OrganizeView

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    view = OrganizeView(Config())
    view.resize(900, 760)
    view.show()
    app.processEvents()

    title = view.findChild(QtWidgets.QLabel, "ModeTitle")
    hint = view.findChild(QtWidgets.QLabel, "ModeHint")
    assert title is not None
    assert hint is not None
    title_pos = title.mapTo(view, QtCore.QPoint(0, 0))
    hint_pos = hint.mapTo(view, QtCore.QPoint(0, 0))
    assert hint_pos.x() > title_pos.x() + title.width()
    assert abs(hint_pos.y() - title_pos.y()) <= 8
    assert hint.width() > title.width() * 2
    view.close()


def test_organize_view_removes_dry_run_checkbox_and_emits_live_run(tmp_path, monkeypatch):
    monkeypatch.setenv("FOLDER1004_HOME", str(tmp_path / "appdata"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    from folder1004.config import Config
    from folder1004.ui.views import OrganizeView

    folder = tmp_path / "target"
    folder.mkdir()
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    view = OrganizeView(Config())
    checkboxes = [child.text() for child in view.findChildren(QtWidgets.QCheckBox)]
    assert not any("Dry" in text or "미리보기" in text for text in checkboxes)

    emitted = []
    view.start_requested.connect(
        lambda path, recursive, dry_run, mode: emitted.append((path, recursive, dry_run, mode))
    )
    view.path_bar.set_path(str(folder))
    view._on_start()
    assert emitted
    assert emitted[-1][2] is False
    view.close()


def test_long_pages_are_scrollable_at_compact_height():
    from folder1004.config import Config
    from folder1004.ui.views import OrganizeView, SettingsView

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    for view_cls, anchor_name in (
        (OrganizeView, "btn_primary"),
        (SettingsView, "cmb_appearance"),
    ):
        view = view_cls(Config())
        view.resize(720, 520)
        view.show()
        app.processEvents()

        scroll = view.findChild(QtWidgets.QScrollArea, "PageScroll")
        anchor = getattr(view, anchor_name)
        assert scroll is not None
        assert scroll.widget() is not None
        assert scroll.verticalScrollBar().maximum() > 0

        scroll.ensureWidgetVisible(anchor)
        app.processEvents()
        anchor_rect = QtCore.QRect(anchor.mapTo(scroll.viewport(), QtCore.QPoint(0, 0)), anchor.size())
        assert scroll.viewport().rect().intersects(anchor_rect)
        view.close()


def test_folder_mode_options_wrap_without_overlap():
    from folder1004.config import Config
    from folder1004.ui.views import OrganizeView

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    view = OrganizeView(Config())
    view.resize(520, 560)
    view.show()
    app.processEvents()

    radios = [view.rad_new, view.rad_inc, view.rad_add]
    rects = [
        QtCore.QRect(radio.mapTo(view, QtCore.QPoint(0, 0)), radio.size()).adjusted(1, 1, -1, -1)
        for radio in radios
    ]
    for i, rect in enumerate(rects):
        for other in rects[i + 1:]:
            assert not rect.intersects(other)
    view.close()


def test_guidance_toggle_buttons_do_not_overlap_when_checked():
    from folder1004.config import Config
    from folder1004.ui.views import OrganizeView

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    view = OrganizeView(Config())
    view.resize(560, 760)
    view.show()
    for button in view.classification_preset_buttons.values():
        button.setChecked(True)
    app.processEvents()
    buttons = list(view.classification_preset_buttons.values())
    rects = []
    for button in buttons:
        top_left = button.mapTo(view, QtCore.QPoint(0, 0))
        rects.append(QtCore.QRect(top_left, button.size()).adjusted(1, 1, -1, -1))
    for i, rect in enumerate(rects):
        for other in rects[i + 1:]:
            assert not rect.intersects(other)
    view.close()


def test_guidance_tags_do_not_overlap_custom_text_box():
    from folder1004.config import Config
    from folder1004.ui.views import OrganizeView

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    view = OrganizeView(Config())
    view.resize(560, 900)
    view.show()
    for button in view.classification_preset_buttons.values():
        button.setChecked(True)
    app.processEvents()
    tag_bottom = max(
        button.mapTo(view, QtCore.QPoint(0, 0)).y() + button.height()
        for button in view.classification_preset_buttons.values()
    )
    edit_top = view.edit_custom_classification_guidance.mapTo(view, QtCore.QPoint(0, 0)).y()
    assert tag_bottom < edit_top
    assert not hasattr(view, "classification_preset_area")
    view.close()
