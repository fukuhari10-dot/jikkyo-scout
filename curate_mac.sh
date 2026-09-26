#!/bin/bash
# 載せる配信者を選び直す（集め直さない。Mac のターミナルで： bash curate_mac.sh ）
cd "$(dirname "$0")"
if [ -z "$YT_API_KEY" ]; then read -p "YouTube APIキーを貼り付けて Enter: " YT_API_KEY; fi
export YT_API_KEY
python3 curate.py
