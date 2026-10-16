#!/bin/sh
# Remove GhostVault as installed by install.sh: the application in ~/.local/opt/GhostVault,
# the ghostvault command and the applications-menu entry.
#
#   ~/.local/opt/GhostVault/uninstall.sh
#
# Your GhostVault data folder (~/.local/share/ghostvault, or $XDG_DATA_HOME/ghostvault) is
# kept: it holds the keys to your ghosted files, which cannot be restored without them (or
# without a recovery file). Delete it yourself only when you no longer need any of them.

set -eu

fail() {
    printf '%s\n' "uninstall.sh: $*" >&2
    exit 1
}

[ -n "${HOME:-}" ] || fail "HOME is not set"
data_home=${XDG_DATA_HOME:-$HOME/.local/share}
case $data_home in
    /*) ;;
    *) data_home=$HOME/.local/share ;;
esac
app_dir=$HOME/.local/opt/GhostVault
link=$HOME/.local/bin/ghostvault
desktop=$data_home/applications/ghostvault.desktop
icon=$data_home/icons/hicolor/256x256/apps/ghostvault.png
marker=.ghostvault-installation

if [ ! -d "$app_dir" ] || [ -L "$app_dir" ] || [ ! -f "$app_dir/$marker" ]; then
    fail "there is no GhostVault installed by install.sh in $app_dir"
fi
for exe in /proc/[0-9]*/exe; do
    if [ "$(readlink "$exe" 2> /dev/null)" = "$app_dir/ghostvault" ]; then
        fail "GhostVault is running from $app_dir; close it, then run uninstall.sh again"
    fi
done

if [ -L "$link" ] && [ "$(readlink "$link")" = "$app_dir/ghostvault" ]; then
    rm -f "$link"
fi
tryexec="TryExec=$(printf '%s' "$app_dir/ghostvault" | sed -e 's/\\/\\\\/g')"
if [ -f "$desktop" ] && grep -qxF "$tryexec" "$desktop"; then
    rm -f "$desktop"
    if command -v update-desktop-database > /dev/null 2>&1; then
        update-desktop-database -q "$data_home/applications" 2> /dev/null || true
    fi
fi
if [ -f "$icon" ] && cmp -s "$icon" "$app_dir/ghostvault.png"; then
    rm -f "$icon"
fi
rm -rf "$app_dir"  # last: this script is inside it

printf '%s\n' "GhostVault was removed."
printf '%s\n' "Your data folder was kept: $data_home/ghostvault"
printf '%s\n' "It holds the keys to your ghosted files. Delete it only when you no longer need any of them."
