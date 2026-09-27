#!/bin/bash
# 球団ファンのYouTuber（現地観戦・応援・試合の振り返りなど）をもっと集めて、載せる人をえらぶ（Mac のターミナルで： bash more_fan_mac.sh ）
cd "$(dirname "$0")"
if [ -z "$YT_API_KEY" ]; then read -p "YouTube APIキーを貼り付けて Enter: " YT_API_KEY; fi
export YT_API_KEY
set -e
echo "1/2 プロ野球の球団ファンのYouTuberを広く集めています…（2〜4分）"
python3 fetch.py --mode wide --genre fan
echo "2/2 えらぶ画面を開きます。「⚾ 野球ファン」のタブでチェックを付けて「この内容で保存」を押してください"
python3 curate.py
