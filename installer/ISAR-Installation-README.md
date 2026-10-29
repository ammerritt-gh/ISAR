# Installing ISAR on Windows

> **Status:** live

This guide is for installing ISAR on a Windows computer when you do not use git, Python or
conda. You download one file, double-click it, answer a few questions and wait. Everything
ISAR needs goes into one folder that you choose. The installer does not change your PATH or
the registry, and it does not touch any other Python or conda environment you may already
have, including a developer's `isar` environment.

If you work from a source checkout instead, use `setup-isar-env.bat` and `run-isar-gui.bat` in
the repository. This guide does not cover that.

## Words used here

- **Installation folder:** the one folder that holds everything the installer puts down. You
  choose it during install.
- **Environment:** the private Python program set that ISAR runs in. It lives inside the
  installation folder.
- **Lock:** the exact list of every package the environment is built from, each at a fixed
  version. The same list gives the same environment each time.
- **Self-test:** a check the installer runs on ISAR before it reports success.
- **Workspace:** the folder ISAR keeps for one experiment, whether finished or still being set up.
- **Launcher:** `ISAR-Launcher.bat`, the menu you use after installing.

## Step 1: Download the installer

Get the installer from the ISAR releases page on GitHub. There are two files:

- `WINDOWS-install-ISAR-vX.Y.Z.bat` installs exactly that release. Use it when someone gave you
  a version number.
- `WINDOWS-install-ISAR-latest.bat` asks GitHub for the newest published release at the moment
  you run it, and then installs that release. If no release has been published yet, it says so
  and stops.

You can save the file in any folder. Its own folder may have spaces or accented letters in its
name.

## Step 2: Run it and choose a folder

Double-click the file. A console window opens and asks where to install ISAR. It suggests a
default folder: `ISAR` inside your user folder, for example `C:\Users\<you>\ISAR`. If your user
folder's path breaks the folder rules below (a space in your user name is one example), the
suggestion is `C:\ISAR` instead. Type another folder if you prefer.

The installation folder must follow these rules:

- Only plain letters A to Z, digits, a dot, a dash and an underscore. No spaces and no accented
  letters.
- Not a network path (`\\server\...`) or a device path, and no `..` in it.
- Not inside a protected Windows system folder.
- No junction or symbolic link anywhere on the path.

The reason is that the command-line tools that build ISAR handle spaces, accents and links
unreliably. A folder that breaks a rule is refused with a message, and nothing is created. The
installer also checks that the folder is writable before it offers it to you.

The installer then offers to put an ISAR shortcut on your Desktop. It uses the real Desktop
folder, even when Windows has moved it.

From here the install runs on its own. Keep the console window open until it says the install
has finished. No ISAR window opens during the install; ISAR is tested in the background. A slow
internet connection makes the download slow, and the environment is large, so the install can
take a long time.

## What the installer does

1. It works out the exact release, or the exact commit for a test install, and takes the lock and
   the source from that one commit.
2. Before it writes any program files, it writes an ownership marker and an install record, so
   an interrupted install can still be recognised and finished.
3. It downloads a private package manager (micromamba) and checks it against a fixed checksum.
4. It builds the environment from the lock. Every package comes from that list; nothing is
   swapped in later.
5. It downloads ISAR's source at that commit into `app\` and installs it into the environment.
6. It writes the launcher and runs the self-test.

The installer keeps its package caches inside the installation folder.

### The self-test

The self-test runs ISAR from the installed copy and checks four things:

- ISAR's modules load from the installed copy, not from anywhere else.
- A committed resolution curve loads through ISAR's own reader, and a small model is evaluated
  with it.
- The `isar` command runs `--help`.
- The ISAR GUI starts without showing a window and reports back within a time limit. The check
  needs a fresh report written by this same run, so an old result or a process that merely
  started does not count.

The self-test uses a throwaway profile and a throwaway workspace folder. It never opens, changes
or ingests into your real experiments.

## The end screen

The last screen says one of four things:

- **Installed and checked.** ISAR is installed and passed its self-test. Start it from the
  launcher.
- **Installed, but the self-test failed.** The files are in place, but ISAR did not pass its
  check. Run Repair from the launcher. If that also fails, collect the diagnostics and send them
  (see below).
- **Failed.** The install did not finish. The screen shows the last lines of the log and opens
  the full log. It also says what was and was not left behind. Running the same installer again
  into the same folder finishes an install that was interrupted partway.
- **Cancelled or refused.** Nothing was changed. You said no, or the folder was refused.

The end screen also names the log file.

## Logs and diagnostics

Every install, start, repair and uninstall writes its own log file, in `%LOCALAPPDATA%\ISAR\logs\`.
The name shows the operation, the date and time, and a random code, so two runs never share a
file. If that folder cannot be written, the logs go to `%TEMP%\ISAR-logs\`. Launch logs are
kept for the ten most recent starts.

If something fails and you need help, use **Collect diagnostics** in the launcher. It makes a zip
file in `%LOCALAPPDATA%\ISAR\diagnostics\`. The zip holds the install, launch, repair and uninstall
logs, ISAR's own GUI logs, and the install records. It works even when the environment is broken.
Nothing is sent anywhere by the installer. You decide whether to send the zip. The logs can contain
folder paths, which include your Windows user name, so send them only to the person helping you.

**If it fails, send the diagnostics zip** to whoever gave you ISAR.

## The launcher

The launcher is `ISAR-Launcher.bat` inside the installation folder. The `ISAR Launcher` shortcut in
the same folder opens it too. A Desktop shortcut, if you chose one, points into this installation.
Double-click the launcher to open the menu:

- **Start ISAR** starts the ISAR window.
- **Repair** rebuilds a broken or deleted environment. It works even when the Python or git
  inside the environment is broken, because it uses only the files kept beside the launcher. It
  re-downloads and checks micromamba if needed, rebuilds the environment from the recorded lock,
  restores the recorded source commit, reinstalls ISAR and runs the self-test. Repair never
  changes your release. Before it changes anything, it asks you to close ISAR if ISAR from this
  installation is running.
- **Collect diagnostics** makes the zip described above.
- **Uninstall** is described below.

## Updating to a newer release

Run the newer installer, either the versioned one for that release or the `latest` one. It finds
the existing installation and suggests its folder.

- **Same folder.** The installation is replaced in place. Every question is asked first, and the
  downloads are fetched and checked before anything is removed. If an update fails partway, run
  the installer again into the same folder to finish it.
- **Different folder.** The new installation is built and self-tested in the new folder first.
  Only after that succeeds is the old installation removed. If the download or the self-test
  fails, the old installation keeps working and can still be found.
- A new folder inside the old installation, or the old installation inside the new folder, is
  refused.

Before an update changes anything, it checks whether ISAR from this installation is running and
asks you to close it.

## Uninstalling

There are two ways to uninstall:

1. Open the launcher and choose **Uninstall**.
2. If the launcher is gone or broken, download `WINDOWS-uninstall-ISAR.bat` from the releases
   page and double-click it. It finds the installation from a record it keeps, or asks you for the
   folder. It uses the same rules as the launcher's Uninstall. It is a separate file so that it
   still works when the installation itself is damaged.

Uninstall removes only what the installer put there:

- the ISAR copy (`app\`), with anything else you saved inside that copy;
- the environment (`env\`), the private micromamba (`micromamba\`) and its package caches
  (`mamba\`);
- the launcher files and the ownership records;
- the Desktop shortcut, but only if it points at this installation.

Anything else in the installation folder stays, and the folder itself is removed only if it is
then empty. Other Python or conda environments on the computer, including a developer's `isar`
environment, are never touched.

**Workspaces are never deleted.** If an ISAR workspace sits inside the installation folder, for
example inside the ISAR copy, Uninstall stops before deleting anything and names the workspace.
Move it out of the installation folder, then run Uninstall again. Update and Repair follow the same
rule. Your workspaces are normally in the ISAR folder inside your Documents folder, and in the
recently used folders that ISAR lists. That list may not be complete.

Uninstall then asks: **delete ISAR's profile and logs too?** The answer defaults to **No**.

- **No** keeps the GUI profile and logs and the earlier install, launch and repair logs.
- **Yes** deletes the GUI profile and logs, in `%APPDATA%\isar_gui\` (or its fallback folders
  `%USERPROFILE%\isar_gui\` or `%USERPROFILE%\.isar_gui\`), and the earlier install, launch and
  repair logs.

Either answer keeps the log of the uninstall itself, outside the folders it deletes. The end
screen says where that log is. Diagnostics zips are not part of either answer; delete them by hand
if you want them gone.

If an uninstall is interrupted, the ownership records stay until everything is gone. Run
Uninstall again to finish it.

## Where things are

| Where | What it is |
|---|---|
| `ISAR-Launcher.bat`, `ISAR Launcher.lnk` | The menu, and a shortcut to it. Start here. |
| `app\` | ISAR's source, installed from the release. Do not edit it. |
| `env\` | The Python environment. The `isar` command is in `env\Scripts\`. The installer does not add it to your PATH. |
| `micromamba\`, `mamba\` | The private package manager and its package cache. |
| `launcher\` | The installer's copy and the scripts the menu calls. |
| `launcher\isar-win-64.lock` | The package list, exactly as downloaded. |
| `.isar-install-root`, `INSTALL_INFO.txt` | The ownership marker and install record. Do not delete them. |
| `tracked-workspace-markers.txt` | The list of ISAR files that ship with the source, so they are never mistaken for your workspaces. |
| `%APPDATA%\isar_gui\` | ISAR's GUI profile and logs. |
| `%LOCALAPPDATA%\ISAR\logs\` | The installer's logs. |
| `%LOCALAPPDATA%\ISAR\diagnostics\` | Diagnostics zips. |
| `%LOCALAPPDATA%\ISAR\install-record.txt` | Helps the uninstaller find the installation. It proves nothing about ownership. |
| `Documents\ISAR` | The default folder for your workspaces. |

## Testers

The installer has test-only switches. `/test-fail` is accepted only with `/ref` on an install,
or on a Repair of an installation recorded as a test install.

- `WINDOWS-install-ISAR.bat /ref <branch>` installs the current commit of that branch, instead of
  a release. A test installation records itself as one, and Repair restores the recorded commit,
  never a release tag.
- `/test-fail <point>` forces a failure to test the failure paths. `<point>` is `selftest` (the
  self-test fails), `selftest-hang` (the GUI start hangs until the self-test times out), or
  `download` (the lock download fails before anything is removed).
- `ISAR_TEST_DESKTOP`, an environment variable: when set, it replaces the Desktop folder the installer reports.

Exit codes for every installer script: 0 success; 1 failed; 2 cancelled or refused, nothing
changed; 3 installed or repaired, but the self-test failed.

The deliberate acceptance run is `tools\installer\run-installer-acceptance.bat` in the source
checkout. It takes tens of minutes and is not part of the routine test run. It installs into fresh
folders with a scrubbed environment, so the run behaves like a new user's machine.

## Release checklist (operator)

1. Set the release version in `installer\WINDOWS-install-ISAR.bat` to the new tag. A routine test
   checks that it matches the version in `pyproject.toml`. Then copy `installer\WINDOWS-install-ISAR.bat`
   over `installer\WINDOWS-uninstall-ISAR.bat`: the two must stay byte-identical, and a routine test checks it.
2. Regenerate the lock with `python tools/installer/make_lock.py`, only if the dependencies changed.
3. Push the release branch and run `tools\installer\run-installer-acceptance.bat` against it.
4. Tag the release and publish it on GitHub. Publishing stays a manual step.
5. Attach these three files to the release by hand:
   - `installer\WINDOWS-install-ISAR.bat`, renamed `WINDOWS-install-ISAR-vX.Y.Z.bat`;
   - `installer\WINDOWS-install-ISAR-latest.bat`;
   - `installer\WINDOWS-uninstall-ISAR.bat`.
6. Download the attached `WINDOWS-install-ISAR-vX.Y.Z.bat` from the release page and run it. Do
   this before you tell anyone about the release.
7. After the first release only: link both installers from the README and the releases page, then
   run the latest installer once.
