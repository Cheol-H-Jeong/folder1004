"""Local folder profile detection and health scoring.

This module intentionally uses only local filenames, paths, sizes and
metadata.  It does not call the network or inspect secrets, so it can run
before the LLM planner and in tests/offline builds.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
import re

from .config import CLASSIFICATION_GUIDANCE_PRESETS
from .models import FileEntry

_PRESET_LABELS = {str(p.get("label") or "") for p in CLASSIFICATION_GUIDANCE_PRESETS}


@dataclass
class FolderProfileSummary:
    profile_id: str
    label: str
    confidence: float
    matched_signals: list[str] = field(default_factory=list)
    recommended_preset_names: list[str] = field(default_factory=list)
    health_score: int = 100
    health_level: str = "좋음"
    health_reasons: list[str] = field(default_factory=list)
    file_count: int = 0
    root_file_count: int = 0
    total_bytes: int = 0
    extension_counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "profile_id": self.profile_id,
            "label": self.label,
            "confidence": self.confidence,
            "matched_signals": list(self.matched_signals),
            "recommended_preset_names": list(self.recommended_preset_names),
            "health_score": self.health_score,
            "health_level": self.health_level,
            "health_reasons": list(self.health_reasons),
            "file_count": self.file_count,
            "root_file_count": self.root_file_count,
            "total_bytes": self.total_bytes,
            "extension_counts": dict(self.extension_counts),
        }


_PROFILE_RULES: list[tuple[str, str, tuple[str, ...], tuple[str, ...]]] = [
    (
        "downloads", "다운로드/임시 보관함",
        ("download", "downloads", "다운로드", "kakaotalk", "카카오톡", "desktop", "바탕화면"),
        ("setup", "install", "installer", "download", "tmp", "temp", "crdownload", ".exe", ".dmg", ".zip"),
    ),
    (
        "photos", "사진/촬영 자료",
        ("photo", "photos", "picture", "pictures", "image", "images", "studio", "촬영", "사진", "고객"),
        ("jpg", "jpeg", "png", "heic", "raw", "dng", "screenshot", "스크린샷", "촬영", "customer", "client"),
    ),
    (
        "school", "수업/학기 자료",
        ("school", "class", "lecture", "course", "semester", "학교", "수업", "강의", "학기"),
        ("homework", "assignment", "exam", "lecture", "과제", "시험", "강의", "학생", "syllabus"),
    ),
    (
        "business", "업무/프로젝트 문서",
        ("work", "project", "business", "proposal", "contract", "docs", "업무", "프로젝트", "사업"),
        ("contract", "invoice", "proposal", "meeting", "minutes", "견적", "계약", "정산", "회의", "보고서"),
    ),
    (
        "research", "연구/참고 자료",
        ("research", "paper", "papers", "thesis", "논문", "연구", "자료"),
        ("paper", "thesis", "journal", "dataset", "논문", "연구", "reference", "bib"),
    ),
    (
        "code", "코드/개발 자료",
        ("src", "source", "code", "dev", "repo", "project", "개발", "코드"),
        (".py", ".js", ".ts", ".json", ".yaml", ".yml", ".html", ".css", "readme", "package"),
    ),
    (
        "media", "영상/음원 자료",
        ("video", "videos", "movie", "music", "audio", "영상", "음원"),
        (".mp4", ".mov", ".mkv", ".avi", ".mp3", ".wav", ".flac", ".m4a"),
    ),
    (
        "documents", "일반 문서함",
        ("documents", "docs", "문서", "서류"),
        (".pdf", ".docx", ".hwp", ".hwpx", ".xlsx", ".pptx", ".txt", ".md"),
    ),
]

_PROFILE_PRESETS = {
    "downloads": ["업무/용도 중심", "버림 후보 분리", "보수적으로 정리"],
    "photos": ["사람/고객 중심", "날짜/기간 중심", "보수적으로 정리"],
    "school": ["수업/학기 중심", "날짜/기간 중심", "보수적으로 정리"],
    "business": ["프로젝트 중심", "업무/용도 중심", "보수적으로 정리"],
    "research": ["프로젝트 중심", "날짜/기간 중심", "보수적으로 정리"],
    "code": ["프로젝트 중심", "업무/용도 중심", "보수적으로 정리"],
    "media": ["날짜/기간 중심", "업무/용도 중심", "보수적으로 정리"],
    "documents": ["업무/용도 중심", "날짜/기간 중심", "보수적으로 정리"],
    "mixed": ["프로젝트 중심", "업무/용도 중심", "보수적으로 정리"],
}

_TEMP_RX = re.compile(r"(~\$|\.tmp$|\.temp$|\.part$|\.crdownload$|autosave|복구|임시)", re.I)
_INSTALLER_EXTS = {".exe", ".msi", ".dmg", ".pkg", ".deb", ".rpm", ".appimage"}
_ARCHIVE_EXTS = {".zip", ".tar", ".gz", ".7z", ".rar"}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".bmp", ".svg", ".raw", ".dng"}
_CODE_EXTS = {".py", ".js", ".ts", ".json", ".yaml", ".yml", ".html", ".css", ".sh", ".go", ".rs"}
_DOC_EXTS = {".pdf", ".doc", ".docx", ".hwp", ".hwpx", ".ppt", ".pptx", ".xls", ".xlsx", ".txt", ".md"}


def analyze_folder_profile(
    root: Path,
    entries: list[FileEntry],
    recursive: bool = False,
) -> FolderProfileSummary:
    root = Path(root)
    entries = list(entries or [])
    ext_counts = Counter((e.ext or "(noext)").lower() for e in entries)
    total_bytes = sum(max(0, int(e.size or 0)) for e in entries)
    root_file_count = _root_file_count(root, entries)
    profile_id, label, confidence, signals = _detect_profile(root, entries, ext_counts)
    health_score, health_level, health_reasons = _score_health(
        root, entries, ext_counts, root_file_count, recursive=recursive,
    )
    presets = [p for p in _PROFILE_PRESETS.get(profile_id, _PROFILE_PRESETS["mixed"]) if p in _PRESET_LABELS]
    return FolderProfileSummary(
        profile_id=profile_id,
        label=label,
        confidence=confidence,
        matched_signals=signals,
        recommended_preset_names=presets,
        health_score=health_score,
        health_level=health_level,
        health_reasons=health_reasons,
        file_count=len(entries),
        root_file_count=root_file_count,
        total_bytes=total_bytes,
        extension_counts=dict(ext_counts.most_common(12)),
    )


def _root_file_count(root: Path, entries: list[FileEntry]) -> int:
    try:
        root_resolved = root.resolve()
    except OSError:
        root_resolved = root
    count = 0
    for entry in entries:
        try:
            if Path(entry.path).resolve().parent == root_resolved:
                count += 1
        except OSError:
            if Path(entry.path).parent == root:
                count += 1
    return count


def _detect_profile(
    root: Path,
    entries: list[FileEntry],
    ext_counts: Counter[str],
) -> tuple[str, str, float, list[str]]:
    root_text = str(root).lower()
    names = "\n".join(e.name.lower() for e in entries[:250])
    ext_total = max(1, sum(ext_counts.values()))
    scores: dict[str, float] = {}
    signals: dict[str, list[str]] = {}

    for pid, label, path_words, file_words in _PROFILE_RULES:
        score = 0.0
        sig: list[str] = []
        path_hits = [w for w in path_words if w.lower() in root_text]
        file_hits = [w for w in file_words if w.lower() in names]
        if path_hits:
            score += min(0.45, 0.18 * len(path_hits))
            sig.append("경로 단서: " + ", ".join(path_hits[:3]))
        if file_hits:
            score += min(0.35, 0.08 * len(file_hits))
            sig.append("파일명 단서: " + ", ".join(file_hits[:4]))
        if pid == "photos":
            ratio = sum(ext_counts.get(e, 0) for e in _IMAGE_EXTS) / ext_total
            if ratio >= 0.35:
                score += min(0.35, ratio)
                sig.append(f"이미지 비율 {ratio:.0%}")
        elif pid == "code":
            ratio = sum(ext_counts.get(e, 0) for e in _CODE_EXTS) / ext_total
            if ratio >= 0.25:
                score += min(0.35, ratio)
                sig.append(f"코드 파일 비율 {ratio:.0%}")
        elif pid == "documents":
            ratio = sum(ext_counts.get(e, 0) for e in _DOC_EXTS) / ext_total
            if ratio >= 0.45:
                score += min(0.30, ratio / 2)
                sig.append(f"문서 파일 비율 {ratio:.0%}")
        elif pid == "downloads":
            ratio = (
                sum(ext_counts.get(e, 0) for e in _INSTALLER_EXTS | _ARCHIVE_EXTS)
                / ext_total
            )
            if ratio >= 0.20:
                score += min(0.35, ratio)
                sig.append(f"설치/압축 파일 비율 {ratio:.0%}")
        scores[pid] = score
        signals[pid] = sig

    priority = ["photos", "school", "business", "research", "code", "downloads", "media", "documents"]
    best = max(priority, key=lambda pid: (scores.get(pid, 0.0), -priority.index(pid)))
    best_score = scores.get(best, 0.0)
    if best_score < 0.22:
        return "mixed", "혼합 자료함", 0.35 if entries else 0.0, ["뚜렷한 단일 프로필 단서가 부족함"]
    label = next(label for pid, label, _p, _f in _PROFILE_RULES if pid == best)
    confidence = max(0.35, min(0.95, best_score))
    return best, label, round(confidence, 2), signals.get(best, [])[:5]


def _score_health(
    root: Path,
    entries: list[FileEntry],
    ext_counts: Counter[str],
    root_file_count: int,
    *,
    recursive: bool,
) -> tuple[int, str, list[str]]:
    file_count = len(entries)
    if file_count == 0:
        return 100, "좋음", ["정리할 파일이 없습니다"]

    score = 100
    reasons: list[str] = []

    root_ratio = root_file_count / max(1, file_count)
    if root_file_count >= 30 or root_ratio >= 0.75:
        penalty = 25 if root_file_count >= 50 or root_ratio >= 0.9 else 16
        score -= penalty
        reasons.append(f"최상위에 파일 {root_file_count}개가 직접 놓여 있음")
    elif root_file_count >= 10:
        score -= 8
        reasons.append(f"최상위 직접 파일 {root_file_count}개")

    installer_count = sum(ext_counts.get(e, 0) for e in _INSTALLER_EXTS)
    archive_count = sum(ext_counts.get(e, 0) for e in _ARCHIVE_EXTS)
    temp_count = sum(1 for e in entries if _TEMP_RX.search(e.name))
    if installer_count:
        score -= min(18, 4 + installer_count * 3)
        reasons.append(f"설치/실행 파일 {installer_count}개")
    if temp_count:
        score -= min(18, 5 + temp_count * 4)
        reasons.append(f"임시/다운로드 잔여 파일 {temp_count}개")
    if archive_count >= 5:
        score -= min(12, archive_count)
        reasons.append(f"압축 파일 {archive_count}개")

    duplicate_name_count = _duplicate_name_count(entries)
    if duplicate_name_count:
        score -= min(18, 5 + duplicate_name_count * 2)
        reasons.append(f"중복 의심 파일명 {duplicate_name_count}개")

    if not recursive and _has_many_subdirs(root):
        score -= 7
        reasons.append("하위 폴더가 있어도 이번 실행은 최상위만 검사하도록 설정됨")

    score = max(0, min(100, score))
    if score >= 85:
        level = "좋음"
    elif score >= 65:
        level = "보통"
    elif score >= 40:
        level = "정리 필요"
    else:
        level = "심각"
    if not reasons:
        reasons.append("큰 정리 위험 신호가 적음")
    return score, level, reasons[:6]


def _duplicate_name_count(entries: list[FileEntry]) -> int:
    normalized = [re.sub(r"\s*\(\d+\)(?=\.)", "", e.name.lower()) for e in entries]
    counts = Counter(normalized)
    return sum(n - 1 for n in counts.values() if n > 1)


def _has_many_subdirs(root: Path) -> bool:
    try:
        return sum(1 for child in root.iterdir() if child.is_dir()) >= 3
    except OSError:
        return False
