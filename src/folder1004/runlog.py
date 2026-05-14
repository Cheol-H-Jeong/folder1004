"""Per-run logger.

Every invocation of Folder1004 (GUI launch *and* each Organize run, plus
each CLI ``--cli`` call) gets a fresh timestamped log file under
``~/.folder1004/logs/``.  We attach a ``logging.FileHandler`` to the root
logger so every module's existing ``log.warning(...)`` / ``log.info(...)``
call lands there, plus we install ``sys.excepthook`` so unhandled
exceptions are captured with a full stack trace.

The module also exposes :func:`current_log_path` for the UI to surface a
"로그 파일 열기" button.
"""
from __future__ import annotations

import datetime as _dt
import faulthandler
import logging
import os
import platform
import re
import sys
import threading
import traceback
from pathlib import Path
from typing import Optional

from .config import default_paths

_lock = threading.Lock()
_active_handler: Optional[logging.Handler] = None
_active_path: Optional[Path] = None
_install_count = 0
_thread_hook_installed = False


# Patterns we MUST never write to disk.  Hits anywhere in a log record
# (message, args, exception text) are replaced before reaching the file.
_SECRET_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    # Google AI Studio API keys: "AIza" + 35 chars
    (re.compile(r"AIza[0-9A-Za-z_\-]{35}"), "[REDACTED_KEY]"),
    # OpenAI / generic "sk-…" tokens (covers sk-, sk-proj-, sk-ant-, …)
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"), "[REDACTED_KEY]"),
    # Bearer tokens in Authorization headers
    (
        re.compile(r"(?i)Authorization:\s*Bearer\s+[A-Za-z0-9_\-.=]+"),
        "Authorization: Bearer [REDACTED]",
    ),
    # ?key=… / &key=… query strings (Google APIs put the key here)
    (re.compile(r"(?i)([?&]key=)[A-Za-z0-9_\-]+"), r"\1[REDACTED]"),
    # Generic api_key=… / api-key=… / X-Api-Key: …
    (
        re.compile(r"(?i)(api[_\-]?key\s*[:=]\s*['\"]?)[A-Za-z0-9_\-]{12,}"),
        r"\1[REDACTED]",
    ),
    # Long hex tokens (matches the local llama-server style)
    (re.compile(r"\b[0-9a-f]{48,}\b"), "[REDACTED_KEY]"),
)


def _redact(text: str) -> str:
    if not text or not isinstance(text, str):
        return text
    out = text
    for pat, replacement in _SECRET_PATTERNS:
        out = pat.sub(replacement, out)
    return out


class _RedactingFormatter(logging.Formatter):
    """Strips API keys / bearer tokens from every log record before
    they land in the file.  Defence-in-depth against accidental leaks
    even when a caller logs an Authorization header verbatim.
    """

    def format(self, record: logging.LogRecord) -> str:
        return _redact(super().format(record))


def _format_handler() -> logging.Formatter:
    return _RedactingFormatter(
        fmt="%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _silence_chatty_third_parties() -> None:
    """Demote loggers that tend to dump URLs (containing ``?key=``) and
    full request bodies at DEBUG level to WARNING.  Defence-in-depth on
    top of the formatter-level redaction.
    """
    for name in ("urllib3", "urllib3.connectionpool", "requests", "PIL"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _memory_snapshot() -> str:
    """Return a small best-effort memory summary for crash diagnostics."""
    try:
        if sys.platform.startswith("win"):
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                mib = 1024 * 1024
                return (
                    f"mem_load={stat.dwMemoryLoad}% "
                    f"phys={stat.ullAvailPhys // mib}/{stat.ullTotalPhys // mib}MiB "
                    f"pagefile={stat.ullAvailPageFile // mib}/{stat.ullTotalPageFile // mib}MiB"
                )
        else:
            import resource

            rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            # Linux reports KiB, macOS reports bytes.
            rss_mib = rss / 1024 if sys.platform.startswith("linux") else rss / (1024 * 1024)
            return f"max_rss={rss_mib:.1f}MiB"
    except Exception as exc:  # pragma: no cover - platform-dependent
        return f"memory_snapshot_unavailable={exc}"
    return "memory_snapshot_unavailable"


def _install_exception_hooks() -> None:
    global _install_count, _thread_hook_installed
    if _install_count == 0:
        previous = sys.excepthook

        def _hook(exc_type, exc, tb):
            logging.getLogger("folder1004.crash").error(
                "Unhandled exception:\n%s",
                "".join(traceback.format_exception(exc_type, exc, tb)),
            )
            previous(exc_type, exc, tb)

        sys.excepthook = _hook
        _install_count += 1

    if not _thread_hook_installed and hasattr(threading, "excepthook"):
        previous_thread_hook = threading.excepthook

        def _thread_hook(args):  # type: ignore[no-untyped-def]
            logging.getLogger("folder1004.crash").error(
                "Unhandled thread exception in %s:\n%s",
                getattr(args.thread, "name", "<unknown>"),
                "".join(
                    traceback.format_exception(
                        args.exc_type, args.exc_value, args.exc_traceback
                    )
                ),
            )
            previous_thread_hook(args)

        threading.excepthook = _thread_hook
        _thread_hook_installed = True


def start_session(tag: str = "session") -> Path:
    """Open a fresh log file for this run and return its path.

    Calling ``start_session`` again rotates to a new file.  Idempotent under
    threads — only one handler is ever attached.
    """
    global _active_handler, _active_path
    with _lock:
        paths = default_paths()
        paths.ensure()
        stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file = paths.logs_dir / f"{tag}_{stamp}.log"

        # Remove the previously installed handler so we don't double-write
        # to a stale file from an earlier run.
        root = logging.getLogger()
        if _active_handler is not None:
            try:
                if faulthandler.is_enabled():
                    faulthandler.disable()
                root.removeHandler(_active_handler)
                _active_handler.close()
            except Exception:
                pass

        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(_format_handler())
        # Make sure the root logger lets DEBUG through to our file even if
        # the existing console handler is at INFO/WARNING.
        if root.level > logging.DEBUG:
            root.setLevel(logging.DEBUG)
        root.addHandler(handler)

        _install_exception_hooks()
        try:
            faulthandler.enable(file=handler.stream, all_threads=True)
        except Exception as exc:  # pragma: no cover - depends on runtime
            logging.getLogger("folder1004.runlog").warning(
                "faulthandler enable failed: %s", exc
            )

        _silence_chatty_third_parties()
        _active_handler = handler
        _active_path = log_file
        logging.getLogger("folder1004.runlog").info(
            "log session started: %s (pid=%d, python=%s, platform=%s, "
            "frozen=%s, exe=%s, cwd=%s, %s)",
            log_file,
            os.getpid(),
            sys.version.split()[0],
            platform.platform(),
            bool(getattr(sys, "frozen", False)),
            sys.executable,
            os.getcwd(),
            _memory_snapshot(),
        )
        return log_file


def current_log_path() -> Optional[Path]:
    return _active_path


def log_exception(label: str, exc: BaseException) -> None:
    """Convenience helper used by callers that want to capture handled
    exceptions with a full stack trace into the per-run log file.
    """
    logging.getLogger("folder1004.runlog").error(
        "%s: %s\n%s",
        label,
        exc,
        "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
    )
