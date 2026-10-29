**I would keep the batch approach and most of this design, but tighten several contracts before implementation.** The main gaps are the exact dependency-installation procedure, Repair’s ability to recover a broken environment, the isolation of the GUI self-test, and the promise to preserve workspaces during replacement and removal.

There are also several places where “as TAVI does” overstates what TAVI currently implements. Those are good improvements to make in ISAR; they need to be treated as new work with explicit acceptance cases.

I checked TAVI’s installer history and current scripts at [`bd173f8`](https://github.com/ammerritt-gh/TAVI-Triple-Axis-Virtual-Instrument/tree/bd173f864f2a29cc0441a588e92c6b7a5ed748cf/installer), and ISAR’s source at [`93b4f61`](https://github.com/ammerritt-gh/ISAR/tree/93b4f61f9b249f259e68e186b3b0fb8a4eeffd2f). This is a review of the source and history; I have not executed a Windows installation.

## 1. What TAVI’s commits actually teach us

The proposed design already captures many of the right lessons. Here are the historical fixes I would explicitly preserve during implementation.


| TAVI failure and evidence                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | What ISAR should carry forward                                                                                                                          |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Pip replaced parts of a conda Qt installation.** Installing pip PySide6 over conda PySide6 first failed to uninstall it, then, when forced, loaded incompatible Qt DLLs. Fixed in [`46d33e8`](https://github.com/ammerritt-gh/TAVI-Triple-Axis-Virtual-Instrument/commit/46d33e8f430e9c8ac30d8c9048de6af5cc2dcd59).                                                                                                                                                                       | Give each third-party dependency one package-manager owner. Installing ISAR itself must not allow pip to resolve or replace the locked dependencies.    |
| **A launcher used the right source with the wrong Python.** Environment selection by name followed a stale or inherited mamba root. Fixed in [`19574fe`](https://github.com/ammerritt-gh/TAVI-Triple-Axis-Virtual-Instrument/commit/19574fe80ed01fb90ee10e882069b09a941abfde).                                                                                                                                                                                                              | Bind operations to both the private root and the full environment prefix. Verify the imported ISAR source location, too.                                |
| **Batch redirection silently corrupted installation metadata.** A digit immediately before `>` became a stream number, so `LAYOUT` was not written and uninstall rejected genuine installations. Fixed in [`34275d5`](https://github.com/ammerritt-gh/TAVI-Triple-Axis-Virtual-Instrument/commit/34275d5e48e7606d0596846cb886ccfe5ef645ca).                                                                                                                                                 | Use `> "file" echo NAME=value`, and execute a real write/read ownership check in tests. Merely inspecting the batch text would have missed the failure. |
| **A Python helper in** `%TEMP%` **imported somebody else’s** [`copy.py`](http://copy.py)**.** That shadowed the standard library and broke configuration. Also fixed in [`34275d5`](https://github.com/ammerritt-gh/TAVI-Triple-Axis-Virtual-Instrument/commit/34275d5e48e7606d0596846cb886ccfe5ef645ca).                                                                                                                                                                                   | Run helpers from a unique, owned work directory. A log can fall back to TEMP; a Python helper should not run directly from the shared TEMP directory.   |
| **An interrupted installation could not be retried.** It left files but no ownership marker, so the next run rejected the directory. Addressed in [`792f0b2`](https://github.com/ammerritt-gh/TAVI-Triple-Axis-Virtual-Instrument/commit/792f0b2e54b2b50a59948339c89af5786c96ab53).                                                                                                                                                                                                         | Write and verify the local ownership information before creating payload files. Keep it until cleanup has actually finished.                            |
| **Path validation happened after cmd had already interpreted the path.** Passing raw prompt/record values through `call` was too late. Separately, `findstr` character ranges admitted accented characters. Fixed in [`9a67f49`](https://github.com/ammerritt-gh/TAVI-Triple-Axis-Virtual-Instrument/commit/9a67f49215a7517b9214ca54c2da763615ef2ead) and [`f24e20d`](https://github.com/ammerritt-gh/TAVI-Triple-Axis-Virtual-Instrument/commit/f24e20d78bd79d2304b9d0f7e7a959c22666b9a6). | Preserve the variable-based validation entry point and explicit character list. Apply the same care to rejected-path messages and recorded values.      |
| **Uninstall opened another console and risked returning into a deleted batch file.** Fixed in [`179498b`](https://github.com/ammerritt-gh/TAVI-Triple-Axis-Virtual-Instrument/commit/179498bc3d68846c729ce60602bdaa7352c693db).                                                                                                                                                                                                                                                             | Copy the uninstaller outside the installation, leave the installation as working directory, and hand off without returning into removed scripts.        |
| **Two runs in one second erased the first log.** Fixed in [`c1b8052`](https://github.com/ammerritt-gh/TAVI-Triple-Axis-Virtual-Instrument/commit/c1b80523864f02b71c2acd6c40357366f445dea6). Desktop cleanup was also changed to remove only a shortcut targeting that installation in [`70d7665`](https://github.com/ammerritt-gh/TAVI-Triple-Axis-Virtual-Instrument/commit/70d7665dccdab5983391bba881eacd616dae02d5).                                                                     | Preserve collision-resistant log names and target-checked shortcut removal. Use Windows’ actual Desktop location.                                       |


That is a useful collection of fixes to inherit. **The implementation should carry the relevant regression cases along with the code.** Otherwise a seemingly harmless simplification during adaptation can undo the reason a peculiar-looking batch construct exists.

## 2. Correct the description of the TAVI baseline

I would change the design’s introductory claim from “inherits those fixes” to something closer to:

> Adapt the current TAVI batch structure and its relevant regression tests. Implement ISAR’s stronger environment-locking, repair, ownership and relocation requirements explicitly.

There are four material differences.

### TAVI does not currently lock the whole environment

Its current Windows installer constructs a package list with some fixed versions and many unpinned packages. ISAR’s exact transitive lock is therefore a new capability. [TAVI installer source](https://github.com/ammerritt-gh/TAVI-Triple-Axis-Virtual-Instrument/blob/bd173f864f2a29cc0441a588e92c6b7a5ed748cf/installer/WINDOWS-install-TAVI-v1.4.1.bat#L507-L522)

### TAVI’s Repair does not rebuild its environment

The repair script explicitly says it does not install or upgrade packages. It fetches the release, checks it out, and performs an import check. It requires the existing environment and sends the user back to the installer if that environment is missing. ISAR is promising substantially more useful repair behavior. [TAVI repair source](https://github.com/ammerritt-gh/TAVI-Triple-Axis-Virtual-Instrument/blob/bd173f864f2a29cc0441a588e92c6b7a5ed748cf/installer/launchers/update-tavi.bat)

### TAVI does not write the matched local ownership pair before its payload

It writes the external locator with `STATE=installing` early, but writes `INSTALL_INFO.txt` and the local ownership marker near the end. **ISAR’s proposed early matched pair is an improvement worth keeping.** [TAVI installer source](https://github.com/ammerritt-gh/TAVI-Triple-Axis-Virtual-Instrument/blob/bd173f864f2a29cc0441a588e92c6b7a5ed748cf/installer/WINDOWS-install-TAVI-v1.4.1.bat#L446-L461), [local metadata creation](https://github.com/ammerritt-gh/TAVI-Triple-Axis-Virtual-Instrument/blob/bd173f864f2a29cc0441a588e92c6b7a5ed748cf/installer/WINDOWS-install-TAVI-v1.4.1.bat#L688-L712)

### Automatic discovery and relocation are also new work

The current TAVI installer defaults to its profile/fallback location and uses the locator to recognize a partial install at the selected location. I did not find the proposed behavior that automatically suggests an existing installation elsewhere and removes it when a different destination is chosen. Treat that as an ISAR requirement, rather than a mechanical port. [TAVI folder selection and partial-install handling](https://github.com/ammerritt-gh/TAVI-Triple-Axis-Virtual-Instrument/blob/bd173f864f2a29cc0441a588e92c6b7a5ed748cf/installer/WINDOWS-install-TAVI-v1.4.1.bat)

One small but consequential source detail: **the TAVI** `v1.4.1` **tag does not contain** `WINDOWS-install-TAVI-v1.4.1.bat`**.** That file was added afterward by [`c794026`](https://github.com/ammerritt-gh/TAVI-Triple-Axis-Virtual-Instrument/commit/c794026c5b119c17e6f50565988faf81293accc4), as a restamp of the tested installer. Name the exact adaptation commit and files in the implementation plan.

## 3. Specify the lock format and installation procedure

“Every package and its exact version” is necessary, but insufficiently precise for a conda environment. The lock should identify the **platform, package builds, sources and hashes**, as well as versions. Micromamba’s explicit specification format includes exact package/build URLs and checksums and is platform-specific. [documentation](https://mamba.readthedocs.io/en/stable/user_guide/micromamba.html?utm_source=chatgpt.com)

For this project, I would favor:

- A locked Windows third-party environment, preferably using conda for that stack.
- ISAR installed from the owned source checkout.
- No dependency resolution during the ISAR installation step.

That follows directly from TAVI’s Qt failure. It also avoids leaving the crucial behavior implicit in a later `pip install`.

### Include the GUI and build dependencies

ISAR’s current conda environment file contains only Python and pip. Its GUI dependencies—PySide6, pyqtgraph and watchdog—are optional dependencies in `[gui]`, and setuptools is a separate build requirement. A check that considers only the base runtime dependency names will miss part of the delivered application. [ISAR environment file](https://github.com/ammerritt-gh/ISAR/blob/93b4f61f9b249f259e68e186b3b0fb8a4eeffd2f/environment-isar.yml), [pyproject.toml](https://github.com/ammerritt-gh/ISAR/blob/93b4f61f9b249f259e68e186b3b0fb8a4eeffd2f/pyproject.toml)

The gate should verify that:

1. Every required base and GUI dependency is represented.
2. Its locked version satisfies the declared requirement.
3. The build/install tools are pinned.
4. A fresh reconstruction passes the installed application checks.

The second condition matters: changing a requirement from `foo>=1` to `foo>=2` will pass a name-only test even if the lock still contains `foo==1.5`.

The test and calibration extras need not become part of the user installation.

### Make editable installation explicit

Keeping a full repository copy does not, by itself, make source-relative resource lookup work. The existing BL43 curve helper computes its directory relative to its own `__file__`; it explicitly documents that a wheel installation does not contain those curves. [BL43 curve helper](https://github.com/ammerritt-gh/ISAR/blob/93b4f61f9b249f259e68e186b3b0fb8a4eeffd2f/isar/synth/bl43_curves.py#L16-L20)

For this first installer, the smallest compatible choice is to **retain editable installation into the private environment**, with the checkout treated as installed application files. The intended local package step is equivalent to:

```
python -m pip install --no-deps --no-build-isolation -e "<owned-source>"
```

That must run through the private interpreter, with the required build backend already installed from the lock. Pip documents that `--no-deps` prevents dependency installation, while `--no-build-isolation` requires the build dependencies to be supplied beforehand. [pip documentation v26.2.1](https://pip.pypa.io/en/stable/cli/pip_install/?utm_source=chatgpt.com)

Verify imports and resource lookup from a working directory outside the checkout. Otherwise the source directory can accidentally make a broken installation look correct.

### Close the bootstrap loop

There is an ordering gap in the current prose:

- The lock is in the repository.
- Git is inside the environment.
- The environment must be built from the lock.

Specify that Windows tooling downloads the release’s lock **before** environment creation. Then micromamba builds the locked environment containing Git, and that Git retrieves the source.

Resolve the requested tag or test branch to a source commit once, and use that identity consistently for the lock and checkout. Record the source commit and lock hash. This also gives Repair something precise to reproduce.

If pip-managed dependencies are included in the lock, choose and test a format/installer combination that actually installs them. Conda-lock specifically warns that many tools silently ignore pip dependencies embedded in explicit lock files. [conda-lock](https://conda.github.io/conda-lock/pip/?utm_source=chatgpt.com)

## 4. Repair must work when the environment is broken

This is the main place where a straightforward TAVI port would fail the proposed user experience.

**Repair should not need Python or Git from the environment it is rebuilding.** Keep the release-specific recovery entry point and required installation information beside the launcher, outside both the source and environment directories.

A compact implementation can reuse the same environment-building path as installation. It needs to be able to:

- Recover the exact lock.
- Re-download and verify the private micromamba if necessary.
- Rebuild the environment without invoking its old Python.
- Restore the recorded source revision where needed.
- Reinstall ISAR and repeat the same checks.

Testing Repair immediately after a successful installation proves relatively little. The useful acceptance case is **remove or break the installed Python/Qt, then run Repair**. Diagnostics should remain available in that state as well.

### `/ref` needs its own recorded source identity

TAVI’s `/ref` is particularly dangerous to copy unchanged. It installs a branch while retaining the normal release value in `TAVI_VERSION`; it records the branch separately. Repair reads the release value and checks out that release tag. [Installer metadata](https://github.com/ammerritt-gh/TAVI-Triple-Axis-Virtual-Instrument/blob/bd173f864f2a29cc0441a588e92c6b7a5ed748cf/installer/WINDOWS-install-TAVI-v1.4.1.bat#L688-L705), [repair checkout](https://github.com/ammerritt-gh/TAVI-Triple-Axis-Virtual-Instrument/blob/bd173f864f2a29cc0441a588e92c6b7a5ed748cf/installer/launchers/update-tavi.bat#L43-L85)

Under the ISAR acceptance procedure, that would either:

- Fail because the first release tag does not exist yet; or
- Switch the installation away from the job branch once the tag exists.

Record the **actual installed commit**, independently of the release label. Repair should reproduce that commit and lock, including for test installations. A test installation should visibly identify itself as such.

## 5. Make the GUI self-test isolated, bounded and meaningful

The existing application gives you useful pieces to reuse, but the design needs to say how they are used.

### Isolate every installer self-test

ISAR’s ordinary no-argument startup restores the most recent workspace. The main window also starts the folder watcher and restores that workspace’s watched folders. An invisible startup using the real profile can therefore open a real experiment and potentially trigger ingestion during an update or repair. [Application startup](https://github.com/ammerritt-gh/ISAR/blob/93b4f61f9b249f259e68e186b3b0fb8a4eeffd2f/isar_gui/app.py#L128-L152), [watcher startup](https://github.com/ammerritt-gh/ISAR/blob/93b4f61f9b249f259e68e186b3b0fb8a4eeffd2f/isar_gui/main_window.py#L418-L423), [restored watched folders](https://github.com/ammerritt-gh/ISAR/blob/93b4f61f9b249f259e68e186b3b0fb8a4eeffd2f/isar_gui/main_window.py#L552-L598)

**The disposable profile must be part of the shipped self-test, not just the developer’s acceptance harness.**

Set temporary profile locations before startup and set `ISAR_EXPERIMENTS_ROOT` explicitly. ISAR already provides that override. Changing `APPDATA` and `LOCALAPPDATA` alone does not redirect its Documents-based experiment root. [Workspace root implementation](https://github.com/ammerritt-gh/ISAR/blob/93b4f61f9b249f259e68e186b3b0fb8a4eeffd2f/isar_gui/workspace_manager.py#L63-L76)

### Require success from the current invocation

ISAR already has `ISAR_GUI_SMOKE_MS` and a “main window shown” log message. However, that message is written before the event loop begins, and the normal GUI log persists across launches. Its exception handler can also show a modal error dialog. [Application implementation](https://github.com/ammerritt-gh/ISAR/blob/93b4f61f9b249f259e68e186b3b0fb8a4eeffd2f/isar_gui/app.py)

I would require:

- A fresh result file or invocation-specific readiness token.
- Successful main-window construction and at least one event-loop turn.
- Successful process exit.
- An outer timeout with cleanup of the test process.
- Noninteractive error reporting in self-test mode.

An old success line, a successfully spawned process, or a process that remains alive should not satisfy the check.

### Exercise Windows Qt

If “without showing a window” is implemented using `QT_QPA_PLATFORM=offscreen`, it selects a different platform backend. Qt’s native Windows backend is `qwindows`, and `QT_QPA_PLATFORM` explicitly overrides the default selection. Therefore an offscreen pass does not establish that the native Windows plugin loads. [Qt 6.12.0](https://doc.qt.io/qt-6/qpa.html?utm_source=chatgpt.com)

Prefer a hidden native-platform startup for the installed-machine check, or add a small native `QApplication` check alongside the offscreen application test. Keep the actual desktop-launch acceptance step as well.

### Add one small ISAR-specific operation

TAVI eventually checked a real simulation because GUI startup alone did not establish that the application could do its job. ISAR’s equivalent can be much smaller:

- Load a committed test resolution curve through the real reader.
- Evaluate a tiny model or perform another deterministic analysis operation.
- Optionally create and reopen a tiny disposable workspace.

This should take seconds, use committed/generated data, and remain independent of TAVI, an LLM service, and lengthy fitting passes.

Finally, make the CLI command explicit: `isar --help` is suitable for checking the installed entry point. Bare `isar` currently fails argument parsing because a subcommand is required. [CLI parser](https://github.com/ammerritt-gh/ISAR/blob/93b4f61f9b249f259e68e186b3b0fb8a4eeffd2f/isar/cli/main.py#L37-L39)

## 6. Resolve the workspace-preservation contradiction

This is the clearest data-loss issue in the design.

It currently promises both:

> The source copy is removed whole, after warning about workspaces inside it.

and:

> Workspaces are never deleted, wherever the user saved them.

Both cannot hold for a workspace inside the source copy.

This is a realistic accident. The CLI currently defaults `isar init` output to a directory under the current working directory. Someone running it from the application folder can create a workspace there through an ordinary command. [CLI workspace creation](https://github.com/ammerritt-gh/ISAR/blob/93b4f61f9b249f259e68e186b3b0fb8a4eeffd2f/isar/cli/init_cmd.py#L129-L137)

**My recommendation is to preserve the stronger promise:** if a proposed deletion would include an ISAR workspace, stop that deletion and tell the user which workspace needs moving.

Apply the rule to:

- Uninstall.
- In-place replacement.
- Relocation cleanup.
- Repair, if it replaces source files or directories.

Handle pending experiments as well as fully initialized workspaces. Use the application’s workspace conventions, rather than inventing a separate definition in the installer.

The warning about arbitrary extra files inside the application directory can remain. But a warning followed by deletion should not be described as workspace preservation.

Also, the end screen should distinguish known workspace locations from a complete inventory. ISAR’s recent-workspace list is not a catalogue of every workspace the user has ever created.

## 7. Improve replacement ordering and ownership handling

### Complete decisions and preflight before removal

TAVI’s present installer removes the old application and environment before reaching its final “Continue with installation?” prompt. Later fixes made cancellation and failure screens admit that the old installation had already been removed. That sequencing is a lesson to improve upon here. [Removal sequence](https://github.com/ammerritt-gh/TAVI-Triple-Axis-Virtual-Instrument/blob/bd173f864f2a29cc0441a588e92c6b7a5ed748cf/installer/WINDOWS-install-TAVI-v1.4.1.bat#L300-L317), [later confirmation](https://github.com/ammerritt-gh/TAVI-Triple-Axis-Virtual-Instrument/blob/bd173f864f2a29cc0441a588e92c6b7a5ed748cf/installer/WINDOWS-install-TAVI-v1.4.1.bat#L436-L446)

For ISAR:

- Finish all user decisions first.
- Validate the destination and test that it is writable.
- Retrieve and verify release metadata and downloads where practical.
- Then begin destructive replacement.

For relocation, **build and check the new installation at its final chosen location before removing the old one**. “One installation remains” can describe a successfully completed relocation; it need not force deletion of the working copy before an unreliable network operation.

For replacement at the same location, a recoverable reinstall is reasonable for this prototype. An elaborate rollback mechanism is unnecessary, but failures must retain enough identity and recovery code for the next attempt.

Avoid building a conda environment under a temporary prefix and simply renaming it into place. Package installation can write the destination prefix into files; TAVI itself warns that its installed environment cannot be moved afterward. [documentation](https://mamba.readthedocs.io/en/latest/advanced_usage/detailed_operations.html?utm_source=chatgpt.com)

### Reject overlapping destinations

Allow the same canonical installation root or a separate root. Reject a new destination inside the old installation, and reject confusing ancestor/descendant arrangements.

Otherwise “install in `old\app\new`, then remove old” can delete the new installation during cleanup.

### Make ownership authority explicit

The local matched marker and receipt should authorize operations. The `%LOCALAPPDATA%` locator should help find them.

Before the first payload file:

1. Check that existing reserved payload paths are either absent or already owned by this installation.
2. Write the local identity files.
3. Read them back and verify the match.
4. Stop if that fails.

Writing a fresh marker over an unrelated pre-existing `app` directory cannot make that directory installer-owned.

During removal, retain the local identity until every owned payload directory is confirmed gone. TAVI already checks directory existence after deletion rather than trusting the deletion command’s status alone; that is a useful detail to preserve. [TAVI removal verification](https://github.com/ammerritt-gh/TAVI-Triple-Axis-Virtual-Instrument/blob/bd173f864f2a29cc0441a588e92c6b7a5ed748cf/installer/launchers/uninstall-tavi.bat#L153-L178)

Finally, destructive maintenance should detect a running instance of this installation and ask the user to close it before changing files. Scope that check to this installation. A small maintenance lock can also prevent two accidental installer launches from operating on it simultaneously.

## 8. Keep the Windows path and logging rules internally consistent

### Restrict the installation root, while supporting ordinary user paths

The approved ASCII/no-space installation rule is workable. It does not eliminate spaces or Unicode elsewhere.

A user may download the installer to:

```
C:\Users\Jörg Example\Downloads\
```

and install successfully into:

```
C:\ISAR\
```

Their profile, logs, TEMP and Desktop still contain the original user name. Test that whole arrangement. Merely rejecting `C:\ISAR Test` does not establish that a spaced or accented Windows account can install ISAR.

Use Windows’ known-folder location for Desktop. ISAR already does the corresponding thing correctly for Documents through `QStandardPaths`; preserve that behavior. [Workspace root implementation](https://github.com/ammerritt-gh/ISAR/blob/93b4f61f9b249f259e68e186b3b0fb8a4eeffd2f/isar_gui/workspace_manager.py#L63-L76)

Probe the fallback destination for write access before removing anything. If it is unwritable, offer another valid folder.

### Explicitly omit TAVI’s AutoRun cleanup

TAVI’s current installer still deletes the user’s cmd `AutoRun` value if its contents mention mamba or micromamba. That could affect a legitimate existing setup and conflicts with ISAR’s isolation promise. Remove that step from the adaptation. [TAVI AutoRun handling](https://github.com/ammerritt-gh/TAVI-Triple-Axis-Virtual-Instrument/blob/bd173f864f2a29cc0441a588e92c6b7a5ed748cf/installer/WINDOWS-install-TAVI-v1.4.1.bat#L467-L478)

For child command processors the installer controls, `cmd /d` disables AutoRun execution without modifying the registry. [Microsoft Learn](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/cmd?utm_source=chatgpt.com)

Environment isolation should also specify private cache locations and the handling of inherited conda, Python and Qt settings. Micromamba reads configuration from files, environment variables and command-line options; selecting a private prefix alone does not describe all of that behavior. [documentation](https://mamba.readthedocs.io/en/latest/user_guide/configuration.html?utm_source=chatgpt.com)

### Define what happens when the user deletes logs

“Delete profile and logs” conflicts with “every uninstall run leaves a log” unless the final log has a separate lifetime.

A simple rule is:

> The option deletes previously retained profile data and logs. The current uninstall log is retained outside the deleted directories, and the end screen gives its location.

Diagnostics should work with a missing environment, include the actual GUI logs and recognized fallback logs, and write the ZIP outside replaceable payload directories. ISAR’s GUI logger currently falls back to `Path.home()/isar_gui`, which is distinct from the installer’s proposed TEMP fallback. [GUI logging implementation](https://github.com/ammerritt-gh/ISAR/blob/93b4f61f9b249f259e68e186b3b0fb8a4eeffd2f/isar_gui/logging_setup.py)

## 9. Strengthen “Done when” with a few failure cases

The proposed successful lifecycle exercise is useful. The highest-value additions are cases that distinguish a robust installer from one that works only immediately after installation.


| Acceptance case                                                                                                               | Required result                                                                        |
| ----------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| Run the downloaded batch from a spaced/accented profile path; use an empty private cache and no developer Python/Git on PATH. | Bootstrap succeeds using only the declared prerequisites.                              |
| Import ISAR and load a test resource from an unrelated working directory.                                                     | Both come from the owned installation.                                                 |
| Remove/break the installed environment, then choose Repair.                                                                   | Repair restores the recorded commit and lock without requiring the broken environment. |
| Install with `/ref`, then Repair before the first release tag exists.                                                         | Repair preserves the actual tested commit.                                             |
| Interrupt environment creation, then rerun.                                                                                   | The partial installation is recognized and recoverable.                                |
| Make relocation fail during download or self-test.                                                                            | The previous working installation remains usable and discoverable.                     |
| Put a workspace inside the source directory, then update or uninstall.                                                        | The workspace is preserved and the operation explains what needs moving.               |
| Run maintenance while the GUI is open; separately simulate an incomplete deletion.                                            | No premature destructive work; incomplete cleanup keeps its ownership information.     |
| Force GUI startup failure or a hang.                                                                                          | No false success, no indefinite wait, and no real profile/workspace mutation.          |
| Exercise both answers to profile/log deletion.                                                                                | Default No preserves them; Yes follows the documented final-log rule.                  |
| Download and run the actual published release asset.                                                                          | The asset, source identity, lock and installer build agree.                            |


The last row is separate from testing `/ref`. Manual publishing can remain exactly as requested, but the operator’s release procedure should include testing the actual uploaded file before distributing it.

I would also put an independent Windows run before the first external user. One narrowly scoped GitHub-hosted Windows job is a practical option even without local Windows Sandbox. Hosted runners provide fresh VMs, although they contain preinstalled tools, so PATH and environment isolation still matter. Keep an attended native desktop-launch check on your own machine. [GitHub Docs](https://docs.github.com/en/actions/how-tos/write-workflows/choose-where-workflows-run/choose-the-runner-for-a-job?utm_source=chatgpt.com)

Be explicit about invoking the installer acceptance checks: ISAR’s existing Windows launcher smoke is marked `slow` and uses offscreen Qt, so passing the routine test gate alone does not exercise the proposed installation path. [Existing launcher tests](https://github.com/ammerritt-gh/ISAR/blob/93b4f61f9b249f259e68e186b3b0fb8a4eeffd2f/tests/gui/test_launcher.py)

**My approval conditions would be the exact installation/lock procedure, independent Repair, isolated self-test, and consistent workspace preservation.** The remaining items are small implementation requirements that belong in the plan and its acceptance cases. With those written down, this stays a sensible installer for a small prototype group—and ISAR gets the benefit of TAVI’s hard-earned fixes without repeating its most expensive mistakes.