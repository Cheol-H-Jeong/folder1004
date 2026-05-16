from __future__ import annotations

from folder1004.config import (
    Config,
    ORGANIZE_MODE_BUNDLE_REBUILD,
    ORGANIZE_MODE_FULL_REBUILD,
    ORGANIZE_MODE_PRESERVE_EXISTING,
    ORGANIZE_MODE_PRESERVE_FOLDER1004,
)
from folder1004.pipeline import run


def _cfg(mode: str) -> Config:
    cfg = Config()
    cfg.organize_mode = mode
    cfg.dedup_min_bytes = -1
    return cfg


def test_bundle_rebuild_moves_existing_top_folder_as_intact_bundle(tmp_path):
    folder = tmp_path / "A프로젝트"
    nested = folder / "자료"
    nested.mkdir(parents=True)
    (nested / "회의록.txt").write_text("회의 내용", encoding="utf-8")
    (tmp_path / "루트메모.txt").write_text("메모", encoding="utf-8")

    run(tmp_path, _cfg(ORGANIZE_MODE_BUNDLE_REBUILD), recursive=False, dry_run=False, force_mock=True)

    assert not folder.exists()
    moved = list(tmp_path.glob("*/A프로젝트/자료/회의록.txt"))
    assert moved, sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))


def test_preserve_existing_keeps_top_level_structure_and_stamps_folder(tmp_path):
    existing = tmp_path / "계약서"
    existing.mkdir()
    (existing / "old.txt").write_text("old", encoding="utf-8")
    (tmp_path / "계약서_추가.txt").write_text("new", encoding="utf-8")

    run(tmp_path, _cfg(ORGANIZE_MODE_PRESERVE_EXISTING), recursive=False, dry_run=False, force_mock=True)

    matches = [p for p in tmp_path.iterdir() if p.is_dir() and "계약서" in p.name]
    assert len(matches) == 1
    kept = matches[0]
    assert "[Folder1004·" in kept.name
    assert (kept / "old.txt").exists()
    assert (kept / "계약서_추가.txt").exists()
    assert not (tmp_path / "계약서_추가.txt").exists()


def test_preserve_folder1004_keeps_signed_folder_and_moves_plain_folder_as_bundle(tmp_path):
    signed = tmp_path / "1. 업무 [Folder1004·abc123]"
    signed.mkdir()
    (signed / "old.txt").write_text("old", encoding="utf-8")
    plain = tmp_path / "업무 추가자료"
    plain.mkdir()
    (plain / "회의록.txt").write_text("meeting", encoding="utf-8")

    run(tmp_path, _cfg(ORGANIZE_MODE_PRESERVE_FOLDER1004), recursive=False, dry_run=False, force_mock=True)

    assert signed.exists()
    assert (signed / "old.txt").exists()
    assert (signed / "업무 추가자료" / "회의록.txt").exists()
    assert not plain.exists()


def test_full_rebuild_dissolves_subfolders_and_removes_empty_old_folder(tmp_path):
    old = tmp_path / "A프로젝트"
    old.mkdir()
    (old / "회의록.txt").write_text("회의록", encoding="utf-8")
    (old / "사진.jpg").write_bytes(b"\xff\xd8\xff\xe0")

    run(tmp_path, _cfg(ORGANIZE_MODE_FULL_REBUILD), recursive=False, dry_run=False, force_mock=True)

    assert not old.exists()
    assert list(tmp_path.glob("*/회의록.txt"))
    assert list(tmp_path.glob("*/사진.jpg"))
    assert not list(tmp_path.glob("*/A프로젝트/*"))


def test_ui_full_rebuild_shows_warning_and_emits_full_mode(tmp_path, monkeypatch):
    from PySide6 import QtWidgets
    from folder1004.ui.views import OrganizeView

    folder = tmp_path / "target"
    folder.mkdir()
    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    view = OrganizeView(Config())
    warnings = []
    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "warning",
        lambda *args, **kwargs: warnings.append(args),
    )
    emitted = []
    view.start_requested.connect(
        lambda path, recursive, dry_run, mode: emitted.append((path, recursive, dry_run, mode))
    )
    view.path_bar.set_path(str(folder))
    view.rad_full.setChecked(True)

    view._on_start()

    assert warnings
    assert emitted[-1][3] == ORGANIZE_MODE_FULL_REBUILD
    view.close()
