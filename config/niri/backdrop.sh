#!/bin/sh
# Overview backdrop (Super+A): a blurred, darkened copy of the GNOME wallpaper, shown by its own
# awww-daemon so swaybg keeps the sharp one on the workspaces. Used by niri-backdrop.service:
#   backdrop.sh prepare   make ~/.cache/niri/wallpaper-blur.png (only when the wallpaper changed)
#   backdrop.sh show      wait for the daemon, then hand it the image
set -eu
out=${XDG_CACHE_HOME:-$HOME/.cache}/niri/wallpaper-blur.png
case ${1:-} in
  prepare)
    # shortcut: plain sed strips file://, a percent-encoded path would break (same as niri-swaybg.service)
    src=$(gsettings get org.gnome.desktop.background picture-uri-dark | sed -e "s|^.file://||" -e "s|.\$||")
    mkdir -p "${out%/*}"
    if [ ! -s "$out" ] || [ "$(cat "$out.src" 2>/dev/null)" != "$src" ] || [ "$src" -nt "$out" ]; then
      # shrink first: blurring a 4x-upscaled wallpaper at full size takes seconds, this takes ~0.2 s
      magick "$src" -resize 1720x -blur 0x18 -modulate 60 "$out.tmp.png"
      mv "$out.tmp.png" "$out"
      printf '%s' "$src" > "$out.src"
    fi ;;
  show)
    i=0; until awww query -n backdrop >/dev/null 2>&1; do i=$((i+1)); [ $i -gt 100 ] && exit 1; sleep 0.1; done
    exec awww img -n backdrop --transition-type none "$out" ;;
  *) echo "usage: $0 prepare|show" >&2; exit 2 ;;
esac
