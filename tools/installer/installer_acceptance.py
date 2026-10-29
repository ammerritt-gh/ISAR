"""Acceptance harness for the one-click Windows installer (run on purpose, never by the routine gate).

    python tools/installer/installer_acceptance.py --ref <pushed branch> [--cases a,b|all] [--keep]
    python tools/installer/installer_acceptance.py --self-check        (no network, no install)

It drives the installer scripts the way an outside user's machine would see them: a scrubbed
environment (no developer Python, conda or git on PATH), and a temporary profile, TEMP and
Desktop whose paths hold a space and an accented letter. The real profile, Documents\\ISAR,
%LOCALAPPDATA%\\ISAR and the developer micromamba environments are snapshotted before and
compared after; any difference fails the run. Several cases download the full environment
(tens of minutes in all). Stdlib only; any Python 3.11+ outside the scrubbed environment.

Cases depend on each other's state (install folders i1..i4); naming a subset works only if
the cases it relies on are named too. Command lines and logs follow C-cli / C-layout.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GITHUB = "ammerritt-gh/ISAR"
CREATE_NO_WINDOW = 0x08000000
INSTALL_TIMEOUT = 25 * 60
OTHER_TIMEOUT = 5 * 60
SPACED = "J\u00f6rg Example"  # the accented, spaced profile folder
PLAIN_ROOT = re.compile(r"[A-Za-z]:\\[A-Za-z0-9._\\-]+")  # the installer's plain-character rule
PAYLOAD = [".isar-install-root", "INSTALL_INFO.txt", "ISAR-Launcher.bat", "ISAR Launcher.lnk",
           "launcher", "tracked-workspace-markers.txt", "micromamba", "mamba", "env", "app"]
DROP_PREFIXES = ("CONDA", "MAMBA", "PYTHON", "PIP_", "QT_", "GIT_", "ISAR_")


class Fail(Exception):
    pass


class Skip(Exception):
    pass


def out(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- snapshots of real state

def snapshot(root: Path, hashes: bool = False) -> dict | None:
    """Recursive {relative path: (size, mtime_ns[, sha256])}; None when the folder is absent."""
    if not root.exists():
        return None
    snap = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            p = Path(dirpath) / name
            try:
                st = p.stat()
                item = (st.st_size, st.st_mtime_ns)
                if hashes:
                    item += (hashlib.sha256(p.read_bytes()).hexdigest(),)
            except OSError as exc:
                item = ("unreadable", str(exc))
            snap[str(p.relative_to(root))] = item
    return snap


def diff_snapshots(before: dict | None, after: dict | None) -> list[str]:
    if before is None or after is None:
        return [] if before is after else ["existence changed (%s -> %s)" % (
            "absent" if before is None else "present", "absent" if after is None else "present")]
    lines = ["added: " + k for k in sorted(after.keys() - before.keys())]
    lines += ["removed: " + k for k in sorted(before.keys() - after.keys())]
    lines += ["changed: " + k for k in sorted(before.keys() & after.keys()) if before[k] != after[k]]
    return lines


def documents_folder() -> Path:
    """The Documents folder Windows reports (it may be redirected), not %USERPROFILE%\\Documents."""
    class GUID(ctypes.Structure):
        _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort), ("c", ctypes.c_ushort),
                    ("d", ctypes.c_ubyte * 8)]
    guid = GUID(0xFDD39AD0, 0x238F, 0x46AF, (ctypes.c_ubyte * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7))
    ptr = ctypes.c_wchar_p()
    if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(ptr)) != 0:
        return Path(os.environ["USERPROFILE"]) / "Documents"
    path = Path(ptr.value)
    ctypes.windll.ole32.CoTaskMemFree(ptr)
    return path


def real_watch_list() -> list[tuple[str, Path, bool]]:
    """(label, folder, hash files) for everything the run must leave untouched. Read-only."""
    up = Path(os.environ["USERPROFILE"])
    watch = [("%APPDATA%\\isar_gui", Path(os.environ.get("APPDATA", up / "AppData/Roaming")) / "isar_gui", True),
             ("Documents\\ISAR", documents_folder() / "ISAR", False),
             ("%LOCALAPPDATA%\\ISAR", Path(os.environ.get("LOCALAPPDATA", up / "AppData/Local")) / "ISAR", False)]
    for envs in (up / "AppData/Local/micromamba/envs", up / "AppData/Roaming/mamba/envs"):
        for name in ("isar", "tavi-dev"):
            watch.append((f"conda-meta of {name} under {envs.parent.name}", envs / name / "conda-meta", True))
    return watch


# ---------------------------------------------------------------- scrubbed environments

@dataclass
class Profile:
    """A temporary user: USERPROFILE, APPDATA, LOCALAPPDATA, TEMP and Desktop under one folder."""
    home: Path
    appdata: Path = field(init=False)
    local: Path = field(init=False)
    temp: Path = field(init=False)
    desktop: Path = field(init=False)
    documents: Path = field(init=False)

    def __post_init__(self):
        self.appdata = self.home / "AppData" / "Roaming"
        self.local = self.home / "AppData" / "Local"
        self.temp = self.home / "Temp"
        self.desktop = self.home / "Desktop"
        self.documents = self.home / "Documents"
        for d in (self.appdata, self.local, self.temp, self.desktop, self.documents):
            d.mkdir(parents=True, exist_ok=True)
        # A profile that already holds ISAR user data, so "left alone" assertions can fail.
        sentinel = self.appdata / "isar_gui" / "harness-sentinel.txt"
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text("profile sentinel\n", encoding="utf-8", newline="\n")

    @property
    def log_dirs(self) -> list[Path]:
        return [self.local / "ISAR" / "logs", self.temp / "ISAR-logs"]

    def env(self, extra: dict | None = None) -> dict:
        sysroot = os.environ.get("SYSTEMROOT", r"C:\Windows")
        env = {k.upper(): v for k, v in os.environ.items()
               if not k.upper().startswith(DROP_PREFIXES) and k.upper() not in ("VIRTUAL_ENV", "PATH")}
        env["PATH"] = ";".join([sysroot + r"\System32", sysroot, sysroot + r"\System32\Wbem",
                                sysroot + r"\System32\WindowsPowerShell\v1.0"])
        env.update(USERPROFILE=str(self.home), APPDATA=str(self.appdata), LOCALAPPDATA=str(self.local),
                   TEMP=str(self.temp), TMP=str(self.temp), ISAR_TEST_DESKTOP=str(self.desktop))
        env.update({k.upper(): v for k, v in (extra or {}).items()})
        return env


def kill_tree(pid: int) -> None:
    subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, check=False)


def gui_pids(base: Path) -> list[int]:
    """Pids of ISAR GUI processes started from this installation, matched by command line (never image name)."""
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like ('*' + $env:ISAR_H_BASE + '\\*') "
          "-and $_.CommandLine -like '*isar_gui*' } | ForEach-Object { $_.ProcessId }")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env={**os.environ, "ISAR_H_BASE": str(base)})
    if r.returncode != 0:
        out(f"  could not list the ISAR GUI processes: {r.stderr.strip()[-200:]}")
    return [int(x) for x in r.stdout.split() if x.isdigit()]


def stop_gui(base: Path) -> None:
    for pid in gui_pids(base):
        out(f"    stopping ISAR GUI pid {pid} under {base}")
        kill_tree(pid)


def quote_arg(a: str) -> str:
    return a if re.fullmatch(r"[A-Za-z0-9_./:\\=-]+", a) else '"%s"' % a


@dataclass
class Run:
    rc: int | None
    status: str  # done | timeout | killed
    secs: float
    text: str
    logs: list = field(default_factory=list)
    log_text: str = ""

    def lines(self) -> list[str]:
        return [ln.strip() for ln in self.log_text.splitlines()]

    def outcome(self) -> str | None:
        found = [ln[len("OUTCOME="):] for ln in self.lines() if ln.startswith("OUTCOME=")]
        return found[-1] if found else None


def run_proc(cmdline, env: dict, cwd: Path, timeout: float, out_path: Path, kill_when=None) -> Run:
    """Run with output to a file; kill the tree by pid on timeout or when kill_when() turns true."""
    t0 = time.monotonic()
    status = "done"
    with open(out_path, "wb") as sink:
        p = subprocess.Popen(cmdline, env=env, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=sink,
                             stderr=subprocess.STDOUT, creationflags=CREATE_NO_WINDOW)
        while True:
            try:
                p.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                pass
            if time.monotonic() - t0 > timeout:
                status = "timeout"
            elif kill_when is not None and kill_when():
                status = "killed"
            if status != "done":
                kill_tree(p.pid)
                p.wait(timeout=60)
                break
    text = out_path.read_bytes().decode("oem", errors="replace")
    return Run(p.returncode, status, time.monotonic() - t0, text)


def read_logs(logs: list[Path]) -> str:
    return "\n".join(p.read_bytes().decode("utf-8", errors="replace") for p in logs if p.exists())


# ---------------------------------------------------------------- the context cases share

class Ctx:
    def __init__(self, root: Path, installer: Path, branch: str | None):
        self.root, self.installer, self.branch = root, installer, branch
        self.runs = root / "runs"
        self.runs.mkdir(parents=True, exist_ok=True)
        self.p1 = Profile(root / SPACED / "profile one")
        self.p2 = Profile(root / SPACED / "Zo\u00eb second")
        self.downloads = self.p1.home.parent / "Downloads" / "ISAR setup"
        self.downloads.mkdir(parents=True, exist_ok=True)
        self.base = {n: root / n for n in ("i1", "i2", "i3", "i4")}
        self.seq = 0
        self.state: dict = {}

    def copy_script(self, name: str) -> Path:
        src = self.installer.parent / name
        if not src.exists():
            raise Fail(f"{name} is missing next to the installer ({src.parent})")
        dst = self.downloads / name
        shutil.copyfile(src, dst)
        return dst

    def logs_now(self, prof: Profile) -> set:
        return {p for d in prof.log_dirs if d.exists() for p in d.glob("*.log")}

    def script(self, prof: Profile, script: Path, args: list[str], label: str, timeout: float = OTHER_TIMEOUT,
               kill_line: str | None = None, extra_env: dict | None = None, cwd: Path | None = None) -> Run:
        """cmd /d /c <script> <args>, scrubbed, hidden; the new logs are read once afterwards."""
        self.seq += 1
        before = self.logs_now(prof)
        cmdline = 'cmd /d /s /c ""%s" %s"' % (script, " ".join(quote_arg(a) for a in args))

        def new_logs():
            return sorted(self.logs_now(prof) - before)

        trigger = (lambda: kill_line in [ln.strip() for ln in read_logs(new_logs()).splitlines()]) if kill_line else None
        stem = f"{self.seq:02d}-{label}"
        res = run_proc(cmdline, prof.env(extra_env), cwd or script.parent, timeout, self.runs / f"{stem}.out.txt", trigger)
        res.logs = new_logs()
        res.log_text = read_logs(res.logs)
        for i, lg in enumerate(res.logs):
            shutil.copyfile(lg, self.runs / f"{stem}.{i}.{lg.name}")
        out(f"    {label}: rc={res.rc} {res.status} {res.secs:.0f}s outcome={res.outcome()}")
        return res

    def exe(self, prof: Profile, argv: list[str], label: str, cwd: Path | None = None, timeout: float = 180,
            extra_env: dict | None = None) -> Run:
        self.seq += 1
        return run_proc(argv, prof.env(extra_env), cwd or self.root, timeout, self.runs / f"{self.seq:02d}-{label}.out.txt")

    def py(self, base: Path, args: list[str], label: str, prof: Profile | None = None, **kw) -> Run:
        return self.exe(prof or self.p1, [str(base / "env" / "python.exe"), "-I", *args], label, **kw)

    def selftest(self, base: Path, prof: Profile, label: str) -> Run:
        work = prof.temp / ("st-" + uuid.uuid4().hex[:8])
        work.mkdir(parents=True)
        return self.py(base, ["-m", "isar.install_check", "--expect-root", str(base / "app"), "--work", str(work)],
                       label, prof, cwd=work, timeout=240)

    def install_args(self, base: Path, *more: str) -> list[str]:
        return ["/dir", str(base), "/yes", "/ref", self.branch, *more]


def expect(res: Run, rc: int, outcome: str | None, *lines: str) -> None:
    if res.status != "done":
        raise Fail(f"run {res.status} after {res.secs:.0f}s")
    if res.rc != rc:
        raise Fail(f"exit code {res.rc}, expected {rc}")
    if outcome is not None and res.outcome() != outcome:
        raise Fail(f"OUTCOME={res.outcome()}, expected {outcome}")
    for ln in lines:
        if not any(x.startswith(ln) for x in res.lines()):
            raise Fail(f"log has no line starting {ln!r}")


def read_info(base: Path) -> dict:
    path = base / "INSTALL_INFO.txt"
    if not path.exists():
        raise Fail(f"{path} is missing")
    return dict(ln.split("=", 1) for ln in path.read_text(encoding="utf-8").splitlines() if "=" in ln)


def locator(prof: Profile) -> dict:
    path = prof.local / "ISAR" / "install-record.txt"
    if not path.exists():
        return {}
    return dict(ln.split("=", 1) for ln in path.read_text(encoding="utf-8").splitlines() if "=" in ln)


def tree(base: Path) -> dict:
    return snapshot(base) or {}


def assert_payload_gone(base: Path) -> None:
    left = [n for n in PAYLOAD if (base / n).exists()]
    if left:
        raise Fail("owned payload still present: " + ", ".join(left))


def assert_unchanged(before: dict, after: dict, what: str) -> None:
    d = diff_snapshots(before, after)
    if d:
        raise Fail(f"{what} changed: " + "; ".join(d[:5]))


def need(ctx: Ctx, key: str, case: str):
    if key not in ctx.state:
        raise Fail(f"needs case [{case}] to have passed first")
    return ctx.state[key]


def find_launcher_python(base: Path) -> Path:
    return base / "env" / "python.exe"


# ---------------------------------------------------------------- the cases

def case_refuse_paths(ctx: Ctx):
    inst = ctx.copy_script(ctx.installer.name)
    for tag, folder in (("space", ctx.root / "with space"), ("accent", ctx.root / "caf\u00e9")):
        res = ctx.script(ctx.p1, inst, ["/dir", str(folder), "/yes", "/ref", ctx.branch], "refuse-" + tag)
        expect(res, 2, "refused")
        if folder.exists():
            raise Fail(f"{folder.name} was created")
        if "REFUSE" not in res.text.upper() and "REFUS" not in res.log_text.upper():
            raise Fail("no reason printed")


def case_fresh(ctx: Ctx):
    base = ctx.base["i1"]
    inst = ctx.copy_script(ctx.installer.name)
    ctx.state["inst"] = inst
    res = ctx.script(ctx.p1, inst, ctx.install_args(base), "install", INSTALL_TIMEOUT)
    expect(res, 0, "installed", "STEP=selftest ok")
    # a native start is proven by the hidden native GUI line (K1), or in the fallback form by a native-plugin PASS
    native = any(ln.startswith(("CHECK gui PASS platform windows", "CHECK native-plugin PASS")) for ln in res.lines())
    if not native or "SELFTEST PASS" not in res.log_text:
        raise Fail("no native start proven: need 'CHECK gui PASS platform windows' or 'CHECK native-plugin PASS', and 'SELFTEST PASS'")
    for name in PAYLOAD[:-3] + ["launcher/run-isar.bat", "launcher/collect-diagnostics.bat",
                                "launcher/WINDOWS-install-ISAR.bat", "launcher/isar-win-64.lock"]:
        if not (base / name).exists():
            raise Fail(f"{name} missing from the installation")
    code = ("import isar, isar.synth.bl43_curves as c; print('F=' + isar.__file__); print('C=' + str(c.CURVES_DIR))")
    chk = ctx.py(base, ["-c", code], "provenance", cwd=ctx.root)
    app = str(base / "app").lower()
    paths = [ln[2:].strip().lower() for ln in chk.text.splitlines() if ln[:2] in ("F=", "C=")]
    if chk.rc != 0 or len(paths) != 2 or not all(p.startswith(app) for p in paths):
        raise Fail("isar / CURVES_DIR do not resolve under app: " + chk.text[-300:])
    info = read_info(base)
    head = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    lock = hashlib.sha256((REPO / "installer" / "isar-win-64.lock").read_bytes()).hexdigest()
    if info.get("SOURCE_COMMIT") != head:
        raise Fail(f"SOURCE_COMMIT {info.get('SOURCE_COMMIT')} is not the branch commit {head}")
    if info.get("LOCK_SHA256") != lock:
        raise Fail("LOCK_SHA256 differs from the committed lock")
    ctx.state["i1"] = info["INSTALL_ID"]


def case_launcher(ctx: Ctx):
    base = ctx.base["i1"]
    need(ctx, "i1", "fresh")
    result, nonce = ctx.p1.temp / "launcher-result.json", uuid.uuid4().hex
    ok = False
    try:
        res = ctx.script(ctx.p1, base / "ISAR-Launcher.bat", ["start"], "launcher-start",
                         extra_env={"ISAR_GUI_SELFTEST_RESULT": str(result), "ISAR_GUI_SELFTEST_NONCE": nonce})
        expect(res, 0, None)
        deadline = time.monotonic() + 120  # the launcher may return before the GUI has written
        while not result.exists() and time.monotonic() < deadline:
            time.sleep(1)
        if not result.exists():
            raise Fail("the GUI wrote no self-test result")
        got = json.loads(result.read_text(encoding="utf-8"))
        if got.get("nonce") != nonce:
            raise Fail("result nonce does not match")
        if not [p for p in ctx.logs_now(ctx.p1) if p.name.startswith("launch-")]:
            raise Fail("no launch-*.log")
        ok = True
    finally:
        if not ok:  # a hung GUI would otherwise refuse the cases after this one
            stop_gui(base)


def case_running(ctx: Ctx):
    base, inst = ctx.base["i1"], need(ctx, "inst", "fresh")
    gui = subprocess.Popen([str(find_launcher_python(base)), "-I", "-m", "isar_gui"], cwd=str(ctx.root),
                           env=ctx.p1.env({"QT_QPA_PLATFORM": "offscreen", "ISAR_EXPERIMENTS_ROOT": str(ctx.p1.documents / "ISAR")}),
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=CREATE_NO_WINDOW)
    try:
        time.sleep(15)
        if gui.poll() is not None:
            raise Fail(f"the offscreen GUI exited early (code {gui.returncode})")
        before = tree(base)
        for label, script, args in (("install", inst, ctx.install_args(base)),
                                    ("repair", base / "ISAR-Launcher.bat", ["repair", "/yes"]),
                                    ("uninstall", base / "ISAR-Launcher.bat", ["uninstall", "/yes"])):
            res = ctx.script(ctx.p1, script, args, "running-" + label, INSTALL_TIMEOUT)
            expect(res, 2, "refused")
            if not any(ln.startswith("ISAR_RUNNING=") for ln in res.lines()):
                raise Fail(f"{label}: no ISAR_RUNNING= line")
            assert_unchanged(before, tree(base), f"{label}: the installation")
    finally:
        kill_tree(gui.pid)


def case_workspace(ctx: Ctx):
    base, inst = ctx.base["i1"], need(ctx, "inst", "fresh")
    ws = base / "app" / "my-run"
    ws.mkdir()
    (ws / "card.policy.toml").write_text("# harness workspace\n", encoding="utf-8", newline="\n")
    try:
        before = tree(base)
        for label, script, args in (("install", inst, ctx.install_args(base)),
                                    ("uninstall", base / "ISAR-Launcher.bat", ["uninstall", "/yes"])):
            res = ctx.script(ctx.p1, script, args, "workspace-" + label, INSTALL_TIMEOUT)
            expect(res, 2, "refused")
            if not any(ln.startswith("WORKSPACE_FOUND=") and "my-run" in ln for ln in res.lines()):
                raise Fail(f"{label}: no WORKSPACE_FOUND= line naming my-run")
            assert_unchanged(before, tree(base), f"{label}: the installation")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def case_broken_env(ctx: Ctx):
    base = ctx.base["i1"]
    need(ctx, "i1", "fresh")
    shutil.rmtree(base / "env")
    zips_before = set((ctx.p1.local / "ISAR" / "diagnostics").glob("*.zip")) if (ctx.p1.local / "ISAR" / "diagnostics").exists() else set()
    res = ctx.script(ctx.p1, base / "ISAR-Launcher.bat", ["diagnostics", "/yes"], "diagnostics")
    expect(res, 0, None)
    zips = set((ctx.p1.local / "ISAR" / "diagnostics").glob("isar-diagnostics-*.zip")) - zips_before
    if len(zips) != 1:
        raise Fail(f"{len(zips)} new diagnostics zips, expected 1")
    with zipfile.ZipFile(next(iter(zips))) as z:
        names = [n.replace("\\", "/").rsplit("/", 1)[-1] for n in z.namelist()]
    if "INSTALL_INFO.txt" not in names or not any(n.startswith("install-") and n.endswith(".log") for n in names):
        raise Fail("zip lacks INSTALL_INFO.txt or the install log: " + ", ".join(names))
    info = read_info(base)
    res = ctx.script(ctx.p1, base / "ISAR-Launcher.bat", ["repair", "/yes"], "repair", INSTALL_TIMEOUT)
    expect(res, 0, "repaired", "STEP=selftest ok")
    if "CHECK gui PASS" not in res.log_text:
        raise Fail("repair log has no 'CHECK gui PASS'")
    after = read_info(base)
    for key in ("INSTALL_ID", "SOURCE_REF", "SOURCE_COMMIT", "LOCK_SHA256", "MAMBA_SHA256"):
        if info.get(key) != after.get(key):
            raise Fail(f"repair changed {key}")
    head = subprocess.run(["git", "-c", "safe.directory=*", "-C", str(base / "app"), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    if head != info["SOURCE_COMMIT"]:
        raise Fail(f"app is at {head}, record says {info['SOURCE_COMMIT']}")


def case_replace_hang(ctx: Ctx):
    base, inst = ctx.base["i1"], need(ctx, "inst", "fresh")
    watched = lambda: (snapshot(ctx.p1.appdata, True), snapshot(ctx.p1.documents))
    before = watched()
    res = ctx.script(ctx.p1, inst, ctx.install_args(base, "/test-fail", "selftest-hang"), "install-hang", INSTALL_TIMEOUT)
    expect(res, 3, "installed-selftest-failed")
    assert_unchanged(before[0] or {}, (watched()[0]) or {}, "the temporary profile")
    assert_unchanged(before[1] or {}, (watched()[1]) or {}, "the temporary workspaces")
    res = ctx.script(ctx.p1, inst, ctx.install_args(base), "install-again", INSTALL_TIMEOUT)
    expect(res, 0, "installed")
    if read_info(base)["INSTALL_ID"] != need(ctx, "i1", "fresh"):
        raise Fail("INSTALL_ID changed on the second install")


def case_interrupt_install(ctx: Ctx):
    base, inst = ctx.base["i2"], need(ctx, "inst", "fresh")
    res = ctx.script(ctx.p2, inst, ctx.install_args(base), "install-killed", INSTALL_TIMEOUT, kill_line="STEP=env-create start")
    if res.status != "killed":
        raise Fail(f"run was not killed on STEP=env-create start ({res.status}, rc={res.rc})")
    if not (base / ".isar-install-root").exists() or read_info(base).get("STATE") != "installing":
        raise Fail("marker / STATE=installing record missing after the kill")
    if locator(ctx.p2).get("ISAR_BASE", "").lower() != str(base).lower():
        raise Fail("locator does not name the interrupted installation")
    res = ctx.script(ctx.p2, inst, ctx.install_args(base, "/desktop", "yes"), "install-rerun", INSTALL_TIMEOUT)
    expect(res, 0, "installed")


def desktop_links(prof: Profile) -> dict:
    """{shortcut file name: target path} for every .lnk on the temporary Desktop (read via WScript.Shell)."""
    ps = ("$s=New-Object -ComObject WScript.Shell; Get-ChildItem -LiteralPath $env:ISAR_H_DIR -Filter *.lnk | "
          "ForEach-Object { $_.Name + '|' + $s.CreateShortcut($_.FullName).TargetPath }")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env={**prof.env(), "ISAR_H_DIR": str(prof.desktop)})
    return dict(ln.rsplit("|", 1) for ln in r.stdout.splitlines() if "|" in ln)


def case_relocate_fail(ctx: Ctx):
    i2, i3, inst = ctx.base["i2"], ctx.base["i3"], need(ctx, "inst", "fresh")
    links = desktop_links(ctx.p2)
    if not links:
        raise Fail("no Desktop shortcut to <i2> (the interrupt-install rerun asked for /desktop yes)")
    for fail_point in ("download", "selftest"):
        res = ctx.script(ctx.p2, inst, ctx.install_args(i3, "/desktop", "yes", "/test-fail", fail_point),
                         "relocate-" + fail_point, INSTALL_TIMEOUT)
        if res.status != "done" or res.rc in (0, None):
            raise Fail(f"/test-fail {fail_point}: expected a non-zero exit, got {res.status}/{res.rc}")
        st = ctx.selftest(i2, ctx.p2, "i2-selftest-" + fail_point)
        if st.rc != 0 or "SELFTEST PASS" not in st.text:
            raise Fail(f"<i2> no longer passes a self-test after /test-fail {fail_point}")
        if locator(ctx.p2).get("ISAR_BASE", "").lower() != str(i2).lower():
            raise Fail(f"locator no longer names <i2> after /test-fail {fail_point}")
        if desktop_links(ctx.p2) != links:
            raise Fail(f"Desktop shortcut changed after /test-fail {fail_point}")
    res = ctx.script(ctx.p2, inst, ctx.install_args(i3, "/desktop", "yes"), "relocate-retry", INSTALL_TIMEOUT)
    expect(res, 0, "installed")
    if (i2 / "env").exists() or (i2 / ".isar-install-root").exists():
        raise Fail("<i2>'s payload is still present after the relocation")
    if locator(ctx.p2).get("ISAR_BASE", "").lower() != str(i3).lower():
        raise Fail("locator does not name <i3>")


def sha_tree(root: Path) -> dict:
    return snapshot(root, True) or {}


def case_decoy(ctx: Ctx):
    i3 = ctx.base["i3"]
    decoy = ctx.root / "decoy-mamba"
    (decoy / "envs" / "isar" / "conda-meta").mkdir(parents=True)
    (decoy / "envs" / "isar" / "conda-meta" / "history").write_text("decoy\n", encoding="utf-8", newline="\n")
    (decoy / "envs" / "isar" / "sentinel.txt").write_text("do not touch\n", encoding="utf-8", newline="\n")
    notes = i3 / "my-notes.txt"
    notes.write_text("keep me\n", encoding="utf-8", newline="\n")
    decoy_before, logs_before = sha_tree(decoy), {p.name for p in ctx.logs_now(ctx.p2)}
    profile_before = sha_tree(ctx.p2.appdata)
    res = ctx.script(ctx.p2, i3 / "ISAR-Launcher.bat", ["uninstall", "/yes", "/delete-profile", "no"], "uninstall-keep",
                     INSTALL_TIMEOUT, extra_env={"MAMBA_ROOT_PREFIX": str(decoy), "CONDA_PREFIX": str(decoy)})
    expect(res, 0, "uninstalled")
    assert_payload_gone(i3)
    if not notes.exists() or not i3.exists():
        raise Fail("my-notes.txt or the base folder was removed")
    assert_unchanged(decoy_before, sha_tree(decoy), "the decoy developer root")
    assert_unchanged(profile_before, sha_tree(ctx.p2.appdata), "the temporary GUI profile")
    if not logs_before <= {p.name for p in ctx.logs_now(ctx.p2)}:
        raise Fail("earlier logs were removed")


def case_interrupt_uninstall(ctx: Ctx):
    base, inst = ctx.base["i1"], need(ctx, "inst", "fresh")
    res = ctx.script(ctx.p1, base / "ISAR-Launcher.bat", ["uninstall", "/yes"], "uninstall-killed", INSTALL_TIMEOUT,
                     kill_line="STEP=remove-env start")
    if res.status != "killed":
        raise Fail(f"run was not killed on STEP=remove-env start ({res.status}, rc={res.rc})")
    if not (base / ".isar-install-root").exists() or read_info(base).get("STATE") != "removing":
        raise Fail("marker / STATE=removing record missing after the kill")
    earlier = {p.name for p in ctx.logs_now(ctx.p1)}
    res = ctx.script(ctx.p1, inst, ["/uninstall", "/dir", str(base), "/yes", "/delete-profile", "yes"], "uninstall-finish", INSTALL_TIMEOUT)
    expect(res, 0, "uninstalled")
    assert_payload_gone(base)
    if (ctx.p1.appdata / "isar_gui").exists():
        raise Fail("the temporary GUI profile was not deleted by /delete-profile yes")
    # the end screen names the uninstall log; match its file name only (the path's accent garbles in the console code page)
    named = re.findall(r"uninstall-\d{8}-\d{6}-[0-9a-f]{8}\.log", res.text)
    now = {p.name for p in ctx.logs_now(ctx.p1)}
    if not named or named[-1] not in now:
        raise Fail("the uninstall log named on the last screen is not present")
    survivors = (earlier & now) - set(named)
    if survivors:
        raise Fail("earlier logs survived /delete-profile yes: " + ", ".join(sorted(survivors)))


def case_standalone_uninstall(ctx: Ctx):
    base, inst = ctx.base["i4"], need(ctx, "inst", "fresh")
    res = ctx.script(ctx.p1, inst, ctx.install_args(base), "install-i4", INSTALL_TIMEOUT)
    expect(res, 0, "installed")
    shutil.rmtree(base / "launcher")
    uninst = ctx.copy_script("WINDOWS-uninstall-ISAR.bat")
    res = ctx.script(ctx.p1, uninst, ["/yes", "/delete-profile", "no"], "standalone-uninstall")
    expect(res, 0, "uninstalled")
    assert_payload_gone(base)


def case_latest(ctx: Ctx):
    try:
        urllib.request.urlopen(urllib.request.Request(f"https://api.github.com/repos/{GITHUB}/releases/latest",
                                                      headers={"User-Agent": "isar-acceptance"}), timeout=30)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise Skip(f"could not ask GitHub for releases (HTTP {exc.code})")
    except OSError as exc:
        raise Skip(f"could not ask GitHub for releases ({exc})")
    else:
        raise Skip("a release exists; the latest installer is tested after the first release")
    latest = ctx.copy_script("WINDOWS-install-ISAR-latest.bat")
    prof = Profile(ctx.root / SPACED / "latest")
    before = sha_tree(prof.home)
    res = ctx.script(prof, latest, ["/yes"], "latest")
    expect(res, 2, None)
    if "release" not in res.text.lower():
        raise Fail("the end screen does not say that no release exists")
    after = {k: v for k, v in sha_tree(prof.home).items() if "ISAR" + os.sep + "logs" not in k and "ISAR-logs" not in k}
    assert_unchanged({k: v for k, v in before.items() if "ISAR-logs" not in k}, after, "the temporary profile")


CASES = [("refuse-paths", case_refuse_paths), ("fresh", case_fresh), ("launcher", case_launcher),
         ("running", case_running), ("workspace", case_workspace), ("broken-env", case_broken_env),
         ("replace-hang", case_replace_hang), ("interrupt-install", case_interrupt_install),
         ("relocate-fail", case_relocate_fail), ("decoy", case_decoy),
         ("interrupt-uninstall", case_interrupt_uninstall), ("standalone-uninstall", case_standalone_uninstall),
         ("latest", case_latest)]


# ---------------------------------------------------------------- run, report, self-check

def rmtree_force(path: Path) -> None:
    def onerr(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    shutil.rmtree(path, onerror=onerr)


def git_out(*args: str) -> str:
    r = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def preflight(branch: str) -> None:
    head = git_out("rev-parse", "HEAD")
    remote = git_out("ls-remote", "origin", branch).split()
    if not remote or remote[0] != head:
        raise SystemExit(f"refusing: HEAD {head[:10]} is not origin/{branch} ({remote[0][:10] if remote else 'branch not found'}); push first")
    try:
        urllib.request.urlopen(urllib.request.Request(f"https://api.github.com/repos/{GITHUB}/commits/{branch}",
                                                      headers={"User-Agent": "isar-acceptance"}), timeout=30)
    except OSError as exc:
        raise SystemExit(f"refusing: the unauthenticated GitHub API does not answer for {branch} ({exc}); "
                         "the installer needs the repository to be public")


def run_all(args) -> int:
    installer = Path(args.installer or REPO / "installer" / "WINDOWS-install-ISAR.bat").resolve()
    if not installer.exists():
        raise SystemExit(f"installer not found: {installer}")
    names = [n for n, _ in CASES] if args.cases == "all" else [c.strip() for c in args.cases.split(",")]
    bad = set(names) - {n for n, _ in CASES}
    if bad:
        raise SystemExit("unknown case(s): " + ", ".join(sorted(bad)))
    preflight(args.ref)
    root = Path(args.root) if args.root else Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "Temp" / ("isar-acceptance-" + time.strftime("%Y%m%d-%H%M%S"))
    if not PLAIN_ROOT.fullmatch(str(root)):
        raise SystemExit(f"--root {root} must hold only letters, digits, dot, dash and underscore (no space or accent)")
    if root.exists() and any(root.iterdir()):
        raise SystemExit(f"--root {root} is not empty")
    root.mkdir(parents=True, exist_ok=True)
    watch = [(label, path, h, snapshot(path, h)) for label, path, h in real_watch_list()]
    ctx = Ctx(root, installer, args.ref)
    v = ctx.exe(ctx.p1, ["cmd", "/d", "/c", str(installer), "/validate-only", str(root)], "validate-root")
    if "ACCEPT" not in v.text:
        raise SystemExit(f"the installer does not accept --root {root}: {v.text.strip()[-200:]}")

    t0, report, failed = time.monotonic(), [], False
    for name, fn in CASES:
        if name not in names:
            continue
        out(f"[{name}]")
        c0 = time.monotonic()
        try:
            fn(ctx)
            verdict = "PASS"
        except Skip as exc:
            verdict = f"SKIP {exc}"
        except Fail as exc:
            verdict, failed = f"FAIL {exc}", True
        except Exception as exc:  # a harness bug is still a failed case, never a vanished run
            verdict, failed = f"FAIL harness error: {type(exc).__name__}: {exc}", True
        if "timeout" in verdict and verdict.startswith("FAIL run timeout"):
            verdict = "TIMEOUT " + verdict[len("FAIL "):]
        report.append(f"{name:22s} {verdict}  ({time.monotonic() - c0:.0f} s)")
        out("  " + report[-1])
    for label, path, h, before in watch:
        d = diff_snapshots(before, snapshot(path, h))
        if d:
            failed = True
            report.append(f"REAL STATE CHANGED  {label}: " + "; ".join(d[:5]))
    report.append(f"{'all cases':22s} {'FAILED' if failed else 'ok'}  ({time.monotonic() - t0:.0f} s); runs and logs in {ctx.runs}")
    text = "\n".join(report) + "\n"
    (root / "report.txt").write_text(text, encoding="utf-8", newline="\r\n")
    out("\n" + text + f"report: {root / 'report.txt'}")
    if not (args.keep or failed):
        for child in root.iterdir():
            if child.name not in ("report.txt", "runs"):
                rmtree_force(child) if child.is_dir() else child.unlink()
    else:
        out(f"scratch tree kept: {root}")
    return 1 if failed or any(r.split()[1:2] == ["TIMEOUT"] for r in report) else 0


def self_check() -> int:
    if sys.platform != "win32":
        raise SystemExit("the harness is Windows-only")
    root = Path(tempfile.mkdtemp(prefix="isar-acc-selfcheck-"))
    try:
        assert PLAIN_ROOT.fullmatch(str(root)), f"temp root {root} is not plain; set TEMP to a plain folder"
        ctx = Ctx(root, root / "WINDOWS-install-ISAR.bat", "none")
        for prof in (ctx.p1, ctx.p2):
            env = prof.env({"MAMBA_ROOT_PREFIX": "x"})
            for key in ("USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "ISAR_TEST_DESKTOP"):
                v = env[key]
                assert " " in v and any(ord(ch) > 127 for ch in v), f"{key}={v} lacks a space or an accented letter"
                assert Path(v).is_dir() or key == "ISAR_TEST_DESKTOP" or Path(v).parent.exists(), key
            assert env["MAMBA_ROOT_PREFIX"] == "x"
        env = ctx.p1.env()
        os.environ["CONDA_PREFIX"], os.environ["PIP_INDEX_URL"], os.environ["PYTHONPATH"] = "c", "p", "q"
        os.environ["ISAR_ENV"] = "isar"
        try:
            env = ctx.p1.env()
        finally:
            for k in ("CONDA_PREFIX", "PIP_INDEX_URL", "PYTHONPATH", "ISAR_ENV"):
                del os.environ[k]
        assert not [k for k in env if k.startswith(DROP_PREFIXES) and k != "ISAR_TEST_DESKTOP"], "a dropped variable survived"
        for tool in ("python", "conda", "micromamba", "git"):
            assert shutil.which(tool, path=env["PATH"]) is None, f"{tool} found on the scrubbed PATH"
        for base in ctx.base.values():
            assert PLAIN_ROOT.fullmatch(str(base)), f"install folder {base} breaks the plain-character rule"
        # snapshots: identical when untouched, different after a touch, absent compares equal
        watched = root / "watched"
        watched.mkdir()
        (watched / "a.txt").write_text("one", encoding="utf-8", newline="\n")
        s0 = snapshot(watched, True)
        assert diff_snapshots(s0, snapshot(watched, True)) == []
        (watched / "a.txt").write_text("two", encoding="utf-8", newline="\n")
        assert diff_snapshots(s0, snapshot(watched, True)), "a changed file went unnoticed"
        assert diff_snapshots(None, snapshot(root / "nothing")) == []
        assert diff_snapshots(None, s0), "a folder appearing went unnoticed"
        # the real-state watch list builds without touching anything
        assert len(real_watch_list()) == 7
        # a script in the spaced, accented Downloads folder runs through the cmd form with its exit code
        bat = ctx.downloads / "probe.bat"
        bat.write_bytes(b"@echo off\r\necho got=%1\r\necho STEP=probe start >> \"%LOCALAPPDATA%\\ISAR\\logs\\probe-20260101-000000-aaaaaaaa.log\"\r\nexit /b 7\r\n")
        (ctx.p1.local / "ISAR" / "logs").mkdir(parents=True)
        res = ctx.script(ctx.p1, bat, ["a b"], "probe")
        assert res.rc == 7 and "a b" in res.text, (res.rc, res.text)
        assert res.logs and "STEP=probe start" in res.log_text
        # timeout kills by pid and reports it
        sleeper = [sys.executable, "-c", "import time; time.sleep(120)"]
        res = run_proc(sleeper, ctx.p1.env(), root, 2, root / "t.txt")
        assert res.status == "timeout" and res.secs < 30, (res.status, res.secs)
        # the log trigger kills a long run on its line
        marker = root / "marker.txt"
        child = [sys.executable, "-c", f"import time, pathlib; pathlib.Path(r'{marker}').write_text('go'); time.sleep(120)"]
        res = run_proc(child, ctx.p1.env(), root, 60, root / "k.txt", kill_when=marker.exists)
        assert res.status == "killed" and res.secs < 30, (res.status, res.secs)
        # the GUI lookup finds a process by its command line under one installation only, and stop_gui kills it by pid
        fake_gui = [sys.executable, "-c", "import time; time.sleep(120)", str(ctx.base["i1"] / "app" / "isar_gui")]
        proc = subprocess.Popen(fake_gui, env=ctx.p1.env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
        try:
            assert proc.pid in gui_pids(ctx.base["i1"]), "gui_pids missed a process under its installation"
            assert gui_pids(ctx.base["i2"]) == [], "gui_pids matched a process of another installation"
            stop_gui(ctx.base["i1"])
            proc.wait(timeout=30)  # raises if the kill by pid did not reach the process
        finally:
            if proc.poll() is None:
                kill_tree(proc.pid)
        out("self-check: ok")
        return 0
    finally:
        rmtree_force(root)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--ref", help="the pushed branch the test switch installs (required for a run)")
    ap.add_argument("--installer", help="default: installer\\WINDOWS-install-ISAR.bat in this checkout")
    ap.add_argument("--root", help="scratch folder, plain characters only; default a fresh one under %%LOCALAPPDATA%%\\Temp")
    ap.add_argument("--cases", default="all", help="comma-separated case names, or all: " + ", ".join(n for n, _ in CASES))
    ap.add_argument("--keep", action="store_true", help="keep the scratch tree after the run")
    ap.add_argument("--self-check", action="store_true", help="check the harness itself; no network, no install")
    args = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    if args.self_check:
        return self_check()
    if not args.ref:
        ap.error("--ref <branch> is required")
    return run_all(args)


if __name__ == "__main__":
    sys.exit(main())
