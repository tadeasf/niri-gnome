#!/usr/bin/env bash
# Symlink every file under config/ into ~/.config, so this repo stays the live copy:
# edit your setup as usual, then `git commit` here. Existing files are moved to *.bak-<timestamp>.
#   ./install.sh          link configs
#   ./install.sh --deps   install packages first (dnf, pacman, zypper or apt), then link
set -euo pipefail

repo=$(cd "$(dirname "$0")" && pwd)
cfg=${XDG_CONFIG_HOME:-$HOME/.config}
stamp=$(date +%Y%m%d-%H%M%S)

# Same set per distro, in this order: session pieces, Alt+Tab switcher + menus (PyGObject, gtk4-layer-shell,
# gdbus), switcher window previews (gstreamer + pipewire), fonts (UI + Nerd Font bar icons), desktop tools.
if [[ ${1:-} == --deps ]]; then
  if command -v dnf >/dev/null; then
    sudo dnf install -y niri waybar rofi mako swaybg swaylock mate-polkit \
      python3-gobject gtk4-layer-shell glib2 \
      gstreamer1-plugins-base pipewire-gstreamer \
      mozilla-fira-sans-fonts cascadia-code-nf-fonts \
      nm-connection-editor pavucontrol wireplumber playerctl brightnessctl swayosd udiskie awww ImageMagick hyprlock
  elif command -v pacman >/dev/null; then
    sudo pacman -S --needed --noconfirm niri waybar rofi mako swaybg swaylock mate-polkit \
      python-gobject gtk4-layer-shell glib2 \
      gst-plugins-base gst-plugin-pipewire \
      ttf-fira-sans ttf-cascadia-code-nerd \
      nm-connection-editor pavucontrol wireplumber playerctl brightnessctl swayosd udiskie
  elif command -v zypper >/dev/null; then
    sudo zypper install -y niri waybar rofi mako swaybg swaylock mate-polkit \
      python3-gobject typelib-1_0-Gtk-4_0 typelib-1_0-Gtk4LayerShell-1_0 glib2-tools \
      gstreamer-plugins-base gstreamer-plugin-pipewire \
      mozilla-fira-fonts \
      NetworkManager-connection-editor pavucontrol wireplumber playerctl brightnessctl
    echo "NOTE: openSUSE has no Cascadia Nerd Font package; install any Nerd Font for the bar icons."
  elif command -v apt-get >/dev/null; then
    sudo apt-get install -y waybar rofi mako-notifier swaybg swaylock mate-polkit \
      python3-gi gir1.2-gtk-4.0 gir1.2-gtk4layershell-1.0 libglib2.0-bin \
      gstreamer1.0-plugins-base gstreamer1.0-pipewire \
      fonts-cascadia-code \
      network-manager-applet pavucontrol wireplumber playerctl brightnessctl
    echo "NOTE: Debian/Ubuntu don't package niri, Fira Sans or a Nerd Font; install those yourself."
  else
    echo "Unknown package manager: install the packages from the dnf line above under your distro's names." >&2
  fi
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
done < <(find . -type f -not -path "*/__pycache__/*" -print0)

# bar, switcher, menus etc. are user units WantedBy=niri.service: they start with niri only, never in GNOME
if command -v systemctl >/dev/null; then
  systemctl --user daemon-reload
  (cd "$repo/config/systemd/user" && systemctl --user enable -- *.service) && echo "enabled    niri session units"
fi

if command -v niri >/dev/null; then
  niri validate -c "$cfg/niri/config.kdl" >/dev/null 2>&1 && echo "niri config is valid" \
    || echo "WARNING: niri validate failed, run: niri validate -c $cfg/niri/config.kdl"
fi
echo "Done. Log out and pick \"Niri\" on the GDM login screen (or reload: niri hot-reloads its config)."
