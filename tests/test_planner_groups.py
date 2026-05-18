from pathlib import Path
from datetime import datetime

from folder1004.models import FileEntry
from folder1004.planner import _plan_from_dict


def _entry(tmp_path: Path, name: str) -> FileEntry:
    p = tmp_path / name
    p.write_text(name)
    now = datetime.now()
    return FileEntry(
        path=p,
        name=name,
        ext=p.suffix,
        size=p.stat().st_size,
        created=now,
        modified=now,
        accessed=now,
    )


def test_missing_category_groups_are_numbered_from_001(tmp_path):
    entries = [_entry(tmp_path, "report.txt"), _entry(tmp_path, "contract.txt")]
    out = _plan_from_dict(
        {
            "categories": [
                {"id": "reports", "name": "보고서"},
                {"id": "contracts", "name": "계약"},
            ],
            "assignments": [
                {"path": str(entries[0].path), "primary": "reports"},
                {"path": str(entries[1].path), "primary": "contracts"},
            ],
        },
        entries,
    )

    groups = {c.id: c.group for c in out.categories}
    assert groups["reports"] == 1
    assert groups["contracts"] == 2
    assert groups["misc"] == 999


def test_non_misc_999_group_is_reassigned_but_related_groups_are_preserved(tmp_path):
    entries = [
        _entry(tmp_path, "alpha.txt"),
        _entry(tmp_path, "beta.txt"),
        _entry(tmp_path, "gamma.txt"),
    ]
    out = _plan_from_dict(
        {
            "categories": [
                {"id": "alpha", "name": "Alpha", "group": 7},
                {"id": "beta", "name": "Beta", "group": 7},
                {"id": "gamma", "name": "Gamma", "group": 999},
            ],
            "assignments": [
                {"path": str(entries[0].path), "primary": "alpha"},
                {"path": str(entries[1].path), "primary": "beta"},
                {"path": str(entries[2].path), "primary": "gamma"},
            ],
        },
        entries,
    )

    groups = {c.id: c.group for c in out.categories}
    assert groups["alpha"] == 7
    assert groups["beta"] == 7
    assert groups["gamma"] == 1
    assert groups["misc"] == 999


def test_missing_groups_skip_explicit_groups_and_catchall_stays_999(tmp_path):
    entries = [
        _entry(tmp_path, "alpha.txt"),
        _entry(tmp_path, "beta.txt"),
        _entry(tmp_path, "gamma.txt"),
        _entry(tmp_path, "misc.txt"),
    ]
    out = _plan_from_dict(
        {
            "categories": [
                {"id": "alpha", "name": "Alpha", "group": 1},
                {"id": "beta", "name": "Beta", "group": 2},
                {"id": "gamma", "name": "Gamma"},
                {"id": "misc", "name": "기타", "group": 5},
            ],
            "assignments": [
                {"path": str(entries[0].path), "primary": "alpha"},
                {"path": str(entries[1].path), "primary": "beta"},
                {"path": str(entries[2].path), "primary": "gamma"},
                {"path": str(entries[3].path), "primary": "misc"},
            ],
        },
        entries,
    )

    groups = {c.id: c.group for c in out.categories}
    assert groups["alpha"] == 1
    assert groups["beta"] == 2
    assert groups["gamma"] == 3
    assert groups["misc"] == 999


def test_invalid_groups_are_reassigned_to_real_category_numbers(tmp_path):
    entries = [_entry(tmp_path, "negative.txt"), _entry(tmp_path, "large.txt")]
    out = _plan_from_dict(
        {
            "categories": [
                {"id": "negative", "name": "Negative", "group": -3},
                {"id": "large", "name": "Large", "group": 1500},
            ],
            "assignments": [
                {"path": str(entries[0].path), "primary": "negative"},
                {"path": str(entries[1].path), "primary": "large"},
            ],
        },
        entries,
    )

    groups = {c.id: c.group for c in out.categories}
    assert groups["negative"] == 1
    assert groups["large"] == 2
    assert groups["misc"] == 999
