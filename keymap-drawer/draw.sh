#!/bin/sh
# キー配置図（keymap-drawer/mona2.svg）を config/mona2.keymap から作り直す。リポジトリのどこから実行してもよい
# 引数は .github/workflows/draw.yml の parse_args / draw_args と同じにしておく
set -eu
cd "$(dirname "$0")/.."

L4='L4（左親指の「-」を押したまま、右親指の「SPACE」を押す）'
L5='L5（左親指の「BSPC」を押したまま、右親指の「SPACE」を押す）'
BLE='BLE（右親指の「SPACE」を押したまま、左親指の「-」を押す）'

uvx --from keymap-drawer keymap -c keymap_drawer.config.yaml parse -z config/mona2.keymap \
  --layer-names BASE SYM NUM NAV "$L4" "$L5" L6 L7 L8 SCROLL MOUSE "$BLE" > keymap-drawer/mona2.yaml
uvx --from keymap-drawer keymap -c keymap_drawer.config.yaml draw keymap-drawer/mona2.yaml keymap-drawer/mona2_combos.yaml \
  -j config/mona2.json -s BASE SYM NUM NAV "$L4" "$L5" MOUSE "$BLE" > keymap-drawer/mona2.svg
echo "keymap-drawer/mona2.svg を作り直しました"
