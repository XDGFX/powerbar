#!/bin/bash
# One-command install: SwiftBar and macmon via Homebrew if missing, plugin
# wired in, SwiftBar launched and set to start at login.
#
# Deliberately not executable in the repo: if SwiftBar's plugin folder is
# pointed at this clone, SwiftBar runs every executable file in it as a
# plugin, and this script would be run on every SwiftBar launch. Invoke it as
# `bash install.sh`.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLUGIN="powerbar.stream.py"

if ! command -v brew >/dev/null; then
    echo "Homebrew is required (https://brew.sh) — install it and re-run." >&2
    exit 1
fi

if ! brew list --cask swiftbar >/dev/null 2>&1 && [ ! -d /Applications/SwiftBar.app ]; then
    echo "Installing SwiftBar…"
    brew install --cask swiftbar
fi

if ! command -v macmon >/dev/null && [ ! -x /opt/homebrew/bin/macmon ]; then
    echo "Installing macmon…"
    brew install macmon
fi

chmod +x "$SCRIPT_DIR/$PLUGIN"

# If SwiftBar already has a plugin folder, copy the plugin into it — a copy,
# not a symlink, because SwiftBar's folder-watcher is unreliable with
# symlinks. Otherwise point SwiftBar at this clone, so future `git pull`s
# update the plugin in place.
existing=$(defaults read com.ameba.SwiftBar PluginDirectory 2>/dev/null || true)
if [ -n "$existing" ] && [ "$existing" != "$SCRIPT_DIR" ]; then
    cp "$SCRIPT_DIR/$PLUGIN" "$existing/"
    chmod +x "$existing/$PLUGIN"
    echo "Plugin copied into $existing (re-run this script after a git pull to update)."
else
    defaults write com.ameba.SwiftBar PluginDirectory "$SCRIPT_DIR"
    echo "SwiftBar plugin folder set to $SCRIPT_DIR."
fi

# Start at login, added only once.
if ! osascript -e 'tell application "System Events" to get the name of every login item' 2>/dev/null | grep -q SwiftBar; then
    osascript -e 'tell application "System Events" to make login item at end with properties {path:"/Applications/SwiftBar.app", hidden:true}' >/dev/null
fi

# Launch only if not already running: on macOS 26 a reopen event makes
# SwiftBar show its "SwiftBar is already running" menu-bar recovery alert.
# A streaming plugin is started once by SwiftBar, so an already-running
# SwiftBar needs a refresh to pick up a copied or updated plugin.
if pgrep -xq SwiftBar; then
    open -g 'swiftbar://refreshallplugins'
else
    open -a SwiftBar
fi
echo "Done — look for the watts figure in your menu bar."
