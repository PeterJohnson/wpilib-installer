#!/usr/bin/env bash
set -euo pipefail
# Hosted runners include unrelated third-party APT sources. An outage there
# must not prevent installing Ubuntu's signed native GUI/IFW dependencies.
source_list=$(mktemp)
trap 'rm -f "$source_list"' EXIT
cat > "$source_list" <<'SOURCES'
deb [signed-by=/usr/share/keyrings/ubuntu-archive-keyring.gpg] http://archive.ubuntu.com/ubuntu noble main universe restricted multiverse
deb [signed-by=/usr/share/keyrings/ubuntu-archive-keyring.gpg] http://archive.ubuntu.com/ubuntu noble-updates main universe restricted multiverse
deb [signed-by=/usr/share/keyrings/ubuntu-archive-keyring.gpg] http://security.ubuntu.com/ubuntu noble-security main universe restricted multiverse
SOURCES
chmod 644 "$source_list"
options=(-o "Dir::Etc::sourcelist=$source_list" -o 'Dir::Etc::sourceparts=-' -o 'APT::Get::List-Cleanup=0' -o 'Acquire::Retries=3')
sudo apt-get "${options[@]}" update
sudo apt-get "${options[@]}" install -y 7zip libxkbcommon-x11-0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0 libxcb-cursor0 libxcb-shape0 libxcb-xfixes0 libxcb-sync1 libx11-xcb1 "$@"
