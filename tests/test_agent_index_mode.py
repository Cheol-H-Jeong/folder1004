from __future__ import annotations

import json

from folder1004.config import Config, ORGANIZE_MODE_METADATA_INDEX
from folder1004.pipeline import run


def _jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_metadata_index_default_is_non_destructive_and_writes_agent_artifacts(tmp_path):
    src_dir = tmp_path / "기존분류" / "회의"
    src_dir.mkdir(parents=True)
    doc = src_dir / "회의록.txt"
    doc.write_text("범정부AI 회의 내용", encoding="utf-8")
    root_file = tmp_path / "루트메모.md"
    root_file.write_text("행안부 메모", encoding="utf-8")

    op = run(tmp_path, Config(), recursive=False, dry_run=False, force_mock=True)

    assert op.total_moved == 0
    assert doc.exists()
    assert root_file.exists()
    meta = tmp_path / ".folder1004"
    assert (meta / "agent_map.md").exists()
    assert (meta / "folder_index.jsonl").exists()
    assert (meta / "file_index.jsonl").exists()
    assert (tmp_path / "000_FOLDER1004_AGENT_MAP.md").exists()
    rows = _jsonl(meta / "file_index.jsonl")
    assert {r["path"] for r in rows if r.get("status") == "present"} >= {
        "기존분류/회의/회의록.txt",
        "루트메모.md",
    }
    assert getattr(op, "agent_index").files == 2


def test_metadata_index_parses_parser_required_docs_and_reuses_cache_incrementally(tmp_path):
    pptx = tmp_path / "보고서.rtf"
    pptx.write_text(r"{\rtf1\ansi Folder1004 special contract text}", encoding="utf-8")
    cfg = Config()
    cfg.organize_mode = ORGANIZE_MODE_METADATA_INDEX

    op1 = run(tmp_path, cfg, recursive=True, dry_run=False, force_mock=True)
    docs = _jsonl(tmp_path / ".folder1004" / "document_index.jsonl")
    assert docs and docs[0]["parser_required"] is True
    assert docs[0]["parse_status"] in {"success", "empty"}
    if docs[0]["parse_status"] == "success":
        text_path = tmp_path / docs[0]["text_cache_path"]
        assert text_path.exists()
        assert "Folder1004" in text_path.read_text(encoding="utf-8", errors="ignore")

    op2 = run(tmp_path, cfg, recursive=True, dry_run=False, force_mock=True)
    assert getattr(op2, "agent_index").docs_reused >= getattr(op1, "agent_index").docs_reused
    assert pptx.exists()


def test_metadata_index_marks_deleted_files_on_update(tmp_path):
    p = tmp_path / "old.docx"
    p.write_text("not really docx", encoding="utf-8")
    cfg = Config()
    run(tmp_path, cfg, recursive=True, dry_run=False, force_mock=True)
    p.unlink()

    run(tmp_path, cfg, recursive=True, dry_run=False, force_mock=True)
    rows = _jsonl(tmp_path / ".folder1004" / "file_index.jsonl")
    deleted = [r for r in rows if r.get("path") == "old.docx" and r.get("status") == "deleted"]
    assert deleted


def test_metadata_index_prunes_stale_document_text_cache(tmp_path):
    p = tmp_path / "old.rtf"
    p.write_text(r"{\rtf1\ansi stale searchable body}", encoding="utf-8")
    cfg = Config()
    run(tmp_path, cfg, recursive=True, dry_run=False, force_mock=True)
    first_docs = _jsonl(tmp_path / ".folder1004" / "document_index.jsonl")
    assert first_docs
    old_cache = tmp_path / first_docs[0]["text_cache_path"]
    assert old_cache.exists()

    p.unlink()
    run(tmp_path, cfg, recursive=True, dry_run=False, force_mock=True)

    assert not old_cache.exists()
    assert not list((tmp_path / ".folder1004" / "doc_text").glob("sha256_*"))
    assert not list((tmp_path / ".folder1004" / "doc_meta").glob("sha256_*.json"))
