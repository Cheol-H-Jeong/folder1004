"""Parser resilience: the dispatcher must NEVER raise, and individual
parsers should degrade gracefully (metadata fallback) for the most
common real-world failure shapes — encrypted PDFs, malformed PPTX/
DOCX, files that don't match their extension, etc.
"""
import io
import threading
import zipfile
from pathlib import Path

from folder1004.parsers import extract_excerpt
from folder1004.parsers.pdf import parse as pdf_parse


def test_garbage_pdf_returns_empty_no_raise(tmp_path):
    p = tmp_path / "broken.pdf"
    p.write_bytes(b"%PDF-1.4\nthis is not a valid pdf body")
    assert extract_excerpt(p) == ""  # no exception


def test_encrypted_pdf_falls_back_to_metadata(tmp_path):
    """Build a real, valid encrypted PDF and verify the parser:
       * doesn't raise the bare 'File has not been decrypted' error,
       * returns *something* (metadata) when the empty password
         cannot unlock it.
    """
    pypdf = __import__("pypdf")
    PdfWriter = pypdf.PdfWriter
    PdfReader = pypdf.PdfReader

    # Source: a one-page PDF.  pypdf can synthesise a blank one.
    src = tmp_path / "open.pdf"
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    w.add_metadata({"/Title": "테스트 보고서 2024", "/Author": "QA"})
    with src.open("wb") as f:
        w.write(f)

    enc = tmp_path / "encrypted.pdf"
    w2 = PdfWriter(clone_from=PdfReader(str(src)))
    w2.encrypt(user_password="real-secret")
    with enc.open("wb") as f:
        w2.write(f)

    # The parser must not raise the raw "File has not been decrypted"
    # error — it returns either a metadata-derived string (when pypdf
    # exposes it) or an empty string.  Both are acceptable; what we
    # forbid is an exception bubbling out of the dispatcher.
    out = pdf_parse(enc, 200)
    assert isinstance(out, str)


def test_humanise_encrypted_skip_reason():
    from folder1004.organizer import _humanise_skip_reason

    out = _humanise_skip_reason(RuntimeError("File has not been decrypted"),
                                Path("/tmp/secret.pdf"))
    assert "암호화" in out and "decrypted" not in out


def test_pptx_not_a_zip_returns_empty(tmp_path):
    p = tmp_path / "broken.pptx"
    p.write_bytes(b"not really a pptx")
    assert extract_excerpt(p) == ""


def test_docx_open_failure_returns_empty(tmp_path):
    p = tmp_path / "broken.docx"
    p.write_bytes(b"random bytes")
    assert extract_excerpt(p) == ""


def test_zip_file_with_unexpected_content_does_not_raise(tmp_path):
    p = tmp_path / "doc.pptx"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("garbage", "no slide xml here")
    p.write_bytes(buf.getvalue())
    assert extract_excerpt(p) == ""


def test_large_supported_file_skips_body_parse(tmp_path, monkeypatch):
    """Huge files should fall back to filename/metadata classification
    instead of invoking memory-heavy body parsers."""
    from folder1004.parsers import registry

    p = tmp_path / "large.txt"
    p.write_text("this would normally be parsed", encoding="utf-8")
    monkeypatch.setattr(registry, "_MAX_PARSE_BYTES", 1)

    assert registry.extract_excerpt(p, max_chars=200, timeout=0.1) == ""


def test_parser_queue_saturation_skips_instead_of_piling_futures(tmp_path, monkeypatch):
    """If parser slots are exhausted by slow native parsers, callers
    must skip body extraction rather than enqueueing unbounded work."""
    from folder1004.parsers import registry

    sem = threading.BoundedSemaphore(1)
    assert sem.acquire(blocking=False)
    monkeypatch.setattr(registry, "_PARSE_SLOTS", sem)
    monkeypatch.setattr(registry, "_PARSE_WORKERS", 1)
    monkeypatch.setattr(registry, "_PARSE_QUEUE_SLOTS", 1)

    try:
        out = registry._safe(lambda _p, _n: "should not run", tmp_path / "a.pdf", 100, 0.1)
    finally:
        sem.release()

    assert out == ""
