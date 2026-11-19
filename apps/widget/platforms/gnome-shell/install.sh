#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
extension_uuid="chronix-widget@dyung.me"
api_config_dir="$HOME/.config/chronix-widget"
api_config="$api_config_dir/api-base-url"
bundle_dir="$(mktemp -d)"
trap 'rm -rf "$bundle_dir"' EXIT

mkdir -p "$api_config_dir"

if [[ ! -f "$api_config" ]]; then
  echo 'http://127.0.0.1:8766' > "$api_config"
fi

gnome-extensions pack --force --out-dir "$bundle_dir" "$repo_root/platforms/gnome-shell"
gnome-extensions install --force "$bundle_dir/$extension_uuid.shell-extension.zip"

if gnome-extensions info "$extension_uuid" >/dev/null 2>&1; then
  gnome-extensions enable "$extension_uuid"
else
  echo "Extension installed. This GNOME Shell session has not indexed it yet; log out and back in to load it."
fi

echo "Chronix GNOME extension installed. API URL: $(cat "$api_config")"
