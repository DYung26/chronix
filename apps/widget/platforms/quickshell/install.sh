#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
config_home="$HOME/.config"
quickshell_dir="$config_home/quickshell"
config_link="$quickshell_dir/chronix-widget"
api_config_dir="$config_home/chronix-widget"
api_config="$api_config_dir/api-base-url"
service_dir="$config_home/systemd/user"

mkdir -p "$quickshell_dir" "$api_config_dir" "$service_dir"

if [[ -e "$config_link" && ! -L "$config_link" ]]; then
  echo "Refusing to replace existing non-symlink config: $config_link" >&2
  exit 1
fi

ln -sfn "$repo_root/platforms/quickshell" "$config_link"

if [[ ! -f "$api_config" ]]; then
  echo 'http://127.0.0.1:8766' > "$api_config"
fi

install -m 0644 "$repo_root/systemd/user/chronix-widget-quickshell.service" "$service_dir/chronix-widget-quickshell.service"

systemctl --user daemon-reload
systemctl --user enable --now chronix-widget-quickshell.service
echo "Installed Chronix Quickshell overlay. API URL: $(cat "$api_config")"
