from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def collector(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    module_path = Path(__file__).parents[2] / "tools" / "docs" / "collect_evidence.py"
    spec = importlib.util.spec_from_file_location("collect_evidence_under_test", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    repo = tmp_path / "repo"
    (repo / "docs" / "assets").mkdir(parents=True)
    (repo / "out").mkdir()
    (repo / "test_data").mkdir()
    (repo / "test_data" / "input.dat").write_text("fixed input\n", encoding="utf-8")
    for script in ("gen_a.py", "gen_b.py"):
        (repo / "docs" / script).write_text("# test generator\n", encoding="utf-8")
    (repo / "docs" / "assets" / "README.md").write_text("index\n", encoding="utf-8")

    bundle = [
        {
            "name": "a",
            "source": "source_a",
            "command": "python docs/gen_a.py",
            "what": "group a",
            "replaces": ["citation a"],
            "files": ["result.txt", "figures/plot.png"],
        },
        {
            "name": "b",
            "source": "source_b",
            "command": "python docs/gen_b.py",
            "what": "group b",
            "replaces": ["citation b"],
            "files": ["result.csv"],
        },
    ]
    monkeypatch.setattr(module, "REPO", repo)
    monkeypatch.setattr(module, "OUT", repo / "out")
    monkeypatch.setattr(module, "ASSETS", repo / "docs" / "assets")
    monkeypatch.setattr(module, "MANIFEST", repo / "docs" / "assets" / "MANIFEST.json")
    monkeypatch.setattr(module, "TEST_DATA", repo / "test_data")
    monkeypatch.setattr(module, "BUNDLE", bundle)
    module._real_code_provenance = module.code_provenance
    monkeypatch.setattr(
        module,
        "code_provenance",
        lambda: ("a" * 40, False, None),
    )
    return module


def _generator_runner(collector, calls: list[list[str]], *, mutate_input: bool = False):
    def run(argv, **kwargs):
        calls.append(list(argv))
        script = Path(argv[1]).name
        if script == "gen_a.py":
            root = collector.OUT / "source_a"
            (root / "figures").mkdir(parents=True)
            (root / "result.txt").write_text("a\n", encoding="utf-8")
            (root / "figures" / "plot.png").write_bytes(b"png")
            (root / "figures" / "plot.caption.md").write_text("caption\n", encoding="utf-8")
            if mutate_input:
                (collector.TEST_DATA / "input.dat").write_text("changed\n", encoding="utf-8")
        elif script == "gen_b.py":
            root = collector.OUT / "source_b"
            root.mkdir(parents=True)
            (root / "result.csv").write_text("x\n1\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0)

    return run


def _asset_snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _collect_ok(collector, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []
    monkeypatch.setattr(collector.subprocess, "run", _generator_runner(collector, calls))
    assert collector.collect() == 0
    return calls


def test_collect_generates_in_declared_order_clears_stale_and_writes_schema_v3(
    collector, monkeypatch: pytest.MonkeyPatch
):
    stale = collector.OUT / "source_a"
    stale.mkdir()
    (stale / "stale.txt").write_text("old\n", encoding="utf-8")

    calls = _collect_ok(collector, monkeypatch)

    assert calls == [
        [sys.executable, str(collector.REPO / "docs" / "gen_a.py")],
        [sys.executable, str(collector.REPO / "docs" / "gen_b.py")],
    ]
    assert not (collector.OUT / "source_a" / "stale.txt").exists()
    manifest = json.loads(collector.MANIFEST.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 3
    assert manifest["code_revision"] == "a" * 40
    assert manifest["code_dirty"] is False
    assert manifest["code_diff_sha256"] is None
    assert manifest["test_data_fingerprint"]["present"] is True
    assert [group["name"] for group in manifest["groups"]] == ["a", "b"]
    assert all(
        group["input_fingerprint"] == manifest["test_data_fingerprint"]
        for group in manifest["groups"]
    )
    assert [record["path"] for record in manifest["groups"][0]["files"]] == [
        "a/result.txt", "a/figures/plot.png", "a/figures/plot.caption.md",
    ]
    assert collector.check() == 0


def test_generator_failure_leaves_committed_assets_untouched(
    collector, monkeypatch: pytest.MonkeyPatch
):
    (collector.ASSETS / "a").mkdir()
    (collector.ASSETS / "a" / "old.txt").write_text("old\n", encoding="utf-8")
    collector.MANIFEST.write_text('{"old": true}\n', encoding="utf-8")
    before = _asset_snapshot(collector.ASSETS)

    def fail(argv, **kwargs):
        raise subprocess.CalledProcessError(2, argv)

    monkeypatch.setattr(collector.subprocess, "run", fail)
    assert collector.collect() == 1
    assert _asset_snapshot(collector.ASSETS) == before


def test_input_fingerprint_change_aborts_before_promotion(
    collector, monkeypatch: pytest.MonkeyPatch
):
    collector.MANIFEST.write_text('{"old": true}\n', encoding="utf-8")
    before = _asset_snapshot(collector.ASSETS)
    monkeypatch.setattr(
        collector.subprocess, "run",
        _generator_runner(collector, [], mutate_input=True),
    )
    assert collector.collect() == 1
    assert _asset_snapshot(collector.ASSETS) == before


def test_missing_test_data_aborts_before_generator(collector, monkeypatch: pytest.MonkeyPatch):
    (collector.TEST_DATA / "input.dat").unlink()
    collector.TEST_DATA.rmdir()
    called = False

    def unexpected(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(collector.subprocess, "run", unexpected)
    assert collector.collect() == 1
    assert called is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "../escape"),
        ("name", "CON"),
        ("name", "trailing."),
        ("source", "C:relative"),
        ("source", "nested/source"),
        ("file", "../escape.txt"),
        ("file", "folder\\windows.txt"),
        ("file", "file.txt:stream"),
    ],
)
def test_unsafe_bundle_paths_fail_before_generation(
    collector, monkeypatch: pytest.MonkeyPatch, field: str, value: str
):
    if field == "file":
        collector.BUNDLE[0]["files"][0] = value
    else:
        collector.BUNDLE[0][field] = value
    called = False

    def unexpected(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(collector.subprocess, "run", unexpected)
    assert collector.collect() == 1
    assert called is False


def test_case_colliding_artifacts_are_rejected(collector):
    collector.BUNDLE[0]["files"] = ["same.txt", "SAME.TXT"]
    with pytest.raises(collector.EvidenceError, match="case-colliding"):
        collector._declarations()


@pytest.mark.parametrize(
    "files",
    [
        ["node", "node/child.txt"],
        ["node/child.txt", "NODE"],
    ],
)
def test_file_directory_prefix_collisions_are_rejected(collector, files: list[str]):
    collector.BUNDLE[0]["files"] = files
    with pytest.raises(collector.EvidenceError, match="file/directory prefix collision"):
        collector._declarations()


@pytest.mark.parametrize("invalid", ['<', '>', '"', '|', '?', '*'])
def test_windows_invalid_filename_characters_are_rejected(collector, invalid: str):
    collector.BUNDLE[0]["files"][0] = f"bad{invalid}name.txt"
    with pytest.raises(collector.EvidenceError, match="unsafe a artifact path"):
        collector._declarations()


def test_declared_bundle_paths_remain_windows_safe(collector):
    groups = collector._declarations()
    assert [group["name"] for group in groups] == ["a", "b"]


def test_missing_png_caption_aborts_without_asset_mutation(
    collector, monkeypatch: pytest.MonkeyPatch
):
    before = _asset_snapshot(collector.ASSETS)

    def run(argv, **kwargs):
        result = _generator_runner(collector, [])(argv, **kwargs)
        caption = collector.OUT / "source_a" / "figures" / "plot.caption.md"
        if caption.exists():
            caption.unlink()
        return result

    monkeypatch.setattr(collector.subprocess, "run", run)
    assert collector.collect() == 1
    assert _asset_snapshot(collector.ASSETS) == before


def test_symlink_artifact_is_rejected(collector, monkeypatch: pytest.MonkeyPatch):
    target = collector.REPO / "outside.txt"
    target.write_text("outside\n", encoding="utf-8")

    def run(argv, **kwargs):
        script = Path(argv[1]).name
        if script == "gen_a.py":
            root = collector.OUT / "source_a"
            (root / "figures").mkdir(parents=True)
            try:
                (root / "result.txt").symlink_to(target)
            except OSError as exc:
                pytest.skip(f"symlink creation unavailable: {exc}")
            (root / "figures" / "plot.png").write_bytes(b"png")
            (root / "figures" / "plot.caption.md").write_text("caption\n", encoding="utf-8")
        else:
            root = collector.OUT / "source_b"
            root.mkdir()
            (root / "result.csv").write_text("x\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(collector.subprocess, "run", run)
    assert collector.collect() == 1


def test_dangling_symlink_artifact_is_rejected(collector, monkeypatch: pytest.MonkeyPatch):
    def run(argv, **kwargs):
        script = Path(argv[1]).name
        if script == "gen_a.py":
            root = collector.OUT / "source_a"
            (root / "figures").mkdir(parents=True)
            try:
                (root / "result.txt").symlink_to(collector.REPO / "does-not-exist")
            except OSError as exc:
                pytest.skip(f"symlink creation unavailable: {exc}")
            (root / "figures" / "plot.png").write_bytes(b"png")
            (root / "figures" / "plot.caption.md").write_text("caption\n", encoding="utf-8")
        else:
            root = collector.OUT / "source_b"
            root.mkdir()
            (root / "result.csv").write_text("x\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(collector.subprocess, "run", run)
    assert collector.collect() == 1


def test_symlink_parent_of_artifact_is_rejected(collector, monkeypatch: pytest.MonkeyPatch):
    def run(argv, **kwargs):
        script = Path(argv[1]).name
        if script == "gen_a.py":
            root = collector.OUT / "source_a"
            root.mkdir(parents=True)
            real_figures = collector.OUT / "real_figures"
            real_figures.mkdir()
            try:
                (root / "figures").symlink_to(real_figures, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"directory symlink creation unavailable: {exc}")
            (root / "result.txt").write_text("a\n", encoding="utf-8")
            (real_figures / "plot.png").write_bytes(b"png")
            (real_figures / "plot.caption.md").write_text("caption\n", encoding="utf-8")
        else:
            root = collector.OUT / "source_b"
            root.mkdir()
            (root / "result.csv").write_text("x\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(collector.subprocess, "run", run)
    assert collector.collect() == 1


def test_stage_refuses_preexisting_destination_symlink_without_writing_target(
    collector, tmp_path: Path
):
    groups = collector._declarations()
    source_root = tmp_path / "sources"
    source_root.mkdir()
    source = source_root / "result.txt"
    source.write_text("new\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("do not overwrite\n", encoding="utf-8")
    stage = tmp_path / "stage"
    (stage / "a").mkdir(parents=True)
    try:
        (stage / "a" / "result.txt").symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    sources = {"a": [("result.txt", source)], "b": []}

    with pytest.raises(collector.EvidenceError, match="symlink or reparse point"):
        collector._stage(stage, groups, sources, {"present": True, "n_files": 1, "sha256": "0" * 64})
    assert outside.read_text(encoding="utf-8") == "do not overwrite\n"


def test_stage_refuses_preexisting_directory_at_artifact_destination(
    collector, tmp_path: Path
):
    group = collector._declarations()[0]
    source = tmp_path / "source.txt"
    source.write_text("new\n", encoding="utf-8")
    stage = tmp_path / "stage"
    destination = stage / "a" / "result.txt"
    destination.mkdir(parents=True)

    with pytest.raises(collector.EvidenceError, match="destination already exists"):
        collector._stage(
            stage,
            [group],
            {"a": [("result.txt", source)]},
            {"present": True, "n_files": 1, "sha256": "0" * 64},
        )
    assert destination.is_dir()
    assert list(destination.iterdir()) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows junction test")
def test_junction_is_rejected_as_reparse_point(collector, tmp_path: Path):
    target = tmp_path / "target"
    junction = tmp_path / "junction"
    target.mkdir()
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        pytest.skip(f"junction creation unavailable: {result.stderr or result.stdout}")
    with pytest.raises(collector.EvidenceError, match="reparse point"):
        collector._require_directory(junction, label="junction")
    with pytest.raises(collector.EvidenceError, match="reparse point"):
        collector._contained(tmp_path, "junction", label="junction")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO creation unavailable")
def test_nonregular_artifact_is_rejected(collector, tmp_path: Path):
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(collector.EvidenceError, match="not a regular file"):
        collector._require_regular(fifo, label="artifact")


def test_promotion_failure_restores_old_bundle(collector, monkeypatch: pytest.MonkeyPatch):
    (collector.ASSETS / "a").mkdir()
    (collector.ASSETS / "a" / "old.txt").write_text("old\n", encoding="utf-8")
    collector.MANIFEST.write_text('{"old": true}\n', encoding="utf-8")
    before = _asset_snapshot(collector.ASSETS)
    monkeypatch.setattr(collector.subprocess, "run", _generator_runner(collector, []))
    real_replace = collector.os.replace
    failed = False

    def flaky_replace(source, destination):
        nonlocal failed
        source_path = Path(source)
        if not failed and source_path.name == "b" and ".evidence-stage-" in str(source_path):
            failed = True
            raise OSError("injected promotion failure")
        return real_replace(source, destination)

    monkeypatch.setattr(collector.os, "replace", flaky_replace)
    assert collector.collect() == 1
    assert failed is True
    assert _asset_snapshot(collector.ASSETS) == before


def test_keyboard_interrupt_during_promotion_restores_old_bundle(
    collector, monkeypatch: pytest.MonkeyPatch
):
    for group in ("a", "b"):
        (collector.ASSETS / group).mkdir()
        (collector.ASSETS / group / "old.txt").write_text(f"old {group}\n", encoding="utf-8")
    collector.MANIFEST.write_text('{"old": true}\n', encoding="utf-8")
    before = _asset_snapshot(collector.ASSETS)
    monkeypatch.setattr(collector.subprocess, "run", _generator_runner(collector, []))
    real_replace = collector.os.replace
    interrupted = False

    def interrupting_replace(source, destination):
        nonlocal interrupted
        source_path = Path(source)
        if not interrupted and source_path.name == "b" and ".evidence-stage-" in str(source_path):
            interrupted = True
            raise KeyboardInterrupt()
        return real_replace(source, destination)

    monkeypatch.setattr(collector.os, "replace", interrupting_replace)
    with pytest.raises(KeyboardInterrupt):
        collector.collect()
    assert interrupted is True
    assert _asset_snapshot(collector.ASSETS) == before


def test_code_provenance_clean_worktree(collector, monkeypatch: pytest.MonkeyPatch):
    outputs = {
        ("rev-parse", "HEAD"): b"1" * 40 + b"\n",
        ("diff", "--binary", "HEAD", "--"): b"",
        ("ls-files", "--others", "--exclude-standard", "-z"): b"",
    }
    monkeypatch.setattr(collector, "_git_output", lambda args: outputs[tuple(args)])
    assert collector._real_code_provenance() == ("1" * 40, False, None)


def test_code_provenance_dirty_digest_covers_diff_and_untracked_contents(
    collector, monkeypatch: pytest.MonkeyPatch
):
    untracked = collector.REPO / "new.txt"
    untracked.write_text("first\n", encoding="utf-8")
    outputs = {
        ("rev-parse", "HEAD"): b"2" * 40 + b"\n",
        ("diff", "--binary", "HEAD", "--"): b"binary diff bytes\n",
        ("ls-files", "--others", "--exclude-standard", "-z"): b"new.txt\0",
    }
    monkeypatch.setattr(collector, "_git_output", lambda args: outputs[tuple(args)])

    revision, dirty, first_digest = collector._real_code_provenance()
    assert revision == "2" * 40
    assert dirty is True
    assert collector._valid_sha256(first_digest)
    assert collector._real_code_provenance()[2] == first_digest

    untracked.write_text("second\n", encoding="utf-8")
    assert collector._real_code_provenance()[2] != first_digest

    untracked.write_text("first\n", encoding="utf-8")
    outputs[("diff", "--binary", "HEAD", "--")] = b"different diff bytes\n"
    assert collector._real_code_provenance()[2] != first_digest


def test_code_provenance_failure_is_fatal(collector, monkeypatch: pytest.MonkeyPatch):
    def fail(args):
        raise collector.EvidenceError("git unavailable")

    monkeypatch.setattr(collector, "_git_output", fail)
    with pytest.raises(collector.EvidenceError, match="git unavailable"):
        collector._real_code_provenance()


def test_check_reports_malformed_manifest(collector, capsys: pytest.CaptureFixture[str]):
    collector.MANIFEST.write_text("{broken", encoding="utf-8")
    assert collector.check() == 1
    assert "invalid manifest" in capsys.readouterr().err


def test_check_rejects_unsafe_manifest_path_before_hashing(
    collector, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    _collect_ok(collector, monkeypatch)
    manifest = json.loads(collector.MANIFEST.read_text(encoding="utf-8"))
    manifest["groups"][0]["files"][0]["path"] = "../outside.txt"
    collector.MANIFEST.write_text(json.dumps(manifest), encoding="utf-8")
    assert collector.check() == 1
    assert "unsafe manifest artifact path" in capsys.readouterr().err


@pytest.mark.parametrize(
    "mutation",
    [
        lambda manifest: manifest.update(schema_version=1),
        lambda manifest: manifest.update(collected="2026/07/26"),
        lambda manifest: manifest.update(code_revision="not-a-revision"),
        lambda manifest: manifest.update(code_dirty="false"),
        lambda manifest: manifest.update(code_dirty=True, code_diff_sha256=None),
        lambda manifest: manifest.update(code_dirty=False, code_diff_sha256="f" * 64),
        lambda manifest: manifest["groups"][0].update(command="python wrong.py"),
        lambda manifest: manifest["groups"][0].update(input_fingerprint={"present": True}),
        lambda manifest: manifest["groups"][0]["files"].pop(),
    ],
)
def test_check_rejects_semantic_manifest_drift(
    collector, monkeypatch: pytest.MonkeyPatch, mutation
):
    _collect_ok(collector, monkeypatch)
    manifest = json.loads(collector.MANIFEST.read_text(encoding="utf-8"))
    mutation(manifest)
    collector.MANIFEST.write_text(json.dumps(manifest), encoding="utf-8")
    assert collector.check() == 1


def test_check_still_accepts_the_legacy_raw_byte_schema(
    collector, monkeypatch: pytest.MonkeyPatch
):
    _collect_ok(collector, monkeypatch)
    manifest = json.loads(collector.MANIFEST.read_text(encoding="utf-8"))
    manifest["schema_version"] = 2
    for group in manifest["groups"]:
        for record in group["files"]:
            record["sha256"] = collector.sha256_of(collector.ASSETS / record["path"])
    collector.MANIFEST.write_text(json.dumps(manifest), encoding="utf-8")
    assert collector.check() == 0


def test_content_sha256_ignores_line_endings_but_not_content(collector, tmp_path: Path):
    def digest(name: str, payload: bytes) -> str:
        path = tmp_path / name
        path.write_bytes(payload)
        return collector.content_sha256(path)

    for name, lf, crlf, changed in [
        ("j.json", b'{"a": 1,\n "b": 2}', b'{"a": 1,\r\n "b": 2}', b'{"a": 1, "b": 3}'),
        ("t.toml", b"a = 1\nb = 2\n", b"a = 1\r\nb = 2\r\n", b"a = 1\nb = 3\n"),
        ("t.txt", b"one\ntwo\n", b"one\r\ntwo\r\n", b"one\nthree\n"),
    ]:
        assert digest(name, lf) == digest(name, crlf)
        assert digest(name, lf) != digest(name, changed)

    assert digest("f.png", b"\x89PNG\x00\x01") != digest("f.png", b"\x89PNG\x00\x02")

    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(collector.EvidenceError, match="unparsable"):
        collector.content_sha256(tmp_path / "bad.json")


def test_content_sha256_canonicalizes_toml_temporal_values(collector, tmp_path: Path):
    """tomllib yields datetime/date/time objects; pre-repair those escaped json.dumps as a raw
    TypeError. They must canonicalize deterministically, stay EOL-independent, and stay
    content-sensitive."""
    def digest(payload: bytes) -> str:
        path = tmp_path / "temporal.toml"
        path.write_bytes(payload)
        return collector.content_sha256(path)

    lf = b"day = 2026-07-30\nstamp = 2026-07-30T12:34:56\nclock = 12:34:56\n"
    crlf = lf.replace(b"\n", b"\r\n")
    changed = lf.replace(b"T12:34:56", b"T12:34:57")
    assert digest(lf) == digest(crlf)
    assert digest(lf) != digest(changed)


@pytest.mark.parametrize("name, payload", [
    ("nan.json", b'{"a": NaN}'),
    ("inf.json", b'{"a": Infinity}'),
    ("nan.toml", b"a = nan\n"),
    ("inf.toml", b"a = -inf\n"),
])
def test_content_sha256_rejects_nonfinite_numbers(collector, tmp_path: Path, name, payload):
    # Canonical structured identity must not admit content the rest of ISAR treats as invalid
    # fingerprint input — and the failure is a clean EvidenceError, never a traceback.
    path = tmp_path / name
    path.write_bytes(payload)
    with pytest.raises(collector.EvidenceError):
        collector.content_sha256(path)


def test_canonical_structured_value_never_leaks_typeerror(collector):
    with pytest.raises(collector.EvidenceError, match="unsupported"):
        collector._canonical_structured_value({"key": object()})


def test_check_retains_changed_missing_and_extra_diagnostics(
    collector, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    _collect_ok(collector, monkeypatch)
    (collector.ASSETS / "a" / "result.txt").write_text("changed\n", encoding="utf-8")
    (collector.ASSETS / "b" / "result.csv").unlink()
    (collector.ASSETS / "a" / "extra.txt").write_text("extra\n", encoding="utf-8")
    assert collector.check() == 1
    errors = capsys.readouterr().err
    assert "changed since collection: a/result.txt" in errors
    assert "missing manifest artifact" in errors
    assert "present but unlisted: a/extra.txt" in errors
