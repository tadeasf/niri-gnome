#!/usr/bin/env bash
# Symlink every file under config/ into ~/.config, so this repo stays the live copy:
# edit your setup as usual, then `git commit` here. Existing files are moved to *.bak-<timestamp>.
#   ./install.sh          link configs
#   ./install.sh --deps   install Fedora packages first, then link
set -euo pipefail

repo=$(cd "$(dirname "$0")" && pwd)
cfg=${XDG_CONFIG_HOME:-$HOME/.config}
stamp=$(date +%Y%m%d-%H%M%S)

deps=(
  niri waybar rofi fuzzel mako swaybg swaylock mate-polkit        # session pieces
  python3-gobject gtk4-layer-shell glib2                           # Alt+Tab switcher
  gstreamer1-plugins-base pipewire-gstreamer                       # switcher window previews
  mozilla-fira-sans-fonts cascadia-code-nf-fonts                   # fonts (UI + bar icons)
  nm-connection-editor pavucontrol wireplumber playerctl brightnessctl
)

if [[ ${1:-} == --deps ]]; then
  sudo dnf install -y "${deps[@]}"
fi

cd "$repo/config"
while IFS= read -r -d '' f; do
  f=${f#./}
  src=$repo/config/$f
  dst=$cfg/$f
  mkdir -p "$(dirname "$dst")"
  if [[ -L $dst && $(readlink "$dst") == "$src" ]]; then
    continue
  fi
  if [[ -e $dst || -L $dst ]]; then
    mv "$dst" "$dst.bak-$stamp"
    echo "backed up  $dst -> $dst.bak-$stamp"
  fi
  ln -s "$src" "$dst"
  echo "linked     $dst"
done < <(find . -type f -print0)

if command -v niri >/dev/null; then
  niri validate -c "$cfg/niri/config.kdl" >/dev/null 2>&1 && echo "niri config is valid" \
    || echo "WARNING: niri validate failed, run: niri validate -c $cfg/niri/config.kdl"
fi
echo "Done. Log out and pick \"Niri\" on the GDM login screen (or reload: niri hot-reloads its config)."
