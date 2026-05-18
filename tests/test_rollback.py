from __future__ import annotations

from datetime import datetime, timezone

from folder1004.index import IndexDB
from folder1004.models import Category, MovedFile, OperationResult, SkippedFile
from folder1004.rollback import rollback_latest


def test_rollback_latest_restores_files_and_deletes_operation(tmp_path):
    db = IndexDB(tmp_path / "index.sqlite3")
    root = tmp_path / "root"
    root.mkdir()
    original = root / "loose.txt"
    organized_dir = root / "001. 업무 [Folder1004·abcd]"
    organized_dir.mkdir()
    current = organized_dir / "loose.txt"
    current.write_text("content")
    shortcut = root / "loose.txt.url"
    shortcut.write_text("shortcut")

    op = OperationResult(
        target_root=root,
        started_at=datetime.now(tz=timezone.utc),
        finished_at=datetime.now(tz=timezone.utc),
        dry_run=False,
        categories=[Category(id="work", name="업무")],
        moved=[MovedFile(original, current, "work", shortcuts=[shortcut])],
        skipped=[SkippedFile(root / "skip.txt", "n/a")],
        total_scanned=1,
    )
    db.record_operation(op)
    assert op.operation_id is not None

    result = rollback_latest(db)

    assert result.operation_id == op.operation_id
    assert result.moved == 1
    assert result.deleted_shortcuts == 1
    assert original.read_text() == "content"
    assert not current.exists()
    assert not shortcut.exists()
    assert db.latest_real_operation_id() is None
    assert db.operation_file_rows(op.operation_id) == []
    db.close()
