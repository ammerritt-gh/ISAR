"""Routine regression checks for the Windows installer scripts (installer/*.bat).

Each check guards one trap TAVI's installer hit (or one rule of plan D2/D3). The static rules are
functions over the script text, and each is shown to fire on a broken snippet, so a rule that can
no longer fail is caught too. The dynamic checks run the real scripts through ``cmd /d /s /c`` with
LOCALAPPDATA, APPDATA, TEMP and the Desktop moved into ``tmp_path`` (TEMP with a space and an
accented letter), on fake installations; nothing here touches the network or a real profile.
"""
import hashlib
import os
import re
import shutil
import subprocess
import tomllib
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="the installer is Windows batch")

REPO = Path(__file__).resolve().parents[2]
INSTALLER = REPO / "installer"
ENGINE = INSTALLER / "WINDOWS-install-ISAR.bat"
LAUNCHER = INSTALLER / "launchers" / "ISAR-Launcher.bat"
RUNNER = INSTALLER / "launchers" / "run-isar.bat"
BATS = sorted(INSTALLER.rglob("*.bat"))
PLAIN = re.compile(r"[A-Za-z]:\\[A-Za-z0-9._\\-]+")
ID = "0123456789abcdef0123456789abcdef"
COMMIT = "89abcdef" * 5


def _text(path):
    return path.read_bytes().decode("ascii")


def _code_lines(text):
    """Lines that are batch code, not a ``::`` or ``rem`` comment."""
    return [ln for ln in text.splitlines() if not re.match(r"\s*(::|rem\b)", ln, re.I)]


# ---------------------------------------------------------------- static rules

def crlf_ascii_problems(data: bytes):
    problems = []
    if any(b > 127 for b in data):
        problems.append("non-ASCII byte")
    if re.search(rb"(?<!\r)\n", data):
        problems.append("a line ending that is not CRLF")
    return problems


def d2_offenders(text):
    """No pipe, no command-form for /f, child cmd only with /d, powershell only non-interactive."""
    bad = []
    for ln in _code_lines(text):
        if "|" in ln:
            bad.append(("pipe", ln))
        if re.search(r"(?i)\bfor\s+/f\b.*\bin\s*\(\s*['`]", ln):
            bad.append(("command-form for /f", ln))
        if re.search(r"(?i)(?<![\w.\\-])cmd(\.exe)?\s+/(?!d\b)", ln):
            bad.append(("cmd without /d first", ln))
        if re.search(r"(?i)(^|[\s('])powershell(\.exe)?\s+-", ln) and not (
                re.search(r"(?i)-NoProfile\b", ln) and re.search(r"(?i)-NonInteractive\b", ln)):
            bad.append(("powershell without -NoProfile -NonInteractive", ln))
    return bad


def registry_offenders(text):
    return [ln for ln in _code_lines(text) if re.search(r"(?i)(^|[\s&(])(reg(\.exe)?\s|setx\b)", ln)]


def echo_redirect_offenders(text):
    """``echo X=%V%>>file`` turns a trailing digit of V into a stream number (TAVI's LAYOUT loss)."""
    bad = []
    for ln in _code_lines(text):
        m = re.search(r"(?i)\becho[\s.(]", ln)
        if m and ">" in ln[m.end():]:
            bad.append(ln)
    return bad


_LABEL = re.compile(r"(?m)^\s*:([A-Za-z_][A-Za-z0-9_-]*)\s*$")
_TARGET = re.compile(r"(?i)\b(?:goto\s+:?|call\s+:)([A-Za-z_][A-Za-z0-9_-]*)")


def missing_labels(text):
    labels = {m.group(1).lower() for m in _LABEL.finditer(text)} | {"eof"}
    targets = {m.group(1).lower() for ln in _code_lines(text) for m in _TARGET.finditer(ln)}
    return sorted(targets - labels)


def micromamba_offenders(text):
    """Every micromamba call names its root (-r) and prefix (-p) as quoted paths, never a name."""
    bad = []
    for ln in _code_lines(text):
        m = re.search(r'"%MM_EXE%"\s+(.*)', ln)
        if not m or not re.match(r"(create|install|update|remove|run|env)\b", m.group(1)):
            continue
        args = m.group(1)
        if re.search(r"(^|\s)(-n|--name)(\s|=)", args) or not re.search(r'-r\s+"[^"]+"', args) \
                or not re.search(r'-p\s+"[^"]+"', args):
            bad.append(ln)
    return bad


def long_path_offenders(text):
    """ISAR's source holds paths past 260 characters once under <base>\\app: git needs
    core.longpaths on every call, and a recursive removal goes through the \\\\?\\ form."""
    bad = []
    for ln in _code_lines(text):
        if re.match(r'\s*"%GIT%"\s', ln) and not re.match(r'\s*"%GIT%" -c core\.longpaths=true\s', ln):
            bad.append(ln)
        # the private working and staging folders are the scripts' own, under %TEMP%
        if re.search(r'(?i)\brd\s+/s\b', ln) and not re.search(r"%(WORK|STAGE)%", ln) and '"\\\\?\\' not in ln:
            bad.append(ln)
    return bad


def test_static_rules_fire_on_broken_snippets():
    assert long_path_offenders('"%GIT%" -C "%APP%" checkout -q --detach --force %COMMIT%')
    assert long_path_offenders('if exist "%BASE%\\app\\" rd /s /q "%BASE%\\app" 2>nul')
    assert not long_path_offenders('"%GIT%" -c core.longpaths=true -C "%APP%" init -q\r\n'
                                   'if exist "%RM_PATH%\\" rd /s /q "\\\\?\\%RM_PATH%" 2>nul')
    assert crlf_ascii_problems(b"@echo off\nset A=1\r\n")
    assert crlf_ascii_problems("echo J\u00f6rg\r\n".encode("utf-8"))
    assert d2_offenders('set X| findstr /x "X=1" >nul\r\n')
    assert d2_offenders("for /f \"delims=\" %%S in ('powershell -NoProfile -NonInteractive -Command x') do set S=%%S")
    assert d2_offenders('start "" /b cmd /c "%HB%"')
    assert d2_offenders('powershell -NoProfile -Command "x" > "%F%"')
    assert not d2_offenders('cmd /d /c "%X%"\r\n:: a | comment\r\nfor /f "usebackq" %%A in ("%F%") do set A=%%A')
    assert registry_offenders('reg delete "HKCU\\Software\\Microsoft\\Command Processor" /v AutoRun /f')
    assert registry_offenders("setx PATH x")
    assert echo_redirect_offenders('echo LAYOUT=%LAYOUT%>> "%RF%"')
    assert not echo_redirect_offenders('>>"%RF%" echo(LAYOUT=%LAYOUT%')
    assert missing_labels("goto nowhere\r\n:somewhere\r\n") == ["nowhere"]
    assert micromamba_offenders('"%MM_EXE%" create -y -n isar -f "%L%"')
    assert micromamba_offenders('"%MM_EXE%" create -y -p "%B%\\env" -f "%L%"')
    assert not micromamba_offenders('"%MM_EXE%" create -y --no-rc -r "%B%\\mamba" -p "%B%\\env" -f "%L%"')


@pytest.mark.parametrize("path", BATS, ids=lambda p: p.name)
def test_bat_is_ascii_crlf_and_follows_the_batch_rules(path):
    data = path.read_bytes()
    assert not crlf_ascii_problems(data), path
    text = data.decode("ascii")
    assert text.startswith("@echo off\r\nsetlocal DisableDelayedExpansion\r\n"), path
    assert not d2_offenders(text), d2_offenders(text)
    assert not registry_offenders(text)
    assert not echo_redirect_offenders(text), echo_redirect_offenders(text)
    assert not missing_labels(text), missing_labels(text)
    assert not micromamba_offenders(text), micromamba_offenders(text)
    assert not long_path_offenders(text), long_path_offenders(text)


def test_bat_files_are_crlf_in_the_index():
    """Raw GitHub downloads serve the stored bytes (D3), so the index itself must hold CRLF."""
    out = subprocess.run(["git", "-C", str(REPO), "ls-files", "--eol", "--", "installer"],
                         capture_output=True, text=True, check=True).stdout
    eol = {ln.split("\t", 1)[1]: ln.split()[0] for ln in out.splitlines() if "\t" in ln}
    for path in BATS:
        rel = path.relative_to(REPO).as_posix()
        assert eol.get(rel) == "i/crlf", f"{rel}: {eol.get(rel, 'not in the index')}"


def test_lock_format_check_accepts_the_lock_as_served(tmp_path):
    """The engine's own @EXPLICIT check, run on the committed lock's bytes (stored and served LF):
    findstr /x and $ match only before a CR, so an exact-line check refused every real lock."""
    line = next(ln for ln in _code_lines(_text(ENGINE)) if "@EXPLICIT" in ln and "findstr" in ln)
    lock = (INSTALLER / "isar-win-64.lock").read_bytes()
    assert b"\r" not in lock  # the case under test: the lock as raw GitHub serves it
    work = tmp_path / "wörk dir"
    work.mkdir()
    probe = tmp_path / "probe.bat"
    probe.write_bytes(("@echo off\r\nsetlocal DisableDelayedExpansion\r\n%s\r\n"
                       "if errorlevel 1 (echo REFUSED) else echo PASSED\r\n" % line).encode("ascii"))
    env = _sandbox(tmp_path, WORK=str(work))
    for content, want in ((lock, "PASSED"), (b"# platform: win-64\nhttps://x/a.conda#00\n", "REFUSED")):
        (work / "isar-win-64.lock").write_bytes(content)
        rc, out = run_bat(probe, env=env)
        assert out.strip() == want, (line, out)


def test_remove_entry_removes_a_tree_past_260_characters(tmp_path):
    """The engine's own :remove_entry, run on an app folder whose deepest path passes 260
    characters, and on an env that is a junction (the junction goes, its target stays)."""
    text = _text(ENGINE)
    start = text.index("\r\n:remove_entry\r\n") + 2
    helper = text[start:text.index("goto :eof\r\n", start) + len("goto :eof\r\n")]
    base = _plain(tmp_path) / "b"
    deep = "\\\\?\\" + str(base / "app")
    for i in range(6):
        deep += "\\" + f"d{i}" * 25
    os.makedirs(deep)
    with open(deep + "\\f.txt", "w", encoding="utf-8") as fh:
        fh.write("x")
    assert len(deep) > 300
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.txt").write_text("not ISAR's\n", encoding="utf-8")
    mk = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(base / "env"), str(target)],
                        capture_output=True, text=True)
    assert mk.returncode == 0, mk.stdout + mk.stderr
    probe = tmp_path / "probe.bat"
    probe.write_bytes(("@echo off\r\nsetlocal DisableDelayedExpansion\r\nset \"GB=%s\"\r\n"
                       "call :remove_entry app\r\ncall :remove_entry env\r\nexit /b 0\r\n%s"
                       % (base, helper)).encode("ascii"))
    rc, out = run_bat(probe, env=_sandbox(tmp_path))
    assert rc == 0, out
    assert sorted(p.name for p in base.iterdir()) == [], out
    assert (target / "keep.txt").exists()


def test_release_constant_matches_pyproject():
    version = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    assert re.search(r'(?m)^set "ISAR_RELEASE=v%s"\r?$' % re.escape(version), _text(ENGINE))


def launcher_problems(text):
    """Menu choices by exact code (choice gives 255 on error, 0 on Ctrl-C: an "if errorlevel" ladder
    sends both into its top option), and Uninstall handed over without call or start (no return into
    a file being deleted, no second window)."""
    code = _code_lines(text)
    bad = [ln for ln in code if re.search(r"(?i)\bif\s+(not\s+)?errorlevel\s+\d+\s+goto\s+(menu_|quit)", ln)]
    if 'set "PICK=%ERRORLEVEL%"' not in text or any(f'if "%PICK%"=="{n}" goto' not in text for n in "12345"):
        bad.append("menu does not compare exact choice codes")
    handover = [ln for ln in code if "/uninstall" in ln and "%ENGINE%" in ln]
    bad += [ln for ln in handover if re.search(r"(?i)\b(call|start)\b", ln)]
    return bad if handover else bad + ["no Uninstall handover"]


def test_launcher_menu_compares_exact_codes_and_hands_over_uninstall():
    text = _text(LAUNCHER)
    assert not launcher_problems(text), launcher_problems(text)
    assert launcher_problems(text.replace('if "%PICK%"=="4" goto menu_uninstall', "if errorlevel 4 goto menu_uninstall"))
    assert launcher_problems(text.replace('"%ENGINE%" /uninstall', 'call "%ENGINE%" /uninstall'))


# ---------------------------------------------------------------- running the scripts

SANDBOXED = ("USERPROFILE", "LOCALAPPDATA", "APPDATA", "TEMP", "TMP", "ISAR_TEST_DESKTOP", "ISAR_EXPERIMENTS_ROOT")


def _sandbox(tmp_path, **extra):
    root = tmp_path / "Zo\u00eb user"
    env = {k.upper(): v for k, v in os.environ.items()}  # one entry per name: Windows names ignore case
    # USERPROFILE stays plain: the system-folder refusal test names it as a path the installer must refuse.
    paths = {"LOCALAPPDATA": root / "local", "APPDATA": root / "roaming", "TEMP": root / "T mp",
             "TMP": root / "T mp", "ISAR_TEST_DESKTOP": root / "Desktop",
             "ISAR_EXPERIMENTS_ROOT": root / "Documents" / "ISAR", "USERPROFILE": tmp_path / "profile"}
    for key, path in paths.items():
        path.mkdir(parents=True, exist_ok=True)
        env[key] = str(path)
    env.update({k.upper(): v for k, v in extra.items()})
    # The real profile holds the operator's GUI settings: a routine run must never reach it.
    for key in SANDBOXED:
        assert Path(env[key]).is_relative_to(tmp_path), (key, env[key])
    return env


def test_sandbox_moves_every_profile_path_under_tmp_path(tmp_path):
    env = _sandbox(tmp_path)
    for key in SANDBOXED:
        assert Path(env[key]).is_relative_to(tmp_path), (key, env[key])


def run_bat(script, *args, env, timeout=90):
    line = 'cmd /d /s /c ""%s" %s"' % (script, " ".join('"%s"' % a for a in args))
    r = subprocess.run(line, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
    return r.returncode, r.stdout.decode("oem", errors="replace") + r.stderr.decode("oem", errors="replace")


def _plain(path):
    if not PLAIN.fullmatch(str(path)):
        pytest.skip(f"tmp_path {path} is not a plain path the installer accepts")
    return path


ACCEPT = [r"C:\ISAR", "C:\\ISAR\\", r"C:\ISAR-test_1.2\sub"]
REFUSE = [
    (r"C:\ISAR Data", "space"), ("C:\\J\u00f6rg", "accented lowercase"), ("C:\\ISAR-\u00e9", "accented"),
    ("C:\\ISAR-\u0151", "letter outside the console code page"), ("C:\\ISAR\u20ac", "euro sign"),
    (r"C:\ISAR\..\x", "dot-dot"), (r"\\server\share\ISAR", "UNC"), (r"\\.\C:\ISAR", "device"),
    (r"\\?\C:\ISAR", "long-path prefix"), ("C:\\", "drive root"), ("C:", "bare drive"), ("ISAR", "relative"),
    ('C:\\ISAR"', "quote"), ('"C:\\ISAR"', "surrounding quotes"), (r"C:\ISAR&rem", "ampersand"),
    (r"C:\ISAR^x", "caret"), (r"C:\ISAR(old)", "parentheses"), (r"C:\a:b", "second colon"),
    (r"C:\ISAR.", "trailing dot"), (r"C:\a\\b", "empty component"),
]


@pytest.mark.parametrize("value", ACCEPT)
def test_validate_only_accepts_plain_paths(tmp_path, value):
    rc, out = run_bat(ENGINE, "/validate-only", value, env=_sandbox(tmp_path))
    assert (rc, out.strip()) == (0, "ACCEPT"), out


def test_validate_only_accepts_an_existing_plain_folder(tmp_path):
    target = _plain(tmp_path) / "existing"
    target.mkdir()
    rc, out = run_bat(ENGINE, "/validate-only", str(target), env=_sandbox(tmp_path))
    assert (rc, out.strip()) == (0, "ACCEPT"), out


@pytest.mark.parametrize("value,label", REFUSE, ids=[c[1] for c in REFUSE])
def test_validate_only_refuses(tmp_path, value, label):
    rc, out = run_bat(ENGINE, "/validate-only", value, env=_sandbox(tmp_path))
    assert rc == 2 and out.startswith("REFUSE "), (label, out)


@pytest.mark.parametrize("which", ["profile", "windows", "inside-windows"])
def test_validate_only_refuses_system_folders(tmp_path, which):
    # Program Files cannot be faked here (Windows resets ProgramFiles for each new process) and its
    # real path holds a space; "inside-windows" exercises the same inside-a-system-folder check.
    env = _sandbox(tmp_path)
    value = {"profile": env["USERPROFILE"], "windows": os.environ["SystemRoot"],
             "inside-windows": os.environ["SystemRoot"] + r"\ISAR"}[which]
    if which == "profile" and not PLAIN.fullmatch(value):
        pytest.skip("the profile path is refused for its characters already")
    rc, out = run_bat(ENGINE, "/validate-only", value, env=env)
    assert rc == 2 and out.startswith("REFUSE "), out


def test_validate_only_refuses_a_junction_on_the_path(tmp_path):
    real = _plain(tmp_path) / "real"
    real.mkdir()
    link = tmp_path / "link"
    mk = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(link), str(real)], capture_output=True, text=True)
    assert mk.returncode == 0, mk.stdout + mk.stderr
    rc, out = run_bat(ENGINE, "/validate-only", str(link / "ISAR"), env=_sandbox(tmp_path))
    assert rc == 2 and "junction" in out, out


# ---------------------------------------------------------------- ownership on fake folders

def _record(base, **over):
    rec = dict(LAYOUT="1", INSTALL_ID=ID, STATE="complete", ISAR_BASE=str(base), INSTALL_KIND="test",
               ISAR_RELEASE="v0.1.0", SOURCE_REF="acs/job-branch", SOURCE_COMMIT=COMMIT, LOCK_SHA256="ab" * 32,
               MAMBA_SHA256="cd" * 32, MAMBA_VERSION="2.5.0-1", INSTALLER_BUILD="1",
               INSTALLED_AT="2026-10-29T12:00:00")
    rec.update(over)
    return "".join(f"{k}={v}\r\n" for k, v in rec.items() if v is not None)


def fake_install(base, marker_id=ID, record=True, **over):
    base.mkdir(parents=True, exist_ok=True)
    if marker_id is not None:
        (base / ".isar-install-root").write_bytes(f"INSTALL_ID={marker_id}\r\n".encode())
    if record:
        (base / "INSTALL_INFO.txt").write_bytes(_record(base, **over).encode())
    return base


def inspect(tmp_path, base):
    rc, out = run_bat(ENGINE, "/inspect", str(base), env=_sandbox(tmp_path))
    assert rc == 0, out
    fields = dict(ln.split("=", 1) for ln in out.splitlines() if "=" in ln)
    return fields.get("OWNERSHIP"), fields.get("STATE")


def test_inspect_absent_and_empty_are_none(tmp_path):
    base = _plain(tmp_path) / "b"
    assert inspect(tmp_path, base) == ("none", None)
    base.mkdir()
    (base / "my-notes.txt").write_text("unrelated\n", encoding="utf-8")
    assert inspect(tmp_path, base) == ("none", None)


def test_inspect_owned_pair_reports_its_state(tmp_path):
    base = fake_install(_plain(tmp_path) / "b", STATE="installing")
    (base / "env").mkdir()
    assert inspect(tmp_path, base) == ("owned", "installing")


@pytest.mark.parametrize("marker,record", [(True, False), (False, True)], ids=["marker-alone", "record-alone"])
def test_inspect_half_a_pair_is_partial(tmp_path, marker, record):
    base = fake_install(_plain(tmp_path) / "b", marker_id=ID if marker else None, record=record)
    assert inspect(tmp_path, base) == ("partial", None)


@pytest.mark.parametrize("case", ["partial-beside-env", "mismatched-ids", "bad-commit", "base-elsewhere",
                                  "bad-marker", "payload-without-records"])
def test_inspect_foreign(tmp_path, case):
    base = _plain(tmp_path) / "b"
    if case == "partial-beside-env":
        fake_install(base, record=False)
        (base / "env").mkdir()
    elif case == "mismatched-ids":
        fake_install(base, marker_id="f" * 32)
    elif case == "bad-commit":
        fake_install(base, SOURCE_COMMIT="main & echo hacked")
    elif case == "base-elsewhere":
        fake_install(base, ISAR_BASE=str(tmp_path / "other"))
    elif case == "bad-marker":
        fake_install(base, marker_id="1-2-3-4")
    else:
        (base / "app").mkdir(parents=True)
    assert inspect(tmp_path, base)[0] == "foreign"


# ---------------------------------------------------------------- arguments and the launcher

def _logs(env):
    return sorted((Path(env["LOCALAPPDATA"]) / "ISAR" / "logs").glob("install-*.log"))


@pytest.mark.parametrize("args,why", [
    (["/test-fail", "selftest"], "test switch without /ref"),
    (["/ref", "main&echo x"], "ref outside its grammar"),
    (["/bogus"], "unknown switch"),
    (["/desktop", "maybe"], "desktop value"),
])
def test_install_refuses_bad_arguments_before_anything(tmp_path, args, why):
    env = _sandbox(tmp_path)
    base = _plain(tmp_path) / "isar-b"
    rc, out = run_bat(ENGINE, "/dir", str(base), "/yes", *args, env=env)
    assert rc == 2, (why, out)
    assert not base.exists(), why
    log = _logs(env)[-1].read_text(encoding="utf-8", errors="replace").splitlines()
    assert log[-1] == "OUTCOME=refused" and not any(ln.startswith("STEP=") for ln in log), (why, log)


def test_launcher_start_without_an_environment_says_repair_and_logs(tmp_path):
    env = _sandbox(tmp_path)
    base = fake_install(_plain(tmp_path) / "b")
    (base / "launcher").mkdir()
    for src in (LAUNCHER, RUNNER):
        dst = base / ("ISAR-Launcher.bat" if src == LAUNCHER else "launcher/run-isar.bat")
        dst.write_bytes(src.read_bytes())
    logs = Path(env["LOCALAPPDATA"]) / "ISAR" / "logs"
    logs.mkdir(parents=True)
    for i in range(12):  # older launch logs: only the ten newest may stay
        old = logs / f"launch-20200101-0000{i:02d}-0000000{i % 10}.log"
        old.write_text("old\n", encoding="utf-8")
        os.utime(old, (1_600_000_000 + i, 1_600_000_000 + i))
    rc, out = run_bat(base / "ISAR-Launcher.bat", "start", env=env)
    assert rc == 1 and "Repair" in out, out
    launch = sorted(logs.glob("launch-*.log"))
    assert len(launch) == 10
    newest = max(launch, key=lambda p: p.stat().st_mtime)
    assert "pythonw.exe is missing" in newest.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------- Repair and Collect diagnostics

COLLECTOR = INSTALLER / "launchers" / "collect-diagnostics.bat"


def _launcher_copies(base):
    (base / "launcher").mkdir(exist_ok=True)
    (base / "ISAR-Launcher.bat").write_bytes(LAUNCHER.read_bytes())
    (base / "launcher" / "WINDOWS-install-ISAR.bat").write_bytes(ENGINE.read_bytes())
    (base / "launcher" / "collect-diagnostics.bat").write_bytes(COLLECTOR.read_bytes())


def test_collect_diagnostics_works_with_the_environment_gone(tmp_path):
    env = _sandbox(tmp_path)
    base = fake_install(_plain(tmp_path) / "b")
    _launcher_copies(base)
    local, roaming = Path(env["LOCALAPPDATA"]), Path(env["APPDATA"])
    (local / "ISAR" / "logs").mkdir(parents=True)
    (local / "ISAR" / "logs" / "install-20261029-120000-0a1b2c3d.log").write_text("STEP=env-create failed\n")
    (local / "ISAR" / "install-record.txt").write_text(f"ISAR_BASE={base}\n")
    (roaming / "isar_gui").mkdir()
    (roaming / "isar_gui" / "isar-gui.log").write_text("gui log\n")
    rc, out = run_bat(base / "ISAR-Launcher.bat", "diagnostics", "/yes", env=env)
    assert rc == 0, out
    zips = list((local / "ISAR" / "diagnostics").glob("isar-diagnostics-*.zip"))
    assert len(zips) == 1 and zips[0].name in out, out
    with zipfile.ZipFile(zips[0]) as z:
        names = {n.replace("\\", "/").rsplit("/", 1)[-1]: n for n in z.namelist()}
        summary = z.read(names["summary.txt"]).decode("oem")
    for want in ("INSTALL_INFO.txt", ".isar-install-root", "install-record.txt", "isar-gui.log",
                 "install-20261029-120000-0a1b2c3d.log"):
        assert want in names, (want, sorted(names))
    assert "env\\python.exe: missing" in summary, summary
    assert not list(Path(env["TEMP"]).glob("isar-diagnostics-*")), "the staging folder was left behind"


def test_start_checks_isar_is_installed_before_it_starts_the_gui():
    """Static: an environment without ISAR in it is refused before pythonw runs; otherwise the GUI dies
    with no window while the launch log says it started. (The dynamic case is below.)"""
    text = _text(RUNNER)
    assert "Lib\\site-packages\\isar-*.dist-info" in text
    assert text.index("isar-*.dist-info") < text.index('start "" "%ENVP%\\pythonw.exe"')


def test_start_on_an_unfinished_install_says_repair_and_does_not_start(tmp_path):
    """An update or Repair stopped after the environment was built, before ISAR went in: Start says so,
    names Repair and does not start pythonw (the stub is never run)."""
    env = _sandbox(tmp_path)
    base = fake_install(_plain(tmp_path) / "b")
    (base / "launcher").mkdir()
    (base / "env").mkdir()
    (base / "env" / "pythonw.exe").write_bytes(b"x")  # present, but ISAR was never put in
    runner = base / "launcher" / "run-isar.bat"
    runner.write_bytes(RUNNER.read_bytes())
    rc, out = run_bat(runner, env=env)
    assert rc == 1 and "Repair" in out, out
    newest = max((Path(env["LOCALAPPDATA"]) / "ISAR" / "logs").glob("launch-*.log"), key=lambda p: p.stat().st_mtime)
    log = newest.read_text(encoding="utf-8", errors="replace")
    assert "is not installed" in log and "start returned" not in log, log


def test_release_checklist_copies_the_engine_over_the_uninstaller():
    """Static: the uninstaller is a byte copy of the engine, so the checklist's version bump must
    carry the copy too, or the byte-identity test fails on the next gate."""
    text = " ".join((INSTALLER / "ISAR-Installation-README.md").read_text(encoding="utf-8").split())
    step = text[text.index("1. Set the release version"):text.index("2. Regenerate the lock")]
    assert "copy `installer\\WINDOWS-install-ISAR.bat` over `installer\\WINDOWS-uninstall-ISAR.bat`" in step, step


def test_collect_diagnostics_attended_result_is_not_exited_past():
    """Static: an attended run must reach its Explorer window and pause. Written as ``if defined YES endlocal
    & exit`` the if binds only endlocal, so the exit runs either way; the group keeps both under the if.
    (Attended runs open Explorer and wait for a key, so no routine test runs that path.)"""
    text = _text(COLLECTOR)
    assert not re.search(r"(?i)\bif\b[^&(]*\sendlocal\s*&", text), "an if binds only endlocal"
    assert "if defined YES (endlocal & exit /b 0)" in text and "if defined YES (endlocal & exit /b 1)" in text


def repairable(base, **over):
    """A fake complete installation whose kept lock and micromamba match the record."""
    base.mkdir(parents=True)
    (base / "launcher").mkdir()
    (base / "micromamba").mkdir()
    lock, mm = b"# platform: win-64\n@EXPLICIT\n", b"not really micromamba"
    (base / "launcher" / "isar-win-64.lock").write_bytes(lock)
    (base / "micromamba" / "micromamba.exe").write_bytes(mm)
    (base / "env").mkdir()
    (base / "env" / "python.exe").write_bytes(b"x")
    (base / "app").mkdir()
    fake_install(base, LOCK_SHA256=hashlib.sha256(lock).hexdigest(), MAMBA_SHA256=hashlib.sha256(mm).hexdigest(),
                 **over)
    _launcher_copies(base)
    return base


def _tree(path):
    return sorted((str(p.relative_to(path)), p.stat().st_size, p.stat().st_mtime_ns)
                  for p in path.rglob("*") if not p.is_dir())


@pytest.mark.parametrize("case", ["no-dir", "not-complete", "test-fail-on-release", "lock-changed",
                                  "workspace-in-env", "env-is-a-junction", "isar-running"])
def test_repair_stops_before_removing_anything(tmp_path, case):
    """Repair (through the launcher) refuses or fails with the installation untouched whenever its
    ownership, its kept files, a workspace inside env, a link, or a running ISAR says so."""
    env = _sandbox(tmp_path)
    over = {"not-complete": {"STATE": "installing"}, "test-fail-on-release": {"INSTALL_KIND": "release"}}
    base = repairable(_plain(tmp_path) / "b", **over.get(case, {}))
    args, want_rc, want_line, proc = ["repair", "/yes"], 2, None, None
    outside = tmp_path / "outside"
    if case == "no-dir":
        script, args = base / "launcher" / "WINDOWS-install-ISAR.bat", ["/repair", "/yes"]
    else:
        script = base / "ISAR-Launcher.bat"
    if case == "test-fail-on-release":
        args += ["/test-fail", "selftest"]
    elif case == "lock-changed":
        (base / "launcher" / "isar-win-64.lock").write_bytes(b"# platform: win-64\n@EXPLICIT\nx\n")
        want_rc = 1
    elif case == "workspace-in-env":
        (base / "env" / "my run").mkdir()
        (base / "env" / "my run" / "card.policy.toml").write_text("# a user's workspace\n")
        want_line = "WORKSPACE_FOUND=" + str(base / "env" / "my run")
    elif case == "env-is-a-junction":
        (base / "env" / "python.exe").unlink()
        (base / "env").rmdir()
        outside.mkdir()
        (outside / "keep.txt").write_text("not ISAR's\n")
        mk = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(base / "env"), str(outside)],
                            capture_output=True, text=True)
        assert mk.returncode == 0, mk.stdout + mk.stderr
    elif case == "isar-running":
        shutil.copyfile(Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe", base / "env" / "cmd.exe")
        proc = subprocess.Popen([str(base / "env" / "cmd.exe"), "/d", "/c", "ping -n 60 127.0.0.1 >nul"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=0x08000000)
        want_line = f"ISAR_RUNNING={proc.pid}"
    before = _tree(base)
    try:
        rc, out = run_bat(script, *args, env=env)
    finally:
        if proc:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
            proc.wait(timeout=30)
    log = sorted((Path(env["LOCALAPPDATA"]) / "ISAR" / "logs").glob("repair-*.log"))[-1]
    lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    assert rc == want_rc, (case, rc, out)
    assert lines[-1] == ("OUTCOME=failed" if want_rc == 1 else "OUTCOME=refused"), (case, lines[-5:])
    assert not any(ln.startswith("STEP=remove-") for ln in lines), case
    if want_line:
        assert want_line in lines, (case, [ln for ln in lines if "=" in ln][-8:])
    reason = {"no-dir": "needs the installation folder", "not-complete": "never finished installing",
              "test-fail-on-release": "only on a test installation", "lock-changed": "is not the one",
              "workspace-in-env": "a workspace is saved", "env-is-a-junction": "junction or link",
              "isar-running": "is running"}[case]
    assert any(ln.startswith("REASON: ") and reason in ln for ln in lines), (case, lines[-3:])
    assert _tree(base) == before, case
    if case == "env-is-a-junction":
        assert (outside / "keep.txt").exists()


# ---------------------------------------------------------------- Uninstall, replacement and relocation

UNINSTALLER = INSTALLER / "WINDOWS-uninstall-ISAR.bat"
PAYLOAD = [".isar-install-root", "INSTALL_INFO.txt", "ISAR-Launcher.bat", "ISAR Launcher.lnk", "launcher",
           "tracked-workspace-markers.txt", "micromamba", "mamba", "env", "app"]


def _shortcut(lnk, target):
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:T_LNK); $s.TargetPath=$env:T_TGT; $s.Save()")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, text=True,
                       env={**os.environ, "T_LNK": str(lnk), "T_TGT": str(target)})
    assert r.returncode == 0 and Path(lnk).exists(), r.stderr


def full_fake(base, tmp_path, **over):
    """A complete fake installation: every payload entry, the engine copy in launcher\\, an app
    tree with a path past 260 characters, a tracked marker listed as ISAR's own, and a junction
    inside app pointing at a folder outside (which must survive)."""
    repairable(base, **over)
    for name in ("mamba", "micromamba"):
        (base / name).mkdir(exist_ok=True)
        (base / name / "x.bin").write_bytes(b"x")
    deep = "\\\\?\\" + str(base / "app")
    for i in range(6):
        deep += "\\" + f"d{i}" * 25
    os.makedirs(deep)
    with open(deep + "\\f.txt", "w", encoding="utf-8") as fh:
        fh.write("x")
    tracked = base / "app" / "docs" / "campaigns" / "c1"
    tracked.mkdir(parents=True)
    (tracked / "card.policy.toml").write_text("# ISAR's own fixture\n", encoding="utf-8")
    (base / "tracked-workspace-markers.txt").write_text("docs\\campaigns\\c1\\card.policy.toml\r\n", encoding="utf-8")
    outside = tmp_path / "outside-target"
    outside.mkdir(exist_ok=True)
    (outside / "keep.txt").write_text("not ISAR's\n", encoding="utf-8")
    mk = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(base / "app" / "linked"), str(outside)],
                        capture_output=True, text=True)
    assert mk.returncode == 0, mk.stdout + mk.stderr
    _shortcut(base / "ISAR Launcher.lnk", base / "ISAR-Launcher.bat")
    return base


def _locator(env, base):
    loc = Path(env["LOCALAPPDATA"]) / "ISAR"
    loc.mkdir(parents=True, exist_ok=True)
    (loc / "install-record.txt").write_text(f"ISAR_BASE={base}\r\nINSTALL_ID={ID}\r\nSTATE=complete\r\n",
                                            encoding="utf-8")
    return loc / "install-record.txt"


def _last_log(env, op):
    logs = sorted((Path(env["LOCALAPPDATA"]) / "ISAR" / "logs").glob(f"{op}-*.log"), key=lambda p: p.stat().st_mtime)
    return logs[-1], logs[-1].read_text(encoding="utf-8", errors="replace").splitlines()


def _no_leftovers(env):
    left = [p.name for p in Path(env["TEMP"]).iterdir() if p.name.startswith(("isar-handoff-", "isar-uninstall-"))]
    assert not left, f"working or hand-off folders left in TEMP: {left}"


def test_standalone_uninstaller_is_the_engine_byte_for_byte():
    assert UNINSTALLER.read_bytes() == ENGINE.read_bytes()


@pytest.mark.parametrize("delete_profile", ["no", "yes"])
def test_uninstall_through_the_launcher_removes_exactly_the_payload(tmp_path, delete_profile):
    """The menu's Uninstall: hand-over to a copy in TEMP, then only the owned payload goes (a tree
    past 260 characters included, a junction's target kept), the unrelated file and the base stay,
    the own Desktop shortcut and the locator go, and the profile and earlier logs go only on yes."""
    env = _sandbox(tmp_path)
    base = full_fake(_plain(tmp_path) / "b", tmp_path)
    (base / "my-notes.txt").write_text("keep me\n", encoding="utf-8")
    loc = _locator(env, base)
    desk = Path(env["ISAR_TEST_DESKTOP"])
    _shortcut(desk / "ISAR Launcher.lnk", base / "ISAR-Launcher.bat")
    profile = Path(env["APPDATA"]) / "isar_gui"
    profile.mkdir()
    (profile / "session.json").write_text('{"recent_workspaces": ["D:\\\\runs\\\\exp1"]}', encoding="utf-8")
    logs = Path(env["LOCALAPPDATA"]) / "ISAR" / "logs"
    logs.mkdir(parents=True)
    (logs / "install-20200101-000000-00000000.log").write_text("old\n", encoding="utf-8")
    rc, out = run_bat(base / "ISAR-Launcher.bat", "uninstall", "/yes", "/delete-profile", delete_profile, env=env)
    log, lines = _last_log(env, "uninstall")
    assert rc == 0 and lines[-1] == "OUTCOME=uninstalled", (out, lines[-6:])
    assert sorted(p.name for p in base.iterdir()) == ["my-notes.txt"], out
    assert (tmp_path / "outside-target" / "keep.txt").exists()
    assert not (desk / "ISAR Launcher.lnk").exists() and not loc.exists()
    assert log.name in out and "D:\\runs\\exp1" in out, out
    for step in ("remove-app", "remove-env", "remove-mamba", "remove-micromamba", "remove-launcher", "remove-records"):
        assert f"STEP={step} ok" in lines, step
    kept = delete_profile == "no"
    assert profile.exists() == kept and (logs / "install-20200101-000000-00000000.log").exists() == kept
    assert log.exists()
    _no_leftovers(env)


@pytest.mark.parametrize("case", ["no-marker", "mismatched-ids", "bad-record", "workspace-in-app",
                                  "workspace-in-profile", "isar-running", "env-is-a-junction"])
def test_uninstall_stops_before_removing_anything(tmp_path, case):
    env = _sandbox(tmp_path)
    base = full_fake(_plain(tmp_path) / "b", tmp_path)
    args, want_line, proc = ["/uninstall", "/dir", str(base), "/yes"], None, None
    if case == "no-marker":
        (base / ".isar-install-root").unlink()
    elif case == "mismatched-ids":
        (base / ".isar-install-root").write_bytes(b"INSTALL_ID=" + b"f" * 32 + b"\r\n")
    elif case == "bad-record":
        (base / "INSTALL_INFO.txt").write_bytes(_record(base, SOURCE_REF="main & echo hacked").encode())
    elif case == "workspace-in-app":
        (base / "app" / "my run").mkdir()
        (base / "app" / "my run" / "card.policy.toml").write_text("# a user's workspace\n")
        want_line = "WORKSPACE_FOUND=" + str(base / "app" / "my run")
    elif case == "workspace-in-profile":
        ws = Path(env["APPDATA"]) / "isar_gui" / "saved"
        ws.mkdir(parents=True)
        (ws / "gui.state.json").write_text("{}")
        args += ["/delete-profile", "yes"]
        want_line = "WORKSPACE_FOUND=" + str(ws)
    elif case == "isar-running":
        shutil.copyfile(Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe", base / "env" / "cmd.exe")
        proc = subprocess.Popen([str(base / "env" / "cmd.exe"), "/d", "/c", "ping -n 60 127.0.0.1 >nul"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=0x08000000)
        want_line = f"ISAR_RUNNING={proc.pid}"
    elif case == "env-is-a-junction":
        shutil.rmtree(base / "env")
        mk = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(base / "env"), str(tmp_path / "outside-target")],
                            capture_output=True, text=True)
        assert mk.returncode == 0, mk.stdout + mk.stderr
    before = _tree(base)
    try:
        rc, out = run_bat(ENGINE, *args, env=env)
    finally:
        if proc:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
            proc.wait(timeout=30)
    _log, lines = _last_log(env, "uninstall")
    assert (rc, lines[-1]) == (2, "OUTCOME=refused"), (case, out, lines[-5:])
    assert not any(ln.startswith("STEP=remove-") for ln in lines), case
    if want_line:
        assert want_line in lines, (case, [ln for ln in lines if "=" in ln][-8:])
    assert _tree(base) == before, case
    assert (tmp_path / "outside-target" / "keep.txt").exists()


def test_standalone_uninstaller_finds_the_installation_from_the_locator(tmp_path):
    """Under its own name, with no /dir and the launcher folder gone, it takes the base from the
    locator and uninstalls under the same rules; it refuses /repair and /ref."""
    env = _sandbox(tmp_path)
    base = full_fake(_plain(tmp_path) / "b", tmp_path)
    shutil.rmtree(base / "launcher")
    loc = _locator(env, base)
    downloads = tmp_path / "Down loads"
    downloads.mkdir()
    uninst = downloads / "WINDOWS-uninstall-ISAR.bat"
    uninst.write_bytes(UNINSTALLER.read_bytes())
    other = tmp_path / "other" / "ISAR-Launcher.bat"  # a Desktop shortcut of the same name, not this installation's
    _shortcut(Path(env["ISAR_TEST_DESKTOP"]) / "ISAR Launcher.lnk", other)
    before = _tree(base)
    for refused in (["/repair", "/dir", str(base), "/yes"], ["/ref", "main", "/yes"]):
        rc, out = run_bat(uninst, *refused, env=env)
        assert rc == 2 and "only uninstalls" in out, (refused, out)
    assert _tree(base) == before
    rc, out = run_bat(uninst, "/yes", env=env)
    _log, lines = _last_log(env, "uninstall")
    assert rc == 0 and lines[-1] == "OUTCOME=uninstalled", (out, lines[-6:])
    assert not base.exists() and not loc.exists(), out
    assert (tmp_path / "outside-target" / "keep.txt").exists()
    assert (Path(env["ISAR_TEST_DESKTOP"]) / "ISAR Launcher.lnk").exists(), "another installation's shortcut went"


def test_uninstall_after_an_interruption_finishes_the_job(tmp_path):
    """STATE=removing with app and env already gone (a stop mid-uninstall) is still owned, and a
    rerun removes the rest."""
    env = _sandbox(tmp_path)
    base = full_fake(_plain(tmp_path) / "b", tmp_path, STATE="removing")
    subprocess.run(["cmd", "/d", "/c", "rd", "/s", "/q", "\\\\?\\" + str(base / "app")], check=True)
    shutil.rmtree(base / "env")
    rc, out = run_bat(ENGINE, "/uninstall", "/dir", str(base), "/yes", env=env)
    assert rc == 0, out
    assert not base.exists(), out


@pytest.mark.parametrize("case", ["same-workspace", "same-running", "relocate-workspace-in-old",
                                  "relocate-inside-old", "from-the-kept-copy"])
def test_replacement_and_relocation_stop_before_anything(tmp_path, case):
    """Install over an owned folder (same folder) or with the locator naming another installation
    (relocation): the running check, the workspace scan and the overlap rule refuse before any
    download or change; and the copy kept in launcher\\ never installs."""
    env = _sandbox(tmp_path)
    old = full_fake(_plain(tmp_path) / "old", tmp_path)
    script, target, want_line, proc = ENGINE, _plain(tmp_path) / "new", None, None
    if case.startswith("same"):
        target = old
    else:
        _locator(env, old)
    if case in ("same-workspace", "relocate-workspace-in-old"):
        (old / "app" / "my-run").mkdir()
        (old / "app" / "my-run" / "card.policy.toml").write_text("# a user's workspace\n")
        want_line = "WORKSPACE_FOUND=" + str(old / "app" / "my-run")
    elif case == "same-running":
        shutil.copyfile(Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe", old / "env" / "cmd.exe")
        proc = subprocess.Popen([str(old / "env" / "cmd.exe"), "/d", "/c", "ping -n 60 127.0.0.1 >nul"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=0x08000000)
        want_line = f"ISAR_RUNNING={proc.pid}"
    elif case == "relocate-inside-old":
        target = old / "nested"
    elif case == "from-the-kept-copy":
        script = old / "launcher" / "WINDOWS-install-ISAR.bat"
    before = _tree(old)
    try:
        rc, out = run_bat(script, "/dir", str(target), "/yes", "/ref", "acs/job-branch", env=env)
    finally:
        if proc:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
            proc.wait(timeout=30)
    _log, lines = _last_log(env, "install")
    assert (rc, lines[-1]) == (2, "OUTCOME=refused"), (case, out, lines[-6:])
    assert not any(ln.startswith(("STEP=", "GET ")) for ln in lines), (case, lines)
    if want_line:
        assert want_line in lines, (case, [ln for ln in lines if "=" in ln][-8:])
    assert _tree(old) == before, case
    if target != old:
        assert not target.exists(), case


# ---------------------------------------------------------------- the latest installer

LATEST = INSTALLER / "WINDOWS-install-ISAR-latest.bat"


@pytest.mark.parametrize("args,says", [(["/ref", "acs/job-branch"], "test installs"),
                                       (["/test-fail", "selftest"], "test installs"),
                                       (["/repair"], "ISAR-Launcher.bat"), (["/uninstall"], "WINDOWS-uninstall-ISAR.bat")])
def test_latest_refuses_switches_it_does_not_take(tmp_path, args, says):
    """Refused before the first GitHub request and before any release installer runs; its own log says
    what to use instead."""
    env = _sandbox(tmp_path)
    rc, out = run_bat(LATEST, *args, "/yes", env=env)
    _log, lines = _last_log(env, "install-latest")
    assert (rc, lines[-1]) == (2, "OUTCOME=refused"), (args, out, lines[-4:])
    assert not any(ln.startswith(("GET ", "RUNNING ")) for ln in lines), (args, lines)
    assert any(ln.startswith("REASON: ") and says in ln for ln in lines), (args, lines[-3:])
    assert not list(Path(env["TEMP"]).glob("isar-latest-*")), "the working folder was left behind"


# ---------------------------------------------------------------- repair batch 1 (S1U5 review findings)

def _label_block(text, name):
    """The lines from label :name to the next blank line (the engine's labelled sections have none inside)."""
    lines = text.splitlines()
    out = []
    for ln in lines[lines.index(":" + name):]:
        if not ln.strip():
            break
        out.append(ln)
    return out


def _run_snippet(tmp_path, body, env):
    bat = tmp_path / "snippet.bat"
    bat.write_bytes(("@echo off\r\nsetlocal DisableDelayedExpansion\r\n" + "\r\n".join(body) + "\r\n").encode("ascii"))
    return run_bat(bat, env=env)


def mm_create_offenders(text):
    """Every micromamba create runs with --no-shortcuts and with USERPROFILE moved (call :mm_profile_on the line before)."""
    lines = _code_lines(text)
    bad = []
    for i, ln in enumerate(lines):
        if re.search(r'"%MM_EXE%"\s+create\b', ln):
            if "--no-shortcuts" not in ln:
                bad.append(("no --no-shortcuts", ln))
            if lines[i - 1].strip() != "call :mm_profile_on":
                bad.append(("profile not moved", ln))
    return bad


def test_every_engine_micromamba_create_is_shortcut_free_and_off_the_real_profile():
    text = _text(ENGINE)
    assert len(re.findall(r'"%MM_EXE%"\s+create\b', text)) == 2  # the build and the --download-only preflight
    assert not mm_create_offenders(text), mm_create_offenders(text)
    assert mm_create_offenders('"%MM_EXE%" create -y --no-rc -r "%B%" -p "%P%" -f "%L%"')
    assert mm_create_offenders('call :mm_profile_on\r\n"%MM_EXE%" create -y --no-rc -r "%B%" -p "%P%" -f "%L%"')
    assert not mm_create_offenders('call :mm_profile_on\r\n"%MM_EXE%" create -y --no-shortcuts -r "%B%" -p "%P%"')


def test_engine_profile_move_is_undone_and_leaves_the_real_profile_alone(tmp_path):
    env = _sandbox(tmp_path)
    real = str(tmp_path / "real profile")
    env["USERPROFILE"] = real
    # The echoed path must be ASCII: cmd writes non-ASCII in the console's code page, which varies.
    env["TEMP"] = env["TMP"] = str(tmp_path / "plain temp")
    (tmp_path / "plain temp").mkdir()
    text = _text(ENGINE)
    body = ['set "WORK=%TEMP%\\w"', 'mkdir "%WORK%"', "call :mm_profile_on", "echo ON=%USERPROFILE%",
            "call :mm_profile_off", "echo OFF=%USERPROFILE%", "exit /b 0", *_label_block(text, "mm_profile_on"),
            *_label_block(text, "mm_profile_off")]
    rc, out = _run_snippet(tmp_path, body, env)
    on = re.search(r"^ON=(.*?)\r?$", out, re.M)
    assert rc == 0 and on, out
    assert on.group(1).startswith(env["TEMP"]) and on.group(1).endswith("mm-profile"), out
    assert f"OFF={real}" in out, out


def test_make_lock_keeps_micromamba_off_the_real_profile(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("make_lock", REPO / "tools" / "installer" / "make_lock.py")
    ml = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ml)
    calls = []

    def fake_run(args, env=None, **kw):
        calls.append((list(args), dict(env or {})))
        return subprocess.CompletedProcess(args, 0, b"# platform: win-64\n@EXPLICIT\nhttps://x/y.conda#abc\n", b"")

    monkeypatch.setattr(ml.subprocess, "run", fake_run)
    monkeypatch.setattr(ml, "LOCK", tmp_path / "out.lock")
    fake_mm = tmp_path / "micromamba.exe"
    fake_mm.write_bytes(b"")
    monkeypatch.setattr("sys.argv", ["make_lock.py", "--micromamba", str(fake_mm)])
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "real profile"))
    ml.main()
    create = [(a, e) for a, e in calls if "create" in a]
    assert len(create) == 1
    args, env = create[0]
    prefix = Path(args[args.index("-p") + 1])
    assert "--no-shortcuts" in args
    assert Path(env["USERPROFILE"]).parent == prefix.parent, env.get("USERPROFILE")  # inside the scratch folder


def preflight_fetch_offenders(text):
    """A failed fetch into the kept source stops the update (PF_FAIL) before anything is removed."""
    lines = _code_lines("\n".join(_label_block(text, "preflight_kept")))
    i = next(n for n, ln in enumerate(lines) if "fetch -q --depth 1 origin" in ln)
    m = re.match(r"if errorlevel 1 goto (\w+)$", lines[i + 1].strip())
    if not m:
        return ["the fetch is not followed by a jump on failure: " + lines[i + 1]]
    after = lines[lines.index(":" + m.group(1)) + 1:]
    return [] if 'set "PF_FAIL=yes"' in after[:5] else ["the failure path does not set PF_FAIL"]


def test_failed_fetch_in_same_folder_preflight_stops_before_removal():
    text = _text(ENGINE)
    assert not preflight_fetch_offenders(text), preflight_fetch_offenders(text)
    assert preflight_fetch_offenders(text.replace("if errorlevel 1 goto preflight_kept_fetchfail",
                                                  'if errorlevel 1 >>"%LOG%" echo([WARN] later'))
    assert preflight_fetch_offenders(text.replace('set "PF_FAIL=yes"\r\nset "ERR=the new version', 'set "ERR=the new version'))


@pytest.mark.parametrize("usable", [True, False], ids=["git-checkout", "not-a-checkout"])
def test_failed_refetch_keeps_a_checkout_git_can_read(tmp_path, usable):
    """After a failed fetch, the kept source is removed only when git cannot read it: a usable checkout
    (with the user's own files in it) stops with ERR, and the same-folder confirmation says so."""
    git = shutil.which("git")
    if git is None:
        pytest.skip("needs git on PATH to tell a checkout from a folder")
    env = _sandbox(tmp_path)
    base = _plain(tmp_path) / "b"
    app = base / "app"
    app.mkdir(parents=True)
    (app / "my-notes.txt").write_text("the user's\r\n", encoding="utf-8")
    if usable:
        subprocess.run([git, "init", "-q", str(app)], check=True)
    text = _text(ENGINE)
    body = [f'set "BASE={base}"', 'set "WORK=%TEMP%"', f'set "GIT={git}"', 'set "LOG=%TEMP%\\snip.log"',
            "call :source_with_refetch", "exit /b 0", *_label_block(text, "source_with_refetch"),
            *_label_block(text, "remove_entry"), ":get_source", 'set "ERR=git could not download the commit"',
            "goto :eof", ":workspace_gate", 'set "GATE=ok"', "goto :eof"]
    rc, out = _run_snippet(tmp_path, body, env)
    assert rc == 0, out
    assert (app / "my-notes.txt").exists() == usable, out


def test_same_folder_confirmation_names_the_refetch_exception():
    lines = _code_lines(_text(ENGINE))
    i = lines.index(":confirm_same")
    block = " ".join(ln[5:] for ln in lines[i + 1:i + 11] if ln.startswith("echo "))
    assert "stay, unless that copy is damaged so that git cannot read it" in block, block


def test_workspace_scan_reaches_paths_past_260_characters():
    """Static: LongPathsEnabled=1 here lets the unprefixed scan pass, so this cannot be a behaviour test.
    The scan roots at a \\\\?\\ path and strips it from what it reports and compares."""
    text = _text(ENGINE)
    scan = "\n".join(_label_block(text, "workspace_gate"))
    assert "$r='\\\\?\\'+[IO.Path]::Combine($b,$t)" in scan
    assert "[IO.Directory]::Exists($r)" in scan and "New-Object IO.DirectoryInfo($r)" in scan
    assert "$e.FullName.Substring(4)" in scan and "$e.DirectoryName.Substring(4)" in scan


def _lnk_target(lnk):
    ps = "(New-Object -ComObject WScript.Shell).CreateShortcut($env:T_LNK).TargetPath"
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, text=True,
                       env={**os.environ, "T_LNK": str(lnk)})
    return r.stdout.strip()


@pytest.mark.parametrize("existing,replaced", [(None, True), ("same", True), ("other", False)])
def test_desktop_shortcut_replaces_only_one_that_opens_this_installation(tmp_path, existing, replaced):
    env = _sandbox(tmp_path)
    base = _plain(tmp_path) / "b"
    base.mkdir()
    _shortcut(base / "ISAR Launcher.lnk", base / "ISAR-Launcher.bat")
    target = {"same": base / "ISAR-Launcher.bat", "other": tmp_path / "other" / "ISAR-Launcher.bat"}.get(existing)
    desk = Path(env["ISAR_TEST_DESKTOP"]) / "ISAR Launcher.lnk"
    if target:
        _shortcut(desk, target)
        before = desk.read_bytes()
    body = ['set "DESKTOP=yes"', f'set "BASE={base}"', 'set "WORK=%TEMP%"', 'set "LOG=%TEMP%\\snip.log"',
            "call :desktop_copy", "exit /b 0", *_label_block(_text(ENGINE), "desktop_copy")]
    rc, out = _run_snippet(tmp_path, body, env)
    assert rc == 0 and desk.exists(), out
    assert (_lnk_target(desk) == str(base / "ISAR-Launcher.bat")) == replaced, out
    assert ("KEPT" in out) == (not replaced), out
    if existing == "other":
        assert desk.read_bytes() == before


def test_latest_failure_shows_its_log_on_screen(tmp_path):
    """Its own failure shows the log's lines (design item 5). Only the tail routine runs here: the
    notepad call opens a window, so it is checked by position below."""
    log = tmp_path / "install-latest.log"
    log.write_text("".join(f"line {n}\r\n" for n in range(1, 6)), encoding="ascii")
    body = [f'set "LOGFILE={log}"', "call :show_tail", "exit /b 0", *_label_block(_text(LATEST), "show_tail")]
    rc, out = _run_snippet(tmp_path, body, _sandbox(tmp_path))
    assert rc == 0 and "The last lines of the log:" in out and "line 5" in out, out


def test_latest_opens_its_log_only_for_an_attended_failure():
    lines = _code_lines(_text(LATEST))
    text = "\n".join(lines)
    assert text.index("if defined YES goto finish_exit") < text.index("call :open_notepad") < text.index("choice /C C")
    assert 'if "%OUTCOME%"=="failed" call :show_tail' in text


def test_latest_logs_an_outcome_after_the_release_installer_returns():
    """Static: the network path cannot run in a routine test. The line after the release installer's
    exit code is logged writes OUTCOME= before the script exits with that code."""
    lines = _code_lines(_text(LATEST))
    i = next(n for n, ln in enumerate(lines) if "RELEASE INSTALLER EXIT" in ln)
    tail = "\n".join(lines[i:i + 9])
    j = tail.index("OUTCOME=%OUTCOME%")
    assert j < tail.index("exit /b %RC%")
    assert 'if "%RC%"=="0" set "OUTCOME=installed"' in tail and 'set "OUTCOME=failed"' in tail
    # exit 3 is the release installer's self-test failure: its install stands, and the log must say so
    assert 'if "%RC%"=="3" set "OUTCOME=installed-selftest-failed"' in tail


def test_running_recheck_after_closing_isar_clears_the_refusal(tmp_path):
    """The running check refuses; the user closes ISAR and presses R. The recheck must clear the refusal,
    or the advertised retry can never succeed."""
    env = _sandbox(tmp_path)
    base = _plain(tmp_path) / "b"
    (base / "env").mkdir(parents=True)
    shutil.copyfile(Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe", base / "env" / "cmd.exe")
    text = _text(ENGINE)
    head = [f'set "GB={base}"', 'set "WORK=%TEMP%"', 'set "LOG=%TEMP%\\snip.log"']
    gate = _label_block(text, "running_gate")
    proc = subprocess.Popen([str(base / "env" / "cmd.exe"), "/d", "/c", "ping -n 60 127.0.0.1 >nul"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=0x08000000)
    try:
        rc, out = _run_snippet(tmp_path, [*head, 'set "YES=yes"', "call :running_gate", "echo GATE=%GATE%",
                                          "exit /b 0", *gate], env)
    finally:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
        proc.wait(timeout=30)
    assert rc == 0 and "GATE=refuse" in out, out
    rc, out = _run_snippet(tmp_path, [*head, 'set "GATE=refuse"', 'set "ERR=ISAR was running"', "call :running_again",
                                      "echo GATE=%GATE%", "echo ERR=%ERR%", "exit /b 0", *gate], env)
    assert rc == 0 and "GATE=ok" in out and re.search(r"^ERR=\r?$", out, re.M), out
