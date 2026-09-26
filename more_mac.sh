#!/bin/bash
# 配信者をもっと集めて、載せる人をえらぶ（Mac のターミナルで： bash more_mac.sh ）
cd "$(dirname "$0")"
if [ -z "$YT_API_KEY" ]; then read -p "YouTube APIキーを貼り付けて Enter: " YT_API_KEY; fi
export YT_API_KEY
set -e
echo "1/2 プロスピの動画を出しているチャンネルを広く集めています…（2〜4分）"
python3 fetch.py --mode wide
echo "2/2 えらぶ画面を開きます。チェックを付けて「この内容で保存」を押してください"
python3 curate.py
