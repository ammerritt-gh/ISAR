@echo off
setlocal DisableDelayedExpansion

:: ISAR Windows installer. One engine with modes (plan D1):
::   WINDOWS-install-ISAR.bat [/dir <path>] [/yes] [/desktop yes or no] [/ref <branch>] [/test-fail <point>]
::   WINDOWS-install-ISAR.bat /repair /dir <base> [/yes] [/test-fail <point>]
::   WINDOWS-install-ISAR.bat /uninstall /dir <base> [/yes] [/delete-profile yes or no]
::   WINDOWS-install-ISAR.bat /validate-only <path>   prints ACCEPT, or REFUSE <reason>
::   WINDOWS-install-ISAR.bat /inspect <path>         test only: OWNERSHIP=none, owned, partial
::                                                    or foreign, and STATE=<state> when owned
:: Exit codes: 0 success, 1 failed, 2 cancelled or refused (nothing changed),
:: 3 installed or repaired but the self-test failed.
:: Test-only: /ref installs a branch's current commit; /test-fail selftest,
:: selftest-hang or download forces a failure; ISAR_TEST_DESKTOP replaces the
:: Desktop Windows reports. The user guide is installer\ISAR-Installation-README.md.
::
:: Batch rules here, each closing a trap TAVI's installer hit:
:: - No pipes and no command-form "for /f": each runs a fresh cmd that runs the
::   user's AutoRun (Anaconda writes one) and can garble what is parsed. Output is
::   captured by redirecting to a file in the private working folder.
:: - Every typed or recorded value is checked against an explicit character list
::   (findstr ranges like A-Z let accented letters through) before any line
::   expands it with %%...%%.
:: - Redirections come first on echo lines: "echo X=1>>f" eats a trailing digit.
:: - No paths from the environment inside parenthesised blocks; labels and goto.
:: - Every python runs with -I, so neither PYTHON* variables nor the current
::   folder (TAVI once imported a stray copy.py from %%TEMP%%) can reach it.
:: - This file must stay a .bat: there a successful "set" leaves ERRORLEVEL
::   alone, so "set ERR=...", then "if errorlevel 1", reads the command before.

set "ISAR_RELEASE=v0.0.2"
set "INSTALLER_BUILD=1"
set "LAYOUT=1"
set "GH_REPO=ammerritt-gh/ISAR"
set "MM_VERSION=2.5.0-1"
:: SHA-256 of micromamba-win-64 2.5.0-1; provenance in setup-isar-env.bat.
set "MM_SHA256=56e3a55be1d8858f51ec9902bbc0825d7a18dc43c8558cd8d8b4e1f3d9af7bb4"

:: Only Windows' own tools on PATH: a user's PATH may put another curl, find or
:: python first. Environment steps prepend the environment's folders.
set "SYS_PATH=%SystemRoot%\System32;%SystemRoot%;%SystemRoot%\System32\Wbem;%SystemRoot%\System32\WindowsPowerShell\v1.0"
set "PATH=%SYS_PATH%"
set "CURL=%SystemRoot%\System32\curl.exe"
set "NoDefaultCurrentDirectoryInExePath="

:: findstr patterns, every character listed (no ranges).
set "D=[0123456789]"
set "H=[0123456789abcdef]"
set "AL=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
set "PATHCHARS=[ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.:\\-]"
:: findstr refuses a pattern past about a dozen classes, so 32-, 40- and
:: 64-hex values are a character check plus a length check (:match_hex).
set "D2=%D%%D%"
set "D4=%D%%D%%D%%D%"
set "ISO_RE=%D4%-%D2%-%D2%T%D2%:%D2%:%D2%"
set "VER_RE=v%D%%D%*\.%D%%D%*\.%D%%D%*"
set "REF_RE=[%AL%][%AL%._/-]*"

set "SELF_DIR=%~dp0"
:: Kept now: the argument loop shifts %0 too.
set "SELF_FILE=%~f0"
:: A copy of this file whose name holds "uninstall" (WINDOWS-uninstall-ISAR.bat)
:: is the standalone uninstaller: the name makes /uninstall the action.
set "SELF_NAME=%~n0"
set "NAME_UNINSTALL="
if /i not "%SELF_NAME:uninstall=%"=="%SELF_NAME%" set "NAME_UNINSTALL=yes"
:: The owned payload folders, as the workspace scan takes them.
set "PAYLOAD_DIRS=app*env*mamba*micromamba*launcher"
set "WORK="
set "LOG=nul"
set "LOGFILE="

if /i "%~1"=="/validate-only" goto validate_only
if /i "%~1"=="/inspect" goto inspect

:: ---------------------------------------------------------------------------
:: Arguments. Values are stored raw and checked after the log is open; an
:: unknown argument is kept only to be logged through "set", never echoed.
:: ---------------------------------------------------------------------------
set "OP=install"
if defined NAME_UNINSTALL set "OP=uninstall"
set "OP_COUNT=0"
set "ARG_DIR="
set "HAVE_DIR="
set "YES="
set "DESKTOP="
set "REF="
set "TEST_FAIL="
set "DEL_PROFILE="
set "ARG_UNKNOWN="

:parse_args
if "%~1"=="" goto args_done
if /i "%~1"=="/dir" goto arg_dir
if /i "%~1"=="/yes" goto arg_yes
if /i "%~1"=="/desktop" goto arg_desktop
if /i "%~1"=="/ref" goto arg_ref
if /i "%~1"=="/test-fail" goto arg_test_fail
if /i "%~1"=="/delete-profile" goto arg_delete_profile
if /i "%~1"=="/repair" goto arg_repair
if /i "%~1"=="/uninstall" goto arg_uninstall
set "ARG_UNKNOWN=%~1"
shift
goto parse_args

:arg_dir
set "HAVE_DIR=yes"
set "ARG_DIR=%~2"
shift
shift
goto parse_args

:arg_yes
set "YES=yes"
shift
goto parse_args

:arg_desktop
set "DESKTOP=%~2"
if not defined DESKTOP set "DESKTOP=missing"
shift
shift
goto parse_args

:arg_ref
set "REF=%~2"
if not defined REF set "REF=-"
shift
shift
goto parse_args

:arg_test_fail
set "TEST_FAIL=%~2"
if not defined TEST_FAIL set "TEST_FAIL=missing"
shift
shift
goto parse_args

:arg_delete_profile
set "DEL_PROFILE=%~2"
if not defined DEL_PROFILE set "DEL_PROFILE=missing"
shift
shift
goto parse_args

:arg_repair
set "OP=repair"
set /a OP_COUNT+=1
shift
goto parse_args

:arg_uninstall
set "OP=uninstall"
set /a OP_COUNT+=1
shift
goto parse_args

:args_done
if "%OP%"=="uninstall" if exist "%SELF_DIR%..\.isar-install-root" goto handoff
if "%OP%"=="uninstall" if exist "%SELF_DIR%.isar-install-root" goto handoff
title ISAR installer
set "OUTCOME="
set "ERR="
set "CUR_STEP="
set "BASE="
set "BASE_CREATED="
set "RECORDS_WRITTEN="
set "INSTALL_ID="
set "SHOW_RELEASE=%ISAR_RELEASE%"
call :open_log_and_work
if not defined WORK goto no_work
>>"%LOG%" echo(ISAR installer %ISAR_RELEASE% build %INSTALLER_BUILD%: %OP%, started %STAMP%
>>"%LOG%" ver
>>"%LOG%" set SELF_DIR
>>"%LOG%" set USERPROFILE
>>"%LOG%" set TEMP
call :clear_inherited

:: --- argument checks: nothing has been changed yet -------------------------
set "ERR=an argument this installer does not know was given; see the log"
if defined ARG_UNKNOWN >>"%LOG%" set ARG_UNKNOWN
if defined ARG_UNKNOWN goto refuse
set "ERR=/repair and /uninstall cannot be combined"
if %OP_COUNT% GTR 1 goto refuse
set "ERR=this is the ISAR uninstaller: it only uninstalls, and takes /dir, /yes and /delete-profile"
if defined NAME_UNINSTALL if not "%OP%"=="uninstall" goto refuse
if defined NAME_UNINSTALL if defined REF goto refuse
if defined NAME_UNINSTALL if defined TEST_FAIL goto refuse
if not defined DESKTOP goto desktop_ok
call :match DESKTOP "yes" "no"
set "ERR=/desktop takes yes or no"
if not "%MATCH%"=="yes" goto refuse
set "ERR=/desktop belongs to an install"
if not "%OP%"=="install" goto refuse
:desktop_ok
if not defined DEL_PROFILE goto del_profile_ok
call :match DEL_PROFILE "yes" "no"
set "ERR=/delete-profile takes yes or no"
if not "%MATCH%"=="yes" goto refuse
set "ERR=/delete-profile belongs to an uninstall"
if not "%OP%"=="uninstall" goto refuse
:del_profile_ok
if not defined REF goto ref_ok
call :match REF "%REF_RE%"
set "ERR=the branch given with /ref may hold only letters, digits, dot, dash, underscore and slash"
if not "%MATCH%"=="yes" goto refuse
set "ERR=/ref is accepted only on an install"
if not "%OP%"=="install" goto refuse
:ref_ok
if not defined TEST_FAIL goto test_fail_ok
call :match TEST_FAIL "selftest" "selftest-hang" "download"
set "ERR=/test-fail takes selftest, selftest-hang or download"
if not "%MATCH%"=="yes" goto refuse
set "ERR=/test-fail is a test switch: it is accepted only together with /ref on an install, or on /repair of a test installation"
if "%OP%"=="uninstall" goto refuse
if "%OP%"=="install" if not defined REF goto refuse
:test_fail_ok
if defined REF >>"%LOG%" echo(TEST RUN: installing branch %REF% instead of release %ISAR_RELEASE%
if defined TEST_FAIL >>"%LOG%" echo(TEST RUN: forced failure at %TEST_FAIL%
if "%OP%"=="repair" goto repair
if "%OP%"=="uninstall" goto uninstall
goto install

:: Uninstall removes the folder this file is in, and cmd reads a batch file
:: line by line as it runs: control passes to a copy in %TEMP%, in this same
:: console (no call, so nothing returns here; no start, so no second window).
:: The copy's name makes it an uninstaller, and it removes its folder at exit.
:handoff
call :new_ids
if not defined HEX goto handoff_failed
set "HANDOFF_DIR=%TEMP%\isar-handoff-%STAMP%-%HEX:~0,8%"
mkdir "%HANDOFF_DIR%" 2>nul
copy /y "%SELF_FILE%" "%HANDOFF_DIR%\WINDOWS-uninstall-ISAR.bat" >nul 2>nul
if not exist "%HANDOFF_DIR%\WINDOWS-uninstall-ISAR.bat" goto handoff_failed
endlocal & cd /d "%HANDOFF_DIR%" & "%HANDOFF_DIR%\WINDOWS-uninstall-ISAR.bat" %*
:handoff_failed
echo.
echo [ERROR] Uninstall could not copy itself to a working folder in:
echo         %TEMP%
echo         Nothing was removed. Download WINDOWS-uninstall-ISAR.bat from the
echo         ISAR releases page on GitHub and double-click it instead.
endlocal & exit /b 1

:no_work
echo.
echo [ERROR] The installer could not make its working folder in:
echo         %TEMP%
echo         PowerShell may be missing or blocked, or the folder is not writable.
endlocal & exit /b 1

:: ===========================================================================
:: Install
:: ===========================================================================
:install
set "INSTALL_KIND=release"
set "SOURCE_REF=%ISAR_RELEASE%"
set "MODE="
set "OLD="
set "ENV_REMOVED="
set "APP_REMOVED="
set "OLD_RESULT="
if defined REF set "INSTALL_KIND=test"
if defined REF set "SOURCE_REF=%REF%"
set "ERR="
echo ============================================================================
echo   ISAR installer, release %ISAR_RELEASE%
if defined REF echo   TEST RUN: installs branch %REF% instead of the release
echo ============================================================================
if defined LOGFILE echo   Everything this installer does is written to:
if defined LOGFILE echo       %LOGFILE%
echo.

call :find_existing
if defined EXISTING >>"%LOG%" echo(Existing installation named by the locator: %EXISTING%

set "VBPATH="
if defined HAVE_DIR goto base_from_arg
if defined YES goto base_from_default
set "ASK_TRIES=0"

:ask_base
call :pick_default
echo Where should ISAR be installed?
echo   Everything goes into this one folder: ISAR, its own Python environment
echo   and the packages it downloads, a few GB in all. The folder name may
echo   contain only letters A-Z, digits, dot, dash and underscore: no spaces
echo   and no accented letters.
if defined DEFAULT_BASE echo   Press Enter to use:  %DEFAULT_BASE%
echo   or type a full path, for example D:\ISAR
set "VBPATH="
set /p "VBPATH=Install folder: "
>>"%LOG%" echo(Asked for the install folder; the answer, unchecked:
>>"%LOG%" set VBPATH
if not defined VBPATH set "VBPATH=%DEFAULT_BASE%"
if not defined VBPATH goto ask_base_again
call :validate_base_var
if not defined VB_REASON goto base_chosen
echo.
echo [REFUSED] That folder cannot be used: %VB_REASON%
echo.
>>"%LOG%" echo(REFUSE %VB_REASON%

:ask_base_again
set /a ASK_TRIES+=1
set "ERR=no usable install folder was given"
if %ASK_TRIES% GEQ 5 goto cancel
goto ask_base

:base_from_default
call :pick_default
set "ERR=no install folder was given, and no default folder is usable"
if not defined DEFAULT_BASE goto refuse
set "VBPATH=%DEFAULT_BASE%"
goto base_check

:base_from_arg
set "VBPATH=%ARG_DIR%"
>>"%LOG%" echo(Install folder given with /dir, unchecked:
>>"%LOG%" set VBPATH

:base_check
call :validate_base_var
if not defined VB_REASON goto base_chosen
echo.
echo [REFUSED] That folder cannot be used: %VB_REASON%
>>"%LOG%" echo(REFUSE %VB_REASON%
set "ERR=the install folder was refused: %VB_REASON%"
goto refuse

:base_chosen
set "BASE=%VBPATH%"
>>"%LOG%" echo(Install folder: %BASE%
call :engine_base_check
if defined ERR goto refuse

:: Two facts decide the mode: who owns the destination, and whether the
:: locator names another valid installation (which this run would replace).
:: same: the destination is an owned installation, replaced in place.
:: relocate: the locator names another one, removed after this one passes its
:: self-test (a retry finds the destination owned, and replaces it in place).
set "CB=%BASE%"
call :classify
>>"%LOG%" echo(Destination ownership: %OWN% %OWN_STATE% %OWN_WHY%
set "DEST_ID=%OWN_ID%"
set "OLD="
if defined EXISTING if /i not "%EXISTING%"=="%BASE%" set "OLD=%EXISTING%"
set "ERR=that folder is not an ISAR installation this installer can prove it owns: %OWN_WHY%"
if "%OWN%"=="foreign" goto refuse
set "DEST_OWNED="
if "%OWN%"=="owned" set "DEST_OWNED=yes"
set "DEST_PARTIAL="
if "%OWN%"=="partial" set "DEST_PARTIAL=yes"
set "MODE=fresh"
if defined DEST_OWNED set "MODE=same"
if defined OLD set "MODE=relocate"
>>"%LOG%" echo(MODE=%MODE%
if not defined OLD goto mode_dest
>>"%LOG%" echo(The installation in %OLD% is removed once this one passes its self-test.
set "PSI_A=%BASE%"
set "PSI_B=%OLD%"
call :inside
set "ERR=%BASE% lies inside the ISAR installation in %OLD%; choose a folder outside it"
if "%INSIDE%"=="yes" goto refuse
set "PSI_A=%OLD%"
set "PSI_B=%BASE%"
call :inside
set "ERR=the ISAR installation in %OLD% lies inside %BASE%; choose another folder"
if "%INSIDE%"=="yes" goto refuse
set "GB=%OLD%"
set "WS_TARGETS=%PAYLOAD_DIRS%"
call :check_before_removal
if "%GATE%"=="refuse" goto refuse
if "%GATE%"=="cancel" goto cancel
if "%GATE%"=="fail" goto fail
:mode_dest
if not defined DEST_OWNED goto mode_checked
set "GB=%BASE%"
set "WS_TARGETS=env*app"
call :check_before_removal
if "%GATE%"=="refuse" goto refuse
if "%GATE%"=="cancel" goto cancel
if "%GATE%"=="fail" goto fail
:mode_checked
set "ERR="
if defined DEST_PARTIAL goto ask_partial
goto ask_confirm

:ask_partial
echo.
echo An earlier install into this folder stopped at its very first step:
echo     %BASE%
echo Its ownership file will be replaced and the install continues from scratch.
>>"%LOG%" echo(The destination holds only a partial ownership pair: an install interrupted at its first step.
if defined YES goto ask_confirm
choice /C YN /N /M "Continue? (Y/N): "
set "ANSWER=%ERRORLEVEL%"
>>"%LOG%" echo(Asked: continue over the interrupted install? choice code %ANSWER%
set "ERR=you answered No"
if not "%ANSWER%"=="1" goto cancel

:ask_confirm
echo.
if "%MODE%"=="same" goto confirm_same
if "%MODE%"=="relocate" goto confirm_relocate
echo ISAR %ISAR_RELEASE% will be installed into:
echo     %BASE%
echo It downloads a private micromamba, builds a private Python environment
echo from ISAR's package lock, downloads ISAR's source and runs a self-test.
echo Nothing outside that folder is changed, apart from the log and a small
echo record under %%LOCALAPPDATA%%\ISAR, and a Desktop shortcut if you want one.
goto confirm_ask
:confirm_same
echo The ISAR installation in this folder will be replaced in place:
echo     %BASE%
echo Its Python environment is rebuilt from the package lock, and anything
echo saved inside %BASE%\env goes with it. Its copy of ISAR's source is moved
echo to the new commit. Files you added there that ISAR does not track stay,
echo unless that copy is damaged so that git cannot read it: then it is
echo downloaded again and they go. Your workspaces elsewhere, your profile and
echo the logs are not touched.
goto confirm_ask
:confirm_relocate
echo ISAR %ISAR_RELEASE% will be installed into:
echo     %BASE%
echo and once it passes its self-test, the installation in
echo     %OLD%
echo is removed: its copy of ISAR's source, Python environment, package cache
echo and launcher. Anything saved inside that copy of ISAR goes with it; other
echo files you put in that folder stay. If the new install fails, the old one
echo is left as it is.
:confirm_ask
if defined YES goto ask_desktop
choice /C YN /N /M "Go ahead? (Y/N): "
set "ANSWER=%ERRORLEVEL%"
>>"%LOG%" echo(Asked: go ahead with the %MODE% install? choice code %ANSWER%
set "ERR=you answered No"
if not "%ANSWER%"=="1" goto cancel

:ask_desktop
if defined DESKTOP goto desktop_decided
set "DESKTOP=yes"
if defined YES set "DESKTOP=no"
if defined YES goto desktop_decided
echo.
echo Put an "ISAR Launcher" shortcut on your Desktop when the install is done?
set "DESK_ANS="
set /p "DESK_ANS=Press Enter for Yes, or type N and Enter for No: "
if not defined DESK_ANS goto desktop_decided
call :match DESK_ANS "N" "n" "No" "NO" "no"
if "%MATCH%"=="yes" set "DESKTOP=no"
:desktop_decided
>>"%LOG%" echo(Desktop shortcut wanted: %DESKTOP%

:: ---------------------------------------------------------------------------
:: Preflight: everything is fetched and checked before the folder is touched
:: beyond a write test.
:: ---------------------------------------------------------------------------
call :step_start preflight "Checking the folder and downloading what the install needs"
if exist "%BASE%\" goto base_exists
mkdir "%BASE%" 2>nul
set "ERR=could not create %BASE%"
if not exist "%BASE%\" goto fail
set "BASE_CREATED=yes"
:base_exists
set "PROBE=%BASE%\isar-write-test-%HEX:~0,8%.tmp"
type nul > "%PROBE%" 2>nul
set "ERR=%BASE% is not writable"
if not exist "%PROBE%" goto fail
del /f /q "%PROBE%" >nul 2>nul

call :step_start micromamba "Downloading micromamba %MM_VERSION% and checking its checksum"
"%CURL%" --fail -sS -L --proto =https -o "%WORK%\micromamba.exe" "https://github.com/mamba-org/micromamba-releases/releases/download/%MM_VERSION%/micromamba-win-64" >>"%LOG%" 2>&1 <nul
set "ERR=micromamba could not be downloaded; check the internet connection and try again"
if errorlevel 1 goto fail
call :sha256 "%WORK%\micromamba.exe"
set "ERR=the downloaded micromamba does not match its pinned checksum, so it was not used"
if /i not "%SHA%"=="%MM_SHA256%" goto fail
call :step_ok

call :step_start resolve "Asking GitHub which source commit to install"
call :resolve
if defined RESOLVE_REFUSED goto refuse
if not defined COMMIT goto fail
>>"%LOG%" echo(SOURCE_COMMIT=%COMMIT%
call :step_ok

call :step_start lock "Downloading the package lock for that commit"
set "LOCK_NAME=isar-win-64.lock"
if "%TEST_FAIL%"=="download" set "LOCK_NAME=isar-win-64.lock.test-fail-download"
"%CURL%" --fail -sS -L --proto =https -o "%WORK%\isar-win-64.lock" "https://raw.githubusercontent.com/%GH_REPO%/%COMMIT%/installer/%LOCK_NAME%" >>"%LOG%" 2>&1 <nul
set "ERR=the package lock could not be downloaded; check the internet connection and try again"
if errorlevel 1 goto fail
:: /b, not /x: the lock is stored and served with LF endings, and findstr
:: matches /x and $ only before a CR.
findstr /b /l /c:"@EXPLICIT" "%WORK%\isar-win-64.lock" >nul 2>nul
set "ERR=the downloaded package lock is not in micromamba's explicit format"
if errorlevel 1 goto fail
call :sha256 "%WORK%\isar-win-64.lock"
set "ERR=the package lock's checksum could not be computed"
if not defined SHA goto fail
set "LOCK_SHA256=%SHA%"
>>"%LOG%" echo(LOCK_SHA256=%LOCK_SHA256%
call :step_ok
set "PF_FAIL="
if defined DEST_OWNED call :preflight_kept
if defined PF_FAIL goto fail
call :step_ok_named preflight

:: -- nothing has been removed or written into the folder before this line --

if not defined DEST_OWNED goto records_new
:: The installation here is replaced in place: once more, right before the
:: first deletion, ISAR must not be running and no workspace may be inside.
set "GB=%BASE%"
set "WS_TARGETS=env*app"
call :check_before_removal
if "%GATE%"=="refuse" goto refuse
if "%GATE%"=="cancel" goto cancel
if "%GATE%"=="fail" goto fail
call :step_start records "Marking the installation as being replaced"
set "CB=%BASE%"
call :classify
set "ERR=the folder changed during the preflight: %OWN_WHY%"
if not "%OWN%"=="owned" goto fail
if not "%OWN_ID%"=="%DEST_ID%" goto fail
set "INSTALL_ID=%DEST_ID%"
set "INSTALLED_AT=%NOW_ISO%"
set "MAMBA_SHA256_REC=%MM_SHA256%"
set "REC_STATE=installing"
call :write_record
set "RECORDS_WRITTEN=yes"
set "CB=%BASE%"
call :classify
set "ERR=the ownership records did not read back correctly: %OWN_WHY%"
if not "%OWN%"=="owned" goto fail
if not "%OWN_ID%"=="%INSTALL_ID%" goto fail
set "LOC_STATE=installing"
if not defined OLD call :write_locator
call :step_ok
call :step_start remove-env "Removing the old Python environment"
cd /d "%WORK%"
set "ENV_REMOVED=yes"
set "GB=%BASE%"
call :remove_entry env
set "ERR=the old environment could not be removed completely (a program may still be using it); close it and run the installer again"
if exist "%BASE%\env" goto fail
call :step_ok
goto records_done

:records_new
call :step_start records "Writing the ownership records"
set "CB=%BASE%"
call :classify
set "ERR=the folder changed during the preflight: %OWN_WHY%"
if "%OWN%"=="owned" goto fail
if "%OWN%"=="foreign" goto fail
if exist "%BASE%\.isar-install-root" del /f /q "%BASE%\.isar-install-root" >nul 2>nul
if exist "%BASE%\INSTALL_INFO.txt" del /f /q "%BASE%\INSTALL_INFO.txt" >nul 2>nul
set "ERR=the earlier ownership files could not be replaced"
if exist "%BASE%\.isar-install-root" goto fail
if exist "%BASE%\INSTALL_INFO.txt" goto fail
call :new_ids
set "ERR=no install ID could be made"
if not defined HEX goto fail
set "INSTALL_ID=%HEX%"
set "INSTALLED_AT=%NOW_ISO%"
set "MAMBA_SHA256_REC=%MM_SHA256%"
>"%BASE%\.isar-install-root" echo(INSTALL_ID=%INSTALL_ID%
set "REC_STATE=installing"
call :write_record
set "RECORDS_WRITTEN=yes"
set "CB=%BASE%"
call :classify
set "ERR=the ownership records did not read back correctly: %OWN_WHY%"
if not "%OWN%"=="owned" goto fail
if not "%OWN_ID%"=="%INSTALL_ID%" goto fail
set "LOC_STATE=installing"
if not defined OLD call :write_locator
call :step_ok
:records_done

call :step_start env-create "Building the Python environment from the lock; this takes several minutes"
if not exist "%BASE%\micromamba\" mkdir "%BASE%\micromamba" 2>nul
copy /y "%WORK%\micromamba.exe" "%BASE%\micromamba\micromamba.exe" >nul 2>nul
set "MM_EXE=%BASE%\micromamba\micromamba.exe"
set "ERR=micromamba could not be placed in %BASE%\micromamba"
if not exist "%MM_EXE%" goto fail
>>"%LOG%" 2>&1 "%MM_EXE%" --version <nul
:: The environment is built from the lock copy kept beside the launcher (the
:: one Repair rebuilds from), whose path is plain, unlike %TEMP%'s.
if not exist "%BASE%\launcher\" mkdir "%BASE%\launcher" 2>nul
copy /y "%WORK%\isar-win-64.lock" "%BASE%\launcher\isar-win-64.lock" >nul 2>nul
set "ERR=the package lock could not be placed in %BASE%\launcher"
if not exist "%BASE%\launcher\isar-win-64.lock" goto fail
echo   The download and build messages go to the log, not this window.
call :create_env
if defined ERR goto fail
call :step_ok

call :step_start source "Downloading ISAR's source at commit %COMMIT%"
call :source_with_refetch
if defined ERR goto fail
call :step_ok

call :step_start isar-install "Installing ISAR into the environment"
call :install_isar
if defined ERR goto fail
call :step_ok

call :step_start launchers "Placing the launcher"
call :place_launchers
if defined ERR goto fail
call :step_ok

call :step_start shortcut "Making the ISAR Launcher shortcut"
call :make_shortcut
if defined SHORTCUT_OK call :step_ok
if not defined SHORTCUT_OK >>"%LOG%" echo(STEP=shortcut failed
if not defined SHORTCUT_OK echo   [WARN] The shortcut could not be made; start ISAR with ISAR-Launcher.bat in the folder.
set "CUR_STEP="

set "REC_STATE=complete"
call :write_record
set "CB=%BASE%"
call :classify
set "ERR=the completed install record did not read back correctly: %OWN_WHY%"
if not "%OWN%"=="owned" goto fail
set "LOC_STATE=complete"
if not defined OLD call :write_locator

call :run_selftest
if "%SELFTEST%"=="pass" set "OUTCOME=installed"
if not "%SELFTEST%"=="pass" set "OUTCOME=installed-selftest-failed"
if "%SELFTEST%"=="pass" if defined OLD call :remove_old
goto finish

:: ===========================================================================
:: Repair: rebuilds the environment from the files kept in the installation
:: (the lock copy, micromamba, the package cache) and restores the recorded
:: source commit. It never asks GitHub for a tag or branch, so it never
:: changes the release, and it uses no Python or git it is repairing.
:: ===========================================================================
:repair
title ISAR Repair
set "ERR="
set "ENV_REMOVED="
set "APP_REMOVED="
echo ============================================================================
echo   ISAR Repair
echo ============================================================================
if defined LOGFILE echo   Everything Repair does is written to:
if defined LOGFILE echo       %LOGFILE%
echo.
set "ERR=Repair needs the installation folder; start it from the ISAR Launcher in that folder"
if not defined HAVE_DIR goto refuse
set "VBPATH=%ARG_DIR%"
>>"%LOG%" echo(Installation folder given with /dir, unchecked:
>>"%LOG%" set VBPATH
call :validate_base_var
set "ERR=the installation folder was refused: %VB_REASON%"
if defined VB_REASON goto refuse
set "BASE=%VBPATH%"
>>"%LOG%" echo(Installation folder: %BASE%
set "CB=%BASE%"
call :classify
>>"%LOG%" echo(Ownership: %OWN% %OWN_STATE% %OWN_WHY%
set "ERR=there is no ISAR installation in %BASE%"
if "%OWN%"=="none" goto refuse
set "ERR=%BASE% is not an ISAR installation this installer can prove it owns: %OWN_WHY%"
if not "%OWN%"=="owned" goto refuse
set "ERR=the installation in %BASE% never finished installing (its record says %OWN_STATE%); run the installer again into the same folder instead"
if not "%OWN_STATE%"=="complete" goto refuse
:: Every value below was checked against its grammar by :read_record.
set "INSTALL_ID=%II_INSTALL_ID%"
set "INSTALL_KIND=%II_INSTALL_KIND%"
set "SOURCE_REF=%II_SOURCE_REF%"
set "COMMIT=%II_SOURCE_COMMIT%"
set "LOCK_SHA256=%II_LOCK_SHA256%"
set "REC_MM_SHA=%II_MAMBA_SHA256%"
set "REC_MM_VER=%II_MAMBA_VERSION%"
set "SHOW_RELEASE=%II_ISAR_RELEASE%"
>>"%LOG%" echo(Recorded: INSTALL_KIND=%INSTALL_KIND% SOURCE_REF=%SOURCE_REF% SOURCE_COMMIT=%COMMIT%
set "ERR=/test-fail is a test switch: Repair accepts it only on a test installation"
if defined TEST_FAIL if not "%INSTALL_KIND%"=="test" goto refuse
set "ERR="

echo Repair rebuilds ISAR's Python environment in:
echo     %BASE%\env
echo from the package lock kept in the installation, puts ISAR's source back at
if "%INSTALL_KIND%"=="test" echo the recorded commit %COMMIT:~0,8% (TEST INSTALLATION of %SOURCE_REF%),
if not "%INSTALL_KIND%"=="test" echo the recorded commit %COMMIT:~0,8% (release %SHOW_RELEASE%),
echo reinstalls ISAR and runs the self-test. Your workspaces, profile and logs
echo are not touched. It takes several minutes.
if defined YES goto repair_running
choice /C YN /N /M "Repair now? (Y/N): "
set "ANSWER=%ERRORLEVEL%"
>>"%LOG%" echo(Asked: repair now? choice code %ANSWER%
set "ERR=you answered No"
if not "%ANSWER%"=="1" goto cancel
set "ERR="

:repair_running
set "GB=%BASE%"
call :running_gate
if "%GATE%"=="refuse" goto refuse
if "%GATE%"=="cancel" goto cancel

call :step_start preflight "Checking the files Repair rebuilds from"
set "LOCK_COPY=%BASE%\launcher\isar-win-64.lock"
set "ERR=the package lock kept in %BASE%\launcher is missing, so Repair cannot rebuild from it; run the installer again into the same folder"
if not exist "%LOCK_COPY%" goto fail
call :sha256 "%LOCK_COPY%"
set "ERR=the package lock kept in %BASE%\launcher is not the one this installation was built from, so Repair will not use it; run the installer again into the same folder"
if not "%SHA%"=="%LOCK_SHA256%" goto fail
set "MM_EXE=%BASE%\micromamba\micromamba.exe"
set "MM_NEED=yes"
if exist "%MM_EXE%" call :sha256 "%MM_EXE%"
if exist "%MM_EXE%" if "%SHA%"=="%REC_MM_SHA%" set "MM_NEED="
if "%TEST_FAIL%"=="download" set "MM_NEED=yes"
if not defined MM_NEED >>"%LOG%" echo(micromamba in the installation matches MAMBA_SHA256.
if not defined MM_NEED goto repair_mm_ok
call :step_start micromamba "Downloading micromamba %REC_MM_VER% again and checking its checksum"
set "MM_URL=https://github.com/mamba-org/micromamba-releases/releases/download/%REC_MM_VER%/micromamba-win-64"
if "%TEST_FAIL%"=="download" set "MM_URL=%MM_URL%.test-fail-download"
"%CURL%" --fail -sS -L --proto =https -o "%WORK%\micromamba.exe" "%MM_URL%" >>"%LOG%" 2>&1 <nul
set "ERR=micromamba could not be downloaded; check the internet connection and try again"
if errorlevel 1 goto fail
call :sha256 "%WORK%\micromamba.exe"
set "ERR=the downloaded micromamba does not match the checksum this installation recorded, so it was not used"
if not "%SHA%"=="%REC_MM_SHA%" goto fail
call :step_ok
:repair_mm_ok
set "GB=%BASE%"
set "WS_TARGETS=env"
call :workspace_gate
if "%GATE%"=="refuse" goto refuse
if "%GATE%"=="fail" goto fail
call :step_ok_named preflight

:: -- nothing has been removed or changed before this line --

call :step_start remove-env "Removing the old Python environment"
cd /d "%WORK%"
set "ENV_REMOVED=yes"
set "GB=%BASE%"
call :remove_entry env
set "ERR=the old environment could not be removed completely (a program may still be using it); close it and run Repair again"
if exist "%BASE%\env" goto fail
call :step_ok

call :step_start env-create "Rebuilding the Python environment from the lock; this takes several minutes"
if not defined MM_NEED goto repair_mm_placed
if not exist "%BASE%\micromamba\" mkdir "%BASE%\micromamba" 2>nul
copy /y "%WORK%\micromamba.exe" "%MM_EXE%" >nul 2>nul
call :sha256 "%MM_EXE%"
set "ERR=micromamba could not be placed in %BASE%\micromamba"
if not "%SHA%"=="%REC_MM_SHA%" goto fail
:repair_mm_placed
echo   The download and build messages go to the log, not this window.
call :create_env
if defined ERR goto fail
call :step_ok

call :step_start source "Putting ISAR's source back at commit %COMMIT%"
call :source_with_refetch
if defined ERR goto fail
call :step_ok

call :step_start isar-install "Installing ISAR into the environment"
call :install_isar
if defined ERR goto fail
call :step_ok

call :run_selftest
if "%SELFTEST%"=="pass" set "OUTCOME=repaired"
if not "%SELFTEST%"=="pass" set "OUTCOME=repaired-selftest-failed"
goto finish

:: ===========================================================================
:: Uninstall: only the owned payload goes, after the ownership proof, the
:: running check and the workspace scan; the profile and logs only on a Yes
:: (default No). The marker and record go last, so a stop can be finished.
:: ===========================================================================
:uninstall
title ISAR Uninstall
set "ERR="
set "MARKED="
set "RM_DONE="
set "RM_FAILED="
set "BASE_LEFT="
set "DESK_REMOVED="
echo ============================================================================
echo   ISAR Uninstall
echo ============================================================================
if defined LOGFILE echo   Everything Uninstall does is written to:
if defined LOGFILE echo       %LOGFILE%
echo.
cd /d "%WORK%"
if defined HAVE_DIR goto un_base_arg
call :find_existing
if defined EXISTING goto un_base_found
set "ERR=no ISAR installation was found from the record this computer keeps; name its folder with /dir"
if defined YES goto refuse
set "ASK_TRIES=0"
:un_ask_base
echo Which folder is ISAR installed in? It is the folder that holds
echo ISAR-Launcher.bat and INSTALL_INFO.txt, for example C:\Users\you\ISAR
set "VBPATH="
set /p "VBPATH=ISAR folder: "
>>"%LOG%" echo(Asked for the installation folder; the answer, unchecked:
>>"%LOG%" set VBPATH
set /a ASK_TRIES+=1
if not defined VBPATH goto un_ask_again
call :validate_base_var
if not defined VB_REASON goto un_base_check
echo.
echo [REFUSED] That folder cannot be an ISAR installation: %VB_REASON%
echo.
:un_ask_again
set "ERR=no ISAR installation folder was given"
if %ASK_TRIES% GEQ 5 goto cancel
goto un_ask_base
:un_base_found
set "VBPATH=%EXISTING%"
>>"%LOG%" echo(The locator record names the installation in %EXISTING%
goto un_base_check
:un_base_arg
set "VBPATH=%ARG_DIR%"
>>"%LOG%" echo(Installation folder given with /dir, unchecked:
>>"%LOG%" set VBPATH
:un_base_check
call :validate_base_var
set "ERR=the installation folder was refused: %VB_REASON%"
if defined VB_REASON goto refuse
set "BASE=%VBPATH%"
set "GB=%BASE%"
>>"%LOG%" echo(Installation folder: %BASE%
set "CB=%BASE%"
call :classify
>>"%LOG%" echo(Ownership: %OWN% %OWN_STATE% %OWN_WHY%
set "ERR=there is no ISAR installation in %BASE%"
if "%OWN%"=="none" goto refuse
set "ERR=%BASE% is not an ISAR installation this uninstaller can prove it owns, so nothing in it is removed: %OWN_WHY%"
if not "%OWN%"=="owned" goto refuse
:: Every value below was checked against its grammar by :read_record.
set "INSTALL_KIND=%II_INSTALL_KIND%"
set "SOURCE_REF=%II_SOURCE_REF%"
set "COMMIT=%II_SOURCE_COMMIT%"
set "SHOW_RELEASE=%II_ISAR_RELEASE%"
set "ERR="
call :running_gate
if "%GATE%"=="refuse" goto refuse
if "%GATE%"=="cancel" goto cancel

echo.
echo Uninstall removes ISAR from:
echo     %BASE%
echo that is its copy of ISAR's source (app), its Python environment (env), its
echo package cache (mamba), micromamba, the launcher files and shortcut, its
echo Desktop shortcut and its ownership records. Anything saved inside that
echo copy of ISAR or its environment goes with it. Other files you put in that
echo folder stay, and so do your workspaces.
if "%INSTALL_KIND%"=="test" echo This is a TEST INSTALLATION of %SOURCE_REF% at %COMMIT:~0,8%.
if defined YES goto un_profile
choice /C YN /N /M "Uninstall ISAR now? (Y/N): "
set "ANSWER=%ERRORLEVEL%"
>>"%LOG%" echo(Asked: uninstall now? choice code %ANSWER%
set "ERR=you answered No"
if not "%ANSWER%"=="1" goto cancel
set "ERR="
:un_profile
if defined DEL_PROFILE goto un_profile_decided
set "DEL_PROFILE=no"
if defined YES goto un_profile_decided
echo.
echo Also delete ISAR's profile and logs? That is the GUI's settings and its
echo list of recent workspaces, its logs, and the earlier install, launch and
echo repair logs. Your workspaces are not touched either way.
set "PROF_ANS="
set /p "PROF_ANS=Type Y and Enter to delete them, or just Enter to keep them: "
if not defined PROF_ANS goto un_profile_decided
call :match PROF_ANS "Y" "y" "Yes" "YES" "yes"
if "%MATCH%"=="yes" set "DEL_PROFILE=yes"
:un_profile_decided
>>"%LOG%" echo(Delete the profile and logs: %DEL_PROFILE%
call :read_ws_places
set "WS_TARGETS=%PAYLOAD_DIRS%"
if "%DEL_PROFILE%"=="yes" call :add_profile_targets
call :workspace_gate
if "%GATE%"=="refuse" goto refuse
if "%GATE%"=="fail" goto fail

:: -- nothing has been removed or changed before this line --

call :mark_removing
if not defined MR_OK goto fail
set "MARKED=yes"
call :remove_payload
if defined RM_FAILED goto fail
if "%DEL_PROFILE%"=="yes" call :remove_profile
if defined RM_FAILED goto fail
call :remove_records
if defined RM_FAILED goto fail
set "OUTCOME=uninstalled"
goto finish

:: ===========================================================================
:: Endings. Every outcome goes through :finish.
:: ===========================================================================
:fail
if defined CUR_STEP >>"%LOG%" echo(STEP=%CUR_STEP% failed
set "OUTCOME=failed"
goto finish

:refuse
set "OUTCOME=refused"
goto finish

:cancel
set "OUTCOME=cancelled"
goto finish

:finish
cd /d "%SystemRoot%" 2>nul
if "%OUTCOME%"=="installed" set "ERR="
if "%OUTCOME%"=="repaired" set "ERR="
if "%OUTCOME%"=="uninstalled" set "ERR="
if "%OUTCOME%"=="installed-selftest-failed" set "ERR=the self-test failed"
if "%OUTCOME%"=="repaired-selftest-failed" set "ERR=the self-test failed"
set "RC=1"
if "%OUTCOME%"=="installed" set "RC=0"
if "%OUTCOME%"=="repaired" set "RC=0"
if "%OUTCOME%"=="uninstalled" set "RC=0"
if "%OUTCOME%"=="installed-selftest-failed" set "RC=3"
if "%OUTCOME%"=="repaired-selftest-failed" set "RC=3"
if "%OUTCOME%"=="refused" set "RC=2"
if "%OUTCOME%"=="cancelled" set "RC=2"
if defined ERR >>"%LOG%" echo(REASON: %ERR%
if not "%OUTCOME%"=="failed" goto finish_no_tail
echo.
echo The last lines of the log:
echo ----------------------------------------------------------------------------
call :show_tail
echo ----------------------------------------------------------------------------
:finish_no_tail
:: An install that stopped before its records leaves nothing behind but an
:: empty folder it made itself, which goes (rd without /s removes only an
:: empty folder).
set "BASE_REMOVED="
if "%BASE_CREATED%"=="yes" if not defined RECORDS_WRITTEN rd "%BASE%" 2>nul
if "%BASE_CREATED%"=="yes" if not defined RECORDS_WRITTEN if not exist "%BASE%\" set "BASE_REMOVED=yes"
:: A failed relocation leaves the Desktop as it was: its shortcut opens the
:: old installation, which still works.
if "%OUTCOME%"=="installed" call :desktop_copy
if "%OUTCOME%"=="installed-selftest-failed" if not "%MODE%"=="relocate" call :desktop_copy
call :end_screen
>>"%LOG%" echo(OUTCOME=%OUTCOME%
call :remove_work
if defined YES goto finish_exit
if "%OUTCOME%"=="failed" call :open_notepad
if "%OUTCOME%"=="installed-selftest-failed" call :open_notepad
if "%OUTCOME%"=="repaired-selftest-failed" call :open_notepad
echo.
if not "%OP%"=="repair" choice /C C /N /M "Press C to close this window."
if "%OP%"=="repair" choice /C C /N /M "Press C to continue."
:finish_exit
:: An uninstall handed over to a copy in %TEMP% removes that copy's folder on
:: its very last line: "(goto)" ends this batch first, so cmd never reads the
:: deleted file again, and the rest of the line, already parsed, still runs.
set "SELF_CLEAN="
for %%I in ("%SELF_DIR%.") do set "SELF_LEAF=%%~nxI"
for %%I in ("%SELF_DIR%..") do set "SELF_PARENT=%%~fI"
if /i "%SELF_PARENT%\"=="%TEMP%\" if /i "%SELF_LEAF:~0,13%"=="isar-handoff-" set "SELF_CLEAN=%SELF_PARENT%\%SELF_LEAF%"
if defined SELF_CLEAN (goto) 2>nul & cd /d "%SystemRoot%" & rd /s /q "\\?\%SELF_CLEAN%" 2>nul & exit /b %RC%
endlocal & exit /b %RC%

:end_screen
echo.
echo ============================================================================
if "%OUTCOME%"=="installed" echo   ISAR IS INSTALLED AND CHECKED
if "%OUTCOME%"=="installed-selftest-failed" echo   ISAR IS INSTALLED, BUT ITS SELF-TEST FAILED
if "%OUTCOME%"=="repaired" echo   ISAR IS REPAIRED AND CHECKED
if "%OUTCOME%"=="repaired-selftest-failed" echo   ISAR IS REPAIRED, BUT ITS SELF-TEST FAILED
if "%OUTCOME%"=="uninstalled" echo   ISAR IS UNINSTALLED
set "OP_WORD=INSTALL"
if "%OP%"=="repair" set "OP_WORD=REPAIR"
if "%OP%"=="uninstall" set "OP_WORD=UNINSTALL"
if "%OUTCOME%"=="failed" echo   THE %OP_WORD% FAILED
if "%OUTCOME%"=="cancelled" echo   THE %OP_WORD% WAS CANCELLED
if "%OUTCOME%"=="refused" echo   THE %OP_WORD% WAS REFUSED
echo ============================================================================
if defined ERR echo Reason: %ERR%
if "%OUTCOME%"=="installed" goto end_installed
if "%OUTCOME%"=="installed-selftest-failed" goto end_selftest
if "%OUTCOME%"=="repaired" goto end_repaired
if "%OUTCOME%"=="repaired-selftest-failed" goto end_repaired_selftest
if "%OUTCOME%"=="uninstalled" goto end_uninstalled
if "%OP%"=="repair" if "%OUTCOME%"=="failed" goto end_repair_failed
if "%OP%"=="uninstall" if "%OUTCOME%"=="failed" goto end_uninstall_failed
if "%OUTCOME%"=="failed" goto end_failed
echo Nothing was installed, removed or changed.
goto end_log

:end_repaired
call :end_where
echo Repair rebuilt the Python environment, put ISAR's source back at the
echo recorded commit and reinstalled ISAR, which passed its self-test.
call :end_repair_removed
echo Start ISAR with "ISAR Launcher" in that folder.
goto end_log

:end_repaired_selftest
call :end_where
echo Repair rebuilt the environment and the source, but ISAR did not pass
echo its self-test:
call :show_check_failures
call :end_repair_removed
echo Choose Collect diagnostics in the launcher and send the zip it makes.
goto end_log

:end_repair_failed
if not defined ENV_REMOVED echo Nothing was removed or changed.
if not defined ENV_REMOVED goto end_log
call :end_repair_removed
echo ISAR cannot start until a Repair finishes. Run Repair again; if it fails
echo again, choose Collect diagnostics in the launcher and send the zip it makes.
goto end_log

:end_repair_removed
echo Removed: the old Python environment (%BASE%\env).
if defined APP_REMOVED echo Removed: the old copy of ISAR's source (%BASE%\app), downloaded again.
echo Your workspaces, profile and logs were not touched.
goto :eof

:end_installed
call :end_where
echo ISAR passed its self-test. Start it with "ISAR Launcher" in that folder.
if "%MODE%"=="fresh" echo Nothing was removed.
if defined ENV_REMOVED call :end_replaced
if "%MODE%"=="relocate" call :end_old
goto end_log

:end_selftest
call :end_where
echo The files are in place, but ISAR did not pass its self-test:
call :show_check_failures
if "%MODE%"=="fresh" echo Nothing was removed.
if defined ENV_REMOVED call :end_replaced
if "%MODE%"=="relocate" echo The earlier installation in %OLD% was not touched, and still works.
echo Try Repair in the launcher; if it fails too, choose Collect diagnostics
echo there and send the zip it makes.
goto end_log

:end_replaced
echo The installation already in this folder was replaced in place: its Python
echo environment was removed and built again, and its source moved to the commit.
if defined APP_REMOVED echo Its old copy of ISAR's source could not be reused, so it was removed and downloaded again.
goto :eof

:end_old
if "%OLD_RESULT%"=="removed" goto end_old_removed
if "%OLD_RESULT%"=="partial" goto end_old_partial
echo The earlier installation in %OLD% was kept: %OLD_WHY%
echo Uninstall it from its own ISAR Launcher when you are ready.
goto :eof
:end_old_removed
echo The earlier installation in %OLD% was removed: %RM_DONE:~2%.
if defined BASE_LEFT echo Its folder stays, because it still holds files that are not ISAR's.
goto :eof
:end_old_partial
echo The earlier installation in %OLD% was only partly removed: %OLD_WHY%
if defined RM_DONE echo Removed from it: %RM_DONE:~2%.
echo Finish it with WINDOWS-uninstall-ISAR.bat, naming that folder.
goto :eof

:end_uninstalled
echo Removed from %BASE%: %RM_DONE:~2%, and the ownership records.
if defined DESK_REMOVED echo Removed: the ISAR Launcher shortcut on the Desktop.
if defined BASE_LEFT echo Kept: the folder itself, which still holds files that are not ISAR's.
if not defined BASE_LEFT echo The folder was empty afterwards, so it was removed too.
call :end_profile
call :end_places
goto end_log

:end_uninstall_failed
if not defined MARKED echo Nothing was removed.
if not defined MARKED goto end_log
if defined RM_DONE echo Removed so far: %RM_DONE:~2%.
echo Uninstall stopped before it finished. The ownership records stay, so the
echo installation is still recognised: close any program using it and run
echo Uninstall again to finish.
call :end_places
goto end_log

:end_profile
if "%DEL_PROFILE%"=="yes" goto end_profile_deleted
echo Kept: ISAR's profile and logs, in %APPDATA%\isar_gui and %LOCALAPPDATA%\ISAR\logs.
goto :eof
:end_profile_deleted
echo Deleted: ISAR's profile and logs, in %APPDATA%\isar_gui, and the earlier
echo install, launch and repair logs. The log of this run is kept.
goto :eof

:end_places
echo Your workspaces were not touched. The places this computer knows of
echo (not necessarily all of them):
if exist "%WORK%\ws_places.txt" type "%WORK%\ws_places.txt"
goto :eof

:end_failed
if "%MODE%"=="relocate" echo The earlier installation in %OLD% was not touched, and still works.
if defined ENV_REMOVED goto end_failed_replaced
if defined RECORDS_WRITTEN goto end_failed_partial
if defined BASE_REMOVED echo Nothing was installed. The empty folder made for it was removed again.
if not defined BASE_REMOVED echo Nothing was installed, and nothing was removed.
goto end_log

:end_failed_replaced
echo The old Python environment in this folder was removed, and the new install
echo did not finish:
echo     %BASE%
echo Its ownership records stay. Run this installer again into the same folder
echo to finish the install.
goto end_log

:end_failed_partial
echo Nothing was removed. What was installed so far stays in:
echo     %BASE%
echo with its ownership records, so it is recognised as ISAR's. Run this
echo installer again into the same folder to finish the install.
goto end_log

:end_where
echo Installed in: %BASE%
if "%INSTALL_KIND%"=="test" echo TEST INSTALLATION of %SOURCE_REF% at %COMMIT:~0,8%, not a release
if not "%INSTALL_KIND%"=="test" echo Release:      %SHOW_RELEASE% (commit %COMMIT:~0,8%)
goto :eof

:end_log
if defined LOGFILE echo The log of this run is:
if defined LOGFILE echo     %LOGFILE%
if not defined LOGFILE echo No log file could be written for this run.
goto :eof

:show_check_failures
findstr /b /l /c:"CHECK " "%LOGFILE%" > "%WORK%\checks.txt" 2>nul
findstr /l /c:" FAIL " "%WORK%\checks.txt" 2>nul
if errorlevel 1 echo   (no CHECK line failed; the self-test itself did not finish, see the log)
goto :eof

:: The last 30 lines of the log with cmd's own tools, written to a file first.
:show_tail
if not defined LOGFILE goto :eof
set "LINES=0"
"%SystemRoot%\System32\find.exe" /c /v "" < "%LOGFILE%" > "%WORK%\lines.txt" 2>nul
set /p "LINES=" < "%WORK%\lines.txt"
call :match LINES "%D%%D%*"
if not "%MATCH%"=="yes" set "LINES=0"
set /a SKIP=LINES-30
if %SKIP% LSS 0 set "SKIP=0"
"%SystemRoot%\System32\more.com" +%SKIP% "%LOGFILE%" > "%WORK%\tail.txt" 2>nul
type "%WORK%\tail.txt"
goto :eof

:open_notepad
if not defined LOGFILE goto :eof
start "" "%SystemRoot%\System32\notepad.exe" "%LOGFILE%"
goto :eof

:remove_work
if not defined WORK goto :eof
cd /d "%SystemRoot%" 2>nul
rd /s /q "%WORK%" 2>nul
goto :eof

:: ===========================================================================
:: Install steps
:: ===========================================================================

:: Resolve the release tag (or the test branch) to one 40-hex commit through
:: the GitHub API. Sets COMMIT, or RESOLVE_REFUSED with ERR.
:resolve
set "COMMIT="
set "RESOLVE_REFUSED="
if "%INSTALL_KIND%"=="test" goto resolve_commit
call :gh_get "releases/tags/%ISAR_RELEASE%"
if "%GH_CODE%"=="200" goto resolve_commit
set "RESOLVE_REFUSED=yes"
set "ERR=release %ISAR_RELEASE% is not published on GitHub"
if "%GH_CODE%"=="404" goto :eof
set "RESOLVE_REFUSED="
call :gh_error
goto :eof
:resolve_commit
call :gh_get "commits/%SOURCE_REF%"
if "%GH_CODE%"=="200" goto resolve_parse
set "RESOLVE_REFUSED=yes"
set "ERR=%SOURCE_REF% was not found in the ISAR repository on GitHub"
if "%GH_CODE%"=="404" goto :eof
if "%GH_CODE%"=="422" goto :eof
set "RESOLVE_REFUSED="
call :gh_error
goto :eof
:resolve_parse
set "PSI_A=%WORK%\gh.json"
del /f /q "%WORK%\sha.txt" >nul 2>nul
powershell -NoProfile -NonInteractive -Command "try { $j=ConvertFrom-Json ([IO.File]::ReadAllText($env:PSI_A)); [string]$j.sha } catch { 'none' }" > "%WORK%\sha.txt" 2>nul <nul
set "COMMIT="
set /p "COMMIT=" < "%WORK%\sha.txt"
call :match_hex COMMIT 40
set "ERR=GitHub's answer named no source commit"
if not "%MATCH%"=="yes" set "COMMIT="
goto :eof

:gh_error
set "ERR=GitHub did not answer (HTTP %GH_CODE%); check the internet connection and try again"
if "%GH_CODE%"=="403" set "ERR=GitHub's limit for anonymous requests was reached; try again in an hour"
if "%GH_CODE%"=="429" set "ERR=GitHub's limit for anonymous requests was reached; try again in an hour"
goto :eof

:: %1 = API path under repos/<owner>/<repo>/. Sets GH_CODE (000 when curl failed).
:gh_get
del /f /q "%WORK%\gh.json" "%WORK%\gh.code" >nul 2>nul
set "GH_URL=https://api.github.com/repos/%GH_REPO%/%~1"
>>"%LOG%" echo(GET %GH_URL%
"%CURL%" -sS -L --proto =https -H "Accept: application/vnd.github+json" -H "User-Agent: isar-installer" -o "%WORK%\gh.json" -w "%%{http_code}" "%GH_URL%" > "%WORK%\gh.code" 2>>"%LOG%" <nul
set "GH_CODE="
set /p "GH_CODE=" < "%WORK%\gh.code"
call :match GH_CODE "%D%%D%%D%"
if not "%MATCH%"=="yes" set "GH_CODE=000"
>>"%LOG%" echo(HTTP %GH_CODE%
goto :eof

:: micromamba writes the prefix into %USERPROFILE%\.conda\environments.txt and
:: conda-forge's git adds a Start Menu shortcut, and neither can be switched
:: off (micromamba 2.5.0 has no register_envs setting; --no-shortcuts is
:: passed too). So every create runs with USERPROFILE moved into the working
:: folder, which is removed at the end: the real profile is never touched.
:mm_profile_on
set "PROFILE_KEEP=%USERPROFILE%"
set "USERPROFILE=%WORK%\mm-profile"
mkdir "%USERPROFILE%" 2>nul
goto :eof
:mm_profile_off
set "USERPROFILE=%PROFILE_KEEP%"
set "PROFILE_KEEP="
goto :eof

:: The environment from the lock copy kept beside the launcher (C-lock), into
:: the installation's own root prefix and package cache. Sets ERR on failure.
:create_env
set "ERR="
call :mm_profile_on
"%MM_EXE%" create -y --no-rc --no-shortcuts -r "%BASE%\mamba" -p "%BASE%\env" -f "%BASE%\launcher\isar-win-64.lock" >>"%LOG%" 2>&1 <nul
set "MM_RC=%ERRORLEVEL%"
call :mm_profile_off
set "ERR=the Python environment could not be built; check the internet connection and the free disk space, then try again"
if not "%MM_RC%"=="0" goto :eof
if not exist "%BASE%\env\python.exe" goto :eof
set "ERR="
goto :eof

:: The source at the commit; when a kept copy cannot be brought there, it is
:: removed and downloaded afresh, after checking that nothing of the user's is
:: saved inside it. Without git a fresh download fails too, so it stays.
:source_with_refetch
call :get_source
if not defined ERR goto :eof
if not exist "%BASE%\app" goto :eof
if not exist "%GIT%" goto :eof
:: A checkout that git can still read is kept whatever failed: a dropped connection
:: must not cost the user's own files. ERR says what to do.
if not exist "%BASE%\app\.git\" goto source_refetch
"%GIT%" -c core.longpaths=true -C "%BASE%\app" rev-parse --git-dir >>"%LOG%" 2>&1 <nul
if not errorlevel 1 goto :eof
:source_refetch
>>"%LOG%" echo(The kept source could not be brought to the commit: %ERR%
>>"%LOG%" echo(It is removed and downloaded again.
echo   The kept copy of ISAR's source could not be reused; downloading it again.
set "GB=%BASE%"
set "WS_TARGETS=app"
call :workspace_gate
if "%GATE%"=="refuse" set "ERR=a workspace is saved inside ISAR's source folder; move it out of %BASE%\app, then try again"
if not "%GATE%"=="ok" goto :eof
cd /d "%WORK%"
set "APP_REMOVED=yes"
call :remove_entry app
set "ERR=the old copy of ISAR's source could not be removed completely; close any program using it and try again"
if exist "%BASE%\app" goto :eof
call :get_source
goto :eof

:: Bring <base>\app to the commit with the environment's git: a new checkout
:: is made, a kept one (Repair) is reused and fetches only when the commit is
:: missing. The tracked workspace markers are written before the checkout (D4).
:get_source
set "ERR="
set "GIT=%BASE%\env\Library\bin\git.exe"
set "ERR=the environment has no git at %GIT%"
if not exist "%GIT%" goto :eof
set "ERR="
call :git_env
set "APP=%BASE%\app"
cd /d "%WORK%"
if exist "%APP%\.git\" goto source_have_repo
if not exist "%APP%\" mkdir "%APP%" 2>nul
set "ERR=could not create %APP%"
if not exist "%APP%\" goto :eof
"%GIT%" -c core.longpaths=true -C "%APP%" init -q >>"%LOG%" 2>&1 <nul
set "ERR=git init failed in %APP%"
if errorlevel 1 goto :eof
"%GIT%" -c core.longpaths=true -C "%APP%" remote add origin "https://github.com/%GH_REPO%.git" >>"%LOG%" 2>&1 <nul
set "ERR=git could not add the ISAR remote"
if errorlevel 1 goto :eof
goto source_fetch
:source_have_repo
>>"%LOG%" echo(Reusing the git checkout kept in %APP%
call :clear_git_locks
"%GIT%" -c core.longpaths=true -C "%APP%" remote get-url origin >>"%LOG%" 2>&1 <nul
if not errorlevel 1 goto source_have_origin
:: An install stopped between git init and the remote's creation leaves none.
"%GIT%" -c core.longpaths=true -C "%APP%" remote add origin "https://github.com/%GH_REPO%.git" >>"%LOG%" 2>&1 <nul
if not errorlevel 1 goto source_have_origin
set "ERR=git could not add the ISAR remote"
goto :eof
:source_have_origin
"%GIT%" -c core.longpaths=true -C "%APP%" cat-file -e "%COMMIT%^{commit}" >>"%LOG%" 2>&1 <nul
if not errorlevel 1 goto source_fetched
:source_fetch
"%GIT%" -c core.longpaths=true -C "%APP%" fetch -q --depth 1 origin %COMMIT% >>"%LOG%" 2>&1 <nul
set "ERR=git could not download commit %COMMIT%; check the internet connection and try again"
if errorlevel 1 goto :eof
:source_fetched
call :write_markers
if defined ERR goto :eof
"%GIT%" -c core.longpaths=true -C "%APP%" checkout -q --detach --force %COMMIT% >>"%LOG%" 2>&1 <nul
set "ERR=git could not check out commit %COMMIT%"
if errorlevel 1 goto :eof
"%GIT%" -c core.longpaths=true -C "%APP%" rev-parse HEAD > "%WORK%\head.txt" 2>>"%LOG%" <nul
set "HEAD_NOW="
set /p "HEAD_NOW=" < "%WORK%\head.txt"
call :match_hex HEAD_NOW 40
set "ERR=the checked-out source is not at commit %COMMIT%"
if not "%MATCH%"=="yes" goto :eof
if not "%HEAD_NOW%"=="%COMMIT%" goto :eof
set "ERR="
goto :eof

:: An interrupted git run leaves a lock file, and every later checkout fails on
:: it. Reached only after the ownership proof and the running check, so the locks
:: are the installation's own.
:clear_git_locks
for /r "%APP%\.git" %%L in (*.lock) do call :clear_one_lock "%%L"
goto :eof
:clear_one_lock
>>"%LOG%" echo(Removing a stale git lock left by an earlier run: %~1
del /f /q "%~1" >nul 2>nul
goto :eof

:: <base>\tracked-workspace-markers.txt: the files at this commit that look like
:: a workspace marker (card.policy.toml, gui.state.json), app-relative with
:: backslashes, so a workspace scan can tell ISAR's own fixtures from a user's.
:write_markers
set "ERR=git could not list the files of commit %COMMIT%"
"%GIT%" -c core.longpaths=true -C "%APP%" ls-tree -r --name-only %COMMIT% > "%WORK%\tree.txt" 2>>"%LOG%" <nul
if errorlevel 1 goto :eof
set "PSI_A=%WORK%\tree.txt"
set "PSI_B=%BASE%\tracked-workspace-markers.txt"
del /f /q "%WORK%\markers.ok" >nul 2>nul
powershell -NoProfile -NonInteractive -Command "try { $o=@(foreach ($l in [IO.File]::ReadAllLines($env:PSI_A)) { $n=$l.Split('/')[-1]; if ($n -ceq 'card.policy.toml' -or $n -ceq 'gui.state.json') { $l.Replace('/','\') } }); [IO.File]::WriteAllLines($env:PSI_B, [string[]]$o); 'ok ' + $o.Count } catch { 'fail ' + $_.Exception.Message }" > "%WORK%\markers.ok" 2>nul <nul
>>"%LOG%" type "%WORK%\markers.ok"
findstr /b /c:"ok " "%WORK%\markers.ok" >nul 2>nul
set "ERR=the list of ISAR's own workspace-like files could not be written"
if errorlevel 1 goto :eof
if not exist "%PSI_B%" goto :eof
set "ERR="
goto :eof

:: The environment's PATH, and git with no system or user configuration and
:: no prompt.
:git_env
call :env_path
type nul > "%WORK%\empty.gitconfig"
set "GIT_CONFIG_NOSYSTEM=1"
set "GIT_CONFIG_GLOBAL=%WORK%\empty.gitconfig"
set "GIT_TERMINAL_PROMPT=0"
goto :eof

:: Same folder (K4): every locked package is fetched and its checksum checked
:: into the kept package cache (micromamba --download-only, against a scratch
:: prefix that is removed again), and the commit is fetched into the kept
:: source with the old environment's git when that git runs. Neither changes
:: a working file. PF_FAIL=yes with ERR when the packages could not be had.
:preflight_kept
set "PF_FAIL=yes"
set "CUR_STEP=preflight"
echo   Downloading and checking every package into the kept package cache.
if not exist "%BASE%\mamba\" mkdir "%BASE%\mamba" 2>nul
set "PF_PREFIX=%BASE%\mamba\envs\isar-preflight"
set "PF_LOCK=%BASE%\mamba\isar-preflight.lock"
copy /y "%WORK%\isar-win-64.lock" "%PF_LOCK%" >nul 2>nul
set "ERR=the package lock could not be placed in %BASE%\mamba"
if not exist "%PF_LOCK%" goto :eof
set "MM_EXE=%WORK%\micromamba.exe"
if exist "%PF_PREFIX%\" rd /s /q "\\?\%PF_PREFIX%" 2>nul
call :mm_profile_on
"%MM_EXE%" create -y --no-rc --no-shortcuts --download-only -r "%BASE%\mamba" -p "%PF_PREFIX%" -f "%PF_LOCK%" >>"%LOG%" 2>&1 <nul
set "PF_RC=%ERRORLEVEL%"
call :mm_profile_off
if exist "%PF_PREFIX%\" rd /s /q "\\?\%PF_PREFIX%" 2>nul
del /f /q "%PF_LOCK%" >nul 2>nul
set "ERR=the packages could not all be downloaded and checked; check the internet connection and the free disk space, then try again"
if not "%PF_RC%"=="0" goto :eof
set "PF_FAIL="
set "ERR="
set "GIT=%BASE%\env\Library\bin\git.exe"
if not exist "%BASE%\app\.git\" goto :eof
if not exist "%GIT%" goto :eof
call :git_env
"%GIT%" -c core.longpaths=true --version >>"%LOG%" 2>&1 <nul
if errorlevel 1 goto preflight_kept_nogit
echo   Downloading commit %COMMIT:~0,8% into the kept copy of ISAR's source.
"%GIT%" -c core.longpaths=true -C "%BASE%\app" fetch -q --depth 1 origin %COMMIT% >>"%LOG%" 2>&1 <nul
if errorlevel 1 goto preflight_kept_fetchfail
set "PATH=%SYS_PATH%"
goto :eof
:preflight_kept_fetchfail
set "PATH=%SYS_PATH%"
>>"%LOG%" echo([FAIL] the kept source could not fetch the commit; nothing has been removed
set "PF_FAIL=yes"
set "ERR=the new version of ISAR's source could not be downloaded; nothing was changed and the installation still works. Check the internet connection, then try again"
goto :eof
:preflight_kept_nogit
>>"%LOG%" echo(The old environment's git does not run; the source is fetched after the rebuild.
set "PATH=%SYS_PATH%"
goto :eof

:: ISAR goes in without resolving or replacing any dependency (D6), and with
:: no pip configuration from the user, the site or the machine.
:install_isar
set "ERR="
call :env_path
set "PIP_CONFIG_FILE=nul"
cd /d "%WORK%"
"%BASE%\env\python.exe" -I -m pip install --no-deps --no-build-isolation --no-index --no-cache-dir --disable-pip-version-check -e "%BASE%\app" >>"%LOG%" 2>&1 <nul
set "ERR=pip could not install ISAR into the environment"
if errorlevel 1 goto :eof
set "ERR="
goto :eof

:place_launchers
set "ERR=could not create %BASE%\launcher"
if not exist "%BASE%\launcher\" mkdir "%BASE%\launcher" 2>nul
if not exist "%BASE%\launcher\" goto :eof
set "ERR=could not copy the launcher files from %BASE%\app\installer"
copy /y "%BASE%\app\installer\launchers\ISAR-Launcher.bat" "%BASE%\ISAR-Launcher.bat" >nul 2>nul
if errorlevel 1 goto :eof
copy /y "%BASE%\app\installer\launchers\run-isar.bat" "%BASE%\launcher\run-isar.bat" >nul 2>nul
if errorlevel 1 goto :eof
copy /y "%BASE%\app\installer\launchers\collect-diagnostics.bat" "%BASE%\launcher\collect-diagnostics.bat" >nul 2>nul
if errorlevel 1 goto :eof
copy /y "%BASE%\app\installer\WINDOWS-install-ISAR.bat" "%BASE%\launcher\WINDOWS-install-ISAR.bat" >nul 2>nul
if errorlevel 1 goto :eof
set "ERR="
goto :eof

:: <base>\ISAR Launcher.lnk, target <base>\ISAR-Launcher.bat: Uninstall removes
:: a Desktop copy only when it points there.
:make_shortcut
set "SHORTCUT_OK="
set "PSI_A=%BASE%\ISAR Launcher.lnk"
set "PSI_B=%BASE%\ISAR-Launcher.bat"
set "PSI_C=%BASE%"
powershell -NoProfile -NonInteractive -Command "try { $s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:PSI_A); $s.TargetPath=$env:PSI_B; $s.WorkingDirectory=$env:PSI_C; $s.Description='ISAR Launcher'; $s.Save(); 'ok' } catch { 'fail ' + $_.Exception.Message }" >>"%LOG%" 2>&1 <nul
if exist "%PSI_A%" set "SHORTCUT_OK=yes"
goto :eof

:: The self-test (C-selftest) from a private folder outside the installation.
:: Python supervises the GUI start, its timeout and its nonce (D5); this only
:: reads the exit code.
:run_selftest
set "SELFTEST=fail"
call :step_start selftest "Running the self-test (ISAR starts without a window; up to a few minutes)"
call :env_path
set "ST_DIR=%WORK%\selftest"
mkdir "%ST_DIR%" 2>nul
set "ST_TIMEOUT="
if "%TEST_FAIL%"=="selftest" set "ISAR_GUI_SELFTEST_FORCE=fail"
if "%TEST_FAIL%"=="selftest-hang" set "ISAR_GUI_SELFTEST_FORCE=hang"
if "%TEST_FAIL%"=="selftest-hang" set "ST_TIMEOUT=--timeout 20"
cd /d "%ST_DIR%"
>>"%LOG%" echo(SELFTEST_STARTED_AT %TIME%
"%BASE%\env\python.exe" -I -m isar.install_check --expect-root "%BASE%\app" --work "%ST_DIR%" %ST_TIMEOUT% >>"%LOG%" 2>&1 <nul
set "ST_RC=%ERRORLEVEL%"
>>"%LOG%" echo(SELFTEST_ENDED_AT %TIME%
cd /d "%WORK%"
set "ISAR_GUI_SELFTEST_FORCE="
if "%ST_RC%"=="0" set "SELFTEST=pass"
if "%SELFTEST%"=="pass" call :step_ok
if not "%SELFTEST%"=="pass" >>"%LOG%" echo(STEP=selftest failed
set "CUR_STEP="
goto :eof

:: A copy of the shortcut on the Desktop Windows reports (it follows OneDrive
:: and policy redirection), or on ISAR_TEST_DESKTOP in a test.
:desktop_copy
if not "%DESKTOP%"=="yes" goto :eof
if not exist "%BASE%\ISAR Launcher.lnk" goto :eof
set "PSI_A=%BASE%\ISAR Launcher.lnk"
del /f /q "%WORK%\desk.txt" >nul 2>nul
powershell -NoProfile -NonInteractive -Command "try { $d=$env:ISAR_TEST_DESKTOP; if (-not $d) { $d=[Environment]::GetFolderPath('Desktop') }; if (-not $d) { throw 'Windows reports no Desktop folder' }; $t=[IO.Path]::Combine($d,'ISAR Launcher.lnk'); $w=New-Object -ComObject WScript.Shell; if ([IO.File]::Exists($t)) { $o=$w.CreateShortcut($t).TargetPath; $m=$w.CreateShortcut($env:PSI_A).TargetPath; if ($o -ine $m) { 'KEPT The Desktop already has an ISAR Launcher shortcut that opens ' + $o + ', not this installation; it was left as it is. This installation opens from ' + $env:PSI_A; return } }; Copy-Item -LiteralPath $env:PSI_A -Destination $t -Force -ErrorAction Stop; 'OK A copy of the ISAR Launcher shortcut is on the Desktop: ' + $t } catch { 'FAIL The shortcut could not be copied to the Desktop: ' + $_.Exception.Message }" > "%WORK%\desk.txt" 2>nul <nul
type "%WORK%\desk.txt" 2>nul
>>"%LOG%" type "%WORK%\desk.txt"
goto :eof

:: Removes the owned payload entry %1, a name directly under GB: a folder
:: by rd /s /q on its \\?\ form, which reaches paths past 260 characters
:: (ISAR's source holds some) and removes a junction itself, never what it
:: points to; a file by del. The caller checks that it is gone.
:remove_entry
set "RM_PATH=%GB%\%~1"
if exist "%RM_PATH%\" rd /s /q "\\?\%RM_PATH%" 2>nul
if exist "%RM_PATH%" if not exist "%RM_PATH%\" del /f /q /a "%RM_PATH%" >nul 2>nul
goto :eof

:: The running check, then the workspace scan of WS_TARGETS, on the
:: installation GB; GATE as those set it.
:check_before_removal
call :running_gate
if not "%GATE%"=="ok" goto :eof
call :workspace_gate
goto :eof

:: Removes the owned payload of the installation GB, each entry confirmed
:: gone: app, env, mamba, micromamba, then the launcher files, its shortcut
:: and the Desktop shortcut that opens this installation (no other). Stops at
:: the first entry that stays (RM_FAILED, with ERR); RM_DONE lists what went.
:remove_payload
set "RM_FAILED="
set "RM_DONE="
cd /d "%WORK%"
call :remove_step app "its copy of ISAR's source (app)"
call :remove_step env "its Python environment (env)"
call :remove_step mamba "its package cache (mamba)"
call :remove_step micromamba "micromamba"
if defined RM_FAILED goto :eof
call :step_start remove-launcher "Removing the launcher files and shortcuts"
call :remove_entry launcher
call :remove_entry ISAR-Launcher.bat
call :remove_entry "ISAR Launcher.lnk"
set "ERR=the launcher files in %GB% could not all be removed"
set "RM_FAILED=launcher"
if exist "%GB%\launcher" goto remove_step_failed
if exist "%GB%\ISAR-Launcher.bat" goto remove_step_failed
if exist "%GB%\ISAR Launcher.lnk" goto remove_step_failed
set "RM_FAILED="
call :remove_desktop_link
set "RM_DONE=%RM_DONE%, the launcher files"
call :step_ok
goto :eof

:remove_step
if defined RM_FAILED goto :eof
call :step_start remove-%~1 "Removing %~2"
call :remove_entry %~1
set "ERR=%GB%\%~1 could not be removed completely (a program may still be using it); close it and try again"
set "RM_FAILED=%~1"
if exist "%GB%\%~1" goto remove_step_failed
set "RM_FAILED="
set "RM_DONE=%RM_DONE%, %~2"
call :step_ok
goto :eof
:remove_step_failed
>>"%LOG%" echo(STEP=%CUR_STEP% failed
set "CUR_STEP="
goto :eof

:: The Desktop shortcut ISAR Launcher.lnk goes only when it opens GB's
:: ISAR-Launcher.bat (TAVI's rule); any other shortcut of that name stays.
:remove_desktop_link
set "PSI_A=%GB%\ISAR-Launcher.bat"
del /f /q "%WORK%\desk.txt" >nul 2>nul
powershell -NoProfile -NonInteractive -Command "try { $d=$env:ISAR_TEST_DESKTOP; if (-not $d) { $d=[Environment]::GetFolderPath('Desktop') }; if (-not $d) { 'NONE Windows reports no Desktop folder' } else { $l=[IO.Path]::Combine($d,'ISAR Launcher.lnk'); if (-not [IO.File]::Exists($l)) { 'NONE no ISAR Launcher shortcut on the Desktop' } else { $t=(New-Object -ComObject WScript.Shell).CreateShortcut($l).TargetPath; if ($t -ieq $env:PSI_A) { Remove-Item -LiteralPath $l -Force -ErrorAction Stop; 'REMOVED ' + $l } else { 'KEPT ' + $l + ' opens ' + $t + ', not this installation' } } } } catch { 'ERROR ' + $_.Exception.Message }" > "%WORK%\desk.txt" 2>nul <nul
>>"%LOG%" type "%WORK%\desk.txt"
set "DESK_REMOVED="
findstr /b /l /c:"REMOVED " "%WORK%\desk.txt" >nul 2>nul
if not errorlevel 1 set "DESK_REMOVED=yes"
goto :eof

:: STATE=removing in GB's record, every other value as recorded (each one
:: checked against its grammar by :classify). MR_OK=yes when it reads back.
:mark_removing
set "MR_OK="
set "CB=%GB%"
call :classify
set "ERR=%GB% is no longer an ISAR installation this can prove it owns: %OWN_WHY%"
if not "%OWN%"=="owned" goto :eof
set "MR_ID=%OWN_ID%"
set "RF=%GB%\INSTALL_INFO.txt"
>"%RF%" echo(LAYOUT=%II_LAYOUT%
>>"%RF%" echo(INSTALL_ID=%II_INSTALL_ID%
>>"%RF%" echo(STATE=removing
>>"%RF%" echo(ISAR_BASE=%II_ISAR_BASE%
>>"%RF%" echo(INSTALL_KIND=%II_INSTALL_KIND%
>>"%RF%" echo(ISAR_RELEASE=%II_ISAR_RELEASE%
>>"%RF%" echo(SOURCE_REF=%II_SOURCE_REF%
>>"%RF%" echo(SOURCE_COMMIT=%II_SOURCE_COMMIT%
>>"%RF%" echo(LOCK_SHA256=%II_LOCK_SHA256%
>>"%RF%" echo(MAMBA_SHA256=%II_MAMBA_SHA256%
>>"%RF%" echo(MAMBA_VERSION=%II_MAMBA_VERSION%
>>"%RF%" echo(INSTALLER_BUILD=%II_INSTALLER_BUILD%
>>"%RF%" echo(INSTALLED_AT=%II_INSTALLED_AT%
set "CB=%GB%"
call :classify
set "ERR=the install record in %GB% did not read back correctly after marking it as being removed"
if not "%OWN%"=="owned" goto :eof
if not "%OWN_STATE%"=="removing" goto :eof
if not "%OWN_ID%"=="%MR_ID%" goto :eof
>>"%LOG%" echo(The record in %GB% now says STATE=removing.
set "MR_OK=yes"
set "ERR="
goto :eof

:: The locator goes when it names GB; then the tracked-marker list, the
:: record and the marker, last; then GB itself, only when it is now empty
:: (rd without /s). BASE_LEFT=yes when the folder stays.
:remove_records
set "RM_FAILED="
call :step_start remove-records "Removing the ownership records"
call :remove_locator
del /f /q /a "%GB%\tracked-workspace-markers.txt" >nul 2>nul
del /f /q /a "%GB%\INSTALL_INFO.txt" >nul 2>nul
del /f /q /a "%GB%\.isar-install-root" >nul 2>nul
set "ERR=the ownership records in %GB% could not be removed"
set "RM_FAILED=records"
if exist "%GB%\tracked-workspace-markers.txt" goto remove_step_failed
if exist "%GB%\INSTALL_INFO.txt" goto remove_step_failed
if exist "%GB%\.isar-install-root" goto remove_step_failed
set "RM_FAILED="
call :step_ok
cd /d "%WORK%"
set "BASE_LEFT=yes"
rd "%GB%" 2>nul
if not exist "%GB%\" set "BASE_LEFT="
goto :eof

:remove_locator
if not defined LOCALAPPDATA goto :eof
set "LOC_FILE=%LOCALAPPDATA%\ISAR\install-record.txt"
if not exist "%LOC_FILE%" goto :eof
set "LOC_BASE="
for /f "usebackq tokens=1,* delims==" %%A in ("%LOC_FILE%") do if "%%A"=="ISAR_BASE" set "LOC_BASE=%%B"
call :match LOC_BASE "%PATHCHARS%%PATHCHARS%*"
if not "%MATCH%"=="yes" goto :eof
if /i not "%LOC_BASE%"=="%GB%" goto :eof
del /f /q "%LOC_FILE%" >nul 2>nul
>>"%LOG%" echo(Removed the locator record, which named %GB%.
goto :eof

:: Relocation, after the new installation passed its self-test: the locator
:: moves to it first, then the old installation goes (the running check and
:: the workspace scan once more first). OLD_RESULT: removed, kept or partial.
:remove_old
set "LOC_STATE=complete"
call :write_locator
set "OLD_RESULT=kept"
set "GB=%OLD%"
set "WS_TARGETS=%PAYLOAD_DIRS%"
call :check_before_removal
set "OLD_WHY=%ERR%"
set "ERR="
if not "%GATE%"=="ok" goto remove_old_kept
>>"%LOG%" echo(STEP=remove-old start
echo.
echo [remove-old] Removing the earlier installation in %OLD%
call :mark_removing
set "OLD_WHY=%ERR%"
if not defined MR_OK goto remove_old_kept
call :remove_payload
if not defined RM_FAILED call :remove_records
if defined RM_FAILED goto remove_old_partial
>>"%LOG%" echo(STEP=remove-old ok
set "OLD_RESULT=removed"
set "ERR="
goto :eof
:remove_old_kept
>>"%LOG%" echo(The earlier installation in %OLD% was kept: %OLD_WHY%
set "ERR="
goto :eof
:remove_old_partial
>>"%LOG%" echo(STEP=remove-old failed
set "OLD_RESULT=partial"
set "OLD_WHY=%ERR%"
set "ERR="
goto :eof

:: STEP=profile: the GUI profile folders and every earlier log, keeping this
:: run's own log. Workspaces are never here; the scan has checked.
:remove_profile
call :step_start profile "Deleting ISAR's profile and the earlier logs"
set "PROF_LEFT="
if defined APPDATA call :remove_abs "%APPDATA%\isar_gui"
if defined USERPROFILE call :remove_abs "%USERPROFILE%\isar_gui"
if defined USERPROFILE call :remove_abs "%USERPROFILE%\.isar_gui"
set "PSI_A="
if defined LOCALAPPDATA set "PSI_A=%LOCALAPPDATA%\ISAR\logs"
set "PSI_B=%TEMP%\ISAR-logs"
set "PSI_C=%LOGFILE%"
del /f /q "%WORK%\oldlogs.txt" >nul 2>nul
powershell -NoProfile -NonInteractive -Command "try { $n=0; $keep=[string]$env:PSI_C; foreach ($d in @($env:PSI_A, $env:PSI_B)) { if ($d -and [IO.Directory]::Exists($d)) { foreach ($f in [IO.Directory]::GetFiles($d, '*.log')) { if ($f -ine $keep) { [IO.File]::Delete($f); $n++ } } } }; 'DELETED ' + $n + ' earlier log files' } catch { 'ERROR ' + $_.Exception.Message }" > "%WORK%\oldlogs.txt" 2>nul <nul
>>"%LOG%" type "%WORK%\oldlogs.txt"
findstr /b /l /c:"DELETED " "%WORK%\oldlogs.txt" >nul 2>nul
if errorlevel 1 set "PROF_LEFT=yes"
set "ERR=ISAR's profile or the earlier logs could not all be deleted; see the log"
set "RM_FAILED=profile"
if defined PROF_LEFT goto remove_step_failed
set "RM_FAILED="
call :step_ok
goto :eof

:remove_abs
set "RA=%~1"
if not "%RA:~1,2%"==":\" goto :eof
if exist "%RA%\" rd /s /q "\\?\%RA%" 2>nul
if exist "%RA%" set "PROF_LEFT=yes"
if exist "%RA%" >>"%LOG%" echo(Could not delete %RA%
goto :eof

:add_profile_targets
if defined APPDATA set "WS_TARGETS=%WS_TARGETS%*%APPDATA%\isar_gui"
if defined USERPROFILE set "WS_TARGETS=%WS_TARGETS%*%USERPROFILE%\isar_gui*%USERPROFILE%\.isar_gui"
goto :eof

:: The workspace places the end screen names: the experiments root
:: (ISAR_EXPERIMENTS_ROOT, else Documents\ISAR as Windows reports Documents)
:: and the GUI's recent workspaces, read now, before any profile deletion.
:read_ws_places
set "PSI_D=%WORK%\ws_places.txt"
del /f /q "%PSI_D%" "%WORK%\ws_places.ok" >nul 2>nul
powershell -NoProfile -NonInteractive -Command "try { $o=New-Object Collections.Generic.List[string]; $r=$env:ISAR_EXPERIMENTS_ROOT; if (-not $r) { $r=[IO.Path]::Combine([Environment]::GetFolderPath('MyDocuments'),'ISAR') }; $o.Add('    ' + $r); if ($env:APPDATA) { $s=[IO.Path]::Combine($env:APPDATA,'isar_gui','session.json'); if ([IO.File]::Exists($s)) { $j=ConvertFrom-Json ([IO.File]::ReadAllText($s)); foreach ($w in @($j.recent_workspaces)) { if ($w) { $o.Add('    ' + [string]$w) } } } }; [IO.File]::WriteAllLines($env:PSI_D, [string[]]$o, (New-Object Text.UTF8Encoding $false)); 'ok ' + $o.Count } catch { 'fail ' + $_.Exception.Message }" > "%WORK%\ws_places.ok" 2>nul <nul
>>"%LOG%" echo(Workspace places named on the end screen:
>>"%LOG%" type "%WORK%\ws_places.ok"
if exist "%PSI_D%" >>"%LOG%" type "%PSI_D%"
goto :eof

:: PATH for running the environment's programs: its activation folders, then
:: Windows' own.
:env_path
set "EP=%BASE%\env"
set "PATH=%EP%;%EP%\Library\mingw-w64\bin;%EP%\Library\usr\bin;%EP%\Library\bin;%EP%\Scripts;%EP%\bin;%SYS_PATH%"
goto :eof

:: Is ISAR from the installation GB running (any process whose program lies
:: under <GB>\env)? GATE=ok when none, else refuse (with ERR, and under /yes)
:: or cancel. Attended, the user may close ISAR and check again.
:running_gate
set "GATE=ok"
:running_again
set "PSI_A=%GB%\env\"
del /f /q "%WORK%\running.txt" >nul 2>nul
powershell -NoProfile -NonInteractive -Command "try { $b=[IO.Path]::GetFullPath($env:PSI_A); $n=0; foreach ($p in @(Get-CimInstance -ClassName Win32_Process -ErrorAction Stop)) { $e=$p.ExecutablePath; if ($e -and $e.StartsWith($b, [StringComparison]::OrdinalIgnoreCase)) { 'ISAR_RUNNING=' + $p.ProcessId; $n++ } }; 'COUNT=' + $n } catch { 'ERROR ' + $_.Exception.Message }" > "%WORK%\running.txt" 2>nul <nul
findstr /b /l /c:"COUNT=0" "%WORK%\running.txt" >nul 2>nul
if not errorlevel 1 goto running_clear
findstr /b /l /c:"COUNT=" "%WORK%\running.txt" >nul 2>nul
if not errorlevel 1 goto running_found
>>"%LOG%" type "%WORK%\running.txt"
set "ERR=could not check whether ISAR is running (PowerShell did not answer); see the log"
set "GATE=refuse"
goto :eof
:: A clean recheck clears the refusal and its message, so the retry can go ahead.
:running_clear
>>"%LOG%" echo(No ISAR from this installation is running now.
set "GATE=ok"
set "ERR="
goto :eof
:running_found
>>"%LOG%" findstr /b /l /c:"ISAR_RUNNING=" "%WORK%\running.txt"
set "ERR=ISAR from this installation is running; close every ISAR window, then try again"
echo.
echo ISAR from this installation is running (its process IDs):
findstr /b /l /c:"ISAR_RUNNING=" "%WORK%\running.txt"
set "GATE=refuse"
if defined YES goto :eof
choice /C RC /N /M "Close every ISAR window, then press R to check again, or C to cancel: "
set "ANSWER=%ERRORLEVEL%"
>>"%LOG%" echo(Asked: ISAR is running; check again? choice code %ANSWER%
if "%ANSWER%"=="1" goto running_again
set "ERR=ISAR from this installation was still running"
set "GATE=cancel"
goto :eof

:: Workspace scan (D4) of the folders in WS_TARGETS, separated by * (a name
:: under the installation GB, or a full path): every card.policy.toml or
:: gui.state.json, without following junctions or links, except the files
:: listed in GB's tracked-workspace-markers.txt under app. GATE=ok when none; refuse when
:: one is found (each folder logged as WORKSPACE_FOUND= and shown) or a
:: target is itself a link; fail when the scan could not run.
:workspace_gate
set "GATE=ok"
set "PSI_A=%GB%"
set "PSI_B=%WS_TARGETS%"
set "PSI_C=%GB%\tracked-workspace-markers.txt"
set "PSI_D=%WORK%\ws_found.txt"
del /f /q "%WORK%\ws.txt" "%PSI_D%" >nul 2>nul
powershell -NoProfile -NonInteractive -Command "try { $b=$env:PSI_A; $skip=@{}; if ([IO.File]::Exists($env:PSI_C)) { foreach ($l in [IO.File]::ReadAllLines($env:PSI_C)) { if ($l) { $skip[[IO.Path]::Combine($b,'app',$l).ToLowerInvariant()]=1 } } }; $found=New-Object Collections.Generic.List[string]; $link=''; $rp=[IO.FileAttributes]::ReparsePoint; foreach ($t in $env:PSI_B.Split('*')) { $r='\\?\'+[IO.Path]::Combine($b,$t); if (-not [IO.Directory]::Exists($r)) { continue }; $ri=New-Object IO.DirectoryInfo($r); if ($ri.Attributes -band $rp) { $link=$t; break }; $st=New-Object Collections.Stack; $st.Push($ri); while ($st.Count) { $d=$st.Pop(); foreach ($e in $d.EnumerateFileSystemInfos()) { if ($e.Attributes -band [IO.FileAttributes]::Directory) { if (-not ($e.Attributes -band $rp)) { $st.Push($e) } } elseif ($e.Name -ieq 'card.policy.toml' -or $e.Name -ieq 'gui.state.json') { if (-not $skip.ContainsKey($e.FullName.Substring(4).ToLowerInvariant())) { $dn=$e.DirectoryName.Substring(4); if (-not $found.Contains($dn)) { $found.Add($dn) } } } } } }; if ($link) { 'LINK ' + $link } else { $o=@(foreach ($f in $found) { 'WORKSPACE_FOUND=' + $f }); [IO.File]::WriteAllLines($env:PSI_D, [string[]]$o, (New-Object Text.UTF8Encoding $false)); 'SCAN ' + $found.Count } } catch { 'ERROR ' + $_.Exception.Message }" > "%WORK%\ws.txt" 2>nul <nul
>>"%LOG%" echo(Workspace scan in %GB% of %WS_TARGETS%:
>>"%LOG%" type "%WORK%\ws.txt"
findstr /b /l /c:"SCAN 0" "%WORK%\ws.txt" >nul 2>nul
if not errorlevel 1 goto :eof
findstr /b /l /c:"SCAN " "%WORK%\ws.txt" >nul 2>nul
if not errorlevel 1 goto workspace_found
set "GATE=refuse"
set "ERR=a folder this would remove is a junction or link, which may point somewhere else entirely; nothing was removed"
findstr /b /l /c:"LINK " "%WORK%\ws.txt" >nul 2>nul
if not errorlevel 1 goto :eof
set "GATE=fail"
set "ERR=the folders to be removed could not be checked for workspaces; see the log"
goto :eof
:workspace_found
>>"%LOG%" type "%PSI_D%"
echo.
echo A workspace is saved inside a folder this would remove:
type "%PSI_D%"
echo Move each folder named above somewhere else, then try again.
set "ERR=a workspace is saved inside a folder this would remove; move it out first"
set "GATE=refuse"
goto :eof

:: ===========================================================================
:: Records
:: ===========================================================================

:: INSTALL_INFO.txt from the current values; REC_STATE is the state.
:write_record
set "RF=%BASE%\INSTALL_INFO.txt"
>"%RF%" echo(LAYOUT=%LAYOUT%
>>"%RF%" echo(INSTALL_ID=%INSTALL_ID%
>>"%RF%" echo(STATE=%REC_STATE%
>>"%RF%" echo(ISAR_BASE=%BASE%
>>"%RF%" echo(INSTALL_KIND=%INSTALL_KIND%
>>"%RF%" echo(ISAR_RELEASE=%ISAR_RELEASE%
>>"%RF%" echo(SOURCE_REF=%SOURCE_REF%
>>"%RF%" echo(SOURCE_COMMIT=%COMMIT%
>>"%RF%" echo(LOCK_SHA256=%LOCK_SHA256%
>>"%RF%" echo(MAMBA_SHA256=%MAMBA_SHA256_REC%
>>"%RF%" echo(MAMBA_VERSION=%MM_VERSION%
>>"%RF%" echo(INSTALLER_BUILD=%INSTALLER_BUILD%
>>"%RF%" echo(INSTALLED_AT=%INSTALLED_AT%
goto :eof

:: %LOCALAPPDATA%\ISAR\install-record.txt finds an installation; it authorises
:: nothing. LOC_STATE is the state. An unwritable folder costs only the record.
:write_locator
if not defined LOCALAPPDATA goto :eof
if not exist "%LOCALAPPDATA%\ISAR\" mkdir "%LOCALAPPDATA%\ISAR" 2>nul
if not exist "%LOCALAPPDATA%\ISAR\" goto write_locator_failed
>"%LOCALAPPDATA%\ISAR\install-record.txt" echo(ISAR_BASE=%BASE%
>>"%LOCALAPPDATA%\ISAR\install-record.txt" echo(INSTALL_ID=%INSTALL_ID%
>>"%LOCALAPPDATA%\ISAR\install-record.txt" echo(STATE=%LOC_STATE%
goto :eof
:write_locator_failed
>>"%LOG%" echo([WARN] the locator record could not be written under %%LOCALAPPDATA%%\ISAR
goto :eof

:: EXISTING = the base the locator names, when that base validates and holds a
:: proven installation; else empty.
:find_existing
set "EXISTING="
if not defined LOCALAPPDATA goto :eof
if not exist "%LOCALAPPDATA%\ISAR\install-record.txt" goto :eof
set "LOC_BASE="
for /f "usebackq tokens=1,* delims==" %%A in ("%LOCALAPPDATA%\ISAR\install-record.txt") do if "%%A"=="ISAR_BASE" set "LOC_BASE=%%B"
call :match LOC_BASE "%PATHCHARS%%PATHCHARS%*"
if not "%MATCH%"=="yes" goto :eof
set "VBPATH=%LOC_BASE%"
call :validate_base_var
if defined VB_REASON goto :eof
set "CB=%VBPATH%"
call :classify
if "%OWN%"=="owned" set "EXISTING=%CB%"
goto :eof

:: Ownership of the validated folder CB (C-layout). Sets OWN to none, owned,
:: partial or foreign, OWN_STATE and OWN_ID when owned, OWN_WHY otherwise.
:classify
set "OWN=none"
set "OWN_STATE="
set "OWN_ID="
set "OWN_WHY="
if not exist "%CB%" goto :eof
set "OWN=foreign"
set "OWN_WHY=it is a file, not a folder"
if not exist "%CB%\" goto :eof
set "OWN_WHY="
set "OTHER="
if exist "%CB%\ISAR-Launcher.bat" set "OTHER=ISAR-Launcher.bat"
if exist "%CB%\ISAR Launcher.lnk" set "OTHER=ISAR Launcher.lnk"
if exist "%CB%\launcher" set "OTHER=launcher"
if exist "%CB%\tracked-workspace-markers.txt" set "OTHER=tracked-workspace-markers.txt"
if exist "%CB%\micromamba" set "OTHER=micromamba"
if exist "%CB%\mamba" set "OTHER=mamba"
if exist "%CB%\env" set "OTHER=env"
if exist "%CB%\app" set "OTHER=app"
set "HAS_M="
set "HAS_R="
if exist "%CB%\.isar-install-root" set "HAS_M=yes"
if exist "%CB%\INSTALL_INFO.txt" set "HAS_R=yes"
if defined HAS_M goto classify_records
if defined HAS_R goto classify_records
set "OWN=none"
if not defined OTHER goto :eof
set "OWN=foreign"
set "OWN_WHY=it already holds %OTHER% but no ISAR ownership records"
goto :eof
:classify_records
set "M_OK="
set "R_OK="
if defined HAS_M call :read_marker
if defined HAS_R call :read_record
if not defined HAS_M goto classify_partial
if not defined HAS_R goto classify_partial
set "OWN_WHY=its ownership marker does not check out"
if not defined M_OK goto :eof
set "OWN_WHY=its install record does not check out: %R_WHY%"
if not defined R_OK goto :eof
set "OWN_WHY=its marker and install record carry different install IDs"
if not "%M_INSTALL_ID%"=="%II_INSTALL_ID%" goto :eof
set "OWN=owned"
set "OWN_STATE=%II_STATE%"
set "OWN_ID=%II_INSTALL_ID%"
set "OWN_WHY="
goto :eof
:classify_partial
set "OWN_WHY=it holds %OTHER% beside an incomplete ownership pair"
if defined OTHER goto :eof
set "OWN_WHY=its ownership marker does not check out"
if defined HAS_M if not defined M_OK goto :eof
set "OWN_WHY=its install record does not check out: %R_WHY%"
if defined HAS_R if not defined R_OK goto :eof
set "OWN=partial"
set "OWN_WHY="
goto :eof

:read_marker
set "M_INSTALL_ID="
for /f "usebackq tokens=1,* delims==" %%A in ("%CB%\.isar-install-root") do if "%%A"=="INSTALL_ID" set "M_INSTALL_ID=%%B"
call :match_hex M_INSTALL_ID 32
if "%MATCH%"=="yes" set "M_OK=yes"
goto :eof

:: Reads INSTALL_INFO.txt into II_<KEY> and checks every value against its
:: grammar before anything expands it. Sets R_OK, or R_WHY naming the key.
:read_record
for %%K in (LAYOUT INSTALL_ID STATE ISAR_BASE INSTALL_KIND ISAR_RELEASE SOURCE_REF SOURCE_COMMIT LOCK_SHA256 MAMBA_SHA256 MAMBA_VERSION INSTALLER_BUILD INSTALLED_AT) do set "II_%%K="
for /f "usebackq tokens=1,* delims==" %%A in ("%CB%\INSTALL_INFO.txt") do set "II_%%A=%%B"
set "R_WHY=LAYOUT"
call :match II_LAYOUT "%LAYOUT%"
if not "%MATCH%"=="yes" goto :eof
set "R_WHY=INSTALL_ID"
call :match_hex II_INSTALL_ID 32
if not "%MATCH%"=="yes" goto :eof
set "R_WHY=STATE"
call :match II_STATE "installing" "complete" "removing"
if not "%MATCH%"=="yes" goto :eof
set "R_WHY=INSTALL_KIND"
call :match II_INSTALL_KIND "release" "test"
if not "%MATCH%"=="yes" goto :eof
set "R_WHY=ISAR_RELEASE"
call :match II_ISAR_RELEASE "%VER_RE%"
if not "%MATCH%"=="yes" goto :eof
set "R_WHY=SOURCE_REF"
call :match II_SOURCE_REF "%REF_RE%"
if not "%MATCH%"=="yes" goto :eof
set "R_WHY=SOURCE_COMMIT"
call :match_hex II_SOURCE_COMMIT 40
if not "%MATCH%"=="yes" goto :eof
set "R_WHY=LOCK_SHA256"
call :match_hex II_LOCK_SHA256 64
if not "%MATCH%"=="yes" goto :eof
set "R_WHY=MAMBA_SHA256"
call :match_hex II_MAMBA_SHA256 64
if not "%MATCH%"=="yes" goto :eof
set "R_WHY=MAMBA_VERSION"
call :match II_MAMBA_VERSION "%D%[0123456789.-]*"
if not "%MATCH%"=="yes" goto :eof
set "R_WHY=INSTALLER_BUILD"
call :match II_INSTALLER_BUILD "%D%%D%*"
if not "%MATCH%"=="yes" goto :eof
set "R_WHY=INSTALLED_AT"
call :match II_INSTALLED_AT "%ISO_RE%"
if not "%MATCH%"=="yes" goto :eof
set "R_WHY=ISAR_BASE"
call :match II_ISAR_BASE "%PATHCHARS%%PATHCHARS%*"
if not "%MATCH%"=="yes" goto :eof
:: The record's base must be this very folder (and a valid base).
set "RB_KEEP=%VBPATH%"
set "VB_KEEP=%VB_REASON%"
set "VBPATH=%II_ISAR_BASE%"
call :validate_base_var
set "RB_REASON=%VB_REASON%"
set "RB_PATH=%VBPATH%"
set "VBPATH=%RB_KEEP%"
set "VB_REASON=%VB_KEEP%"
if defined RB_REASON goto :eof
if /i not "%RB_PATH%"=="%CB%" goto :eof
set "R_WHY="
set "R_OK=yes"
goto :eof

:: The copy kept in an installation's launcher\ never installs: an update in
:: place would overwrite the running script, and a relocation would delete it.
:engine_base_check
set "ERR="
if not exist "%SELF_DIR%..\.isar-install-root" goto :eof
set "ERR=this is the copy of the installer kept inside an installation, which only repairs and uninstalls; to install, update or move ISAR, run the installer you downloaded"
goto :eof

:: INSIDE=yes when the path PSI_A is PSI_B or lies inside it.
:inside
set "INSIDE=no"
del /f /q "%WORK%\inside.txt" >nul 2>nul
powershell -NoProfile -NonInteractive -Command "try { $a=[IO.Path]::GetFullPath($env:PSI_A).TrimEnd('\'); $b=[IO.Path]::GetFullPath($env:PSI_B).TrimEnd('\'); if ($a -ieq $b -or $a.StartsWith($b + '\', [StringComparison]::OrdinalIgnoreCase)) { 'yes' } else { 'no' } } catch { 'yes' }" > "%WORK%\inside.txt" 2>nul <nul
findstr /x /c:"no" "%WORK%\inside.txt" >nul 2>nul
if errorlevel 1 set "INSIDE=yes"
goto :eof

:: DEFAULT_BASE: the existing installation, else %USERPROFILE%\ISAR, else
:: %SystemDrive%\ISAR when the profile path breaks the folder rules; offered
:: only when writable.
:pick_default
set "DEFAULT_BASE="
if defined EXISTING set "DEFAULT_BASE=%EXISTING%"
if defined EXISTING goto :eof
set "VBPATH=%USERPROFILE%\ISAR"
call :validate_base_var
if defined VB_REASON goto pick_default_system
set "CAND=%VBPATH%"
call :writable_candidate
if defined CAND_OK set "DEFAULT_BASE=%CAND%"
if defined CAND_OK goto :eof
:pick_default_system
set "VBPATH=%SystemDrive%\ISAR"
call :validate_base_var
if defined VB_REASON goto :eof
set "CAND=%VBPATH%"
call :writable_candidate
if defined CAND_OK set "DEFAULT_BASE=%CAND%"
goto :eof

:writable_candidate
set "CAND_OK="
if exist "%CAND%\" goto writable_candidate_exists
mkdir "%CAND%" 2>nul
if exist "%CAND%\" set "CAND_OK=yes"
rd "%CAND%" 2>nul
goto :eof
:writable_candidate_exists
type nul > "%CAND%\isar-write-test-%HEX:~0,8%.tmp" 2>nul
if exist "%CAND%\isar-write-test-%HEX:~0,8%.tmp" set "CAND_OK=yes"
del /f /q "%CAND%\isar-write-test-%HEX:~0,8%.tmp" >nul 2>nul
goto :eof

:: ===========================================================================
:: Small helpers
:: ===========================================================================

:: %1 = variable name, then one or more findstr /x regular expressions for its
:: value. MATCH=yes when the value matches one of them. The value is read from
:: a "set" listing in a file, so it is never expanded on a command line.
:match
set "MATCH=no"
set "MATCH_VAR=%~1"
set "MATCH_FILE=%WORK%\match.txt"
if not defined WORK set "MATCH_FILE=%IDS%.match"
del /f /q "%MATCH_FILE%" >nul 2>nul
> "%MATCH_FILE%" set %MATCH_VAR% 2>nul
:match_next
shift
if "%~1"=="" goto :eof
findstr /r /x /c:"%MATCH_VAR%=%~1" "%MATCH_FILE%" >nul 2>nul
if not errorlevel 1 set "MATCH=yes"
if "%MATCH%"=="yes" goto :eof
goto match_next

:: %1 = variable name, %2 = exact length: MATCH=yes for that many lowercase
:: hex digits. The value is expanded (by "call set") only after the
:: character check has passed.
:match_hex
set "MH_NAME=%~1"
set "MH_LEN=%~2"
call :match %MH_NAME% "%H%%H%*"
if not "%MATCH%"=="yes" goto :eof
set "MATCH=no"
call set "MH_VAL=%%%MH_NAME%%%"
set /a MH_LAST=MH_LEN-1
call set "MH_TAIL=%%MH_VAL:~%MH_LEN%%%"
call set "MH_END=%%MH_VAL:~%MH_LAST%,1%%"
if defined MH_TAIL goto :eof
if not defined MH_END goto :eof
set "MATCH=yes"
goto :eof

:step_start
set "CUR_STEP=%~1"
>>"%LOG%" echo(STEP=%~1 start
echo.
echo [%~1] %~2
goto :eof

:step_ok
>>"%LOG%" echo(STEP=%CUR_STEP% ok
echo   [OK]
set "CUR_STEP="
goto :eof

:step_ok_named
>>"%LOG%" echo(STEP=%~1 ok
goto :eof

:: SHA = lowercase SHA-256 of the file %1, or empty.
:sha256
set "SHA="
set "PSI_A=%~1"
del /f /q "%WORK%\sha256.txt" >nul 2>nul
powershell -NoProfile -NonInteractive -Command "try { (Get-FileHash -Algorithm SHA256 -LiteralPath $env:PSI_A).Hash.ToLower() } catch { 'none' }" > "%WORK%\sha256.txt" 2>nul <nul
set /p "SHA=" < "%WORK%\sha256.txt"
call :match_hex SHA 64
if not "%MATCH%"=="yes" set "SHA="
goto :eof

:: Clears inherited conda, mamba, Python, pip, Qt and git settings and any
:: self-test switch, through a "set" listing in a file (no command-form for).
:clear_inherited
> "%WORK%\env.txt" set
for /f "usebackq delims==" %%V in ("%WORK%\env.txt") do call :clear_one "%%V"
goto :eof
:clear_one
set "CV=%~1"
if /i "%CV:~0,5%"=="CONDA" set "%CV%="
if /i "%CV:~0,5%"=="MAMBA" set "%CV%="
if /i "%CV:~0,6%"=="PYTHON" set "%CV%="
if /i "%CV:~0,4%"=="PIP_" set "%CV%="
if /i "%CV:~0,3%"=="QT_" set "%CV%="
if /i "%CV:~0,4%"=="GIT_" set "%CV%="
if /i "%CV:~0,18%"=="ISAR_GUI_SELFTEST_" set "%CV%="
if /i "%CV%"=="VIRTUAL_ENV" set "%CV%="
goto :eof

:: STAMP (yyyyMMdd-HHmmss), HEX (32 hex) and NOW_ISO (yyyy-MM-ddTHH:mm:ss)
:: from PowerShell, through a file, checked before use. Before the working
:: folder exists the file is a uniquely named one in %TEMP%.
:new_ids
set "STAMP="
set "HEX="
set "NOW_ISO="
set "IDS=%TEMP%\isar-ids-%RANDOM%%RANDOM%%RANDOM%.txt"
if defined WORK set "IDS=%WORK%\ids.txt"
powershell -NoProfile -NonInteractive -Command "(Get-Date -Format 'yyyyMMdd-HHmmss') + ' ' + [guid]::NewGuid().ToString('N') + ' ' + (Get-Date -Format 's')" > "%IDS%" 2>nul <nul
set "ID_STAMP="
set "ID_HEX="
set "ID_ISO="
for /f "usebackq tokens=1,2,3" %%A in ("%IDS%") do set "ID_STAMP=%%A"
for /f "usebackq tokens=1,2,3" %%A in ("%IDS%") do set "ID_HEX=%%B"
for /f "usebackq tokens=1,2,3" %%A in ("%IDS%") do set "ID_ISO=%%C"
call :match ID_STAMP "%D4%%D2%%D2%-%D2%%D2%%D2%"
set "ID_OK=%MATCH%"
call :match ID_ISO "%ISO_RE%"
if not "%MATCH%"=="yes" set "ID_OK=no"
call :match_hex ID_HEX 32
if not "%MATCH%"=="yes" set "ID_OK=no"
if not defined WORK del /f /q "%IDS%" "%IDS%.match" >nul 2>nul
if not "%ID_OK%"=="yes" goto :eof
set "STAMP=%ID_STAMP%"
set "HEX=%ID_HEX%"
set "NOW_ISO=%ID_ISO%"
goto :eof

:: The log (%LOCALAPPDATA%\ISAR\logs, else %TEMP%\ISAR-logs) and the private
:: working folder %TEMP%\isar-<op>-<stamp>-<8 hex>. Each name is used only
:: when free; a clash draws new IDs.
:open_log_and_work
set "TRIES=0"
:olw_again
set /a TRIES+=1
if %TRIES% GTR 4 goto :eof
call :new_ids
if not defined HEX goto olw_again
if defined WORK goto olw_log
set "TRY_WORK=%TEMP%\isar-%OP%-%STAMP%-%HEX:~8,8%"
if exist "%TRY_WORK%" goto olw_again
mkdir "%TRY_WORK%" 2>nul
if not exist "%TRY_WORK%\" goto olw_again
set "WORK=%TRY_WORK%"
:olw_log
if defined LOGFILE goto :eof
set "LOG_DIR="
if defined LOCALAPPDATA set "LOG_DIR=%LOCALAPPDATA%\ISAR\logs"
if defined LOG_DIR if not exist "%LOG_DIR%\" mkdir "%LOG_DIR%" >nul 2>nul
if defined LOG_DIR call :try_log
if defined LOGFILE goto :eof
set "LOG_DIR=%TEMP%\ISAR-logs"
if not exist "%LOG_DIR%\" mkdir "%LOG_DIR%" >nul 2>nul
call :try_log
if defined LOGFILE goto :eof
if "%LOG_CLASH%"=="yes" goto olw_again
echo [WARN] No log file could be written; this run goes ahead without one.
goto :eof

:try_log
set "LOG_CLASH="
if not exist "%LOG_DIR%\" goto :eof
set "TRY_LOG=%LOG_DIR%\%OP%-%STAMP%-%HEX:~0,8%.log"
if exist "%TRY_LOG%" set "LOG_CLASH=yes"
if exist "%TRY_LOG%" goto :eof
type nul > "%TRY_LOG%" 2>nul
if not exist "%TRY_LOG%" goto :eof
set "LOGFILE=%TRY_LOG%"
set "LOG=%TRY_LOG%"
goto :eof

:: ===========================================================================
:: The folder validator (TAVI's, ported). VBPATH holds the raw value; the
:: character list runs before any line expands it. Sets VB_REASON when refused;
:: on success VBPATH has no trailing backslash. Ceiling: it guards against a
:: mistyped or stale path, not a hostile local user.
:: ===========================================================================
:validate_base_var
set "VB_REASON="
if not defined VBPATH set "VB_REASON=the path is empty"
if defined VB_REASON goto :eof
call :match VBPATH "%PATHCHARS%%PATHCHARS%*"
if not "%MATCH%"=="yes" set "VB_REASON=it may contain only letters A-Z, digits, dot, dash, underscore and backslashes: no spaces, accented letters, quotes or other symbols"
if defined VB_REASON goto :eof
if "%VBPATH:~-1%"=="\" set "VBPATH=%VBPATH:~0,-1%"
if not defined VBPATH set "VB_REASON=the path is empty"
if defined VB_REASON goto :eof
if "%VBPATH:~0,2%"=="\\" set "VB_REASON=network and device paths are not supported"
if defined VB_REASON goto :eof
if not "%VBPATH%"=="%VBPATH:..=%" set "VB_REASON=it contains .."
if defined VB_REASON goto :eof
if "%VBPATH:~1,1%"==":" if "%VBPATH:~2%"=="" set "VB_REASON=a whole drive cannot be the ISAR folder"
if defined VB_REASON goto :eof
if not "%VBPATH:~1,1%"==":" set "VB_REASON=it must start with a drive letter, like C:\ISAR"
if defined VB_REASON goto :eof
if not "%VBPATH:~2,1%"=="\" set "VB_REASON=it must start with a drive letter, like C:\ISAR"
if defined VB_REASON goto :eof
if "%VBPATH:~3%"=="" set "VB_REASON=a whole drive cannot be the ISAR folder"
if defined VB_REASON goto :eof
set "VB_REST=%VBPATH:~2%"
if not "%VB_REST%"=="%VB_REST::=%" set "VB_REASON=a colon may appear only after the drive letter"
if defined VB_REASON goto :eof
if "%VBPATH:~-1%"=="." set "VB_REASON=it ends with a dot"
if defined VB_REASON goto :eof
if not "%VBPATH%"=="%VBPATH:\\=%" set "VB_REASON=it contains an empty folder name (two backslashes)"
if defined VB_REASON goto :eof
if /i "%VBPATH%"=="%USERPROFILE%" set "VB_REASON=your user folder itself cannot be the ISAR folder"
if defined VB_REASON goto :eof
if /i "%VBPATH%"=="%APPDATA%" set "VB_REASON=that folder cannot be the ISAR folder"
if defined VB_REASON goto :eof
if /i "%VBPATH%"=="%LOCALAPPDATA%" set "VB_REASON=that folder cannot be the ISAR folder"
if defined VB_REASON goto :eof
if /i "%VBPATH%"=="%SystemDrive%\Users" set "VB_REASON=that folder cannot be the ISAR folder"
if defined VB_REASON goto :eof
if /i "%VBPATH%"=="%ProgramData%" set "VB_REASON=that folder cannot be the ISAR folder"
if defined VB_REASON goto :eof
if exist "%VBPATH%" if not exist "%VBPATH%\" set "VB_REASON=a file of that name already exists"
if defined VB_REASON goto :eof
:: The drive must be a local fixed or removable disk (not a mapped network
:: drive), and the folder must not lie inside Windows or Program Files. The
:: characters are checked once more on the real Unicode value: cmd's listing
:: may write a letter outside the console code page as its closest ASCII one.
set "PSI_A=%VBPATH%"
del /f /q "%WORK%\vb_ps.txt" >nul 2>nul
powershell -NoProfile -NonInteractive -Command "try { $p=$env:PSI_A; $r='ok'; $t=(New-Object IO.DriveInfo($p.Substring(0,1))).DriveType.ToString(); if ($p -cnotmatch '^[A-Za-z0-9_.:\\-]+$') { $r='chars' } elseif ($t -ne 'Fixed' -and $t -ne 'Removable') { $r='drive-' + $t } else { foreach ($n in 'SystemRoot','ProgramFiles','ProgramFiles(x86)','ProgramW6432') { $s=[Environment]::GetEnvironmentVariable($n); if ($s) { $s=$s.TrimEnd('\'); if ($p -ieq $s -or $p.StartsWith($s + '\', [StringComparison]::OrdinalIgnoreCase)) { $r='system' } } } }; $r } catch { 'error' }" > "%WORK%\vb_ps.txt" 2>nul <nul
set "VB_PS="
set /p "VB_PS=" < "%WORK%\vb_ps.txt"
call :match VB_PS "ok" "system" "chars" "drive-[%AL%]*"
if not "%MATCH%"=="yes" set "VB_PS=error"
if "%VB_PS%"=="chars" set "VB_REASON=it may contain only letters A-Z, digits, dot, dash, underscore and backslashes: no spaces, accented letters, quotes or other symbols"
if "%VB_PS%"=="system" set "VB_REASON=it is inside a protected Windows folder (Windows or Program Files)"
if "%VB_PS%"=="drive-Network" set "VB_REASON=it is on a network drive; choose a folder on a local disk"
if "%VB_PS%"=="drive-NoRootDirectory" set "VB_REASON=that drive does not exist"
if "%VB_PS%"=="error" set "VB_REASON=the drive could not be checked (PowerShell did not answer)"
if defined VB_REASON goto :eof
if not "%VB_PS%"=="ok" set "VB_REASON=it is not on a local fixed or removable disk"
if defined VB_REASON goto :eof
:: Walk every existing component: a junction or symbolic link anywhere above
:: the folder would make its real target differ from the path on screen.
set "VB_WALK=%VBPATH%"
:vb_walk
if not defined VB_WALK goto :eof
if "%VB_WALK:~3%"=="" goto :eof
for %%I in ("%VB_WALK%") do set "VB_LEAF=%%~nxI"
for %%I in ("%VB_WALK%") do set "VB_PARENT=%%~dpI"
if not exist "%VB_WALK%\" goto vb_walk_up
dir /a:l /b "%VB_PARENT%" > "%WORK%\links.txt" 2>nul
findstr /i /x /c:"%VB_LEAF%" "%WORK%\links.txt" >nul 2>nul
if not errorlevel 1 set "VB_REASON=%VB_WALK% is a junction or a symbolic link, which may point somewhere else entirely"
if defined VB_REASON goto :eof
:vb_walk_up
if "%VB_PARENT:~-1%"=="\" set "VB_PARENT=%VB_PARENT:~0,-1%"
if /i "%VB_PARENT%"=="%VB_WALK%" goto :eof
set "VB_WALK=%VB_PARENT%"
goto vb_walk

:: ===========================================================================
:: /validate-only <path> and /inspect <path>: print and change nothing (the
:: private working folder they need is removed again).
:: ===========================================================================
:validate_only
set "OP=validate"
call :open_work_only
if not defined WORK goto no_work
set "VBPATH=%~2"
call :validate_base_var
call :remove_work
if defined VB_REASON goto validate_only_refused
echo ACCEPT
endlocal & exit /b 0
:validate_only_refused
echo REFUSE %VB_REASON%
endlocal & exit /b 2

:inspect
set "OP=inspect"
call :open_work_only
if not defined WORK goto no_work
set "VBPATH=%~2"
call :validate_base_var
if defined VB_REASON goto inspect_refused
set "CB=%VBPATH%"
call :classify
echo OWNERSHIP=%OWN%
if "%OWN%"=="owned" echo STATE=%OWN_STATE%
if defined OWN_WHY echo WHY=%OWN_WHY%
call :remove_work
endlocal & exit /b 0
:inspect_refused
call :remove_work
echo REFUSE %VB_REASON%
endlocal & exit /b 2

:open_work_only
set "TRIES=0"
:owo_again
set /a TRIES+=1
if %TRIES% GTR 4 goto :eof
call :new_ids
if not defined HEX goto owo_again
set "TRY_WORK=%TEMP%\isar-%OP%-%STAMP%-%HEX:~8,8%"
if exist "%TRY_WORK%" goto owo_again
mkdir "%TRY_WORK%" 2>nul
if not exist "%TRY_WORK%\" goto owo_again
set "WORK=%TRY_WORK%"
goto :eof
