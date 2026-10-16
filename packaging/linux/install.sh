#!/bin/sh
# Install GhostVault for the current user, without administrator rights:
#
#   ~/.local/opt/GhostVault                         the application (an earlier one is replaced)
#   ~/.local/bin/ghostvault                         the command
#   ~/.local/share/applications/ghostvault.desktop  the applications-menu entry, with its icon
#
# Run it from the unpacked release archive:  ./install.sh
# Remove GhostVault again with:              ~/.local/opt/GhostVault/uninstall.sh
#
# GhostVault's data folder (~/.local/share/ghostvault, or $XDG_DATA_HOME/ghostvault) holds
# the keys to your ghosted files. This script never reads, changes or removes anything in it.

set -eu

fail() {
    printf '%s\n' "install.sh: $*" >&2
    exit 1
}

[ -n "${HOME:-}" ] || fail "HOME is not set"
here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
source_dir=$here/GhostVault
data_home=${XDG_DATA_HOME:-$HOME/.local/share}
case $data_home in
    /*) ;;
    *) data_home=$HOME/.local/share ;;  # the XDG specification: a relative path is ignored
esac
opt_dir=$HOME/.local/opt
app_dir=$opt_dir/GhostVault
bin_dir=$HOME/.local/bin
apps_dir=$data_home/applications
icon_dir=$data_home/icons/hicolor/256x256/apps
marker=.ghostvault-installation

if [ ! -x "$source_dir/ghostvault" ] || [ ! -d "$source_dir/_internal" ]; then
    fail "run this script from the unpacked GhostVault archive ($source_dir/ghostvault is missing)"
fi
for file in uninstall.sh ghostvault.png; do
    [ -f "$here/$file" ] || fail "$here/$file is missing"
done

# Only a folder this script made is ever replaced, and never while GhostVault runs from it.
if [ -e "$app_dir" ] || [ -L "$app_dir" ]; then
    if [ ! -d "$app_dir" ] || [ -L "$app_dir" ] || [ ! -f "$app_dir/$marker" ]; then
        fail "$app_dir exists but was not made by install.sh; move it elsewhere and run install.sh again"
    fi
    for exe in /proc/[0-9]*/exe; do
        if [ "$(readlink "$exe" 2> /dev/null)" = "$app_dir/ghostvault" ]; then
            fail "GhostVault is running from $app_dir; close it, then run install.sh again"
        fi
    done
fi

# The new copy is assembled beside the old one and takes its place only once complete.
mkdir -p "$opt_dir"
staging=$(mktemp -d "$opt_dir/.GhostVault-new.XXXXXX")
previous=""
cleanup() {
    rm -rf "$staging"
    if [ -n "$previous" ] && [ -d "$previous" ] && [ ! -e "$app_dir" ]; then
        mv "$previous" "$app_dir"  # the update stopped half-way: the old version goes back
    fi
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

cp -RP "$source_dir/." "$staging/"
cp "$here/uninstall.sh" "$staging/uninstall.sh"
cp "$here/ghostvault.png" "$staging/ghostvault.png"
chmod 755 "$staging" "$staging/uninstall.sh" "$staging/ghostvault"
printf '%s\n' "This folder was made by GhostVault's install.sh; uninstall.sh removes it." > "$staging/$marker"

if [ -d "$app_dir" ]; then
    previous=$(mktemp -d "$opt_dir/.GhostVault-old.XXXXXX")
    rmdir "$previous"
    mv "$app_dir" "$previous"
fi
mv "$staging" "$app_dir"
if [ -n "$previous" ]; then
    rm -rf "$previous"
    previous=""
fi

# The command. Anything else already called ghostvault (a pip installation, say) is left
# as it is.
mkdir -p "$bin_dir"
link=$bin_dir/ghostvault
command_note=""
if [ -L "$link" ] && [ "$(readlink "$link")" = "$app_dir/ghostvault" ]; then
    :
elif [ -e "$link" ] || [ -L "$link" ]; then
    command_note="$link already exists and was left as it is; start GhostVault from the menu or with $app_dir/ghostvault."
else
    ln -s "$app_dir/ghostvault" "$link"
fi

# The applications-menu entry. As the Desktop Entry Specification asks, Exec= quotes the
# path and escapes the characters it reserves, and every value escapes its backslashes, so
# any home folder works.
desktop_string() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g'
}
desktop_exec() {
    # The single quotes hold sed's expressions, not the shell's.
    # shellcheck disable=SC2016
    printf '%s' "$1" | sed -e 's/[\\"`$]/\\&/g' -e 's/\\/\\\\/g' -e 's/%/%%/g' -e 's/^/"/' -e 's/$/"/'
}
mkdir -p "$apps_dir" "$icon_dir"
cp "$app_dir/ghostvault.png" "$icon_dir/ghostvault.png"
desktop=$apps_dir/ghostvault.desktop
cat > "$desktop.new" << EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=GhostVault
GenericName=Encrypted file storage
Comment=Encrypt files and keep them in your private GitHub repositories
Exec=$(desktop_exec "$app_dir/ghostvault")
TryExec=$(desktop_string "$app_dir/ghostvault")
Icon=ghostvault
Terminal=false
Categories=Utility;FileTools;
StartupWMClass=Ghostvault
EOF
mv "$desktop.new" "$desktop"
if command -v update-desktop-database > /dev/null 2>&1; then
    update-desktop-database -q "$apps_dir" 2> /dev/null || true
fi

trap - EXIT HUP INT TERM
printf '%s\n' "GhostVault is installed in $app_dir."
if [ -n "$command_note" ]; then
    printf '%s\n' "$command_note"
else
    case ":${PATH:-}:" in
        *":$bin_dir:"*) printf '%s\n' "Start it from the applications menu, or with: ghostvault" ;;
        *) printf '%s\n' "Start it from the applications menu, or with: $app_dir/ghostvault ($bin_dir is not on your PATH)" ;;
    esac
fi
printf '%s\n' "Your data folder is not part of the installation: $data_home/ghostvault"
