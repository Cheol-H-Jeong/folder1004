"""High-level orchestration used by both CLI and UI.

This module pulls the scanner/parser/planner/organizer together so that
callers don't need to know about individual stages.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

import concurrent.futures as _futures
import os
import sys

from .config import (
    Config,
    ORGANIZE_MODE_BUNDLE_REBUILD,
    ORGANIZE_MODE_FULL_REBUILD,
    ORGANIZE_MODE_PRESERVE_EXISTING,
    ORGANIZE_MODE_PRESERVE_FOLDER1004,
    default_paths,
    get_api_key,
    normalize_organize_mode,
)
from .index import IndexDB
from .llm import make_llm_client
from .metadata import collect
from .models import FileEntry, LLMUsage, OperationResult, Plan
from .folder_profile import analyze_folder_profile
from .organizer import Organizer
from .parser_cache import ParserCache
from .parsers import extract_excerpt
from .planner import Planner
from .reporter import emit_markdown
from .scanner import scan

log = logging.getLogger(__name__)

ProgressCB = Callable[[str, float], None]


def _env_int(name: str, default: int, *, min_value: int = 1) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        log.warning("invalid %s=%r; using %d", name, raw, default)
        return default
    return max(min_value, value)


def _gather_worker_count(path_count: int) -> int:
    """Bound the outer metadata/body-parse fanout.

    Body extraction itself has a bounded parser pool.  Keeping the outer
    pool modest prevents Windows installer builds from having many worker
    threads simultaneously holding PDF/Office objects while waiting for
    parser timeouts.  ``FOLDER1004_GATHER_WORKERS`` is intentionally an
    environment diagnostic knob, not a persisted UI setting.
    """
    if path_count <= 0:
        return 1
    default_cap = 2 if sys.platform.startswith("win") else 4
    cap = _env_int("FOLDER1004_GATHER_WORKERS", default_cap, min_value=1)
    return max(1, min(path_count, cap))


def gather_entries(
    root: Path,
    config: Config,
    recursive: bool,
    progress: Optional[ProgressCB] = None,
    cancel_check=None,
) -> list[FileEntry]:
    if progress:
        progress("scan: 폴더 검사 시작", 0.0)
    paths = scan(
        root,
        recursive=recursive,
        ignore_patterns=config.ignore_patterns if not config.include_hidden else [],
        max_files=config.max_files,
    )
    if progress:
        progress(f"scan: {len(paths)}개 파일 발견", 0.05)
    # Persistent excerpt cache so unchanged files skip parsing on
    # subsequent runs.  Keyed by (path, mtime, size).
    cache = ParserCache(default_paths().root / "parser_cache.db")

    # Parallel metadata/excerpt fanout.  The actual parser dispatcher has
    # its own small, bounded pool; do not multiply concurrency here.
    workers = _gather_worker_count(len(paths))
    log.info("gather_entries: %d files, %d outer worker(s)", len(paths), workers)

    def _parse_one(idx_p: tuple[int, "Path"]) -> Optional[FileEntry]:
        idx, p = idx_p
        if cancel_check is not None and cancel_check():
            raise RuntimeError("canceled by user")
        if progress:
            progress(f"parse [{idx}/{len(paths)}] {p.name}", idx / max(1, len(paths)))
        try:
            entry = collect(p)
        except Exception as exc:
            log.warning("metadata failed %s: %s", p, exc)
            if progress:
                progress(
                    f"  ⚠ 메타데이터 실패: {p.name} ({exc})",
                    idx / max(1, len(paths)),
                )
            return None
        try:
            entry.content_excerpt = cache.get_or_parse(
                entry.path, entry.modified.timestamp(), entry.size,
                lambda: extract_excerpt(
                    entry.path,
                    max_chars=config.max_excerpt_chars,
                    timeout=config.parse_timeout_s,
                ),
            )
        except Exception as exc:
            log.debug("cache lookup failed for %s: %s", p, exc)
            entry.content_excerpt = extract_excerpt(
                entry.path,
                max_chars=config.max_excerpt_chars,
                timeout=config.parse_timeout_s,
            )
        return entry

    entries: list[FileEntry] = []
    try:
        with _futures.ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="folder1004-pipeline"
        ) as pool:
            for entry in pool.map(_parse_one, enumerate(paths, 1), chunksize=4):
                if entry is not None:
                    entries.append(entry)
    finally:
        cache.close()
    return entries


def _top_level_child(root: Path, path: Path) -> Optional[Path]:
    """Return the direct child of *root* containing *path*, if any."""
    try:
        rel = Path(path).resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    if len(rel.parts) <= 1:
        return None
    return root / rel.parts[0]


def _folder_bundle_entry(folder: Path, members: list[FileEntry]) -> FileEntry:
    """Represent a top-level folder as one classification item.

    Bundle modes must not scatter files already grouped inside a folder.  The
    planner still needs enough signal to pick a destination category, so this
    pseudo entry summarizes the folder name, representative child paths, and a
    compact excerpt sample while its ``path`` points at the directory to move.
    """
    from datetime import datetime, timezone

    now = datetime.now(tz=timezone.utc).astimezone()
    try:
        st = folder.stat()
        created = modified = accessed = now
        try:
            created = collect(folder).created
        except Exception:
            pass
        modified = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).astimezone()
        accessed = datetime.fromtimestamp(st.st_atime, tz=timezone.utc).astimezone()
    except OSError:
        created = modified = accessed = now
    if members:
        size = sum(max(0, int(m.size or 0)) for m in members)
        modified = max((m.modified for m in members), default=modified)
        accessed = max((m.accessed for m in members), default=accessed)
    else:
        size = 0

    samples: list[str] = []
    for m in sorted(members, key=lambda e: str(e.path))[:24]:
        try:
            rel = m.path.relative_to(folder)
        except ValueError:
            rel = Path(m.name)
        line = str(rel)
        excerpt = (m.content_excerpt or "").strip().replace("\n", " ")
        if excerpt:
            line += f" — {excerpt[:140]}"
        samples.append(line)
    excerpt_body = "\n".join(samples)
    content_excerpt = (
        f"[폴더 묶음] 이 항목은 해체하지 말고 폴더째 이동해야 합니다.\n"
        f"기존 폴더명: {folder.name}\n"
        f"하위 파일 수: {len(members)}\n"
        f"대표 파일:\n{excerpt_body}"
    ).strip()
    return FileEntry(
        path=folder,
        name=folder.name,
        ext="[folder]",
        size=size,
        created=created,
        modified=modified,
        accessed=accessed,
        mime="inode/directory",
        content_excerpt=content_excerpt,
    )


def _entries_with_top_level_bundles(
    target_root: Path,
    entries: list[FileEntry],
    *,
    skip_folder1004_folders: bool = False,
) -> tuple[list[FileEntry], int, int]:
    """Collapse files under each direct child folder into one folder entry.

    Returns ``(entries_for_planner, bundle_count, skipped_file_count)``.  Root
    files remain file entries.  Files inside skipped Folder1004 folders are not
    sent to the planner because those folders are preserved as already sorted.
    """
    from .organizer import is_folder1004_folder_name

    root_entries: list[FileEntry] = []
    by_folder: dict[Path, list[FileEntry]] = {}
    skipped = 0
    for e in entries:
        top = _top_level_child(target_root, e.path)
        if top is None:
            root_entries.append(e)
            continue
        if skip_folder1004_folders and is_folder1004_folder_name(top.name):
            skipped += 1
            continue
        by_folder.setdefault(top, []).append(e)
    bundles = [_folder_bundle_entry(folder, members) for folder, members in sorted(by_folder.items(), key=lambda kv: kv[0].name)]
    return root_entries + bundles, len(bundles), skipped


def _root_file_entries_only(target_root: Path, entries: list[FileEntry]) -> tuple[list[FileEntry], int]:
    kept: list[FileEntry] = []
    skipped = 0
    for e in entries:
        if _top_level_child(target_root, e.path) is None:
            kept.append(e)
        else:
            skipped += 1
    return kept, skipped


def run(
    target_root: Path,
    config: Config,
    recursive: bool,
    dry_run: bool,
    index_db: Optional[IndexDB] = None,
    progress: Optional[ProgressCB] = None,
    force_mock: bool = False,
    cancel_check=None,
) -> OperationResult:
    target_root = Path(target_root)

    def _check():
        if cancel_check is not None and cancel_check():
            raise RuntimeError("canceled by user")

    _check()
    mode = normalize_organize_mode(getattr(config, "organize_mode", ""))
    config.organize_mode = mode

    # The folder-handling modes define whether subfolders are inspected.  The
    # safe default modes need recursive metadata so top-level folders can be
    # summarized as intact bundles; the only mode that truly dissolves folders
    # is full_rebuild.
    scan_recursive = recursive or mode in {
        ORGANIZE_MODE_BUNDLE_REBUILD,
        ORGANIZE_MODE_PRESERVE_FOLDER1004,
        ORGANIZE_MODE_FULL_REBUILD,
    }
    entries = gather_entries(target_root, config, scan_recursive, progress, cancel_check)
    _check()
    folder_profile = analyze_folder_profile(target_root, entries, recursive=scan_recursive)
    if progress:
        progress(
            f"profile: {folder_profile.label} · 건강 점수 {folder_profile.health_score}/100 "
            f"({folder_profile.health_level})",
            0.055,
        )

    # ------------------------------------------------------------------
    # Mode resolution.
    # ------------------------------------------------------------------
    # Only the explicit danger mode hides parent paths and classifies every
    # file independently.  The three safer modes preserve existing folder
    # interiors and therefore expose folder names as normal classification
    # signals.
    config.reclassify_mode = mode == ORGANIZE_MODE_FULL_REBUILD

    seed_categories: list[dict] = []
    if mode == ORGANIZE_MODE_BUNDLE_REBUILD:
        entries, bundle_count, _skipped = _entries_with_top_level_bundles(target_root, entries)
        if progress:
            progress(
                f"plan: 새 폴더 체계로 정리 — 기존 하위 폴더 {bundle_count}개를 해체하지 않고 묶음으로 분류",
                0.06,
            )
    elif mode == ORGANIZE_MODE_PRESERVE_EXISTING:
        # 기존 폴더 체계 유지 — 기존 최상위 폴더 전체를 카테고리로 활용하고,
        # 루트에 흩어진 파일만 기존/신규 폴더로 보낸다.
        seed_categories = _seed_categories_from_disk(target_root, fa_only=False)
        entries, skipped_nested = _root_file_entries_only(target_root, entries)
        if progress:
            progress(
                f"plan: 기존 폴더 체계 유지 — 기존 폴더 {len(seed_categories)}개 활용 / "
                f"하위 폴더 내부 파일 {skipped_nested}개 보존 / 새 분류 대상 {len(entries)}개",
                0.06,
            )
    elif mode == ORGANIZE_MODE_PRESERVE_FOLDER1004:
        # Folder1004 폴더만 유지 — signed folders are kept untouched;
        # unsigned folders are moved as intact bundles, not dissolved.
        seed_categories = _seed_categories_from_disk(target_root, fa_only=True)
        entries, bundle_count, skipped_in_folder1004 = _entries_with_top_level_bundles(
            target_root, entries, skip_folder1004_folders=True
        )
        if progress:
            progress(
                f"plan: Folder1004 폴더만 유지 — 기존 Folder1004 폴더 {len(seed_categories)}개 / "
                f"이미 분류된 파일 {skipped_in_folder1004}개 건너뜀 / "
                f"일반 하위 폴더 {bundle_count}개는 묶음으로 분류 / 새 분류 대상 {len(entries)}개",
                0.06,
            )
    elif mode == ORGANIZE_MODE_FULL_REBUILD:
        if progress:
            progress(
                "plan: 모든 폴더 해체 후 재정리 — 기존 폴더명은 참고 힌트로만 사용",
                0.06,
            )

    # ------------------------------------------------------------------
    # Duplicate detection: skip the LLM round-trip for non-canonical
    # copies of the same file and queue them for deletion after the
    # canonical is placed.
    # ------------------------------------------------------------------
    dedup_groups = []
    canonical_only = entries
    min_bytes = int(getattr(config, "dedup_min_bytes", 1_048_576) or 0)
    if dry_run:
        if progress:
            progress("dedup: Dry-Run 모드 — 중복 검사 건너뜀", 0.07)
    elif min_bytes < 0:
        if progress:
            progress("dedup: 비활성 (dedup_min_bytes < 0)", 0.07)
    else:
        from . import dedup as _dedup
        if progress:
            mb_thr = min_bytes / (1 << 20)
            progress(
                f"dedup: 중복 검사 시작 — 임계값 {mb_thr:.1f} MB / "
                f"{len(entries)} 파일 검사",
                0.06,
            )
        dedup_groups = _dedup.find_duplicate_groups(entries, min_bytes=min_bytes)
        if dedup_groups:
            n_dupes = sum(len(g.duplicates) for g in dedup_groups)
            mb_save = sum(g.total_bytes_freed for g in dedup_groups) / (1 << 20)
            if progress:
                progress(
                    f"dedup: 중복 그룹 {len(dedup_groups)}개 / "
                    f"삭제 예정 {n_dupes}개 / ≈ {mb_save:.1f} MB 회수 예정",
                    0.07,
                )
            duplicate_paths = {
                str(d.path) for g in dedup_groups for d in g.duplicates
            }
            canonical_only = [
                e for e in entries if str(e.path) not in duplicate_paths
            ]
        else:
            if progress:
                progress(
                    f"dedup: 임계값 {min_bytes / (1 << 20):.1f} MB 이상 "
                    f"중복 파일 없음",
                    0.07,
                )

    client = None
    key = None
    if not force_mock:
        key = get_api_key(config, provider=config.llm_provider)
        # Try to build the client even when no key — make_llm_client
        # accepts local URLs (Ollama / vLLM / LM Studio) without auth.
        try:
            client = make_llm_client(config, key)
        except Exception as exc:
            log.warning("llm init failed: %s", exc)
            client = None

    if progress:
        from .config import provider_label
        if client is not None:
            key_state = (
                "키 등록됨" if key else "키 없음(로컬 LLM)"
            )
            progress(
                f"plan: {provider_label(config)} ({config.model}) — "
                f"{key_state} / {config.llm_base_url or '(기본 endpoint)'}",
                0.0,
            )
        else:
            # Tell the user *why* we fell to mock — usually means no
            # key for the provider they just switched to.
            reason = (
                "API 키가 등록되지 않음 — 설정에서 현재 provider 의 키를 등록하세요"
                if not key else "LLM 클라이언트 초기화 실패 — 로그 확인"
            )
            progress(
                f"plan: Mock 휴리스틱 모드 ({provider_label(config)} {reason})",
                0.0,
            )
    planner = Planner(
        config, gemini=client, cancel_check=cancel_check,
        seed_categories=seed_categories,
    )
    plan: Plan = planner.plan(canonical_only, progress=progress)
    _check()

    # Add the duplicates back to the plan, each pointing at the same
    # category as its canonical.  The organizer will skip the actual
    # file move (we'll delete them after) but the report shows them.
    if dedup_groups:
        canon_cat: dict[str, str] = {
            str(a.file_path): a.primary_category_id for a in plan.assignments
        }
        from .models import Assignment
        for g in dedup_groups:
            cid = canon_cat.get(str(g.canonical.path))
            if not cid:
                continue
            for d in g.duplicates:
                plan.assignments.append(Assignment(
                    file_path=d.path,
                    primary_category_id=cid,
                    primary_score=1.0,
                    secondary=[],
                    reason=f"중복 — 정본: {Path(g.canonical.path).name}",
                ))

    if progress:
        progress(f"plan: 카테고리 {len(plan.categories)}개 결정됨", 0.95)
        progress(f"organize: 파일 이동 시작 ({len(plan.assignments)}개)", 0.0)
    organizer = Organizer(config)
    excerpts_map = {str(e.path): (e.content_excerpt or "") for e in entries}
    duplicate_paths = (
        {str(d.path) for g in dedup_groups for d in g.duplicates}
        if dedup_groups else set()
    )
    op = organizer.execute(
        target_root, plan, dry_run=dry_run, progress=progress,
        cancel_check=cancel_check, excerpts=excerpts_map,
        skip_paths=duplicate_paths,
    )

    # ------------------------------------------------------------------
    # After the canonical files are in their final folders, delete the
    # duplicates that no longer earn their disk space.
    # ------------------------------------------------------------------
    if dedup_groups and not dry_run:
        from . import dedup as _dedup
        actions = _dedup.remove_duplicate_files(dedup_groups, dry_run=False)
        op.dupes_removed = [(str(d), str(c), b) for d, c, b in actions]
        op.bytes_freed = sum(b for _d, _c, b in actions)
        if progress:
            mb = op.bytes_freed / (1 << 20)
            progress(
                f"dedup: 중복 파일 {len(actions)}개 삭제 완료 — {mb:.1f} MB 회수",
                0.98,
            )

    if client is not None:
        op.llm_usage = LLMUsage(
            request_count=client.request_count,
            prompt_chars=client.prompt_chars,
            response_chars=client.response_chars,
            model=config.model,
            total_duration_s=getattr(client, "total_duration_s", 0.0),
            calls=list(getattr(client, "calls", [])),
        )
    else:
        op.llm_usage = LLMUsage(model="mock")

    op.folder_profile = folder_profile

    # Write the markdown report FIRST so its path is available to
    # ``record_operation`` for storage in stats_json — that lets the
    # History tab open the report on double-click without globbing.
    try:
        op.report_path = emit_markdown(op)
    except Exception as exc:
        log.warning("report failed: %s", exc)

    if index_db is not None and not dry_run:
        try:
            index_db.record_operation(op)
        except Exception as exc:
            log.warning("index record failed: %s", exc)

    return op


def apply_plan(
    target_root: Path,
    config: Config,
    plan: Plan,
    index_db: Optional[IndexDB] = None,
    progress: Optional[ProgressCB] = None,
    cancel_check=None,
) -> OperationResult:
    """Apply a user-confirmed preview plan without re-running analysis/LLM."""
    target_root = Path(target_root)
    mode = normalize_organize_mode(getattr(config, "organize_mode", ""))
    config.organize_mode = mode
    config.reclassify_mode = mode == ORGANIZE_MODE_FULL_REBUILD
    if progress:
        progress(f"organize: 미리보기 확정안 적용 시작 ({len(plan.assignments)}개)", 0.0)
    organizer = Organizer(config)
    op = organizer.execute(
        target_root,
        plan,
        dry_run=False,
        progress=progress,
        cancel_check=cancel_check,
    )
    op.llm_usage = LLMUsage(model="preview-confirmed")
    try:
        op.report_path = emit_markdown(op)
    except Exception as exc:
        log.warning("report failed: %s", exc)
    if index_db is not None:
        try:
            index_db.record_operation(op)
        except Exception as exc:
            log.warning("index record failed: %s", exc)
    return op


def _seed_categories_from_disk(
    target_root: Path, *, fa_only: bool = False,
) -> list[dict]:
    """Build a seed catalogue from the existing top-level folders of
    ``target_root``.

    ``fa_only=False`` (기존 폴더 유지하기): every readable sub-folder becomes a
    seed category — convenient when the user has manually curated the
    layout and only wants the LLM to place new files into existing
    bins.

    ``fa_only=True`` (Folder1004로 이미 생성한 폴더만 유지하기): only folders whose name carries the
    ``[Folder1004·xxxxxx]`` signature added by :func:`folder_signature`
    are used.  Legacy ``[FA·xxxxxx]`` tags are also accepted. Anything
    the user (or another tool) made by hand is left out of the catalogue
    and its contents will be re-evaluated as loose files.
    """
    import re
    from .organizer import (
        is_folder1004_folder_name,
        parse_fa_folder_name,
    )
    if not target_root.is_dir():
        return []
    seeds: list[dict] = []
    for entry in sorted(target_root.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith(".") or entry.name.startswith("__"):
            continue
        raw = entry.name
        if fa_only and not is_folder1004_folder_name(raw):
            continue
        parsed = parse_fa_folder_name(raw)
        if parsed:
            core = parsed["clean_name"] or raw
            time_label = parsed["period"]
            sig = parsed["signature"]
        else:
            # Strip "1. " / "2-" / "3) " sort prefixes.
            core = re.sub(r"^\s*\d+[\.\-_)\]\s]+", "", raw).strip()
            m = re.search(r"[〈(]([^〉)]{1,30})[〉)]\s*$", core)
            time_label = m.group(1).strip() if m else ""
            if m:
                core = core[:m.start()].strip()
            sig = ""
        slug = re.sub(r"[^A-Za-z0-9가-힣]+", "-", core).strip("-").lower()[:40]
        if not slug:
            slug = f"existing-{len(seeds)+1}"
        # If we recovered a Folder1004 signature, prefer it as the slug suffix
        # so a future signature() call regenerates the same tag and
        # the folder is reused on disk instead of being created anew.
        if sig:
            slug = f"{slug}-{sig}"
        seeds.append({
            "id": slug,
            "name": core or raw,
            "description": f"기존 폴더: {raw}",
            "time_label": time_label,
            "duration": "mixed",
            "group": (len(seeds) % 8) + 1,
            "_existing_folder": str(entry),
        })
    return seeds
