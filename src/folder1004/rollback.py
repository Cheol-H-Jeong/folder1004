"""Rollback the most recent Folder1004 organize operation."""
from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .index import IndexDB

log = logging.getLogger(__name__)


@dataclass
class RollbackResult:
    operation_id: int | None = None
    moved: int = 0
    deleted_shortcuts: int = 0
    skipped: list[str] = field(default_factory=list)


def _unique_restore_path(path: Path) -> Path:
    if not path.exists():
        return path
    parent = path.parent
    stem = path.stem
    suffix = path.suffix
    idx = 2
    while True:
        candidate = parent / f"{stem} (rollback {idx}){suffix}"
        if not candidate.exists():
            return candidate
        idx += 1


def rollback_latest(index_db: IndexDB) -> RollbackResult:
    op_id = index_db.latest_real_operation_id()
    if op_id is None:
        return RollbackResult()
    return rollback_operation(index_db, op_id)


def rollback_operation(index_db: IndexDB, op_id: int) -> RollbackResult:
    rows = index_db.operation_file_rows(op_id)
    result = RollbackResult(operation_id=op_id)

    # Remove shortcuts first so Windows does not leave dangling .lnk/.url files.
    for row in rows:
        for sp in row.shortcut_paths:
            p = Path(sp)
            try:
                if p.exists() or p.is_symlink():
                    p.unlink()
                    result.deleted_shortcuts += 1
            except OSError as exc:
                msg = f"바로가기 제거 실패: {p} ({exc})"
                log.warning(msg)
                result.skipped.append(msg)

    # Move deepest paths first.  Directory-bundle moves and file moves both use
    # shutil.move; if the original path is occupied, restore beside it with a
    # conflict suffix rather than overwriting user data.
    for row in sorted(rows, key=lambda r: len(Path(r.new_path).parts), reverse=True):
        src = Path(row.new_path)
        dst = Path(row.original_path)
        try:
            if src.resolve() == dst.resolve():
                continue
        except OSError:
            pass
        if not src.exists():
            result.skipped.append(f"현재 위치 없음: {src}")
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            final_dst = _unique_restore_path(dst)
            shutil.move(str(src), str(final_dst))
            result.moved += 1
        except OSError as exc:
            msg = f"복구 실패: {src} → {dst} ({exc})"
            log.warning(msg)
            result.skipped.append(msg)

    # Remove now-empty category folders, deepest-first, but never remove roots
    # outside paths touched by the operation.
    parents = {Path(r.new_path).parent for r in rows}
    for p in sorted(parents, key=lambda x: len(x.parts), reverse=True):
        try:
            if p.exists() and p.is_dir() and not any(p.iterdir()):
                p.rmdir()
        except OSError:
            pass

    # The search index rows now point to stale paths; delete the operation after
    # filesystem rollback so history/search no longer expose the undone state.
    try:
        index_db.delete_operation(op_id)
    except Exception as exc:
        msg = f"롤백 후 인덱스 정리 실패: operation {op_id} ({exc})"
        log.warning(msg)
        result.skipped.append(msg)
    return result
