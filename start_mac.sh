#!/bin/bash
# はじめて本物のデータを作るとき用（Mac のターミナルで： bash start_mac.sh ）
cd "$(dirname "$0")"
if [ -z "$YT_API_KEY" ]; then read -p "YouTube APIキーを貼り付けて Enter: " YT_API_KEY; fi
export YT_API_KEY
set -e
echo "1/4 プロスピ専門の配信者をさがしています…"
python3 fetch.py --mode seed
echo "2/4 プロスピ専門（OK）の人だけ登録します…"
python3 fetch.py --merge-proposed
echo "3/4 登録者数・動画を集めています…"
python3 fetch.py --mode full
echo "4/4 ライブ・配信予定を集めています…"
python3 fetch.py --mode live
echo ""
echo "できました： $(pwd)/data/latest.json"
echo "アプリの上の黒い帯「タップで本物のデータを読み込む」から、このファイルを選んでください。"
open data 2>/dev/null || true
