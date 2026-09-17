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

# SwiftBar runs every executable file in its plugin folder, so that folder
# must hold plugins and nothing else. Pointing it at this clone — which is
# what this script used to do — makes SwiftBar run LICENSE, README.md and
# install.sh as plugins too. Each one claims a menu bar slot, and once the
# bar is full macOS silently drops the overflow, so the real plugin never
# appears. Install into a dedicated folder instead, reusing whichever folder
# SwiftBar already points at unless that folder is this clone.
existing=$(defaults read com.ameba.SwiftBar PluginDirectory 2>/dev/null || true)
if [ -n "$existing" ] && [ "$existing" != "$SCRIPT_DIR" ]; then
    plugin_dir="$existing"
else
    plugin_dir="${XDG_CONFIG_HOME:-$HOME/.config}/swiftbar"
fi
mkdir -p "$plugin_dir"

# Symlinked rather than copied, so `git pull` updates the plugin in place.
# SwiftBar's folder-watcher doesn't reliably see edits through a symlink, so
# a pull wants a SwiftBar restart rather than a refresh.
ln -sfn "$SCRIPT_DIR/$PLUGIN" "$plugin_dir/$PLUGIN"
defaults write com.ameba.SwiftBar PluginDirectory "$plugin_dir"
echo "Plugin linked into $plugin_dir."

# Start at login, added only once.
if ! osascript -e 'tell application "System Events" to get the name of every login item' 2>/dev/null | grep -q SwiftBar; then
    osascript -e 'tell application "System Events" to make login item at end with properties {path:"/Applications/SwiftBar.app", hidden:true}' >/dev/null
fi

# Launch only if not already running: on macOS 26 a reopen event makes
# SwiftBar show its "SwiftBar is already running" menu-bar recovery alert.
# A streaming plugin is started once by SwiftBar, so an already-running
# SwiftBar needs a nudge to pick up a new or updated plugin — a refresh is
# enough for a plugin added to the folder SwiftBar is already watching, but
# a change of folder is only read at launch, so that needs a restart.
if ! pgrep -xq SwiftBar; then
    open -a SwiftBar
elif [ "$plugin_dir" != "$existing" ]; then
    pkill -x SwiftBar || true
    for _ in 1 2 3 4 5; do pgrep -xq SwiftBar || break; sleep 1; done
    open -a SwiftBar
else
    open -g 'swiftbar://refreshallplugins'
fi
echo "Done — look for the watts figure in your menu bar."
