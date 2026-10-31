@echo off
setlocal EnableDelayedExpansion
REM ============================================================================
REM  Install FragSkills from this folder, so Claude Code can find it.
REM
REM  Most users do not need this script: install the plugin instead, in Claude
REM  Code:  /plugin install fragskills --marketplace Nesvilab/FragSkills
REM  This script is for a clone you edit, or a machine without network access.
REM
REM    install.bat                  install for the current user
REM    install.bat --project        install into .\.claude\skills
REM    install.bat --project DIR    install into DIR\.claude\skills
REM    install.bat --copy           copy the files instead of linking
REM    install.bat --uninstall      remove it again
REM    install.bat --help
REM
REM  By default a directory junction is created, so edits to this folder take
REM  effect immediately.  Junctions do NOT require administrator rights.
REM  Use --copy when this folder is on a drive the machine cannot always reach.
REM ============================================================================

set "SKILL_NAME=fragskills"
set "MODE=user"
set "PROJECT_DIR="
set "METHOD=auto"
set "UNINSTALL=0"
set "FORCE=0"
set "EXITCODE=0"

REM Capture these before the argument loop: `shift` also shifts %0, so %~f0 and
REM %~dp0 stop being this script once any option has been consumed.
set "SELF=%~f0"
set "SOURCE_DIR=%~dp0"
set "SOURCE_DIR=%SOURCE_DIR:~0,-1%"
set "PAUSE_AT_END="

REM Keep the window open only for a plain double-click from Explorer: that is a
REM `cmd /c` invocation naming this script and passing no arguments. Anything
REM with arguments is being scripted and must never block. Read cmdcmdline with
REM delayed expansion: expanded at parse time, a ")" in this folder's path would
REM close the block.
if "%~1"=="" (
    set "CMDLINE=!cmdcmdline!"
    if /i not "!CMDLINE:%~nx0=!"=="!CMDLINE!" set "PAUSE_AT_END=1"
)
if defined FRAGSKILLS_NO_PAUSE set "PAUSE_AT_END="

:parse_args
if "%~1"=="" goto args_done
if /i "%~1"=="--user"      ( set "MODE=user"    & shift & goto parse_args )
if /i "%~1"=="--copy"      ( set "METHOD=copy"  & shift & goto parse_args )
if /i "%~1"=="--link"      ( set "METHOD=link"  & shift & goto parse_args )
if /i "%~1"=="--uninstall" ( set "UNINSTALL=1"  & shift & goto parse_args )
if /i "%~1"=="--force"     ( set "FORCE=1"      & shift & goto parse_args )
if /i "%~1"=="-f"          ( set "FORCE=1"      & shift & goto parse_args )
if /i "%~1"=="--no-pause"  ( set "PAUSE_AT_END=" & shift & goto parse_args )
if /i "%~1"=="--help"      goto usage
if /i "%~1"=="-h"          goto usage
if /i "%~1"=="/?"          goto usage
if /i "%~1"=="--name" (
    if "%~2"=="" ( call :die "--name needs a value" & goto finish )
    set "SKILL_NAME=%~2"
    shift & shift & goto parse_args
)
if /i "%~1"=="--project" (
    set "MODE=project"
    set "NEXTARG=%~2"
    if not "!NEXTARG!"=="" if not "!NEXTARG:~0,1!"=="-" (
        set "PROJECT_DIR=%~2"
        shift
    )
    shift & goto parse_args
)
call :die "unknown option: %~1  (try --help)"
goto finish
:args_done

REM --- resolve source ---------------------------------------------------------
if not exist "%SOURCE_DIR%\SKILL.md" (
    call :die "SKILL.md not found in %SOURCE_DIR%"
    echo        This script must stay inside the FragSkills folder.
    goto finish
)

REM --- resolve target ---------------------------------------------------------
if /i "%MODE%"=="project" (
    if "!PROJECT_DIR!"=="" set "PROJECT_DIR=%CD%"
    if not exist "!PROJECT_DIR!\" (
        call :die "project directory does not exist: !PROJECT_DIR!"
        goto finish
    )
    pushd "!PROJECT_DIR!" && set "PROJECT_DIR=!CD!" && popd
    set "SKILLS_DIR=!PROJECT_DIR!\.claude\skills"
    set "SCOPE_DESC=project (!PROJECT_DIR!)"
) else (
    set "SKILLS_DIR=%USERPROFILE%\.claude\skills"
    set "SCOPE_DESC=user (%USERPROFILE%)"
)
set "TARGET=!SKILLS_DIR!\%SKILL_NAME%"

set "UNINSTALL_HINT="!SELF!" --uninstall"
if /i not "%SKILL_NAME%"=="fragskills" set "UNINSTALL_HINT=!UNINSTALL_HINT! --name "!SKILL_NAME!""
if /i "%MODE%"=="project" set "UNINSTALL_HINT=!UNINSTALL_HINT! --project "!PROJECT_DIR!""

if "%UNINSTALL%"=="1" goto do_uninstall

REM --- install ----------------------------------------------------------------
echo Installing FragSkills
echo   source : %SOURCE_DIR%
echo   target : !TARGET!
echo   scope  : !SCOPE_DESC!
echo.

if exist "!TARGET!" (
    call :is_junction "!TARGET!"
    if "!IS_JUNCTION!"=="1" (
        call :junction_target "!TARGET!"
        if /i "!LINK_TARGET!"=="%SOURCE_DIR%" (
            call :ok "already linked to this folder"
            REM The link is live, but the commands are copies -- refresh them.
            call :install_commands
            call :warn_if_plugin_installed
            goto finish
        )
    )
    if not "%FORCE%"=="1" (
        call :die "!TARGET! already exists."
        echo        Re-run with --force to replace it, or use --name to install
        echo        under a different folder name.
        goto finish
    )
    call :remove_target
    if errorlevel 1 goto finish
    call :warn "replaced the previous !TARGET!"
)

if not exist "!SKILLS_DIR!\" mkdir "!SKILLS_DIR!" 2>nul
if not exist "!SKILLS_DIR!\" (
    call :die "could not create !SKILLS_DIR!"
    goto finish
)

set "INSTALLED_VIA="
if /i not "%METHOD%"=="copy" (
    mklink /J "!TARGET!" "%SOURCE_DIR%" >nul 2>&1
    if not errorlevel 1 (
        set "INSTALLED_VIA=junction"
    ) else (
        if /i "%METHOD%"=="link" (
            call :die "could not create a junction at !TARGET!"
            goto finish
        )
        call :warn "junction failed, falling back to copying"
    )
)

if "!INSTALLED_VIA!"=="" (
    robocopy "%SOURCE_DIR%" "!TARGET!" /E /XD ".git" "__pycache__" /XF "*.pyc" ^
        /NFL /NDL /NJH /NJS /NP >nul 2>&1
    if errorlevel 8 (
        call :die "copying to !TARGET! failed"
        goto finish
    )
    set "INSTALLED_VIA=copy"
)

call :ok "installed via !INSTALLED_VIA!"

if not exist "!TARGET!\SKILL.md" (
    call :die "install verification failed: !TARGET!\SKILL.md is not readable"
    goto finish
)
call :ok "verified !TARGET!\SKILL.md"

call :install_commands
call :warn_if_plugin_installed

REM --- check Python -----------------------------------------------------------
set "PY="
call :try_python python
if "!PY!"=="" call :try_python python3
if "!PY!"=="" call :try_py_launcher
if not "!PY!"=="" (
    call :ok "found !PY!"
) else (
    call :warn "no Python 3.8+ on PATH."
    echo        The skill's scripts need it. Install Python 3 from python.org
    echo        and tick 'Add python.exe to PATH', or the run/parse helpers
    echo        will not work.
    set "PY=python"
)

if "!INSTALLED_VIA!"=="copy" (
    call :warn "this is a copy - later changes in %SOURCE_DIR% will NOT appear here."
    echo        To update, re-run the same command with --force added.
)

echo.
echo Done. Start a new Claude Code session and ask it to run or read a FragPipe
echo analysis; it will pick the skill up automatically.
echo.
echo Optional: set FRAGPIPE_DIR so the skill stops asking which FragPipe to use
echo   setx FRAGPIPE_DIR "C:\FragPipe\FragPipe-24.1-build36"
echo.
echo Verify the skill end to end against a finished FragPipe run:
echo   !PY! "!TARGET!\scripts\selftest.py" --example D:\path\to\fragpipe\output
echo.
echo Uninstall with: !UNINSTALL_HINT!
goto finish

REM --- uninstall --------------------------------------------------------------
:do_uninstall
if not exist "!TARGET!" (
    echo Nothing to remove: !TARGET! does not exist.
    goto finish
)
call :is_junction "!TARGET!"
if not "!IS_JUNCTION!"=="1" (
    REM Only delete a real folder if it really is a copy of this skill.
    set "IS_OURS="
    if exist "!TARGET!\SKILL.md" (
        findstr /i /r /c:"^name:[ ]*fragskills" "!TARGET!\SKILL.md" >nul 2>&1 && set "IS_OURS=1"
    )
    if not "!IS_OURS!"=="1" if not "%FORCE%"=="1" (
        call :die "!TARGET! is not a FragSkills install (no matching SKILL.md)."
        echo        Refusing to delete it. Use --force if you are certain.
        goto finish
    )
)
call :remove_target
if errorlevel 1 goto finish
call :ok "removed !TARGET!"
REM Only the default install owns the shared commands; a --name install never
REM had them, so removing it must leave them alone.
if /i "%SKILL_NAME%"=="fragskills" (
    for %%I in ("!SKILLS_DIR!") do set "CLAUDE_DIR=%%~dpI"
    set "COMMANDS_DIR=!CLAUDE_DIR!commands"
    if exist "!COMMANDS_DIR!\fragskills-*.md" (
        del /Q "!COMMANDS_DIR!\fragskills-*.md" >nul 2>&1
        call :ok "removed the /fragskills-* commands from !COMMANDS_DIR!"
    )
)
goto finish

REM ============================ subroutines ===================================

REM Remove TARGET. For a junction this deletes only the link, never the files
REM it points at -- rmdir does not follow junctions.
:remove_target
call :is_junction "!TARGET!"
if "!IS_JUNCTION!"=="1" (
    rmdir "!TARGET!" 2>nul
    if exist "!TARGET!" rmdir /S /Q "!TARGET!" 2>nul
) else (
    rmdir /S /Q "!TARGET!" 2>nul
)
if exist "!TARGET!" (
    call :die "could not remove !TARGET! - is a program using it?"
    exit /b 1
)
exit /b 0

REM Sets IS_JUNCTION=1 when %1 is a reparse point (junction or symlink).
:is_junction
set "IS_JUNCTION="
fsutil reparsepoint query "%~1" >nul 2>&1 && set "IS_JUNCTION=1"
if "!IS_JUNCTION!"=="" (
    REM Fallback for systems where fsutil is unavailable.
    for /f "delims=" %%J in ('dir /a:l /b "%~dp1." 2^>nul ^| findstr /i /x /c:"%~nx1"') do set "IS_JUNCTION=1"
)
exit /b 0

REM Copy the /fragskills-* slash commands next to the skill. Copied rather than
REM linked: Claude Code reads every *.md directly inside commands\, and per-file
REM symlinks need Developer Mode on Windows. Because they are copies, this runs
REM on every install -- including "already linked" -- so edits take effect.
:install_commands
if not exist "%SOURCE_DIR%\commands\" exit /b 0
for %%I in ("!SKILLS_DIR!") do set "CLAUDE_DIR=%%~dpI"
set "COMMANDS_DIR=!CLAUDE_DIR!commands"
REM Claude Code loads a folder in ~/.claude/skills, or in a trusted project's
REM .claude\skills, that has .claude-plugin\plugin.json as a plugin, and the
REM plugin brings the commands as /fragskills:new and so on. Copies would show
REM them twice.
if exist "%SOURCE_DIR%\.claude-plugin\plugin.json" goto commands_from_plugin
REM The commands address the skill by its default path, so under --name they
REM would point at a skill that is not there. Better to skip and say so.
if /i not "%SKILL_NAME%"=="fragskills" (
    call :warn "skipped the /fragskills-* commands: they load !SKILLS_DIR!\fragskills, not %SKILL_NAME%"
    exit /b 0
)
if not exist "!COMMANDS_DIR!\" mkdir "!COMMANDS_DIR!" 2>nul
REM Prune first, so a command deleted or renamed upstream does not linger here.
if exist "!COMMANDS_DIR!\fragskills-*.md" del /Q "!COMMANDS_DIR!\fragskills-*.md" >nul 2>&1
set "CMD_COUNT=0"
for %%F in ("%SOURCE_DIR%\commands\fragskills-*.md") do (
    copy /Y "%%~fF" "!COMMANDS_DIR!\" >nul 2>&1
    if not errorlevel 1 set /a CMD_COUNT+=1
)
if not "!CMD_COUNT!"=="0" call :ok "installed !CMD_COUNT! /fragskills-* commands into !COMMANDS_DIR!"
exit /b 0

REM Remove the copies an older install left, then say where the commands are.
:commands_from_plugin
if /i "%SKILL_NAME%"=="fragskills" if exist "!COMMANDS_DIR!\fragskills-*.md" (
    del /Q "!COMMANDS_DIR!\fragskills-*.md" >nul 2>&1
    call :ok "removed the old /fragskills-* command copies from !COMMANDS_DIR!"
)
call :ok "the commands load with the skill: /fragskills:new, :run, :status, :report, :stop"
REM Claude Code looks for plugins only in the folder it starts in.
if /i not "%MODE%"=="project" exit /b 0
echo       (when Claude Code starts in !PROJECT_DIR! itself and you trust it;
echo       from a subfolder only the skill loads: ask in plain language)
exit /b 0

REM Both this folder and the marketplace plugin would load: the skill twice.
:warn_if_plugin_installed
findstr /c:"fragskills@nesvilab" "%USERPROFILE%\.claude\plugins\installed_plugins.json" >nul 2>&1
if errorlevel 1 exit /b 0
call :warn "the fragskills plugin is installed too, so Claude Code will show the skill twice."
echo        Keep one: claude plugin uninstall fragskills@nesvilab, or !UNINSTALL_HINT!
exit /b 0

REM Sets LINK_TARGET to the path a junction points at.
:junction_target
set "LINK_TARGET="
for /f "tokens=2 delims=[]" %%T in ('dir /a:l "%~dp1." 2^>nul ^| findstr /i /c:"%~nx1"') do set "LINK_TARGET=%%T"
exit /b 0

:try_python
%~1 -c "import sys; sys.exit(0 if sys.version_info>=(3,8) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=%~1"
exit /b 0

:try_py_launcher
py -3 -c "import sys; sys.exit(0 if sys.version_info>=(3,8) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=py -3"
exit /b 0

:ok
echo OK    %~1
exit /b 0

:warn
echo WARN  %~1
exit /b 0

:die
echo ERROR %~1 1>&2
set "EXITCODE=1"
exit /b 0

:usage
echo Install FragSkills from this folder, so Claude Code can find it.
echo.
echo Most users do not need this script: install the plugin instead, in Claude Code:
echo   /plugin install fragskills --marketplace Nesvilab/FragSkills
echo.
echo   install.bat                  install for the current user
echo                                  (%%USERPROFILE%%\.claude\skills)
echo   install.bat --project        install into .\.claude\skills
echo   install.bat --project DIR    install into DIR\.claude\skills
echo   install.bat --copy           copy the files instead of linking
echo   install.bat --link           fail rather than fall back to copying
echo   install.bat --name NAME      install under a different folder name
echo   install.bat --force          replace an existing installation
echo   install.bat --uninstall      remove it again
echo   install.bat --no-pause       never wait for a keypress at the end
echo   install.bat --help           show this message
echo.
echo By default a directory junction is created, so edits to this folder take
echo effect immediately. Junctions do not require administrator rights.
goto finish

:finish
if defined PAUSE_AT_END (
    echo.
    pause
)
endlocal & exit /b %EXITCODE%
