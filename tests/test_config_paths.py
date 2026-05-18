from pathlib import Path


def test_frozen_portable_marker_keeps_data_next_to_exe(tmp_path, monkeypatch):
    from folder1004 import config

    exe_dir = tmp_path / "folder1004"
    exe_dir.mkdir()
    exe = exe_dir / "folder1004.exe"
    exe.write_text("", encoding="utf-8")
    (exe_dir / "folder1004.portable").write_text("", encoding="utf-8")

    monkeypatch.delenv("FOLDER1004_HOME", raising=False)
    monkeypatch.delenv("FOLDER1004_PORTABLE", raising=False)
    monkeypatch.setattr(config.sys, "frozen", True, raising=False)
    monkeypatch.setattr(config.sys, "executable", str(exe))

    paths = config.default_paths()
    assert paths.root == exe_dir / "data"
    assert paths.config == exe_dir / "data" / "config.json"
    assert paths.logs_dir == exe_dir / "data" / "logs"


def test_folder1004_home_overrides_portable_marker(tmp_path, monkeypatch):
    from folder1004 import config

    exe_dir = tmp_path / "folder1004"
    exe_dir.mkdir()
    exe = exe_dir / "folder1004.exe"
    exe.write_text("", encoding="utf-8")
    (exe_dir / "folder1004.portable").write_text("", encoding="utf-8")
    override = tmp_path / "custom-home"

    monkeypatch.setenv("FOLDER1004_HOME", str(override))
    monkeypatch.setattr(config.sys, "frozen", True, raising=False)
    monkeypatch.setattr(config.sys, "executable", str(exe))

    assert config.default_paths().root == Path(override)


def test_env_portable_mode_uses_exe_dir_data(tmp_path, monkeypatch):
    from folder1004 import config

    exe_dir = tmp_path / "loose"
    exe_dir.mkdir()
    exe = exe_dir / "folder1004.exe"
    exe.write_text("", encoding="utf-8")

    monkeypatch.delenv("FOLDER1004_HOME", raising=False)
    monkeypatch.setenv("FOLDER1004_PORTABLE", "1")
    monkeypatch.setattr(config.sys, "frozen", False, raising=False)
    monkeypatch.setattr(config.sys, "executable", str(exe))

    assert config.default_paths().root == exe_dir / "data"


def test_cli_recursive_defaults_on(tmp_path, monkeypatch):
    import folder1004.__main__ as entry

    seen = {}

    def fake_run_cli(args):
        seen["recursive"] = args.recursive
        seen["path"] = args.path
        return 0

    monkeypatch.setattr(entry, "_run_cli", fake_run_cli)
    assert entry.main(["--cli", "--path", str(tmp_path)]) == 0
    assert seen == {"recursive": True, "path": str(tmp_path)}
