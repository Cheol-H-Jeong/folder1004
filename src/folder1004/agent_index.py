"""Non-destructive Folder1004 agent/human metadata index builder.

The builder keeps the user's folders and filenames untouched.  It writes a
`.folder1004/` sidecar workspace containing JSONL/Markdown indexes and, for
parser-required document formats, reusable plain-text caches so CLI agents can
use `rg`/`jq` without reparsing Office/PDF/HWP files on every search.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from .config import Config
from .metadata import collect
from .models import FileEntry
from .parsers import extract_excerpt
from .parsers.registry import SUPPORTED_EXTENSIONS
from .scanner import ScanTooLargeError

ProgressCB = Callable[[str, float], None]

AGENT_DIR_NAME = ".folder1004"
DOCUMENT_CACHE_EXTENSIONS = {
    ".pdf",
    ".doc",
    ".docx",
    ".ppt",
    ".pptx",
    ".pps",
    ".ppsx",
    ".xls",
    ".xlsx",
    ".hwp",
    ".hwpx",
    ".odt",
    ".rtf",
}
TEXT_LIKE_EXTENSIONS = SUPPORTED_EXTENSIONS - DOCUMENT_CACHE_EXTENSIONS
_DEFAULT_TEXT_MAX_CHARS = 2_000_000
_DEFAULT_CHUNK_CHARS = 1_000_000


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def _rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _json_dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), delete=False
    ) as fh:
        fh.write(text)
        tmp = Path(fh.name)
    tmp.replace(path)


def _write_json(path: Path, obj) -> None:
    _atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    _atomic_write_text(path, "".join(_json_dump(row) + "\n" for row in rows))


def _remove_path_quietly(path: Path) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    except OSError:
        return


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _fingerprint(path: Path) -> dict:
    st = path.stat()
    return {
        "size": int(st.st_size),
        "mtime_ns": int(st.st_mtime_ns),
        "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).astimezone().isoformat(timespec="seconds"),
    }


def _load_jsonl_by_path(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    out: dict[str, dict] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            p = str(row.get("path") or "")
            if p:
                out[p] = row
    except Exception:
        return {}
    return out


def _ignore_name(name: str, patterns: Iterable[str]) -> bool:
    import fnmatch

    if name == AGENT_DIR_NAME:
        return True
    if name == "000_FOLDER1004_AGENT_MAP.md" or name.startswith("Folder1004_Report_"):
        return True
    return any(fnmatch.fnmatch(name, pat) for pat in patterns)


def _read_cached_doc_text(path: Path) -> str | None:
    """Return cached parser text from a file or chunk directory if it exists."""
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="ignore")
        if path.is_dir():
            parts: list[str] = []
            total = 0
            for chunk in sorted(path.glob("chunk_*.txt")):
                text = chunk.read_text(encoding="utf-8", errors="ignore")
                parts.append(text)
                total += len(text)
                if total >= 16_000:
                    break
            return "\n".join(parts)
    except OSError:
        return None
    return None


def _scan_files_and_dirs(root: Path, config: Config) -> tuple[list[Path], list[Path]]:
    root = Path(root).expanduser().resolve()
    ignore = [] if getattr(config, "include_hidden", False) else list(config.ignore_patterns)
    dirs: list[Path] = []
    files: list[Path] = []
    limit = int(getattr(config, "max_files", 5000) or 5000)

    def walk(cur: Path) -> None:
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    name = entry.name
                    if _ignore_name(name, ignore):
                        continue
                    try:
                        if entry.is_symlink():
                            continue
                        p = Path(entry.path)
                        if entry.is_dir(follow_symlinks=False):
                            dirs.append(p)
                            walk(p)
                        elif entry.is_file(follow_symlinks=False):
                            files.append(p)
                            if len(files) > limit:
                                raise ScanTooLargeError(len(files), limit)
                    except PermissionError:
                        continue
        except PermissionError:
            return

    walk(root)
    return sorted(files, key=lambda p: str(p)), sorted(dirs, key=lambda p: str(p))


def _tokens_from_text(text: str, *, limit: int = 16) -> list[str]:
    tokens = []
    seen = set()
    for raw in re.findall(r"[0-9A-Za-z가-힣][0-9A-Za-z가-힣._-]*", text or ""):
        tok = raw.strip("._-")
        if len(tok) < 2:
            continue
        norm = tok.casefold()
        if norm in seen or norm in {"folder1004", "file", "data", "문서", "파일", "자료"}:
            continue
        seen.add(norm)
        tokens.append(tok)
        if len(tokens) >= limit:
            break
    return tokens


@dataclass
class AgentIndexResult:
    files: int = 0
    folders: int = 0
    docs_parsed: int = 0
    docs_reused: int = 0
    docs_failed: int = 0
    deleted: int = 0
    index_dir: Path | None = None
    agent_map: Path | None = None


def build_agent_index(
    target_root: Path,
    config: Config,
    *,
    progress: Optional[ProgressCB] = None,
    cancel_check=None,
) -> AgentIndexResult:
    """Create/update `.folder1004` metadata artifacts without moving files."""
    root = Path(target_root).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(root)

    def check() -> None:
        if cancel_check is not None and cancel_check():
            raise RuntimeError("canceled by user")

    index_dir = root / AGENT_DIR_NAME
    doc_text_dir = index_dir / "doc_text"
    doc_meta_dir = index_dir / "doc_meta"
    folder_cards_dir = index_dir / "folder_cards"
    file_cards_dir = index_dir / "file_cards"
    for d in (doc_text_dir, doc_meta_dir, folder_cards_dir, file_cards_dir):
        d.mkdir(parents=True, exist_ok=True)

    if progress:
        progress("agent-index: 파일/폴더 변경분 스캔", 0.02)
    files, dirs = _scan_files_and_dirs(root, config)
    check()

    previous = _load_jsonl_by_path(index_dir / "file_index.jsonl")
    prev_paths = set(previous)
    current_paths = {_rel(root, p) for p in files}
    deleted_rows = [previous[p] | {"status": "deleted", "deleted_at": _now_iso()} for p in sorted(prev_paths - current_paths)]

    folder_children: dict[str, list[str]] = defaultdict(list)
    for d in dirs:
        parent = _rel(root, d.parent) if d.parent != root else "."
        folder_children[parent].append(d.name)

    folder_file_counts: Counter[str] = Counter()
    folder_size_counts: Counter[str] = Counter()
    folder_ext_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for f in files:
        parent_rel = _rel(root, f.parent) if f.parent != root else "."
        try:
            size = f.stat().st_size
        except OSError:
            size = 0
        folder_file_counts[parent_rel] += 1
        folder_size_counts[parent_rel] += size
        folder_ext_counts[parent_rel][f.suffix.lower() or "[noext]"] += 1

    file_rows: list[dict] = []
    doc_rows: list[dict] = []
    parse_errors: list[dict] = []
    result = AgentIndexResult(files=len(files), folders=len(dirs), deleted=len(deleted_rows), index_dir=index_dir)
    text_max = int(getattr(config, "agent_doc_text_max_chars", _DEFAULT_TEXT_MAX_CHARS) or _DEFAULT_TEXT_MAX_CHARS)
    chunk_chars = int(getattr(config, "agent_doc_text_chunk_chars", _DEFAULT_CHUNK_CHARS) or _DEFAULT_CHUNK_CHARS)
    parse_timeout = float(getattr(config, "parse_timeout_s", 5.0) or 5.0)

    for idx, path in enumerate(files, 1):
        check()
        rel_path = _rel(root, path)
        if progress and (idx == 1 or idx % 50 == 0 or idx == len(files)):
            progress(f"agent-index: 파일 메타 갱신 {idx}/{len(files)}", 0.05 + 0.55 * (idx / max(1, len(files))))
        try:
            entry: FileEntry = collect(path)
            fp = _fingerprint(path)
        except OSError as exc:
            parse_errors.append({"path": rel_path, "error": str(exc), "stage": "metadata"})
            continue

        prev = previous.get(rel_path) or {}
        unchanged = (
            prev.get("size") == fp["size"]
            and prev.get("mtime_ns") == fp["mtime_ns"]
            and prev.get("status", "present") == "present"
        )
        ext = entry.ext.lower()
        parser_required = ext in DOCUMENT_CACHE_EXTENSIONS
        text_cache_path = prev.get("text_cache_path", "") if unchanged else ""
        doc_meta_path = prev.get("doc_meta_path", "") if unchanged else ""
        content_hash = prev.get("sha256", "") if unchanged else ""
        parse_status = prev.get("parse_status", "not_required") if unchanged else "not_required"
        summary = prev.get("summary", "") if unchanged else ""
        keywords = list(prev.get("keywords", []) or []) if unchanged else []

        if parser_required:
            parsed_text_sample = ""
            if unchanged and text_cache_path and (root / text_cache_path).exists():
                result.docs_reused += 1
            else:
                try:
                    content_hash = _sha256(path)
                    text_cache_path = f"{AGENT_DIR_NAME}/doc_text/sha256_{content_hash}.txt"
                    doc_meta_path = f"{AGENT_DIR_NAME}/doc_meta/sha256_{content_hash}.json"
                    text_path = root / text_cache_path
                    cached_text = _read_cached_doc_text(text_path)
                    if cached_text is not None:
                        parsed_text_sample = cached_text
                        parse_status = "success" if cached_text.strip() else "empty"
                        result.docs_reused += 1
                    else:
                        parsed_text = extract_excerpt(path, max_chars=text_max, timeout=parse_timeout)
                        parsed_text_sample = parsed_text
                        if parsed_text.strip():
                            header = (
                                f"# Source: {rel_path}\n"
                                f"# File ID: sha256:{content_hash}\n"
                                f"# Parsed by: Folder1004\n\n"
                            )
                            if len(parsed_text) > chunk_chars:
                                chunk_dir = root / f"{AGENT_DIR_NAME}/doc_text/sha256_{content_hash}"
                                if chunk_dir.exists() and chunk_dir.is_file():
                                    chunk_dir.unlink()
                                chunk_dir.mkdir(parents=True, exist_ok=True)
                                for old in chunk_dir.glob("chunk_*.txt"):
                                    old.unlink()
                                for n, start in enumerate(range(0, len(parsed_text), chunk_chars), 1):
                                    chunk = parsed_text[start:start + chunk_chars]
                                    _atomic_write_text(chunk_dir / f"chunk_{n:04d}.txt", header + chunk)
                                text_cache_path = f"{AGENT_DIR_NAME}/doc_text/sha256_{content_hash}"
                            else:
                                _atomic_write_text(text_path, header + parsed_text)
                            parse_status = "success"
                            result.docs_parsed += 1
                        else:
                            parse_status = "empty"
                            result.docs_failed += 1
                    if parsed_text_sample:
                        keywords = _tokens_from_text(f"{entry.name}\n{parsed_text_sample[:4000]}")
                        summary = parsed_text_sample.strip().replace("\n", " ")[:240]
                except Exception as exc:  # parser/hash failure should not abort indexing
                    parse_status = "failed"
                    result.docs_failed += 1
                    parse_errors.append({"path": rel_path, "error": str(exc), "stage": "parse"})
                    if not content_hash:
                        content_hash = ""
        elif ext in TEXT_LIKE_EXTENSIONS:
            keywords = _tokens_from_text(entry.name)
            parse_status = "native_text"
        else:
            keywords = _tokens_from_text(entry.name)
            parse_status = "not_required"

        if not content_hash and not unchanged:
            # Hash every new/changed file so moves can be correlated later.
            try:
                content_hash = _sha256(path)
            except OSError:
                content_hash = ""

        row = {
            "status": "present",
            "path": rel_path,
            "name": entry.name,
            "ext": ext,
            "mime": entry.mime,
            "size": fp["size"],
            "mtime": fp["mtime"],
            "mtime_ns": fp["mtime_ns"],
            "created": entry.created.isoformat(timespec="seconds"),
            "modified": entry.modified.isoformat(timespec="seconds"),
            "sha256": content_hash,
            "parser_required": parser_required,
            "parse_status": parse_status,
            "text_cache_path": text_cache_path,
            "doc_meta_path": doc_meta_path,
            "keywords": keywords,
            "summary": summary,
            "indexed_at": _now_iso(),
        }
        file_rows.append(row)
        if parser_required:
            doc_row = row | {"file_id": f"sha256:{content_hash}" if content_hash else ""}
            doc_rows.append(doc_row)
            if doc_meta_path:
                _write_json(root / doc_meta_path, doc_row)
                _atomic_write_text(
                    file_cards_dir / f"sha256_{content_hash}.md",
                    _file_card_markdown(doc_row),
                )

    # Keep deleted rows visible for agents/history, but current indexes list present first.
    file_rows_sorted = sorted(file_rows, key=lambda r: r["path"])
    folder_rows = _build_folder_rows(root, dirs, folder_file_counts, folder_size_counts, folder_ext_counts, folder_children)
    doc_rows_sorted = sorted(doc_rows, key=lambda r: r["path"])
    current_doc_hashes = {row["sha256"] for row in doc_rows_sorted if row.get("sha256")}
    _prune_stale_doc_artifacts(doc_text_dir, doc_meta_dir, file_cards_dir, current_doc_hashes)

    if progress:
        progress("agent-index: 인덱스/탐색 지도 작성", 0.82)
    _write_jsonl(index_dir / "file_index.jsonl", file_rows_sorted + deleted_rows)
    _write_jsonl(index_dir / "folder_index.jsonl", folder_rows)
    _write_jsonl(index_dir / "document_index.jsonl", doc_rows_sorted)
    _write_jsonl(index_dir / "parse_errors.jsonl", parse_errors)
    for row in folder_rows:
        fid = row["folder_id"]
        _atomic_write_text(folder_cards_dir / f"{fid}.md", _folder_card_markdown(row))

    search_terms = _build_search_terms(folder_rows, file_rows_sorted, doc_rows_sorted)
    _write_json(index_dir / "search_terms.json", search_terms)
    manifest = {
        "schema_version": 1,
        "generated_at": _now_iso(),
        "root": str(root),
        "files": len(file_rows_sorted),
        "folders": len(folder_rows),
        "documents": len(doc_rows_sorted),
        "docs_parsed": result.docs_parsed,
        "docs_reused": result.docs_reused,
        "docs_failed": result.docs_failed,
        "deleted_since_last_index": len(deleted_rows),
        "mode": "metadata_only",
        "non_destructive": True,
    }
    _write_json(index_dir / "manifest.json", manifest)
    _atomic_write_text(index_dir / "agent_routes.md", _agent_routes_markdown(manifest))
    _atomic_write_text(index_dir / "agent_skill_context.md", _agent_skill_context_markdown(manifest))
    _atomic_write_text(index_dir / "human_guide.md", _human_guide_markdown(manifest))
    agent_map = index_dir / "agent_map.md"
    _atomic_write_text(agent_map, _agent_map_markdown(root, manifest, folder_rows, doc_rows_sorted, search_terms))
    result.agent_map = agent_map

    # Backwards/convenience visible root map. Keep short and overwrite safely.
    _atomic_write_text(root / "000_FOLDER1004_AGENT_MAP.md", _visible_agent_map(agent_map))
    if progress:
        progress(
            f"agent-index: 완료 — 파일 {result.files}개 / 폴더 {result.folders}개 / 문서 캐시 신규 {result.docs_parsed}개·재사용 {result.docs_reused}개",
            0.98,
        )
    return result


def _prune_stale_doc_artifacts(
    doc_text_dir: Path,
    doc_meta_dir: Path,
    file_cards_dir: Path,
    live_hashes: set[str],
) -> None:
    """Remove generated document caches no longer referenced by current docs."""
    live_names = {f"sha256_{h}" for h in live_hashes}
    for child in doc_text_dir.glob("sha256_*"):
        cache_key = child.name if child.is_dir() else child.stem
        if cache_key not in live_names:
            _remove_path_quietly(child)
    for child in doc_meta_dir.glob("sha256_*.json"):
        stem = child.stem
        if stem not in live_names:
            _remove_path_quietly(child)
    for child in file_cards_dir.glob("sha256_*.md"):
        stem = child.stem
        if stem not in live_names:
            _remove_path_quietly(child)


def _folder_id(path_rel: str) -> str:
    return "folder_" + hashlib.sha1(path_rel.encode("utf-8", "ignore")).hexdigest()[:12]


def _build_folder_rows(root, dirs, file_counts, size_counts, ext_counts, children) -> list[dict]:
    rows = []
    all_dirs = [root] + list(dirs)
    for d in all_dirs:
        rel_path = "." if d == root else _rel(root, d)
        child_names = children.get(rel_path, [])[:80]
        exts = dict(ext_counts.get(rel_path, Counter()).most_common(12))
        text = " ".join([d.name, rel_path, " ".join(child_names), " ".join(exts)])
        rows.append({
            "folder_id": _folder_id(rel_path),
            "path": rel_path,
            "name": d.name if d != root else root.name,
            "depth": 0 if rel_path == "." else len(Path(rel_path).parts),
            "file_count_direct": int(file_counts.get(rel_path, 0)),
            "subfolder_count_direct": len(children.get(rel_path, [])),
            "total_size_direct": int(size_counts.get(rel_path, 0)),
            "top_extensions_direct": exts,
            "child_folders": child_names,
            "keywords": _tokens_from_text(text),
            "summary": f"{rel_path} — 파일 {file_counts.get(rel_path, 0)}개, 하위 폴더 {len(children.get(rel_path, []))}개",
        })
    return sorted(rows, key=lambda r: (r["depth"], r["path"]))


def _build_search_terms(folder_rows, file_rows, doc_rows) -> dict:
    terms: dict[str, dict] = {}
    for kind, rows in (("folder", folder_rows), ("file", file_rows), ("document", doc_rows)):
        for row in rows:
            candidates = set(row.get("keywords") or [])
            candidates.update(_tokens_from_text(row.get("path", ""), limit=12))
            for term in candidates:
                bucket = terms.setdefault(term, {"folders": [], "files": [], "documents": []})
                key = kind + "s"
                if row.get("path") not in bucket[key]:
                    bucket[key].append(row.get("path"))
                    bucket[key] = bucket[key][:40]
    return {k: terms[k] for k in sorted(terms)}


def _agent_map_markdown(root, manifest, folders, docs, terms) -> str:
    top_folders = [r for r in folders if r.get("depth") == 1][:40]
    top_docs = docs[:40]
    top_terms = list(terms.keys())[:80]
    lines = [
        "# Folder1004 Agent Map",
        "",
        "이 폴더는 Folder1004가 원본 구조를 변경하지 않고 에이전트/사람 탐색용 메타데이터를 생성한 상태입니다.",
        "",
        "## 빠른 사용법",
        "- 전체 폴더 메타: `.folder1004/folder_index.jsonl`",
        "- 전체 파일 메타: `.folder1004/file_index.jsonl`",
        "- 파싱 문서 본문 캐시: `.folder1004/doc_text/`",
        "- 문서 인덱스: `.folder1004/document_index.jsonl`",
        "- CLI 검색 예: `rg \"검색어\" .folder1004/doc_text .folder1004/*.jsonl`",
        "",
        "## 요약",
        f"- 루트: `{root}`",
        f"- 파일: {manifest['files']}개",
        f"- 폴더: {manifest['folders']}개",
        f"- 파서 필요 문서: {manifest['documents']}개",
        f"- 문서 캐시 신규/재사용/실패: {manifest['docs_parsed']} / {manifest['docs_reused']} / {manifest['docs_failed']}",
        "",
        "## 최상위 폴더",
    ]
    lines.extend(f"- `{r['path']}` — {r['summary']}" for r in top_folders)
    lines.extend(["", "## 문서 텍스트 캐시 후보"])
    lines.extend(f"- `{r['path']}` → `{r.get('text_cache_path') or '캐시 없음'}`" for r in top_docs)
    lines.extend(["", "## 주요 검색 토큰"])
    lines.append(", ".join(top_terms) if top_terms else "—")
    lines.append("")
    return "\n".join(lines)


def _visible_agent_map(agent_map: Path) -> str:
    return (
        "# Folder1004 Agent Map\n\n"
        "상세 탐색 지도와 파일/문서 인덱스는 아래 경로에 있습니다.\n\n"
        f"- `{agent_map.as_posix()}`\n"
        "- `.folder1004/file_index.jsonl`\n"
        "- `.folder1004/folder_index.jsonl`\n"
        "- `.folder1004/doc_text/`\n"
    )


def _agent_routes_markdown(manifest) -> str:
    return (
        "# Folder1004 Agent Routes\n\n"
        "1. 먼저 `.folder1004/agent_map.md`를 읽는다.\n"
        "2. 폴더 후보는 `.folder1004/folder_index.jsonl`에서 찾는다.\n"
        "3. 파일 후보는 `.folder1004/file_index.jsonl`에서 찾는다.\n"
        "4. PDF/Office/HWP 문서 본문은 `.folder1004/doc_text/`에서 `rg`로 검색한다.\n"
        "5. 최종 접근은 `path` 필드의 원본 파일 경로를 사용한다.\n\n"
        f"생성 시각: {manifest['generated_at']}\n"
    )


def _agent_skill_context_markdown(manifest) -> str:
    return (
        "# Folder1004 Skill Context\n\n"
        "Folder1004는 검색기가 아니라 로컬 에이전트가 CLI에서 파일 시스템을 잘 찾도록 준비하는 도구입니다.\n"
        "이 인덱스는 비파괴적으로 생성되었으며 원본 폴더/파일명은 변경하지 않았습니다.\n\n"
        "## Read first\n"
        "- `.folder1004/agent_map.md`\n"
        "- `.folder1004/agent_routes.md`\n"
        "- `.folder1004/file_index.jsonl`\n"
        "- `.folder1004/document_index.jsonl`\n"
    )


def _human_guide_markdown(manifest) -> str:
    return (
        "# Folder1004 Human Guide\n\n"
        "기존 폴더와 파일은 이동/변경하지 않았습니다. `.folder1004/` 아래에 탐색용 지도와 인덱스만 생성했습니다.\n\n"
        f"- 파일: {manifest['files']}개\n"
        f"- 폴더: {manifest['folders']}개\n"
        f"- 문서 텍스트 캐시: {manifest['documents']}개 대상\n"
    )


def _folder_card_markdown(row: dict) -> str:
    return (
        f"# {row.get('path')}\n\n"
        f"- 폴더 ID: `{row.get('folder_id')}`\n"
        f"- 직접 파일 수: {row.get('file_count_direct')}\n"
        f"- 직접 하위 폴더 수: {row.get('subfolder_count_direct')}\n"
        f"- 키워드: {', '.join(row.get('keywords') or []) or '—'}\n\n"
        "## 하위 폴더\n"
        + "\n".join(f"- {x}" for x in (row.get("child_folders") or []))
        + "\n"
    )


def _file_card_markdown(row: dict) -> str:
    return (
        f"# {row.get('name')}\n\n"
        f"- 경로: `{row.get('path')}`\n"
        f"- SHA256: `{row.get('sha256')}`\n"
        f"- 파싱 상태: {row.get('parse_status')}\n"
        f"- 텍스트 캐시: `{row.get('text_cache_path') or ''}`\n"
        f"- 키워드: {', '.join(row.get('keywords') or []) or '—'}\n\n"
        f"## 요약\n{row.get('summary') or '—'}\n"
    )
