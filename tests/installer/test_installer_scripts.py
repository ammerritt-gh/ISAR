"""Routine regression checks for the Windows installer scripts (installer/*.bat).

Each check guards one trap TAVI's installer hit (or one rule of plan D2/D3). The static rules are
functions over the script text, and each is shown to fire on a broken snippet, so a rule that can
no longer fail is caught too. The dynamic checks run the real scripts through ``cmd /d /s /c`` with
LOCALAPPDATA, APPDATA, TEMP and the Desktop moved into ``tmp_path`` (TEMP with a space and an
accented letter), on fake installations; nothing here touches the network or a real profile.
"""
import os
import re
import subprocess
import tomllib
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


def test_static_rules_fire_on_broken_snippets():
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


def test_bat_files_are_crlf_in_the_index():
    """Raw GitHub downloads serve the stored bytes (D3), so the index itself must hold CRLF."""
    out = subprocess.run(["git", "-C", str(REPO), "ls-files", "--eol", "--", "installer"],
                         capture_output=True, text=True, check=True).stdout
    eol = {ln.split("\t", 1)[1]: ln.split()[0] for ln in out.splitlines() if "\t" in ln}
    for path in BATS:
        rel = path.relative_to(REPO).as_posix()
        assert eol.get(rel) == "i/crlf", f"{rel}: {eol.get(rel, 'not in the index')}"


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

def _sandbox(tmp_path, **extra):
    root = tmp_path / "Zo\u00eb user"
    env = {k.upper(): v for k, v in os.environ.items()}  # one entry per name: Windows names ignore case
    for key, sub in (("LOCALAPPDATA", "local"), ("APPDATA", "roaming"), ("TEMP", "T mp"), ("TMP", "T mp"),
                     ("ISAR_TEST_DESKTOP", "Desktop")):
        (root / sub).mkdir(parents=True, exist_ok=True)
        env[key] = str(root / sub)
    env.update({k.upper(): v for k, v in extra.items()})
    return env


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
    value = {"profile": os.environ["USERPROFILE"], "windows": os.environ["SystemRoot"],
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
