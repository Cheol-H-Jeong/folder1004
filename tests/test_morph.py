"""Korean morpheme-based noun extraction.

Skips kiwi-specific assertions if the analyser isn't installed (CI on
PRs without kiwi or limited environments).  When kiwi *is* available
we verify the noise filters (person names, dates, clerical nouns).
"""
import pytest

from folder1004 import morph


def test_module_imports_without_kiwi():
    # extract_nouns must always return something even when kiwi is
    # missing — falls back to the character-class tokeniser.
    out = morph.extract_nouns("한국지역정보개발원 제안발표 v0.5")
    assert out, "fallback (or kiwi) must yield at least one noun"


def test_extract_nouns_keeps_korean_proper_noun():
    out = morph.extract_nouns("한국지역정보개발원 제안발표")
    assert "한국지역정보개발원" in out


def test_extract_nouns_keeps_latin_brand_token():
    out = morph.extract_nouns("AVOCA 분석모듈 v0.5 240820")
    assert "avoca" in [n.casefold() for n in out]


def test_extract_nouns_drops_clerical_nouns():
    out = morph.extract_nouns("프로젝트 알파 최종본 작성요청")
    lowered = [n.casefold() for n in out]
    assert "최종본" not in lowered
    assert "작성요청" not in lowered


def test_extract_nouns_drops_dates_and_versions():
    out = morph.extract_nouns("제안서 v1.0 240820 2024-03-21")
    assert all(not n.replace(".", "").isdigit() for n in out)


def test_windows_defaults_to_safe_fallback(monkeypatch):
    """Windows packaged builds must not construct kiwipiepy.Kiwi by default.

    The native constructor can terminate the whole process with heap
    corruption before Python can catch it, so the safety gate must trigger
    before any import/constructor attempt.
    """
    monkeypatch.delenv("FOLDER1004_ENABLE_KIWI", raising=False)
    monkeypatch.setattr(morph.sys, "platform", "win32")
    morph._kiwi = object()
    morph._kiwi_unavailable = False

    assert morph._get_kiwi() is None
    assert morph.extract_nouns("한국지역정보개발원 제안발표")

    morph._kiwi = None
    morph._kiwi_unavailable = False


def test_windows_safe_fallback_keeps_substantive_proper_tokens(monkeypatch):
    """The no-Kiwi Windows path must still leave enough exact Korean
    identity tokens for rescue matching while excluding bare 2-letter AI.
    """
    monkeypatch.delenv("FOLDER1004_ENABLE_KIWI", raising=False)
    monkeypatch.setattr(morph.sys, "platform", "win32")
    morph._kiwi = None
    morph._kiwi_unavailable = False

    pn = set(morph.extract_proper_nouns("의약품 AI 심사 및 산업지원"))
    assert {"의약품", "심사", "의약품심사"} <= pn
    assert "ai" not in pn

    pn = set(morph.extract_proper_nouns("행안부 범정부 AI 공통기반 문서인식"))
    assert {"행안부", "문서인식"} <= pn

    pn = set(morph.extract_proper_nouns("김민지 kimminji 박규리 parkgyuri"))
    assert "김민지" not in pn
    assert "박규리" not in pn
    assert {"kimminji", "parkgyuri"} <= pn

    morph._kiwi = None
    morph._kiwi_unavailable = False


@pytest.mark.skipif(not morph.is_available(), reason="kiwi not installed")
def test_kiwi_drops_person_name_when_followed_by_given_name():
    """``김철수 제안서`` should yield ``[제안서]`` — the surname+given
    pair is recognised and dropped.
    """
    out = morph.extract_nouns("김철수 제안서")
    # surname '김' should be filtered together with the given-name fragment
    assert "김" not in out
    assert "제안서" in out
