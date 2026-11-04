#!/bin/sh
# Foragent installer.
#
#   curl -fsSL https://raw.githubusercontent.com/JinYoon-builds/foragent/main/install.sh | sh
#   npx foragent
#
# It sets up uv when it is missing, installs the fa command with uv, and registers the
# Foragent Claude Code plugin when Claude Code is installed. It asks before it installs.
# Pass --yes to install without the question (for scripts and CI).
# A second run changes nothing: an existing fa is left alone; fa update owns updates.
#
# FORAGENT_SOURCE overrides what uv installs (default: this repository on GitHub);
# CI sets it to the checked-out copy so a pull request tests its own code.

set -eu

REPO="JinYoon-builds/foragent"
SOURCE="${FORAGENT_SOURCE:-git+https://github.com/$REPO}"
ONE_LINER="curl -fsSL https://raw.githubusercontent.com/JinYoon-builds/foragent/main/install.sh | sh"
UV_INSTALLER="https://astral.sh/uv/install.sh"

say() { printf '%s\n' "$*"; }
fail() { printf 'foragent: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

yes=0
for arg in "$@"; do
  case "$arg" in
    -y|--yes) yes=1 ;;
    -h|--help)
      say "Usage: install.sh [--yes]"
      say "Installs uv (when missing), the Foragent fa command, and the Foragent Claude Code plugin."
      say "  --yes   install without asking"
      exit 0 ;;
    *) fail "unknown option: $arg (try --help)" ;;
  esac
done

finish() {
  if have codex; then
    say ""
    say "Codex was found. Codex as PM is experimental; Claude Code is the supported PM."
  fi
  say ""
  say "Foragent is installed. Open Claude Code in your project folder and type /fa"
}

# A fresh shell (including the next CI step) may not have loaded uv's PATH changes.
# Discover existing tools before downloading, editing profiles, or registering plugins.
original_path="$PATH"
PATH="$PATH:${XDG_BIN_HOME:-$HOME/.local/bin}:$HOME/.local/bin:$HOME/.cargo/bin"
export PATH
if ! have fa && have uv; then
  bin_dir="$(uv tool dir --bin)"
  PATH="$PATH:$bin_dir"
  export PATH
fi
if have fa; then
  say "Foragent is already installed. Nothing was changed. Run fa update to update it."
  finish
  exit 0
fi

say "Foragent installer"
say ""
say "This will:"
if have uv; then
  say "  - use the uv already on this machine ($(command -v uv))"
else
  say "  - install uv, the Python tool installer, from $UV_INSTALLER"
fi
say "  - install the fa command with uv from $SOURCE"
if have claude; then
  say "  - register the Foragent plugin in Claude Code (claude plugin install)"
fi
say ""

if [ "$yes" -ne 1 ]; then
  # Piped through `curl | sh`, stdin is the script, so ask on the terminal itself.
  if (exec </dev/tty) 2>/dev/null; then
    printf 'Install Foragent now? [y/N] ' >/dev/tty
    answer=""
    read -r answer </dev/tty || answer=""
    case "$answer" in
      y|Y|yes|YES|Yes) ;;
      *) say "Nothing was installed."; exit 0 ;;
    esac
  else
    say "No terminal to ask on, so nothing was installed."
    say "To install without the question, run:"
    say "  $ONE_LINER -s -- --yes"
    say "or, with Node:"
    say "  npx foragent --yes"
    exit 0
  fi
fi

fetch() {
  if have curl; then
    curl -LsSf "$1"
  elif have wget; then
    wget -qO- "$1"
  else
    fail "curl or wget is needed to download $1"
  fi
}

if ! have uv; then
  say "Installing uv..."
  fetch "$UV_INSTALLER" | sh
  # uv's installer puts uv in ~/.local/bin (or $XDG_BIN_HOME) and updates shell profiles
  # for new shells; this shell needs the directory now.
  PATH="${XDG_BIN_HOME:-$HOME/.local/bin}:$HOME/.cargo/bin:$PATH"
  export PATH
  have uv || fail "uv was installed but is not on PATH; open a new terminal and run this installer again"
fi

say "Installing the fa command..."
uv tool install "$SOURCE"

bin_dir="$(uv tool dir --bin)"
case ":$original_path:" in
  *":$bin_dir:"*) ;;
  *)
    uv tool update-shell >/dev/null 2>&1 || true
    PATH="$bin_dir:$PATH"
    export PATH
    say "Added $bin_dir to your shell PATH; open a new terminal to use fa there."
    ;;
esac

if have claude; then
  # Only add or install what is missing, so a second run leaves Claude Code as it is.
  if ! claude plugin marketplace list --json 2>/dev/null | grep -q '"name": *"foragent"'; then
    say "Adding the Foragent plugin marketplace to Claude Code..."
    claude plugin marketplace add "$REPO" || fail "could not add the Foragent plugin marketplace; run: claude plugin marketplace add $REPO"
  fi
  if claude plugin list --json 2>/dev/null | grep -q '"id": *"foragent@foragent"'; then
    say "The Foragent plugin is already registered in Claude Code."
  else
    say "Registering the Foragent plugin in Claude Code..."
    claude plugin install "foragent@foragent" || fail "could not install the Foragent plugin; run: claude plugin install foragent@foragent"
  fi
else
  say "Claude Code was not found. After you install it, register the Foragent plugin:"
  say "  claude plugin marketplace add $REPO"
  say "  claude plugin install foragent@foragent"
fi

finish
