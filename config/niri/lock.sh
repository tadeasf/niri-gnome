#!/bin/sh
# Lock screen (Super+Escape and the quick-settings lock button): hyprlock with niri-gnome's own config,
# so a Hyprland ~/.config/hypr/hyprlock.conf is left alone. Avatar = ~/.face or the GNOME account picture.
pidof hyprlock >/dev/null && exit 0
avatar=${XDG_CACHE_HOME:-$HOME/.cache}/niri/avatar
mkdir -p "${avatar%/*}"
for f in "$HOME/.face" "/var/lib/AccountsService/icons/$USER"; do
  [ -r "$f" ] && { ln -sf "$f" "$avatar"; break; }
done
exec hyprlock -c "$HOME/.config/niri/hyprlock.conf"
