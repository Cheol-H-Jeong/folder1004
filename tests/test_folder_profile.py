from pathlib import Path

from folder1004.folder_profile import analyze_folder_profile
from folder1004.metadata import collect


def _write(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _collect_all(paths: list[Path]):
    return [collect(path) for path in paths]


def test_downloads_profile_detects_installers_archives_and_trash_preset(tmp_path):
    root = tmp_path / "Downloads"
    files = _collect_all(
        [
            _write(root / "setup_app.exe"),
            _write(root / "installer.pkg"),
            _write(root / "project_assets.zip"),
            _write(root / "browser_download.crdownload"),
            _write(root / "notes.txt"),
        ]
    )

    summary = analyze_folder_profile(root, files, recursive=False)

    assert summary.profile_id == "downloads"
    assert summary.label == "다운로드/임시 보관함"
    assert summary.confidence >= 0.35
    assert "버림 후보 분리" in summary.recommended_preset_names
    assert summary.extension_counts[".exe"] == 1
    assert summary.extension_counts[".zip"] == 1
    assert summary.file_count == 5


def test_photos_profile_detects_image_and_customer_signals(tmp_path):
    root = tmp_path / "customer_photos_고객"
    files = _collect_all(
        [
            _write(root / "customer_a_photo_001.jpg", b"\xff\xd8\xff"),
            _write(root / "customer_a_photo_002.png", b"\x89PNG\r\n"),
            _write(root / "촬영_고객B.heic"),
            _write(root / "delivery_note.txt"),
        ]
    )

    summary = analyze_folder_profile(root, files, recursive=False)

    assert summary.profile_id == "photos"
    assert summary.label == "사진/촬영 자료"
    assert summary.confidence >= 0.5
    assert "사람/고객 중심" in summary.recommended_preset_names
    assert "날짜/기간 중심" in summary.recommended_preset_names
    assert summary.extension_counts[".jpg"] == 1
    assert summary.extension_counts[".png"] == 1
    assert summary.extension_counts[".heic"] == 1


def test_health_score_drops_for_many_root_installers_archives_and_temp_files(tmp_path):
    root = tmp_path / "Downloads"
    paths: list[Path] = []
    for i in range(20):
        paths.append(_write(root / f"loose_doc_{i}.txt"))
    for i in range(6):
        paths.append(_write(root / f"old_installer_{i}.exe"))
        paths.append(_write(root / f"backup_bundle_{i}.zip"))
        paths.append(_write(root / f"partial_{i}.crdownload"))

    summary = analyze_folder_profile(root, _collect_all(paths), recursive=False)

    assert summary.file_count == 38
    assert summary.root_file_count == 38
    assert summary.health_score < 40
    assert summary.health_level == "심각"
    assert any("최상위" in reason for reason in summary.health_reasons)
    assert any("설치/실행 파일" in reason for reason in summary.health_reasons)
    assert any("임시/다운로드" in reason for reason in summary.health_reasons)
    assert any("압축 파일" in reason for reason in summary.health_reasons)


def test_organize_recommendation_uses_folder_profile_module(tmp_path):
    from folder1004.config import Config
    from folder1004.ui.views import OrganizeView
    from PySide6 import QtWidgets

    root = tmp_path / "customer_photos"
    _write(root / "client_photo_001.jpg", b"\xff\xd8\xff")
    _write(root / "client_photo_002.png", b"\x89PNG\r\n")
    _write(root / "촬영_고객.heic")

    _app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    view = OrganizeView(Config())
    labels, reason = view._classification_style_recommendation(root)

    assert "사람/고객 중심" in labels
    assert "날짜/기간 중심" in labels
    assert "사진/촬영 자료" in reason
    view.close()
