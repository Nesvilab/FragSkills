#!/usr/bin/env bash
#
# Install FragSkills so Claude Code can find it.
#
#   ./install.sh                    install for the current user (~/.claude/skills)
#   ./install.sh --project          install into ./.claude/skills
#   ./install.sh --project /path    install into /path/.claude/skills
#   ./install.sh --copy             copy the files instead of symlinking
#   ./install.sh --uninstall        remove it again
#   ./install.sh --help
#
# By default the skill is symlinked, so edits to this directory take effect
# immediately.  Use --copy when the skill lives somewhere the machine running
# Claude Code cannot reach (a removable drive, a different host).

set -euo pipefail

SKILL_NAME="fragskills"
MODE="user"
PROJECT_DIR=""
INSTALL_METHOD="auto"   # auto | link | copy
UNINSTALL=0
FORCE=0

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- pretty output -----------------------------------------------------------
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
    C_BOLD=$'\033[1m'; C_OFF=$'\033[0m'
else
    C_RED=""; C_GREEN=""; C_YELLOW=""; C_BOLD=""; C_OFF=""
fi
info() { printf '%s\n' "$*"; }
ok()   { printf '%s\n' "${C_GREEN}OK${C_OFF}    $*"; }
warn() { printf '%s\n' "${C_YELLOW}WARN${C_OFF}  $*"; }
die()  { printf '%s\n' "${C_RED}ERROR${C_OFF} $*" >&2; exit 1; }

usage() {
    cat <<'EOF'
Install FragSkills so Claude Code can find it.

  ./install.sh                    install for the current user (~/.claude/skills)
  ./install.sh --project          install into ./.claude/skills
  ./install.sh --project DIR      install into DIR/.claude/skills
  ./install.sh --copy             copy the files instead of symlinking
  ./install.sh --link             fail rather than fall back to copying
  ./install.sh --name NAME        install under a different folder name
  ./install.sh --force            replace an existing installation
  ./install.sh --uninstall        remove it again
  ./install.sh --help             show this message

By default the skill is symlinked, so edits to this directory take effect
immediately.  Use --copy when the skill lives somewhere the machine running
Claude Code cannot reach (a removable drive, a different host).
EOF
    exit 0
}

# --- arguments ---------------------------------------------------------------
while [ $# -gt 0 ]; do
    case "$1" in
        --user)      MODE="user" ;;
        --project)
            MODE="project"
            if [ $# -gt 1 ] && [ "${2#-}" = "$2" ]; then PROJECT_DIR="$2"; shift; fi
            ;;
        --copy)      INSTALL_METHOD="copy" ;;
        --link)      INSTALL_METHOD="link" ;;
        --name)      [ $# -gt 1 ] || die "--name needs a value"; SKILL_NAME="$2"; shift ;;
        --uninstall) UNINSTALL=1 ;;
        --force|-f)  FORCE=1 ;;
        --help|-h)   usage ;;
        *)           die "unknown option: $1  (try --help)" ;;
    esac
    shift
done

# --- resolve source and target ----------------------------------------------
[ -f "$SOURCE_DIR/SKILL.md" ] || die "SKILL.md not found in $SOURCE_DIR.
This script must stay inside the FragSkills directory."

if [ "$MODE" = "project" ]; then
    PROJECT_DIR="${PROJECT_DIR:-$PWD}"
    [ -d "$PROJECT_DIR" ] || die "project directory does not exist: $PROJECT_DIR"
    PROJECT_DIR="$(cd "$PROJECT_DIR" && pwd)"
    SKILLS_DIR="$PROJECT_DIR/.claude/skills"
    SCOPE_DESC="project ($PROJECT_DIR)"
else
    SKILLS_DIR="$HOME/.claude/skills"
    SCOPE_DESC="user ($HOME)"
fi
TARGET="$SKILLS_DIR/$SKILL_NAME"

# --- is an existing target safe to remove? -----------------------------------
# Only ever remove a symlink, or a directory that really is a copy of this
# skill.  Never touch anything else.
target_is_ours() {
    [ -L "$TARGET" ] && return 0
    [ -d "$TARGET" ] && [ -f "$TARGET/SKILL.md" ] \
        && grep -qi '^name:[[:space:]]*fragskills' "$TARGET/SKILL.md" && return 0
    return 1
}

remove_target() {
    if [ -L "$TARGET" ]; then
        rm -f "$TARGET"                 # removes the link, never its contents
    elif [ -d "$TARGET" ]; then
        rm -rf "$TARGET"
    fi
}

COMMANDS_DIR="${SKILLS_DIR%/skills}/commands"

# Copy the /fragskills-* slash commands next to the skill. Copied rather than
# linked: Claude Code reads every *.md directly inside commands/, and per-file
# symlinks need privileges some platforms withhold. Because they are copies,
# this runs on every install -- including "already linked" -- so edits land.
install_commands() {
    [ -d "$SOURCE_DIR/commands" ] || return 0
    # The commands address the skill by its default path, so under --name they
    # would point at a skill that is not there. Better to skip and say so.
    if [ "$SKILL_NAME" != "fragskills" ]; then
        warn "skipped the /fragskills-* commands: they load $SKILLS_DIR/fragskills, not $SKILL_NAME"
        return 0
    fi
    mkdir -p "$COMMANDS_DIR"
    # Prune first, so a command deleted or renamed upstream does not linger here.
    rm -f "$COMMANDS_DIR"/fragskills-*.md
    cmd_count=0
    for f in "$SOURCE_DIR"/commands/fragskills-*.md; do
        [ -f "$f" ] || continue
        cp -f "$f" "$COMMANDS_DIR/" && cmd_count=$((cmd_count + 1))
    done
    [ "$cmd_count" -gt 0 ] && ok "installed $cmd_count /fragskills-* commands into $COMMANDS_DIR"
    return 0
}

# --- uninstall ---------------------------------------------------------------
if [ "$UNINSTALL" -eq 1 ]; then
    if [ ! -e "$TARGET" ] && [ ! -L "$TARGET" ]; then
        info "Nothing to remove: $TARGET does not exist."
        exit 0
    fi
    if ! target_is_ours && [ "$FORCE" -ne 1 ]; then
        die "$TARGET is not a FragSkills install (no matching SKILL.md).
Refusing to delete it. Use --force if you are certain."
    fi
    remove_target
    ok "removed $TARGET"
    # Only the default install owns the shared commands; a --name install never
    # had them, so removing it must leave them alone.
    if [ "$SKILL_NAME" = "fragskills" ] \
       && ls "$COMMANDS_DIR"/fragskills-*.md >/dev/null 2>&1; then
        rm -f "$COMMANDS_DIR"/fragskills-*.md
        ok "removed the /fragskills-* commands from $COMMANDS_DIR"
    fi
    exit 0
fi

# --- install -----------------------------------------------------------------
info "${C_BOLD}Installing FragSkills${C_OFF}"
info "  source : $SOURCE_DIR"
info "  target : $TARGET"
info "  scope  : $SCOPE_DESC"
info ""

if [ -e "$TARGET" ] || [ -L "$TARGET" ]; then
    if [ -L "$TARGET" ] && [ "$(readlink "$TARGET")" = "$SOURCE_DIR" ]; then
        ok "already linked to this directory"
        install_commands          # the link is live, but commands are copies
        exit 0
    fi
    if [ "$FORCE" -ne 1 ]; then
        if target_is_ours; then
            die "$TARGET already exists (a different FragSkills install).
Re-run with --force to replace it."
        fi
        die "$TARGET already exists and is not FragSkills.
Re-run with --force to replace it, or use --name to install under another name."
    fi
    remove_target
    warn "replaced the previous $TARGET"
fi

mkdir -p "$SKILLS_DIR"

installed_via=""
if [ "$INSTALL_METHOD" != "copy" ]; then
    if ln -s "$SOURCE_DIR" "$TARGET" 2>/dev/null; then
        installed_via="symlink"
    elif [ "$INSTALL_METHOD" = "link" ]; then
        die "could not create a symlink at $TARGET"
    else
        warn "symlink failed, falling back to copying"
    fi
fi

if [ -z "$installed_via" ]; then
    mkdir -p "$TARGET"
    # Copy contents, skipping caches and VCS metadata.
    (cd "$SOURCE_DIR" && tar --exclude='.git' --exclude='__pycache__' \
        --exclude='*.pyc' -cf - .) | (cd "$TARGET" && tar -xf -)
    installed_via="copy"
fi

ok "installed via $installed_via"
[ -f "$TARGET/SKILL.md" ] || die "install verification failed: $TARGET/SKILL.md is not readable"
ok "verified $TARGET/SKILL.md"

install_commands

# --- check Python ------------------------------------------------------------
PY=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 \
       && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)' 2>/dev/null; then
        PY="$candidate"; break
    fi
done
if [ -n "$PY" ]; then
    ok "found $PY ($("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])'))"
else
    warn "no Python 3.8+ on PATH. The skill's scripts need it; install Python 3
      or the run/parse helpers will not work."
fi

if [ "$installed_via" = "copy" ]; then
    warn "this is a copy - changes in $SOURCE_DIR will NOT appear here.
      Re-run this installer to update."
fi

info ""
info "${C_BOLD}Done.${C_OFF} Start a new Claude Code session and ask it to run or read"
info "a FragPipe analysis; it will pick the skill up automatically."
info ""
info "Optional: point the skill at your FragPipe install so you can omit --fragpipe"
info "  export FRAGPIPE_BIN=/path/to/fragpipe/bin/fragpipe"
info ""
info "Verify the skill end to end against a finished FragPipe run:"
info "  ${PY:-python3} \"$TARGET/scripts/selftest.py\" --example /path/to/fragpipe/output"
info ""
info "Uninstall with: $0 --uninstall${PROJECT_DIR:+ --project \"$PROJECT_DIR\"}"
