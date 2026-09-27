#!/usr/bin/env python3
"""
チューバースカウト データ収集スクリプト（標準ライブラリのみ）

使い方:
  YT_API_KEY=xxxx python fetch.py --mode live       # 10分ごと: ライブ中・配信予定・新着動画（ついでに試合速報・球団公式チャンネルの新着）
  YT_API_KEY=xxxx python fetch.py --mode full       # 1時間ごと: 登録者数・動画の再生数・トピック・大台達成
  YT_API_KEY=xxxx python fetch.py --mode discover   # 1日1回: 新しい配信者の候補をさがす（人が確認してから channels.json に追加）
  YT_API_KEY=xxxx python fetch.py --mode news       # 4時間ごと: 速報（話題ごとの最新動画）を data/news.json に集める
  YT_API_KEY=xxxx python fetch.py --mode seed       # 最初の1回: seed_names.json の名前・@ハンドルから本物のチャンネルを探して
                                                    #   channels.proposed.json を作る（人が確認して不要な行を消す）
  YT_API_KEY=xxxx python fetch.py --mode wide       # プロスピの動画を出しているチャンネルを広く集める（候補づくり。約1,800ユニット）
  YT_API_KEY=xxxx python fetch.py --mode wide --genre fan   # 球団ファンのYouTuber（現地観戦・応援・振り返りなど）を広く集める
  python curate.py                                  # 候補をブラウザで見ながら、載せる人を選ぶ（channels.json を書きかえる）
  python fetch.py --merge-proposed                  # 確認した channels.proposed.json を channels.json に足す
  python fetch.py --selftest                        # APIを使わずに計算部分だけテスト

出力（アプリはこれだけ読めばよい）:
  data/latest.json   … チャンネル一覧・ライブ・予定・動画・トピック・大台達成をまとめたもの
その他:
  data/candidates.json         … discover で見つかった掲載候補
  data/snap/YYYY-MM-DD.json    … その日の22時台最初の更新での同時視聴者数（「今夜の1位予想」の答え合わせ用）
  data/handles.json            … @ハンドル → チャンネルID の対応（一度調べたら保存）
  data/news.json               … 速報（話題ごとの最新動画・球団公式チャンネルの新着。7日より古いものは消す）
  data/games_npb.json          … 試合日程と結果（NPB の公開日程ページから）
  data/games_live.json         … 今日の試合の点数・回・試合終了（NPB の試合速報ページから。live のついでに約10分ごと）

channels.json には "id"（UC…）か "handle"（@…）のどちらかを書けばよい。
"genre" は "game"（ゲーム実況・プロスピ）か "fan"（プロ野球の球団ファン）。書かなければ "game"。

YouTube の規約（API Developer Policies）に合わせていること:
  - 登録者数・再生数などは YouTube の公開値をそのまま出す。増加数・伸び率などの独自の指標は作らない（III.E.4.h）
  - 複数チャンネルの数値を合計・集計しない（III.E.2）
  - 過去の登録者数の記録は持たない。予想用の記録も30日で消す（III.E.4）
"""
import argparse, datetime as dt, json, os, re, sys, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET

API = "https://www.googleapis.com/youtube/v3/"
KEY = os.environ.get("YT_API_KEY", "")
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
SNAP = os.path.join(DATA, "snap")
JST = dt.timezone(dt.timedelta(hours=9))

RELATED = ["プロスピ", "プロ野球スピリッツ"]
SENMON_RATIO = 0.7   # 最近の動画の7割以上がプロスピなら「プロスピ専門」として載せる
REVIEW_RATIO = 0.5   # 掲載中でも5割を切ったら data/review.json に出して人が確認する
KEEP_DAYS = 30       # YouTube API の規約：登録者数などは30日を超えて保存しない（更新か削除）
DISCOVER_QUERIES = ["プロスピA", "プロ野球スピリッツA", "プロスピA リアタイ", "プロスピ ガチャ", "プロスピ 純正", "プロスピ 生配信"]
# 広く集める（--mode wide）：プロスピ専門でなくても、最近プロスピの動画を出しているチャンネルを候補にする。載せるかは人が選ぶ
WIDE_QUERIES = ["プロスピA", "プロスピ リアタイ", "プロスピ ガチャ", "プロスピ 無課金", "プロスピ 純正", "プロスピ 育成",
                "プロスピ 生配信", "プロ野球スピリッツA", "プロスピ 初心者", "プロスピ オーダー", "プロスピ イベント", "プロスピ OB"]
WIDE_PAGES = 2          # 1つの言葉につき何ページ（50件ずつ）さがすか。search.list は1回100ユニット
WIDE_MIN_RELATED = 2    # 最近15本のうち、プロスピの動画が何本以上あれば候補にするか
WIDE_MIN_SUBS = 100     # 登録者がこれより少ないチャンネルは候補にしない

# ---------- ⚾ プロ野球（球団ファンのYouTuber）：現地観戦・応援・試合の振り返り・ニュース考察・ドラフト・球場グルメ ----------
FAN_DISCOVER_QUERIES = ["プロ野球 現地観戦", "プロ野球 反省会", "応援歌 現地", "プロ野球 ドラフト 考察", "球場グルメ"]
FAN_WIDE_QUERIES = ["プロ野球 現地観戦", "プロ野球 反省会", "プロ野球 試合 振り返り", "応援歌 現地", "プロ野球 ドラフト 考察", "プロ野球 ニュース 考察",
                    "球場グルメ", "野球観戦 vlog", "二軍 ファーム 観戦", "プロ野球 順位 予想", "プロ野球 ファン 雑談", "プロ野球 戦力外 補強"]

# ---------- 速報（話題ごとの最新動画） ----------
# world："yakyu"＝⚾ プロ野球、"game"＝🎮 ゲーム実況。キットの一番上に topics.json（同じ形）を置くと、こちらの代わりに使う
# search.list は1回100ユニット。6つの話題 × 4時間ごと（1日6回）≈ 3,600ユニット
NEWS_TOPICS = [
    {"id": "ohtani", "world": "yakyu", "q": "大谷翔平", "label": "大谷翔平"},
    {"id": "npb", "world": "yakyu", "q": "プロ野球 ハイライト", "label": "プロ野球"},
    {"id": "mlb_jp", "world": "yakyu", "q": "山本由伸 OR 佐々木朗希 OR 今永昇太 OR 鈴木誠也", "label": "日本人メジャー"},
    {"id": "draft", "world": "yakyu", "q": "プロ野球 ドラフト OR 移籍 OR FA", "label": "ドラフト・移籍"},
    {"id": "prospi_new", "world": "game", "q": "プロスピA 新情報 OR 新シリーズ OR 最新情報", "label": "新情報"},
    {"id": "prospi_gacha", "world": "game", "q": "プロスピA ガチャ 新", "label": "ガチャ"},
]
NEWS_KEEP_DAYS = 7     # 速報は7日で消す（YouTube の規約の30日より短い）
NEWS_MAX = 160         # 速報の最大件数
NEWS_HOURS = 48        # 何時間前までの動画をさがすか

# 球団・リーグの公式チャンネル：RSS で新着を見る（APIの割り当てを使わない）。live のついでに約20分ごと
# キットの一番上に news_channels.json（同じ形）を置くと、同じ id の行は置きかえ、新しい id の行は足す（"off": true でその行を使わない）
NEWS_CHANNELS = [
    {"id": "UCXxg0igSYUp0tqdd6luPEnQ", "name": "読売ジャイアンツ", "team": "巨人", "label": "巨人"},
    {"id": "UCqm35j3ustKFyXQVnX5tlXw", "name": "阪神タイガース", "team": "阪神", "label": "阪神"},
    {"id": "UChJI9KrjSgPzv_kfX6yuqhA", "name": "横浜DeNAベイスターズ", "team": "DeNA", "label": "DeNA"},
    {"id": "UCIEmSQYznT9cuTZzBN-x3SQ", "name": "北海道日本ハムファイターズ", "team": "日本ハム", "label": "日本ハム"},
    {"id": "UCbDAmhyRx9bakv-0Gucglgg", "name": "福岡ソフトバンクホークス", "team": "ソフトバンク", "label": "ソフトバンク"},
    {"id": "UC57LcTUKgjDg_K_VJXnmCTg", "name": "中日ドラゴンズ", "team": "中日", "label": "中日"},
    {"id": "UC6qnjAoknKc6nUwhxVYL_DA", "name": "千葉ロッテマリーンズ", "team": "ロッテ", "label": "ロッテ"},
    {"id": "UCE_pCd9bB79Tf8eC_QZHkpA", "name": "オリックス・バファローズ", "team": "オリックス", "label": "オリックス"},
    {"id": "UCt7cNctKXoKece38M9gJV7A", "name": "東京ヤクルトスワローズ", "team": "ヤクルト", "label": "ヤクルト"},
    {"id": "UChLK3zS3-kR21JVTaNovPIg", "name": "埼玉西武ライオンズ", "team": "西武", "label": "西武"},
    {"id": "UC7DjQdai62xSVfCUhiP5Oiw", "name": "東北楽天ゴールデンイーグルス", "team": "楽天", "label": "楽天"},
    {"id": "UC0VGvOEN22JcprH7pZrCwiw", "name": "広島東洋カープ", "team": "広島", "label": "広島"},
    {"id": "UCrjlKkKTAyNn6gekRM3p0Aw", "name": "パ・リーグ（プロ野球チャンネル パ）", "team": "", "label": "パ・リーグ"},
]
OFFICIAL_TOPIC = {"id": "official", "world": "yakyu", "label": "球団公式"}
RSS_EVERY = dt.timedelta(minutes=19)   # 10分ごとの live で、約20分に1回だけ読む（定期実行の小さなずれで30分にならないよう少し短め）


# ---------------- 通信 ----------------
def get(url, params=None, as_json=True):
    if params is not None:
        params = dict(params, key=KEY)
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "tuber-scout/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        body = r.read()
    return json.loads(body) if as_json else body


def chunks(xs, n=50):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def api_channels(ids):
    """channels.list: 50件で1ユニット"""
    out = {}
    for part in chunks(ids):
        res = get(API + "channels", {"part": "snippet,statistics", "id": ",".join(part), "maxResults": 50})
        for it in res.get("items", []):
            sn, st = it["snippet"], it.get("statistics", {})
            out[it["id"]] = {
                "name": sn.get("title", ""),
                "handle": (sn.get("customUrl") or "").lstrip("@"),
                "thumb": sn.get("thumbnails", {}).get("default", {}).get("url", ""),
                "subs": int(st.get("subscriberCount", 0)),  # 1000人以上は上3桁に丸められて返る点に注意
                "views": int(st.get("viewCount", 0)),
                "videos": int(st.get("videoCount", 0)),
                "desc": (sn.get("description") or "")[:1500],  # 応援球団の推定だけに使う（アプリには出さない）
            }
    return out


def api_handle(handle):
    """@ハンドル → チャンネルID（channels.list forHandle: 1ユニット）"""
    res = get(API + "channels", {"part": "id", "forHandle": handle.lstrip("@")})
    items = res.get("items", [])
    return items[0]["id"] if items else None


def api_search_channels(q, n=3):
    """名前でチャンネルをさがす（search.list: 1回100ユニット。seed のときだけ使う）"""
    res = get(API + "search", {"part": "snippet", "q": q, "type": "channel", "regionCode": "JP", "relevanceLanguage": "ja", "maxResults": n})
    return [it["snippet"]["channelId"] for it in res.get("items", [])]


def resolve_seeds(seeds):
    """handle だけ書かれた行にチャンネルIDを入れる。調べた結果は data/handles.json に保存"""
    path = os.path.join(DATA, "handles.json")
    known = load(path, {})
    out, changed = [], False
    for c in seeds:
        c = dict(c)
        if not c.get("id") and c.get("handle"):
            h = c["handle"].lstrip("@").lower()
            if known.get(h) is None:
                known[h] = api_handle(h)
                changed = True
            c["id"] = known[h]
            if not c["id"]:
                print("見つからないハンドル:", c["handle"], file=sys.stderr)
        if c.get("id"):
            out.append(c)
    if changed:
        dump(path, known)
    seen, uniq = set(), []
    for c in out:
        if c["id"] not in seen:
            seen.add(c["id"]); uniq.append(c)
    return uniq


def api_videos(ids):
    """videos.list: 50件で1ユニット"""
    out = {}
    for part in chunks(ids):
        res = get(API + "videos", {"part": "snippet,statistics,liveStreamingDetails,contentDetails", "id": ",".join(part), "maxResults": 50})
        for it in res.get("items", []):
            sn, st, lv = it["snippet"], it.get("statistics", {}), it.get("liveStreamingDetails", {})
            out[it["id"]] = {
                "id": it["id"], "ch": sn["channelId"], "title": sn["title"], "published": sn["publishedAt"],
                "state": sn.get("liveBroadcastContent", "none"),  # live / upcoming / none
                "views": int(st.get("viewCount", 0)),
                "viewers": int(lv["concurrentViewers"]) if lv.get("concurrentViewers") else None,
                "start": lv.get("actualStartTime") or lv.get("scheduledStartTime"),
                "duration": it.get("contentDetails", {}).get("duration"),
            }
    return out


def rss_recent(channel_id, n=5):
    """RSSはAPIの割り当てを消費しない。最新15本まで取れる"""
    try:
        xml = get(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}", as_json=False)
    except Exception as e:
        print("rss error", channel_id, e, file=sys.stderr)
        return []
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
    root = ET.fromstring(xml)
    return [e.find("yt:videoId", ns).text for e in root.findall("a:entry", ns)[:n]]


# ---------------- ファイル ----------------
def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def dump(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def today_jst(now=None):
    return (now or dt.datetime.now(JST)).astimezone(JST).date()


# ---------------- 計算（selftestでも使う） ----------------
def is_related(title):
    return any(k in title for k in RELATED)


# 応援球団の推定：チャンネル名・説明・最近のプロスピ動画のタイトルに出てくる球団名を数える
# 強い言葉（正式名・愛称）は3点、短い呼び名は1点。はっきり1つに決まらないときは付けない
TEAM_WORDS = {
    "ロッテ": (["ロッテ", "マリーンズ", "千葉ロッテ"], ["鴎"]),
    "ソフトバンク": (["ソフトバンク", "ホークス"], ["鷹"]),
    "日本ハム": (["日本ハム", "日ハム", "ファイターズ"], []),
    "楽天": (["楽天", "イーグルス"], ["鷲"]),
    "西武": (["西武", "ライオンズ"], ["獅子"]),
    "オリックス": (["オリックス", "バファローズ"], []),
    "巨人": (["巨人", "ジャイアンツ", "読売"], []),
    "阪神": (["阪神", "タイガース"], ["虎"]),
    "DeNA": (["DeNA", "ベイスターズ", "ベイス"], []),
    "広島": (["カープ", "広島東洋"], ["鯉"]),
    "ヤクルト": (["ヤクルト", "スワローズ"], ["燕"]),
    "中日": (["中日", "ドラゴンズ"], []),
}


def infer_team(name, desc, titles):
    """① チャンネル名・説明に出てくる球団が1つにしぼれればそれ。② だめなら、最近のタイトルで4本以上・6割以上が同じ球団ならそれ"""
    def hits(text):
        out = {}
        for team, (strong, weak) in TEAM_WORDS.items():
            s = sum(text.count(w) for w in strong) * 3 + sum(text.count(w) for w in weak)
            if s:
                out[team] = s
        return out

    def pick(score, need):
        if not score:
            return ""
        top = sorted(score.items(), key=lambda x: -x[1])
        s2 = top[1][1] if len(top) > 1 else 0
        return top[0][0] if top[0][1] >= need and top[0][1] >= s2 * 2 else ""

    sc = hits(name or "")
    for k, v in hits(desc or "").items():
        sc[k] = sc.get(k, 0) + v
    t = pick(sc, 3)
    if t:
        return t
    per = {}
    for title in titles or []:
        h = hits(title)
        for k in h:
            per[k] = per.get(k, 0) + 1
    if not per:
        return ""
    best, n = max(per.items(), key=lambda x: x[1])
    return best if n >= 4 and n >= 0.6 * sum(per.values()) else ""


def team_of(seed, meta_row):
    """channels.json の team：空＝自動（推定）、"-"＝付けない、球団名＝その球団"""
    t = seed.get("team", "")
    if t == "-":
        return ""
    return t or (meta_row or {}).get("team_auto", "")


# ---------- ⚾ プロ野球の話題か（球団ファンのYouTuber用） ----------
# プロ野球ならではの言葉。「野球」「現地」「観戦」だけのような、ほかの話題でも使う言葉は入れない
FAN_WORDS = ["プロ野球", "NPB", "セ・リーグ", "パ・リーグ", "日本シリーズ", "クライマックスシリーズ", "交流戦", "オープン戦", "開幕戦", "ペナント",
             "野球観戦", "現地観戦", "現地参戦", "現地応援", "応援歌", "応援団", "球場", "スタグル", "ドラフト", "二軍", "ファーム", "反省会",
             "スタメン", "ヒーローインタビュー", "戦力外", "自主トレ", "春季キャンプ", "助っ人",
             "ZOZOマリン", "甲子園", "ハマスタ", "横浜スタジアム", "マツダスタジアム", "エスコン", "ベルーナドーム", "バンテリンドーム", "楽天モバイルパーク"]
# 球団名のうち、野球以外でもよく出る言葉（進撃の巨人・楽天市場・西武線など）は「巨人戦」「楽天ファン」のような形のときだけ数える
FAN_AMBIGUOUS = ["巨人", "楽天", "西武", "読売", "中日", "ソフトバンク"]
FAN_TEAM_CTX = ["戦", "ファン", "勝利", "サヨナラ", "打線", "投手", "選手", "監督"]
# ゲームの動画は ⚾ プロ野球 に入れない（🎮 ゲーム実況 のほう）
FAN_GAME_WORDS = ["パワプロ", "eBASEBALL", "プロスピ", "ゲーム", "実況パワフル"]


def is_fan_related(title):
    """プロ野球（本物の試合・球団）の話題で、ゲームの動画ではないタイトルか"""
    t = title or ""
    if is_related(t) or any(w in t.replace("ゲーム差", "") for w in FAN_GAME_WORDS):  # 「3ゲーム差」は野球の言葉なのでゲーム扱いしない
        return False
    if any(w in t for w in FAN_WORDS):
        return True
    for strong, _weak in TEAM_WORDS.values():
        for w in strong:
            if w in FAN_AMBIGUOUS:
                if any(w + c in t for c in FAN_TEAM_CTX):
                    return True
            elif w in t:
                return True
    return False


def genre_of(c):
    """channels.json の genre："fan"＝⚾ プロ野球、それ以外（書いていないときも）＝🎮 ゲーム実況"""
    return "fan" if (c or {}).get("genre") == "fan" else "game"


def rel_fn(genre):
    """ジャンルごとの「関係ある動画か」の判定"""
    return is_fan_related if genre == "fan" else is_related


def fan_ids_of(seeds):
    return {c["id"] for c in seeds if c.get("id") and genre_of(c) == "fan"}


# 試合日程（games.csv）：日付,開始,ホーム,ビジター,球場。# で始まる行は説明。アプリの「球団」で対戦カードを出すのに使う
TEAM_ALIAS = {"千葉ロッテ": "ロッテ", "日ハム": "日本ハム", "SB": "ソフトバンク", "横浜": "DeNA", "横浜DeNA": "DeNA", "東北楽天": "楽天", "埼玉西武": "西武",
              "読売": "巨人", "広島東洋": "広島", "東京ヤクルト": "ヤクルト", "中日ドラゴンズ": "中日", "オリックス・バファローズ": "オリックス"}


# ---------- 試合日程を NPB の公開日程ページから自動で作る（日時・対戦・球場という事実だけを使う） ----------
NPB_URL = "https://npb.jp/games/{y}/schedule_{m:02d}_detail.html"
_TEAMS_RE = "|".join(sorted(TEAM_WORDS, key=len, reverse=True))


def table_rows(html):
    """HTML の表を、行ごとの「セルの文字のリスト」にする（td・th どちらも1セル）"""
    from html.parser import HTMLParser

    class T(HTMLParser):
        def __init__(self):
            super().__init__(); self.rows = []; self.row = None; self.cell = None
        def handle_starttag(self, tag, a):
            if tag == "tr": self.row = []
            elif tag in ("td", "th") and self.row is not None: self.cell = []
            elif tag == "br" and self.cell is not None: self.cell.append(" ")
        def handle_endtag(self, tag):
            if tag in ("td", "th") and self.row is not None and self.cell is not None:
                self.row.append(re.sub(r"\s+", " ", "".join(self.cell)).strip()); self.cell = None
            elif tag == "tr" and self.row is not None:
                self.rows.append(self.row); self.row = None
        def handle_data(self, d):
            if self.cell is not None: self.cell.append(d)

    p = T(); p.feed(html or "")
    return p.rows


def parse_npb_schedule(html, year):
    """NPB の月別日程ページ（表）から [{day,time,home,away,place}] を取り出す。表の書き方が多少変わっても読めるよう、行ごとの文字で判断する"""
    out, cur = [], None
    for cells in table_rows(html):
        text = " ".join(cells)
        dm = re.search(r"(\d{1,2})/(\d{1,2})", cells[0] if cells else "")
        if dm:
            try: cur = dt.date(year, int(dm.group(1)), int(dm.group(2)))
            except ValueError: cur = None
        m = re.search(rf"({_TEAMS_RE})\s*(?:(\d+)\s*)?[-－―ー–]\s*(?:(\d+)\s*)?({_TEAMS_RE})", text)
        if not (cur and m) or m.group(1) == m.group(4):
            continue
        tm = re.search(r"([^\s|｜\d][^|｜]*?)\s*(\d{1,2}:\d{2})", text[m.end():])
        place, time = (tm.group(1).strip()[-12:], tm.group(2).zfill(5)) if tm else ("", "")
        off = "中止" in text or "延期" in text
        g = {"day": cur.isoformat(), "time": time, "home": m.group(1), "away": m.group(4), "place": place, "off": off}
        # 状態：off＝中止・延期、end＝終わった（点数あり）、""＝まだ・わからない
        if off:
            g["status"] = "off"
        elif m.group(2) and m.group(3):
            g["status"], g["score"] = "end", [int(m.group(2)), int(m.group(3))]  # [ホームの点, ビジターの点]
        else:
            g["status"] = ""
        out.append(g)
    return out


def merge_today_games(old, fresh, days):
    """old の試合のうち、fresh と同じ日・ホーム・ビジターのものを fresh で置きかえる（days の日の fresh だけ使う）"""
    fresh = [g for g in fresh if g["day"] in days]
    key = lambda g: (g["day"], g["home"], g["away"])
    new = {key(g) for g in fresh}
    out = [g for g in old if key(g) not in new] + fresh
    return sorted(out, key=lambda g: (g["day"], g.get("time", ""), g["home"]))


def need_today_games(now_jst, today_at):
    """13時〜23時台で、前に読んでから20分以上たっていれば True"""
    if not (13 <= now_jst.hour <= 23):
        return False
    if not today_at:
        return True
    try:
        last = dt.datetime.fromisoformat(today_at)
    except ValueError:
        return True
    return now_jst - last >= dt.timedelta(minutes=20)


def mode_today_games(now_jst=None):
    """今月の日程ページだけ読んで、昨日・今日の試合（結果・中止）を data/games_npb.json に入れる"""
    now_jst = now_jst or dt.datetime.now(JST)
    path = os.path.join(DATA, "games_npb.json")
    cur = load(path, {})
    if not need_today_games(now_jst, cur.get("todayAt")):
        return False
    cur["todayAt"] = now_jst.isoformat(timespec="minutes")  # 失敗しても20分は読み直さない
    today = now_jst.date()
    try:
        html = get(NPB_URL.format(y=today.year, m=today.month), as_json=False).decode("utf-8", "replace")
        fresh = parse_npb_schedule(html, today.year)
        days = {today.isoformat(), (today - dt.timedelta(days=1)).isoformat()}
        cur["games"] = merge_today_games(cur.get("games", []), fresh, days)
    except Exception as e:
        print("今日の試合を読めませんでした", e, file=sys.stderr)
    dump(path, cur)
    return True


def mode_schedule(_seeds=None):
    """今月と来月の日程を読んで data/games_npb.json に保存（失敗したら前のまま）"""
    today = today_jst()
    y, m = today.year, today.month
    nxt = (y + (m == 12), 1 if m == 12 else m + 1)
    games = []
    for yy, mm in ((y, m), nxt):
        try:
            html = get(NPB_URL.format(y=yy, m=mm), as_json=False).decode("utf-8", "replace")
            games += parse_npb_schedule(html, yy)
        except Exception as e:
            print("日程を読めませんでした", yy, mm, e, file=sys.stderr)
    if games:
        keep = [g for g in games if g["day"] >= (today - dt.timedelta(days=1)).isoformat()]
        dump(os.path.join(DATA, "games_npb.json"), {"updated": dt.datetime.now(JST).isoformat(timespec="minutes"), "games": keep})
        print(f"試合日程：{len(keep)}試合")


def game_row(g):
    """アプリに出す形：day,time,home,away,place,status と、終わった試合・試合中だけ score、試合中だけ inning"""
    st = g.get("status") or ("off" if g.get("off") else "")
    row = {k: g.get(k, "") for k in ("day", "time", "home", "away", "place")}
    row["status"] = st
    if st in ("end", "live") and g.get("score"):
        row["score"] = list(g["score"])
    if st == "live" and g.get("inning"):
        row["inning"] = g["inning"]
    return row


# ---------- 試合中の点数・試合終了（NPB 公式の試合速報ページから。点数・回・状態という事実だけを使い、文章・画像は使わない） ----------
# NPB のトップページに今日の試合へのリンクがある：/scores/2026/0927/m-f-23/（1つ目の記号がホーム）
NPB_CODES = {"g": "巨人", "s": "ヤクルト", "db": "DeNA", "c": "広島", "t": "阪神", "d": "中日",
             "m": "ロッテ", "f": "日本ハム", "h": "ソフトバンク", "b": "オリックス", "l": "西武", "e": "楽天"}
NPB_TOP = "https://npb.jp/"
LIVE_EVERY = dt.timedelta(minutes=9)   # 9分に1回まで（10分ごとの live で毎回読める）
LIVE_MAX_REQ = 7                       # 1回の実行で NPB に送るのは7回まで（トップページも数える）


def today_game_links(html, day):
    """NPB のトップページから、その日の試合ページへのリンクを取り出す → [{home, away, url}]"""
    md = f"{day.month:02d}{day.day:02d}"
    out, seen = [], set()
    for m in re.finditer(r"/scores/(\d{4})/(\d{4})/([a-z]+)-([a-z]+)-(\d+)/", html or ""):
        y, d, h, a, n = m.groups()
        if int(y) != day.year or d != md or h not in NPB_CODES or a not in NPB_CODES or h == a:
            continue
        url = f"https://npb.jp/scores/{y}/{d}/{h}-{a}-{n}/index.html"  # index.html を付けないと転送がくり返されて読めない
        if url not in seen:
            seen.add(url)
            out.append({"home": NPB_CODES[h], "away": NPB_CODES[a], "url": url})
    return out


def team_in(text):
    """文字の中にある球団（正式名・愛称・短い名前）。なければ空"""
    for team, (strong, _weak) in TEAM_WORDS.items():
        if team in text or any(w in text for w in strong):
            return team
    return ""


def _status_words(t, off_words):
    """状態を表す言葉から (status, inning) を決める。決まらなければ ("", "")"""
    m = re.search(r"(\d{1,2})回(表|裏)", t)
    if "試合終了" in t:
        return "end", ""
    if any(w in t for w in off_words):
        return "off", ""
    if m:
        return "live", m.group(0)
    if "試合中" in t:
        return "live", ""
    return "", ""


def parse_game_page(html, home, away):
    """NPB の試合速報ページ → {"status": "end"|"live"|"off"|"", "inning": "7回裏" など, "score": [ホームの点, ビジターの点] か None}
    スコア表は「球団名, 1回…9回, 計, H, E」の行。最後の3つが数字の行の、後ろから3つ目が点"""
    html = html or ""
    runs, played = {}, False
    for cells in table_rows(html):
        if len(cells) < 4:
            continue
        i = 0 if cells[0] else 1
        team = team_in(cells[i]) if i < len(cells) - 3 else ""
        if not team or not all(re.fullmatch(r"\d+", c) for c in cells[-3:]):
            continue
        if team not in runs:
            runs[team] = int(cells[-3])
            played = played or any(c not in ("", "-") for c in cells[i + 1:-3])
    text = re.sub(r"<(script|style)\b.*?</\1\s*>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text).translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    marks = " ".join(re.findall(r"【([^】]{1,15})】", text))
    # まず【】の中（【試合終了】【7回裏】など）で決める。なければページ全体（「中止」はメニューなどにも出るので「試合中止」だけ）
    st, inning = _status_words(marks, ("中止", "ノーゲーム"))
    if not st:
        st, inning = _status_words(text, ("試合中止", "ノーゲーム"))
    score = [runs[home], runs[away]] if home in runs and away in runs else None
    if not st and score is not None and played:
        st = "live"
    return {"status": st, "inning": inning, "score": score}


def need_live_scores(now_jst, at):
    """13時〜23時台で、前に読んでから9分以上たっていれば True"""
    if not (13 <= now_jst.hour <= 23):
        return False
    last = parse_time(at) if at else None
    return last is None or last.tzinfo is None or now_jst - last >= LIVE_EVERY


def mode_live_scores(now=None, sleep=time.sleep):
    """試合の時間に、今日の試合の点数・回・試合終了を data/games_live.json に入れる。
    NPB のトップページを1回読み、まだ終わっていない試合のページだけ読む（1回の実行で7回まで・1秒ずつあける）。失敗しても止まらない"""
    try:
        now = (now or dt.datetime.now(JST)).astimezone(JST)
        path = os.path.join(DATA, "games_live.json")
        cur = load(path, {})
        day = now.date().isoformat()
        if not isinstance(cur, dict) or cur.get("day") != day:  # 日が変わったら作り直す
            cur = {"day": day, "games": {}}
        if not need_live_scores(now, cur.get("at")):
            return False
        cur["at"] = now.isoformat(timespec="seconds")  # 失敗しても9分は読み直さない
        games = cur.setdefault("games", {})
        try:
            links = today_game_links(get(NPB_TOP, as_json=False).decode("utf-8", "replace"), now.date())
            sent = 1
            for g in links:
                key = g["home"] + "-" + g["away"]
                old = games.get(key) or {}
                if old.get("status") in ("end", "off"):  # 終わった・中止の試合はもう読まない
                    continue
                if sent >= LIVE_MAX_REQ:
                    break
                sleep(1)
                sent += 1
                try:
                    r = parse_game_page(get(g["url"], as_json=False).decode("utf-8", "replace"), g["home"], g["away"])
                except Exception as e:
                    print("試合速報を読めませんでした", key, e, file=sys.stderr)
                    continue
                if old and not r["status"] and r["score"] is None:  # 何も読めなかったときは前の結果を残す
                    continue
                games[key] = dict(r, url=g["url"], at=cur["at"])
        except Exception as e:
            print("NPB のトップページを読めませんでした", e, file=sys.stderr)
        dump(path, cur)
        return True
    except Exception as e:
        print("試合速報の更新に失敗", e, file=sys.stderr)
        return False


def live_overlay(rows, today):
    """今日の試合に、試合速報で分かった状態・点数・回を重ねる（キーは「ホーム-ビジター」）"""
    lv = load(os.path.join(DATA, "games_live.json"), {})
    if not isinstance(lv, dict) or lv.get("day") != today.isoformat():
        return rows
    games = lv.get("games") or {}
    for row in rows:
        g = games.get(f"{row['home']}-{row['away']}") if row.get("day") == today.isoformat() else None
        if not g or g.get("status") not in ("end", "live", "off"):
            continue
        row["status"] = g["status"]
        row.pop("inning", None)
        if g["status"] == "off":
            row.pop("score", None)
        elif g.get("score"):
            row["score"] = list(g["score"])
        if g["status"] == "live" and g.get("inning"):
            row["inning"] = g["inning"]
    return rows


def upcoming_games(today, days=7):
    """自動の日程（NPB）に、games.csv の手入力を上書きで足す（同じ日・同じ球団は手入力を優先）
    昨日から7日先まで。中止の試合・終わった試合の点数も出す。今日の試合には試合速報（games_live.json）を重ねる"""
    auto = load(os.path.join(DATA, "games_npb.json"), {}).get("games", [])
    auto = [g for g in auto if -1 <= (dt.date.fromisoformat(g["day"]) - today).days < days]
    manual, _ = read_games(os.path.join(HERE, "games.csv"), today, days)
    key = lambda g: (g["day"], g["home"])
    merged = {key(g): game_row(g) for g in auto}
    for g in manual:
        a = merged.get(key(g))
        row = dict(g, status="")
        if a and a["away"] == g["away"]:  # 同じ試合なら、自動で分かった結果・中止はそのまま使う
            row["status"] = a["status"]
            if "score" in a:
                row["score"] = a["score"]
        merged[key(g)] = row
    return live_overlay(sorted(merged.values(), key=lambda g: (g["day"], g["time"])), today)


# ---------- ジャンル・配信する人の自動判定（最近のプロスピ動画のタイトルから） ----------
GENRE_WORDS = {"リアタイ": ["リアタイ"], "ガチャ": ["ガチャ", "連"], "純正": ["純正"], "育成": ["育成", "特訓", "限界突破"], "無課金": ["無課金"],
               "解説": ["解説", "講座", "攻略", "考察"], "初心者向け": ["初心者", "始め方", "序盤"], "イベント周回": ["イベント", "周回"],
               "エンジョイ": ["雑談", "エンジョイ", "まったり"], "査定": ["査定", "評価"]}


# ⚾ プロ野球（球団ファン）のジャンル
FAN_GENRE_WORDS = {"現地観戦": ["現地", "観戦", "参戦", "球場"], "応援": ["応援", "応援歌", "ファン"],
                   "試合振り返り": ["振り返り", "反省会", "試合結果", "ハイライト", "勝利", "敗戦", "サヨナラ"],
                   "ニュース・考察": ["ニュース", "考察", "解説", "トレード", "FA", "戦力外", "補強", "契約"],
                   "ドラフト・二軍": ["ドラフト", "二軍", "ファーム"], "球場グルメ": ["グルメ", "スタグル", "球場飯", "食べ"]}


def auto_tags(titles, words=None):
    words = words or GENRE_WORDS
    cnt = {g: sum(1 for t in titles if any(w in t for w in ws)) for g, ws in words.items()}
    return [g for g, n in sorted(cnt.items(), key=lambda x: -x[1]) if n >= 2][:3]


def auto_tags_for(genre, titles):
    return auto_tags(titles, FAN_GENRE_WORDS if genre == "fan" else GENRE_WORDS)


def auto_streamer(titles):
    return sum(1 for t in titles if any(w in t for w in ("生配信", "ライブ", "LIVE", "Live", "参加型", "配信"))) >= 2


# ---------- 配信者の自動掲載（毎日の discover で） ----------
AUTO_MIN_SUBS = 300
AUTO_MIN_RELATED = 3
OFFICIAL_WORDS = ["公式", "official", "Official", "OFFICIAL", "KONAMI", "コナミ", "NPB", "日本野球機構"]
# ⚾ プロ野球：球団・リーグ・テレビ局・スポーツ新聞などのチャンネルは載せない（ファンのYouTuberだけ）
FAN_OFFICIAL_WORDS = OFFICIAL_WORDS + [
    "パ・リーグTV", "パーソル パ・リーグ", "DAZN", "スポーツナビ", "スポナビ", "ベースボールキング", "BASEBALL KING", "Full-Count", "週刊ベースボール",
    "日テレ", "日本テレビ", "TBS", "フジテレビ", "テレビ朝日", "テレ朝", "NHK", "テレ東", "テレビ東京", "ABEMA", "スカパー",
    "スポニチ", "日刊スポーツ", "報知", "サンスポ", "サンケイスポーツ", "デイリースポーツ", "中日スポーツ", "東スポ", "東京スポーツ"]
# 球団の公式チャンネル（名前がこれと同じ）
TEAM_OFFICIAL_NAMES = ["千葉ロッテマリーンズ", "福岡ソフトバンクホークス", "北海道日本ハムファイターズ", "東北楽天ゴールデンイーグルス", "埼玉西武ライオンズ",
                       "オリックス・バファローズ", "読売ジャイアンツ", "阪神タイガース", "横浜DeNAベイスターズ", "広島東洋カープ", "東京ヤクルトスワローズ", "中日ドラゴンズ"]


def is_official(name, genre="game"):
    n = name or ""
    if genre == "fan":
        return any(w.lower() in n.lower() for w in FAN_OFFICIAL_WORDS) or n.strip() in TEAM_OFFICIAL_NAMES
    return any(w in n for w in OFFICIAL_WORDS)


def auto_pick(meta, titles_by, have, blocked, genre="game"):
    """条件を満たす候補だけを選ぶ：ジャンルの動画が「専門」か「多め」・最近15本中3本以上・登録者300人以上・公式でない・外した人でない
    🎮 ゲーム実況（game）はプロスピの動画、⚾ プロ野球（fan）はゲームでないプロ野球の動画で数える"""
    fn = rel_fn(genre)
    out = []
    for cid, m in meta.items():
        if cid in have or cid in blocked or is_official(m.get("name"), genre):
            continue
        titles = titles_by.get(cid, [])
        rel = [t for t in titles if fn(t)]
        lv = level_of(len(rel), len(titles))
        if m.get("subs", 0) < AUTO_MIN_SUBS or len(rel) < AUTO_MIN_RELATED or lv == "ときどき":
            continue
        if genre == "fan":  # ファンの人は、関係ない動画（日常vlogなど）は出さない。球団・ジャンル・配信する人は自動で付ける
            out.append({"id": cid, "name": m.get("name", ""), "genre": "fan", "tags": auto_tags_for("fan", rel),
                        "team": infer_team(m.get("name"), m.get("desc"), rel), "streamer": auto_streamer(rel), "all": False,
                        "auto": True, "added": today_jst().isoformat()})
        else:
            out.append({"id": cid, "name": m.get("name", ""), "genre": "game", "tags": [], "team": "", "streamer": False, "all": lv == "専門",
                        "auto": True, "added": today_jst().isoformat()})
    return out


def read_games(path, today, days=7):
    import csv
    teams = set(TEAM_WORDS)
    out, bad = [], []
    try:
        with open(path, encoding="utf-8-sig") as f:
            rows = [r for r in csv.reader(f) if r and not r[0].strip().startswith("#")]
    except FileNotFoundError:
        return [], []
    for i, r in enumerate(rows):
        r = [x.strip() for x in r] + [""] * 5
        if r[0] in ("日付", "date"):
            continue
        day, time, home, away, place = r[:5]
        home, away = TEAM_ALIAS.get(home, home), TEAM_ALIAS.get(away, away)
        try:
            y, mo, da = [int(x) for x in day.replace("/", "-").split("-")]
            d = dt.date(y, mo, da)
        except (ValueError, TypeError):
            bad.append(i + 1); continue
        if home not in teams or away not in teams or home == away or not (time == "" or len(time) in (4, 5) and ":" in time):
            bad.append(i + 1); continue
        if 0 <= (d - today).days < days:
            out.append({"day": d.isoformat(), "time": time.zfill(5) if time else "", "home": home, "away": away, "place": place[:20]})
    out.sort(key=lambda g: (g["day"], g["time"]))
    return out, bad


def shown(v, all_ids, fan_ids=()):
    """アプリに出す動画か：ジャンルに合うタイトル（ゲーム実況＝プロスピ、プロ野球＝ゲームでない野球の話題）、または「全部の動画を出す」にした人の動画"""
    return v["ch"] in all_ids or rel_fn("fan" if v["ch"] in fan_ids else "game")(v["title"])


def all_ids_of(seeds):
    return {c["id"] for c in seeds if c.get("all")}


def senmon_ratio(titles, fn=None):
    """タイトルのうちジャンルに合うもの（ふつうはプロスピ関連）の割合"""
    fn = fn or is_related
    return sum(1 for t in titles if fn(t)) / len(titles) if titles else 0.0


def review_list(all_vids, meta, fan_ids=()):
    """掲載中なのに、最近ジャンルの動画（ゲーム実況＝プロスピ、プロ野球＝野球の話題）を出していないチャンネル"""
    by = {}
    for v in all_vids:
        by.setdefault(v["ch"], []).append(v["title"])
    out = []
    for cid, titles in by.items():
        g = "fan" if cid in fan_ids else "game"
        r = senmon_ratio(titles, rel_fn(g))
        if len(titles) >= 3 and r == 0:  # 最近ジャンルの動画を出していない
            out.append({"id": cid, "name": meta.get(cid, {}).get("name", ""), "genre": g, "ratio": round(r, 2), "sample": titles[:3]})
    return out


def prune_old(today):
    """30日より古い予想用の記録を消す。以前の版が作った登録者数の履歴フォルダも消す"""
    import shutil
    for old in ("history", "peaks"):
        shutil.rmtree(os.path.join(DATA, old), ignore_errors=True)
    if os.path.isdir(SNAP):
        for fn in os.listdir(SNAP):
            try:
                day = dt.date.fromisoformat(fn[:10])
            except ValueError:
                continue
            if (today - day).days >= KEEP_DAYS:
                os.remove(os.path.join(SNAP, fn))


# ---------------- 速報 ----------------
def load_topics():
    """topics.json があればそれを使う（形がおかしい行は使わない）。なければ NEWS_TOPICS"""
    rows = load(os.path.join(HERE, "topics.json"), None)
    if isinstance(rows, list):
        ok = []
        for t in rows:
            if not isinstance(t, dict) or t.get("world") not in ("game", "yakyu"):
                continue
            if all(isinstance(t.get(k), str) and t.get(k) for k in ("id", "q", "label")):
                ok.append({k: t[k] for k in ("id", "world", "q", "label")})
        if ok:
            return ok
        print("topics.json が読めないので、ふつうの話題を使います", file=sys.stderr)
    return [dict(t) for t in NEWS_TOPICS]


def news_topics():
    """速報の話題の全部（さがす話題＋球団公式）。球団公式はさがさず RSS で集める"""
    tps = load_topics()
    if not any(t["id"] == OFFICIAL_TOPIC["id"] for t in tps):
        tps.append(dict(OFFICIAL_TOPIC))
    return tps


def load_news_channels():
    """球団・リーグの公式チャンネル。news_channels.json があれば、同じ id は置きかえ・新しい id は足す（"off": true は使わない）"""
    by = {c["id"]: dict(c) for c in NEWS_CHANNELS}
    rows = load(os.path.join(HERE, "news_channels.json"), None)
    for c in rows if isinstance(rows, list) else []:
        if not isinstance(c, dict) or not str(c.get("id", "")).startswith("UC"):
            continue
        if c.get("off"):
            by.pop(c["id"], None)
            continue
        team = c.get("team") if c.get("team") in TEAM_WORDS else ""
        name = str(c.get("name") or c["id"])
        by[c["id"]] = {"id": c["id"], "name": name, "team": team, "label": str(c.get("label") or name)}
    return list(by.values())


def parse_feed(xml, ch, now, hours=NEWS_HOURS):
    """YouTube の RSS（Atom）→ 速報の形。hours 時間より古い動画は入れない"""
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015", "m": "http://search.yahoo.com/mrss/"}
    since = now - dt.timedelta(hours=hours)
    out = []
    for e in ET.fromstring(xml).findall("a:entry", ns):
        vid, pub = e.findtext("yt:videoId", "", ns), parse_time(e.findtext("a:published", "", ns))
        if not vid or pub is None or pub.tzinfo is None or pub < since:
            continue
        th = e.find("m:group/m:thumbnail", ns)
        out.append({"id": vid, "title": e.findtext("a:title", "", ns), "ch": ch["id"],
                    "chName": e.findtext("a:author/a:name", "", ns) or ch.get("name", ""),
                    "published": pub.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "topic": OFFICIAL_TOPIC["id"], "world": "yakyu", "thumb": th.get("url", "") if th is not None else "",
                    "team": ch.get("team", "")})
    return out


def mode_rss_news(now=None):
    """球団・リーグの公式チャンネルの新着（48時間以内）を RSS で読んで data/news.json に足す（約20分に1回。APIの割り当ては使わない）"""
    now = now or dt.datetime.now(dt.timezone.utc)
    path = os.path.join(DATA, "news.json")
    cur = load(path, {})
    cur = cur if isinstance(cur, dict) else {}
    last = parse_time(cur.get("rssAt")) if cur.get("rssAt") else None
    if last is not None and last.tzinfo is not None and now - last < RSS_EVERY:
        return False
    fresh = []
    for ch in load_news_channels():
        try:
            fresh += parse_feed(get(f"https://www.youtube.com/feeds/videos.xml?channel_id={ch['id']}", as_json=False), ch, now)
        except Exception as e:  # 404 などはそのチャンネルだけ飛ばす
            print("公式チャンネルの RSS を読めませんでした（飛ばします）", ch.get("label", ""), ch["id"], e, file=sys.stderr)
    blocked = set(load(os.path.join(HERE, "blocked.json"), []))
    items = merge_news(cur.get("items", []), fresh, news_topics(), blocked, now)
    dump(path, dict(cur, updated=dt.datetime.now(JST).isoformat(timespec="minutes"), rssAt=now.isoformat(timespec="seconds"), items=items))
    print(f"速報（公式チャンネル）：{len(items)}件（今回 {len(fresh)}件）")
    return True


def parse_time(s):
    """YouTube の時刻（…Z）を datetime にする。読めなければ None"""
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def api_news_videos(ids):
    """videos.list（50件で1ユニット）：速報に出すもの（タイトル・チャンネル名・サムネイル・公開時刻）だけ取る"""
    out = {}
    for part in chunks(ids):
        res = get(API + "videos", {"part": "snippet,contentDetails", "id": ",".join(part), "maxResults": 50})
        for it in res.get("items", []):
            sn = it["snippet"]
            th = sn.get("thumbnails", {})
            thumb = (th.get("medium") or th.get("high") or th.get("default") or {}).get("url", "")
            out[it["id"]] = {"id": it["id"], "title": sn.get("title", ""), "ch": sn.get("channelId", ""),
                             "chName": sn.get("channelTitle", ""), "published": sn.get("publishedAt", ""), "thumb": thumb}
    return out


def merge_news(prev, fresh, topics, blocked, now, keep_days=NEWS_KEEP_DAYS, limit=NEWS_MAX):
    """新しく見つかった動画（fresh）と前回の速報（prev）を合わせる。
    同じ動画は1つにまとめ、最初の話題を topic、全部の話題を topics に入れる。
    外した人（blocked）・7日より古いもの・今の話題にないものは消す。新しい順に最大 limit 件"""
    world = {t["id"]: t["world"] for t in topics}
    by = {}
    for v in list(fresh) + list(prev):
        tps = v.get("topics") or [v.get("topic")]
        cur = by.get(v["id"])
        if cur is None:
            by[v["id"]] = cur = dict(v, topics=[])
        if not cur.get("team") and v.get("team"):  # 公式チャンネルの動画なら球団を残す
            cur["team"] = v["team"]
        for t in tps:
            if t and t not in cur["topics"]:
                cur["topics"].append(t)
    since = now - dt.timedelta(days=keep_days)
    out = []
    for v in by.values():
        tps = [t for t in v["topics"] if t in world]
        pub = parse_time(v.get("published"))
        if not tps or v.get("ch") in blocked or pub is None or pub < since:
            continue
        top = v.get("topic") if v.get("topic") in tps else tps[0]
        out.append(dict(v, topic=top, topics=tps, world=world[top]))
    out.sort(key=lambda v: parse_time(v["published"]), reverse=True)
    return out[:limit]


def mode_news(now=None):
    """話題ごとに新しい動画をさがして data/news.json に入れる（前回の分と合わせるので、見つからない回があっても空にならない）"""
    now = now or dt.datetime.now(dt.timezone.utc)
    topics = load_topics()
    since = (now - dt.timedelta(hours=NEWS_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    found = []
    for t in topics:  # search.list は1回100ユニット
        p = {"part": "snippet", "q": t["q"], "type": "video", "order": "date", "regionCode": "JP",
             "relevanceLanguage": "ja", "publishedAfter": since, "maxResults": 15}
        try:
            res = get(API + "search", p)
        except Exception as e:
            print("速報をさがせませんでした", t["id"], e, file=sys.stderr)
            continue
        ids = [it["id"]["videoId"] for it in res.get("items", []) if it.get("id", {}).get("videoId")]
        found.append((t, ids))
    all_ids = list(dict.fromkeys(i for _, ids in found for i in ids))
    vids = api_news_videos(all_ids) if all_ids else {}
    fresh = [dict(vids[i], topic=t["id"]) for t, ids in found for i in ids if i in vids]
    path = os.path.join(DATA, "news.json")
    cur = load(path, {})
    cur = cur if isinstance(cur, dict) else {}
    blocked = set(load(os.path.join(HERE, "blocked.json"), []))
    items = merge_news(cur.get("items", []), fresh, news_topics(), blocked, now)  # 球団公式（RSS）の分も消さない
    dump(path, dict(cur, updated=dt.datetime.now(JST).isoformat(timespec="minutes"), items=items))  # rssAt はそのまま残す
    print(f"速報：{len(items)}件（今回見つかった {len(fresh)}件）")


def news_for_app(now=None):
    """latest.json に入れる速報（7日以内・アプリに出す項目だけ）"""
    now = now or dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(days=NEWS_KEEP_DAYS)
    out = []
    for v in load(os.path.join(DATA, "news.json"), {}).get("items", []):
        pub = parse_time(v.get("published"))
        if pub is not None and pub >= since:
            out.append({k: v.get(k, "") for k in ("id", "title", "ch", "chName", "published", "topic", "world", "thumb", "team")})
    return out


# ---------------- トレンド（速報タブ）：話題が急に増えたときだけタブを作る ----------------
# 速報のタイトル（公式チャンネル＋話題の動画）から「人の名前＋出来事」（例：〇〇 トレード）をさがす。
# 36時間で3つ以上のチャンネルが同じ話題を出し、それまでの5日間より急に増えたらトレンド。
# 見出しの文章は使わない（YouTube の動画タイトルから言葉を数えるだけ）。
TREND_EVENTS = {
    "yakyu": ["トレード", "移籍", "FA", "戦力外", "引退", "退団", "入団", "契約更改", "監督就任", "監督", "解任",
              "休養", "離脱", "抹消", "復帰", "手術", "ノーヒットノーラン", "完全試合", "サヨナラ", "満塁",
              "優勝", "胴上げ", "マジック", "日本シリーズ", "ドラフト", "指名", "新人王", "MVP", "沢村賞",
              "首位打者", "本塁打王", "達成", "初勝利", "初本塁打", "ポスティング", "メジャー挑戦", "侍ジャパン",
              "WBC", "退場", "乱闘", "号", "勝目"],
    "game": ["ガチャ", "新イベント", "イベント", "新シリーズ", "登場", "能力", "最強", "限定", "無料", "神引き", "爆死",
             "アップデート", "不具合", "補償", "コラボ", "リアタイ", "ランキング", "ガチャ更新"],
}
TREND_STOP = set("""ハイライト 試合 速報 公式 今日 昨日 本日 動画 解説 切り抜き 配信 生配信 ライブ プロ野球 野球 選手 結果 最新 情報
全打席 打席 投球 本塁打 安打 勝利 敗戦 今季 今年 来季 日本 球団 ファン 応援 反応 振り返り 特集 密着 舞台裏 まとめ 注目 話題
ダイジェスト インタビュー ヒーロー ヒーローインタビュー 試合後 試合前 練習 キャンプ 球場 観戦 現地 実況 延長 逆転 先発 登板 好投
完封 無失点 奪三振 連勝 連敗 首位 順位 公式戦 セリーグ パリーグ リーグ チャンネル ニュース 独占 映像 必見 衝撃 驚愕 神回
プロスピ プロスピＡ プロスピA スピリッツ プロ野球スピリッツ コナミ KONAMI 攻略 解説動画 無課金 初心者 育成 オーダー
スタメン 打順 投手 捕手 内野手 外野手 野手 助っ人 外国人 新外国人 若手 ベテラン 二軍 一軍 ファーム 月間 週間 年間 シーズン
電撃 真相 決定 発表 正式 衝撃 緊急 本人 理由 今後 予想 考察 感想 結論 裏側 本音 激白 告白 号泣 異例 前代未聞 独自
阪神タイガース 読売ジャイアンツ 中日ドラゴンズ 広島東洋カープ 東京ヤクルトスワローズ 横浜 横浜DeNAベイスターズ 福岡 千葉 北海道 東北 埼玉
""".split())
TREND_NOT_LABEL = ("プロスピ", "スピリッツ", "コナミ", "KONAMI")   # アプリの文字に出さない言葉
TREND_WINDOW_H = 36
TREND_BASE_DAYS = 5
TREND_MIN_CH = 3        # 何チャンネル以上で話題になったらトレンドか
TREND_KEEP_H = 12       # 一度出たら、少なくともこの時間は出しておく
TREND_MAX_H = 72        # どんなに続いても3日で消す（新しい話題に場所をゆずる）
TREND_MAX = 5           # 1つの世界に出すタブの数

# トレンドから自動で作る「今日のお題」の型（{n} は人の名前。名前がない話題は {n} のない型だけ）
_TP_TRADE = ("{n}の{e}、どう思う？", ["いいと思う", "さみしい", "まだわからない", "新天地で応援"])
_TP_MLB = ("{n}のメジャー挑戦、応援する？", ["応援する", "残ってほしい", "複雑", "わからない"])
TREND_POLLS = {
    "トレード": _TP_TRADE, "移籍": _TP_TRADE,
    "FA": ("{n}のFA、どうなると思う？", ["残留", "移籍", "わからない", "どちらでも応援"]),
    "戦力外": ("{n}、ほかの球団で見たい？", ["見たい", "残ってほしかった", "わからない", "おつかれさま"]),
    "引退": ("{n}の引退、ひとことで言うと？", ["ありがとう", "まだ見たかった", "おつかれさま", "泣いた"]),
    "監督就任": ("{n}監督、期待してる？", ["すごく期待", "少し期待", "不安", "様子見"]),
    "監督": ("{n}監督、期待してる？", ["すごく期待", "少し期待", "不安", "様子見"]),
    "解任": ("{n}監督の解任、どう思う？", ["しかたない", "早すぎる", "わからない", "次に期待"]),
    "ポスティング": _TP_MLB, "メジャー挑戦": _TP_MLB,
    "ドラフト": ("今年のドラフト、応援球団は何点？", ["100点", "80点", "60点", "それ以下"]),
    "新シリーズ": ("新シリーズ、引く？", ["引く", "様子見", "引かない", "もう引いた"]),
    "ガチャ": ("今回のガチャ、引く？", ["引く", "様子見", "引かない", "もう引いた"]),
    "アップデート": ("今回のアップデート、どう？", ["いい", "ふつう", "イマイチ", "まだ見てない"]),
}


def trend_poll(keys):
    """トレンドの言葉（[名前, 出来事] か [出来事] か [名前]）からお題を作る。作れなければ None"""
    name = keys[0] if len(keys) == 2 else ("" if keys and keys[0] in TREND_POLLS else (keys[0] if keys else ""))
    ev = keys[-1] if keys and keys[-1] in TREND_POLLS else ""
    if not ev:
        return None
    q, opts = TREND_POLLS[ev]
    if "{n}" in q and not name:
        return None
    q = q.format(n=name, e=ev)
    return {"q": q, "opts": list(opts)} if len(q) <= 40 else None


_TOKEN_RE = re.compile(r"[一-龥々〆ヶ]{2,6}|[ァ-ヴー]{3,12}|[A-Za-z]{2,12}")


def _team_names():
    out = []
    for team, (strong, weak) in TEAM_WORDS.items():
        out += [team] + list(strong)
    return sorted(set(out), key=len, reverse=True)


def title_terms(title, world):
    """タイトルから（出来事, 人やものの名前, 球団）を取り出す"""
    import unicodedata
    t = unicodedata.normalize("NFKC", str(title or ""))
    t = re.sub(r"【[^】]{0,12}】|\[[^\]]{0,12}\]|#\S+|https?://\S+", " ", t)
    teams = []
    for w in _team_names():
        if w in t:
            teams.append(w)
            t = t.replace(w, " ")
    events = []
    for e in sorted(TREND_EVENTS.get(world, []), key=len, reverse=True):
        if e in t:
            events.append(e)
            t = t.replace(e, " ")
    for s in sorted(TREND_STOP, key=len, reverse=True):
        if len(s) >= 2 and s in t:
            t = t.replace(s, " ")
    names = [w for w in _TOKEN_RE.findall(t) if w not in TREND_STOP and not re.fullmatch(r"[A-Za-z]{2}", w)]
    team = ""
    for tm, (strong, _w) in TEAM_WORDS.items():
        if any(x in teams for x in [tm] + list(strong)):
            team = tm
            break
    return events, list(dict.fromkeys(names)), team


def _label_ok(label):
    return 2 <= len(label) <= 16 and not any(x in label for x in TREND_NOT_LABEL)


def detect_trends(items, now, state=None):
    """速報の動画から、いま急に増えた話題を見つける。state は前回の結果（id を同じに保つ・最低12時間は出す）"""
    import hashlib
    state = dict(state or {})
    win = now - dt.timedelta(hours=TREND_WINDOW_H)
    base = now - dt.timedelta(days=TREND_BASE_DAYS) - dt.timedelta(hours=TREND_WINDOW_H)
    rec = {}   # (world, key) -> {chs, vids, teams}
    old = {}   # (world, key) -> チャンネルの集合
    for v in items:
        pub = parse_time(v.get("published"))
        if pub is None or pub < base:
            continue
        world = v.get("world") or "yakyu"
        events, names, team = title_terms(v.get("title"), world)
        keys = [("p", n, e) for n in names for e in events] + [("n", n, "") for n in names] + [("e", "", e) for e in events]
        for k in keys:
            if pub >= win:
                r = rec.setdefault((world,) + k, {"chs": set(), "vids": [], "teams": {}})
                r["chs"].add(v.get("ch") or v["id"])
                r["vids"].append(v["id"])
                if team:
                    r["teams"][team] = r["teams"].get(team, 0) + 1
                if v.get("team"):
                    r["teams"][v["team"]] = r["teams"].get(v["team"], 0) + 2
            else:
                old.setdefault((world,) + k, set()).add(v.get("ch") or v["id"])
    found = []
    for key, r in rec.items():
        world, kind, name, ev = key
        n = len(r["chs"])
        need = TREND_MIN_CH - (1 if kind == "p" else 0)   # 名前＋出来事は2チャンネルからでよい
        if n < need:
            continue
        before = len(old.get(key, ())) / TREND_BASE_DAYS * (TREND_WINDOW_H / 24)
        if n < max(need, before * 3):   # いつもの量と変わらないものはトレンドにしない
            continue
        label = name + ("　" + ev if name and ev else ev)
        if not _label_ok(label.replace("　", "")):
            continue
        score = n * (3 if kind == "p" else 2 if kind == "e" else 1) + len(r["vids"]) * 0.2
        team = max(r["teams"], key=r["teams"].get) if r["teams"] else ""
        found.append({"world": world, "label": label, "keys": [x for x in (name, ev) if x], "score": round(score, 2),
                      "ch": n, "news": list(dict.fromkeys(r["vids"]))[:30], "team": team, "kind": kind})
    found.sort(key=lambda t: -t["score"])
    picked = []
    for t in found:   # 同じ名前・同じ出来事の重なりは、点の高い1つだけ
        if any(set(t["keys"]) & set(p["keys"]) and p["world"] == t["world"] for p in picked):
            continue
        picked.append(t)
    out, nxt = [], {}
    stamp = now.isoformat(timespec="seconds")
    for t in picked:
        tid = "tr_" + hashlib.md5((t["world"] + "|" + t["label"]).encode("utf-8")).hexdigest()[:8]
        prev = state.get(tid) or {}
        first = prev.get("first") or stamp
        if now - parse_time(first) > dt.timedelta(hours=TREND_MAX_H):
            continue
        row = dict(t, id=tid, first=first, last=stamp)
        pl = trend_poll(t["keys"])
        if pl:
            row["poll"] = pl
        out.append(row)
        nxt[tid] = row
    for tid, p in state.items():   # 少し下火になっても12時間は残す
        if tid in nxt or not p.get("last"):
            continue
        if now - parse_time(p["last"]) < dt.timedelta(hours=TREND_KEEP_H) and now - parse_time(p["first"]) < dt.timedelta(hours=TREND_MAX_H):
            row = dict(p, fading=True)
            out.append(row)
            nxt[tid] = row
    res = []
    for w in ("yakyu", "game"):
        res += sorted([t for t in out if t["world"] == w], key=lambda t: (t.get("fading", False), -t["score"]))[:TREND_MAX]
    return res, nxt


def trends_for_app(now=None):
    """latest.json に入れるトレンド。data/trends.json に前回の結果を残す"""
    now = now or dt.datetime.now(dt.timezone.utc)
    items = load(os.path.join(DATA, "news.json"), {}).get("items", [])
    path = os.path.join(DATA, "trends.json")
    state = load(path, {})
    res, nxt = detect_trends(items, now, state if isinstance(state, dict) else {})
    dump(path, nxt)
    keep = ("id", "world", "label", "keys", "news", "team", "first", "ch", "fading", "poll")
    return [{k: t.get(k) for k in keep if t.get(k) not in (None, "")} for t in res]


# ---------------- 各モード ----------------
def build_latest(seed_list=None):
    """アプリが読む latest.json を作る（YouTube の公開値をそのまま並べるだけ）"""
    meta = load(os.path.join(DATA, "channels_meta.json"), {})
    live = load(os.path.join(DATA, "live.json"), {"live": [], "upcoming": [], "recent": []})
    full = load(os.path.join(DATA, "full.json"), {"videos": []})
    seeds = {c["id"]: c for c in (seed_list if seed_list is not None else load(os.path.join(HERE, "channels.json"), [])) if c.get("id")}
    live_by_ch = {v["ch"]: v for v in live["live"]}
    up_by_ch = {}
    for v in live["upcoming"]:
        up_by_ch.setdefault(v["ch"], v)
    chans = []
    for cid, m in meta.items():
        if cid not in seeds:  # 掲載をやめた配信者は出さない
            continue
        s = seeds[cid]
        m = {k: v for k, v in m.items() if k not in ("team_auto", "desc", "tags_auto", "streamer_auto")}
        mr = meta.get(cid) or {}
        chans.append(dict(m, id=cid, genre=genre_of(s), tags=s.get("tags") or mr.get("tags_auto", []), team=team_of(s, mr), streamer=bool(s.get("streamer") or mr.get("streamer_auto")),
                          live=live_by_ch.get(cid), up=up_by_ch.get(cid)))
    videos = {v["id"]: v for v in full["videos"] if v["ch"] in seeds}
    for v in live.get("recent", []):
        if v["ch"] in seeds:
            videos.setdefault(v["id"], v)
    out = {
        "updated": dt.datetime.now(JST).isoformat(timespec="minutes"),
        "source": "YouTube Data API",
        "channels": sorted(chans, key=lambda c: -c.get("subs", 0)),
        "videos": sorted(videos.values(), key=lambda v: v["published"], reverse=True)[:300],
        "pr": load(os.path.join(HERE, "pr.json"), {}).get("id", ""),  # PR枠（{"id": "UC…"} を置くと表示）
        "games": upcoming_games(today_jst()),  # 昨日から7日先の試合（NPBの公開日程から自動＋games.csv の手入力）。結果・中止つき
        "news": news_for_app(),  # 速報（7日以内）
        "topics": [{k: t[k] for k in ("id", "world", "label")} for t in news_topics()],  # 最後に「球団公式」
        "trends": trends_for_app(),  # いま急に増えた話題（アプリではこれだけタブになる）
    }
    art = load(os.path.join(HERE, "stickers.json"), {})  # スタンプを画像に差し替えるとき：{"st_hr": "https://…png"}
    if isinstance(art, dict) and art:
        out["stickerArt"] = {k: v for k, v in art.items() if str(k).startswith("st_") and str(v).startswith("https://")}
    dump(os.path.join(DATA, "latest.json"), out)
    print(f"latest.json: {len(chans)} channels, {len(out['videos'])} videos, {len(live['live'])} live")


def mode_live(seeds):
    ids = []
    for c in seeds:
        ids += rss_recent(c["id"], 3 if c.get("streamer") else 1)
    prev = load(os.path.join(DATA, "live.json"), {"upcoming": []})
    ids += [v["id"] for v in prev.get("upcoming", [])]  # 予定だった配信が始まったかを確認
    vids = api_videos(list(dict.fromkeys(ids)))
    now = dt.datetime.now(dt.timezone.utc)
    al, fi = all_ids_of(seeds), fan_ids_of(seeds)
    live = sorted([v for v in vids.values() if v["state"] == "live" and shown(v, al, fi)], key=lambda v: -(v["viewers"] or 0))
    upcoming = []
    for v in vids.values():
        if v["state"] == "upcoming" and v["start"] and shown(v, al, fi):
            st = dt.datetime.fromisoformat(v["start"].replace("Z", "+00:00"))
            if now - dt.timedelta(hours=1) <= st <= now + dt.timedelta(hours=24):
                upcoming.append(v)
    upcoming.sort(key=lambda v: v["start"])
    recent = [v for v in vids.values() if v["state"] == "none" and shown(v, al, fi)]
    dump(os.path.join(DATA, "live.json"), {"live": live, "upcoming": upcoming, "recent": recent})
    take_snapshot(live, dt.datetime.now(JST))
    try:  # 試合の時間帯だけ、今日の試合の結果・中止を20分ごとに日程ページから読み直す（試合速報が読めないときの予備）
        mode_today_games()
    except Exception as e:
        print("今日の試合の更新に失敗", e, file=sys.stderr)
    try:  # 試合の時間帯だけ、試合速報ページから点数・回・試合終了を読む（約10分ごと）
        mode_live_scores()
    except Exception as e:
        print("試合速報の更新に失敗", e, file=sys.stderr)
    try:  # 球団・リーグ公式チャンネルの新着（RSS・約20分ごと）
        mode_rss_news()
    except Exception as e:
        print("公式チャンネルの速報の更新に失敗", e, file=sys.stderr)


def take_snapshot(live, now_jst):
    """22時台の最初の更新で、その時点の同時視聴者数（YouTube の公開値）を残す。「今夜の1位予想」の答え合わせ用"""
    if now_jst.hour != 22:
        return None
    path = os.path.join(SNAP, now_jst.date().isoformat() + ".json")
    if os.path.exists(path):
        return None
    snap = {v["ch"]: v["viewers"] for v in live if v.get("viewers") is not None}
    dump(path, snap)
    return snap


def mode_full(seeds):
    today = today_jst()
    meta = api_channels([c["id"] for c in seeds])
    ids = []
    for c in seeds:
        ids += rss_recent(c["id"], 5)
    all_vids = list(api_videos(list(dict.fromkeys(ids))).values())
    fi = fan_ids_of(seeds)
    by = {}
    for v in all_vids:
        if rel_fn("fan" if v["ch"] in fi else "game")(v["title"]):  # ジャンルに合う動画のタイトルだけで推定する
            by.setdefault(v["ch"], []).append(v["title"])
    for cid, m in meta.items():
        m["team_auto"] = infer_team(m.get("name"), m.get("desc"), by.get(cid, []))
        m["tags_auto"] = auto_tags_for("fan" if cid in fi else "game", by.get(cid, []))
        m["streamer_auto"] = auto_streamer(by.get(cid, []))
        m.pop("desc", None)  # 説明文は保存しない
    dump(os.path.join(DATA, "channels_meta.json"), meta)
    rv = review_list(all_vids, meta, fi)
    dump(os.path.join(DATA, "review.json"), rv)
    # 最近ジャンルの動画（ゲーム実況＝プロスピ、プロ野球＝野球の話題）を1本も出していない人は自動で外す（また出しはじめたら毎日の自動さがしで戻る）
    drop = {r["id"] for r in rv}
    if drop:
        path = os.path.join(HERE, "channels.json")
        cur = load(path, [])
        keep = [c for c in cur if c.get("id") not in drop]
        if len(keep) != len(cur) and keep:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(keep, f, ensure_ascii=False, indent=1)
            print(f"ジャンルの動画がなくなった {len(cur) - len(keep)}人を外しました")
    if not os.path.exists(os.path.join(DATA, "games_npb.json")):
        mode_schedule()
    al = all_ids_of(seeds)
    dump(os.path.join(DATA, "full.json"), {"videos": [v for v in all_vids if shown(v, al, fi)]})
    prune_old(today)


def discover_queries(today=None):
    """毎日のさがす言葉：(言葉, ジャンル)。ゲーム6つ＋プロ野球5つ＋その日の球団1つ（12球団を日替わり）＝12回×100ユニット"""
    teams = list(TEAM_WORDS)
    team = teams[(today or today_jst()).timetuple().tm_yday % len(teams)]
    return [(q, "game") for q in DISCOVER_QUERIES] + [(q, "fan") for q in FAN_DISCOVER_QUERIES + [f"{team}ファン"]]


def discover_pick(meta, titles_by, known, blocked):
    """ゲーム実況の条件を先に見る（両方に当てはまる人はゲーム実況）。残りの人をプロスピ以外のプロ野球の条件で見る"""
    game = auto_pick(meta, titles_by, known, blocked, "game")
    got = known | {c["id"] for c in game}
    return game + auto_pick(meta, titles_by, got, blocked, "fan")


def mode_discover(seeds):
    known = {c["id"] for c in seeds}
    cands = load(os.path.join(DATA, "candidates.json"), {})
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for q, genre in discover_queries():  # search.list は1回100ユニット
        res = get(API + "search", {"part": "snippet", "q": q, "type": "video", "order": "date", "regionCode": "JP",
                                   "relevanceLanguage": "ja", "publishedAfter": since, "maxResults": 50})
        for it in res.get("items", []):
            cid = it["snippet"]["channelId"]
            if cid in known:
                continue
            c = cands.setdefault(cid, {"name": it["snippet"]["channelTitle"], "hits": 0, "sample": it["snippet"]["title"], "genre": genre})
            c.setdefault("genre", genre)
            c["hits"] += 1
    # 自動掲載：候補を確かめて、条件を満たす人を channels.json に足す（人の確認は不要。外した人は blocked.json で二度と足さない）
    blocked = set(load(os.path.join(HERE, "blocked.json"), []))
    ids = [cid for cid in cands if cid not in known and cid not in blocked][:150]
    meta = api_channels(ids) if ids else {}
    titles_by = {cid: recent_titles(cid) for cid in meta}
    add = discover_pick(meta, titles_by, known, blocked)
    for cid in meta:
        fn = rel_fn(cands[cid].get("genre", "game"))
        rel = [t for t in titles_by[cid] if fn(t)]
        cands[cid].update({"level": level_of(len(rel), len(titles_by[cid])), "checked": today_jst().isoformat()})
    if add:
        path = os.path.join(HERE, "channels.json")
        cur = [c for c in load(path, []) if not str(c.get("id", "")).startswith(("UCxxxx", "UCyyyy"))]
        cur += add
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=1)
        for c in add:
            cands.pop(c["id"], None)
    # 候補は30日たったら消す（ずっと増えないように）
    for cid in [k for k, c in cands.items() if c.get("checked") and (today_jst() - dt.date.fromisoformat(c["checked"])).days > 30]:
        cands.pop(cid, None)
    dump(os.path.join(DATA, "candidates.json"), cands)
    nf = sum(1 for c in add if c.get("genre") == "fan")
    print(f"自動で追加：{len(add)}人（ゲーム実況 {len(add) - nf}・プロ野球 {nf}）・候補：{len(cands)}")
    mode_schedule()


def related_ratio(channel_id):
    """最近の動画タイトルのうちプロスピ関連の割合（RSSなので割り当てを使わない）"""
    try:
        xml = get(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}", as_json=False)
    except Exception:
        return 0.0, 0
    ns = {"a": "http://www.w3.org/2005/Atom"}
    titles = [e.find("a:title", ns).text or "" for e in ET.fromstring(xml).findall("a:entry", ns)]
    return senmon_ratio(titles), len(titles)


def recent_titles(channel_id):
    """最近の動画タイトル（最大15本・RSSなので割り当てを使わない）"""
    try:
        xml = get(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}", as_json=False)
    except Exception:
        return []
    ns = {"a": "http://www.w3.org/2005/Atom"}
    return [e.find("a:title", ns).text or "" for e in ET.fromstring(xml).findall("a:entry", ns)]


def level_of(n_rel, n):
    """選ぶときの目安（アプリには出さない）"""
    r = n_rel / n if n else 0
    return "専門" if r >= SENMON_RATIO and n >= 5 else "多め" if r >= 0.4 else "ときどき"


def mode_wide(seeds, genre="game"):
    """プロスピの動画（genre="fan" のときはゲームでないプロ野球の動画）を出しているチャンネルを広く集めて
    channels.proposed.json に入れる（載せるかは curate で人が選ぶ）。もう片方のジャンルの候補はそのまま残す"""
    fn = rel_fn(genre)
    known = {c["id"] for c in seeds}
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=180)).strftime("%Y-%m-%dT%H:%M:%SZ")
    found = {}
    for i, q in enumerate(FAN_WIDE_QUERIES if genre == "fan" else WIDE_QUERIES):
        token = None
        for _ in range(WIDE_PAGES if i < 6 else 1):
            params = {"part": "snippet", "q": q, "type": "video", "regionCode": "JP", "relevanceLanguage": "ja",
                      "publishedAfter": since, "maxResults": 50}
            if token:
                params["pageToken"] = token
            res = get(API + "search", params)
            for it in res.get("items", []):
                found.setdefault(it["snippet"]["channelId"], it["snippet"]["channelTitle"])
            token = res.get("nextPageToken")
            if not token:
                break
        print(f"  「{q}」まで：{len(found)}チャンネル", flush=True)
    # 前に見つかった候補（毎日の discover）も入れる
    for cid, c in load(os.path.join(DATA, "candidates.json"), {}).items():
        if c.get("genre", "game") == genre:
            found.setdefault(cid, c.get("name", ""))
    meta = api_channels([c for c in found if c not in known])
    prop_path = os.path.join(HERE, "channels.proposed.json")
    old_rows = load(prop_path, [])
    old = {c["id"]: c for c in old_rows}
    out = []
    for k, (cid, m) in enumerate(meta.items()):
        if m["subs"] < WIDE_MIN_SUBS or (genre == "fan" and is_official(m["name"], "fan")):
            continue
        titles = recent_titles(cid)
        rel = [t for t in titles if fn(t)]
        if len(rel) < WIDE_MIN_RELATED:
            continue
        o = old.get(cid, {})
        out.append({"id": cid, "name": m["name"], "handle": m["handle"], "thumb": m["thumb"], "subs": m["subs"], "genre": genre,
                    "team_auto": infer_team(m["name"], m.get("desc"), rel),
                    "rel": len(rel), "n": len(titles), "level": level_of(len(rel), len(titles)), "sample": rel[:3],
                    "tags": o.get("tags", []), "team": o.get("team", ""), "streamer": o.get("streamer", False),
                    "check": "OK" if level_of(len(rel), len(titles)) == "専門" else ""})
        if k % 25 == 24:
            print(f"  確認中… {k + 1}/{len(meta)}", flush=True)
    cnt = {lv: sum(1 for c in out if c["level"] == lv) for lv in ("専門", "多め", "ときどき")}
    n_new = len(out)
    ids = {c["id"] for c in out}
    out += [c for c in old_rows if c.get("genre", "game") != genre and c["id"] not in ids]  # もう片方のジャンルの候補は消さない
    out.sort(key=lambda c: ({"専門": 0, "多め": 1, "ときどき": 2}.get(c.get("level"), 3), -c.get("subs", 0)))
    dump(prop_path, out)
    print(f"{'プロ野球（球団ファン）' if genre == 'fan' else 'ゲーム実況（プロスピ）'}の候補 {n_new}件（専門 {cnt['専門']}・多め {cnt['多め']}・ときどき {cnt['ときどき']}）。次に curate で選んでください")


def mode_seed(_seeds):
    """seed_names.json の名前・ハンドルから本物のチャンネルを探して、確認用の一覧を作る"""
    names = load(os.path.join(HERE, "seed_names.json"), {"handles": [], "names": []})
    ids = {}
    for h in names.get("handles", []):
        cid = api_handle(h)
        if cid:
            ids[cid] = {"from": h}
        else:
            print("見つからないハンドル:", h, file=sys.stderr)
    for n in names.get("names", []):
        for cid in api_search_channels(n["q"], 3):
            ids.setdefault(cid, {"from": n["q"]})
    meta = api_channels(list(ids))
    proposed = []
    for cid, m in meta.items():
        titles = recent_titles(cid)
        rel = [t for t in titles if is_related(t)]
        lv = level_of(len(rel), len(titles))
        proposed.append({"id": cid, "name": m["name"], "handle": m["handle"], "thumb": m["thumb"], "subs": m["subs"], "from": ids[cid]["from"],
                         "team_auto": infer_team(m["name"], m.get("desc"), rel),
                         "rel": len(rel), "n": len(titles), "level": lv, "sample": rel[:3], "tags": [], "team": "", "streamer": False,
                         "check": "OK" if lv == "専門" else "対象外：プロスピ専門ではない"})
    proposed.sort(key=lambda c: (c["check"] != "OK", -c["subs"]))
    dump(os.path.join(HERE, "channels.proposed.json"), proposed)
    ok = sum(1 for c in proposed if c["check"] == "OK")
    print(f"channels.proposed.json: {len(proposed)}件（プロスピ専門 {ok}件）。別人の行は消してから --merge-proposed（OKの行だけ追加されます）")


def merge_proposed():
    prop = load(os.path.join(HERE, "channels.proposed.json"), [])
    path = os.path.join(HERE, "channels.json")
    cur = [c for c in load(path, []) if not str(c.get("id", "")).startswith("UCxxxx") and not str(c.get("id", "")).startswith("UCyyyy")]
    have = {c.get("id") for c in cur}
    added = 0
    for c in prop:
        if c["id"] in have or c.get("check") != "OK":
            continue
        cur.append({"id": c["id"], "name": c["name"], "genre": genre_of(c), "tags": c.get("tags", []), "team": c.get("team", ""), "streamer": c.get("streamer", False)})
        have.add(c["id"]); added += 1
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=1)
    print(f"channels.json に {added}件 追加（合計 {len(cur)}件）")


# ---------------- selftest ----------------
def selftest():
    # トレンド：3チャンネル以上が同じ「名前＋出来事」を出したらタブ。いつも多い話題（大谷）はトレンドにしない
    nowt = dt.datetime(2026, 11, 20, 12, tzinfo=dt.timezone.utc)

    def _it(i, t, ch, h, world="yakyu", team=""):
        return {"id": "v%d" % i, "title": t, "ch": ch, "published": (nowt - dt.timedelta(hours=h)).isoformat().replace("+00:00", "Z"), "world": world, "team": team}
    its = [_it(1, "【速報】佐藤選手が電撃トレード！", "A", 2), _it(2, "佐藤 トレードの真相", "B", 3), _it(3, "佐藤のトレード 反応", "C", 5, team="阪神"),
           _it(4, "大谷翔平 今日のハイライト", "D", 2), _it(5, "大谷翔平 第2打席", "E", 3), _it(6, "大谷翔平 まとめ", "F", 4)]
    its += [_it(10 + i, "大谷翔平 ホームラン", "G%d" % i, 40 + i * 20) for i in range(6)]
    its += [_it(40, "プロスピA 新ガチャ", "H", 3, "game"), _it(41, "プロスピ 新ガチャ", "I", 4, "game")]
    tr, st = detect_trends(its, nowt, {})
    assert [t["label"] for t in tr] == ["佐藤　トレード"], tr
    assert tr[0]["team"] == "阪神" and tr[0]["id"].startswith("tr_") and len(tr[0]["news"]) == 3
    assert tr[0]["poll"] == {"q": "佐藤のトレード、どう思う？", "opts": ["いいと思う", "さみしい", "まだわからない", "新天地で応援"]}, tr[0].get("poll")
    assert trend_poll(["ドラフト"])["q"].startswith("今年のドラフト") and trend_poll(["引退"]) is None and trend_poll(["大谷翔平"]) is None
    assert not any("プロスピ" in t["label"] for t in tr)
    tr2, st2 = detect_trends(its[3:], nowt + dt.timedelta(hours=6), st)
    assert tr2 and tr2[0]["id"] == tr[0]["id"] and tr2[0].get("fading"), "下火でも12時間は残す"
    tr3, _ = detect_trends(its[3:], nowt + dt.timedelta(hours=13), st2)
    assert not tr3, "12時間たったら消す"
    assert title_terms("【速報】阪神・佐藤輝明が電撃トレード", "yakyu")[:2] == (["トレード"], ["佐藤輝明"])
    import tempfile
    global DATA, SNAP, HERE, get
    today = dt.date(2026, 9, 24)
    assert is_related("【プロスピA】x") and not is_related("雑談")
    assert shown({"ch": "A", "title": "リアタイ10連勝"}, {"A"}) and not shown({"ch": "B", "title": "リアタイ10連勝"}, {"A"})
    assert senmon_ratio(["【プロスピA】a", "【プロスピA】b", "雑談"]) == 2 / 3
    assert review_list([{"ch": "X", "title": "雑談"}] * 3 + [{"ch": "Y", "title": "【プロスピA】x"}] * 3, {})[0]["id"] == "X"
    assert infer_team("純正ロッテ部", "千葉ロッテマリーンズ純正で遊んでいます", ["【プロスピA】ロッテ純正"]) == "ロッテ"
    assert infer_team("リアタイ研究所", "", ["【プロスピA】巨人と阪神の選手", "阪神純正"]) == "", "はっきりしないときは付けない"
    assert infer_team("虎党プロスピ", "阪神タイガースが好き", ["【プロスピ】阪神純正"]) == "阪神"
    assert infer_team("ドラフト研究", "", ["プロスピ ドラフト"]) == "", "ドラ・ドラフトは中日にしない"
    assert infer_team("ゲーム部", "", ["【プロスピ】ヤクルト純正"] * 5 + ["【プロスピ】巨人戦"]) == "ヤクルト"
    assert team_of({"team": "-"}, {"team_auto": "阪神"}) == "" and team_of({}, {"team_auto": "阪神"}) == "阪神" and team_of({"team": "巨人"}, {"team_auto": "阪神"}) == "巨人"
    import tempfile as _t
    gp = os.path.join(_t.mkdtemp(), "g.csv")
    with open(gp, "w", encoding="utf-8") as f:
        f.write("# 説明\n日付,開始,ホーム,ビジター,球場\n2026-09-24,18:00,千葉ロッテ,日ハム,ZOZOマリン\n2026/9/25,14:00,阪神,巨人,甲子園\n2026-09-24,18:00,ロッテ,ロッテ,x\n2026-10-30,18:00,広島,中日,マツダ\nあいう\n")
    g, bad = read_games(gp, dt.date(2026, 9, 24))
    assert [x["home"] for x in g] == ["ロッテ", "阪神"] and g[0]["away"] == "日本ハム" and g[1]["day"] == "2026-09-25", g
    assert len(bad) == 2, bad  # 同じ球団どうし・日付でない行
    html = """<table><tr><th>月日</th><th>対戦カード</th><th>球場・開始時間</th></tr>
<tr><td rowspan="2">10/1（木）</td><td><div>阪神</div><div>-</div><div>巨人</div></td><td>甲子園<br>18:00</td></tr>
<tr><td>ロッテ - 日本ハム</td><td>ZOZOマリン 18:00</td></tr>
<tr><td>10/2（金）</td><td>ソフトバンク 3 - 2 西武</td><td>みずほPayPay 18:00</td><td>中止</td></tr>
<tr><td>10/3（土）</td><td>DeNA - 中日</td><td>横浜 14:00</td></tr>
<tr><td>10/4（日）</td><td>楽天 5 - 1 オリックス</td><td>楽天モバイル 13:00</td></tr>
<tr><td>10/8（木）</td><td>セ・CSファーストS</td><td></td></tr></table>"""
    g = parse_npb_schedule(html, 2026)
    got = [(x["day"], x["home"], x["away"], x["time"]) for x in g]
    assert got[:3] == [("2026-10-01", "阪神", "巨人", "18:00"), ("2026-10-01", "ロッテ", "日本ハム", "18:00"), ("2026-10-02", "ソフトバンク", "西武", "18:00")], g
    assert got[3:] == [("2026-10-03", "DeNA", "中日", "14:00"), ("2026-10-04", "楽天", "オリックス", "13:00")], g
    assert g[0]["place"] == "甲子園" and g[1]["place"] == "ZOZOマリン" and g[2]["off"] is True
    assert g[2]["status"] == "off" and "score" not in g[2], "中止の試合は点数を付けない"
    assert g[4]["status"] == "end" and g[4]["score"] == [5, 1] and g[4]["off"] is False, g[4]
    assert g[0]["status"] == "" and "score" not in g[0] and g[3]["status"] == ""
    assert g[4]["place"] == "楽天モバイル", g[4]
    # 今日の試合の読み直し
    old_g = [{"day": "2026-10-04", "time": "13:00", "home": "楽天", "away": "オリックス", "place": "x", "off": False, "status": ""},
             {"day": "2026-10-05", "time": "18:00", "home": "巨人", "away": "中日", "place": "東京ドーム", "off": False, "status": ""}]
    mg = merge_today_games(old_g, g, {"2026-10-04"})
    assert [(x["day"], x["home"], x["status"]) for x in mg] == [("2026-10-04", "楽天", "end"), ("2026-10-05", "巨人", "")], mg
    t0 = dt.datetime(2026, 10, 4, 18, 0, tzinfo=JST)
    assert need_today_games(t0, None) and need_today_games(t0, "こわれた")
    assert not need_today_games(dt.datetime(2026, 10, 4, 12, 59, tzinfo=JST), None), "13時前は読まない"
    assert not need_today_games(t0, "2026-10-04T17:45+09:00") and need_today_games(t0, "2026-10-04T17:40+09:00")
    assert game_row({"day": "d", "time": "", "home": "a", "away": "b", "place": "", "off": True}) == {"day": "d", "time": "", "home": "a", "away": "b", "place": "", "status": "off"}
    assert game_row(g[4])["score"] == [5, 1] and "off" not in game_row(g[4]) and "score" not in game_row(g[2])
    # 速報
    now = dt.datetime(2026, 10, 4, 12, 0, tzinfo=dt.timezone.utc)
    tps = [{"id": "a", "world": "yakyu"}, {"id": "b", "world": "game"}]
    fr = [{"id": "v1", "ch": "C1", "published": "2026-10-04T10:00:00Z", "topic": "a"},
          {"id": "v1", "ch": "C1", "published": "2026-10-04T10:00:00Z", "topic": "b"},
          {"id": "v2", "ch": "BAD", "published": "2026-10-04T11:00:00Z", "topic": "a"},
          {"id": "v3", "ch": "C3", "published": "2026-10-04T11:30:00Z", "topic": "b"}]
    pv = [{"id": "v4", "ch": "C4", "published": "2026-10-01T00:00:00Z", "topic": "a", "topics": ["a"], "world": "yakyu"},
          {"id": "v5", "ch": "C5", "published": "2026-09-20T00:00:00Z", "topic": "a", "topics": ["a"], "world": "yakyu"},
          {"id": "v6", "ch": "C6", "published": "2026-10-03T00:00:00Z", "topic": "gone", "topics": ["gone"], "world": "yakyu"},
          {"id": "v3", "ch": "C3", "published": "2026-10-04T11:30:00Z", "topic": "a", "topics": ["a"], "world": "yakyu"}]
    nw = merge_news(pv, fr, tps, {"BAD"}, now)
    assert [v["id"] for v in nw] == ["v3", "v1", "v4"], nw
    assert nw[1]["topic"] == "a" and nw[1]["topics"] == ["a", "b"] and nw[1]["world"] == "yakyu", nw[1]
    assert nw[0]["topic"] == "b" and nw[0]["topics"] == ["b", "a"] and nw[0]["world"] == "game", "今回見つかった話題を先に"
    assert len(merge_news([], fr[:1] * 3 + fr[3:], tps, set(), now, limit=1)) == 1
    assert parse_time("2026-10-04T10:00:00Z").hour == 10 and parse_time("x") is None
    assert auto_tags(["【プロスピA】リアタイ", "リアタイ最強", "無課金ガチャ", "ガチャ100連"]) == ["リアタイ", "ガチャ"]
    assert auto_streamer(["【生配信】プロスピ", "参加型ライブ"]) and not auto_streamer(["解説"])
    ok_titles = ["【プロスピA】a"] * 12 + ["x"] * 3
    picked = auto_pick({"UC1": {"name": "配信者", "subs": 5000}, "UC2": {"name": "プロスピ公式", "subs": 99999}, "UC3": {"name": "小さい", "subs": 50}, "UC4": {"name": "外した人", "subs": 9000}},
                       {"UC1": ok_titles, "UC2": ok_titles, "UC3": ok_titles, "UC4": ok_titles}, set(), {"UC4"})
    assert [c["id"] for c in picked] == ["UC1"] and picked[0]["all"] is True, picked
    assert level_of(12, 15) == "専門" and level_of(7, 15) == "多め" and level_of(2, 15) == "ときどき"
    # ⚾ プロ野球（球団ファン）
    assert is_fan_related("【現地観戦】ZOZOマリンでロッテ勝利！")
    assert not is_fan_related("【プロスピA】ロッテ純正") and not is_fan_related("今日の晩ごはん")
    assert not is_fan_related("【パワプロ2024】阪神でペナント") and not is_fan_related("実況パワフルプロ野球 サクセス") and not is_fan_related("eBASEBALL プロ野球 決勝")
    assert is_fan_related("阪神タイガース 反省会") and is_fan_related("巨人戦 振り返り") and is_fan_related("首位と3ゲーム差！ホークス逆転勝ち")
    assert not is_fan_related("進撃の巨人 考察") and not is_fan_related("楽天市場でお買い物") and not is_fan_related("ゲーム実況 雑談")
    assert rel_fn("fan") is is_fan_related and rel_fn("game") is is_related and rel_fn(None) is is_related
    assert genre_of({}) == "game" and genre_of({"genre": "fan"}) == "fan" and genre_of({"genre": "x"}) == "game"
    assert shown({"ch": "F", "title": "【現地観戦】甲子園"}, set(), {"F"}) and not shown({"ch": "F", "title": "【プロスピA】ガチャ"}, set(), {"F"})
    assert not shown({"ch": "F", "title": "今日の晩ごはん"}, set(), {"F"}) and shown({"ch": "F", "title": "今日の晩ごはん"}, {"F"}, {"F"})
    assert not shown({"ch": "G", "title": "【現地観戦】甲子園"}, set(), {"F"}), "ゲームの人は今までどおりプロスピだけ"
    rv = review_list([{"ch": "F", "title": "【プロスピA】x"}] * 3 + [{"ch": "H", "title": "阪神 反省会"}] * 3 + [{"ch": "G", "title": "阪神 反省会"}] * 3, {}, {"F", "H"})
    assert sorted(r["id"] for r in rv) == ["F", "G"], rv
    fan_titles = ["【現地観戦】ZOZOマリンでロッテ勝利", "ロッテ 反省会 振り返り", "応援歌 ロッテ 現地", "球場グルメ ZOZOマリン", "ZOZOマリン 球場グルメ 食べ歩き",
                  "ドラフト 考察 ロッテ", "【生配信】ロッテ戦 反省会", "ロッテ 試合 振り返り ライブ配信"] + ["日常vlog"] * 2
    fp = auto_pick({"F1": {"name": "マリーンズ応援ch", "subs": 2000, "desc": "千葉ロッテマリーンズを応援"}, "F2": {"name": "千葉ロッテマリーンズ", "subs": 90000},
                    "F3": {"name": "スポニチ野球", "subs": 90000}, "F4": {"name": "パ・リーグTV", "subs": 900000}, "F5": {"name": "ゲーム実況者", "subs": 5000}},
                   {"F1": fan_titles, "F2": fan_titles, "F3": fan_titles, "F4": fan_titles, "F5": ok_titles}, set(), set(), "fan")
    assert [c["id"] for c in fp] == ["F1"], fp
    assert fp[0]["genre"] == "fan" and fp[0]["all"] is False and fp[0]["team"] == "ロッテ" and fp[0]["streamer"] is True, fp
    assert set(fp[0]["tags"]) <= set(FAN_GENRE_WORDS) and "球場グルメ" in fp[0]["tags"], fp
    assert auto_tags_for("fan", ["【現地観戦】甲子園", "現地で応援", "ドラフト考察", "二軍 ファーム"]) == ["現地観戦", "ドラフト・二軍"]
    assert auto_tags_for("game", ["リアタイ", "リアタイ2"]) == ["リアタイ"]
    both = discover_pick({"B": {"name": "両方", "subs": 5000}, "F1": {"name": "マリーンズ応援ch", "subs": 2000}},
                         {"B": ok_titles, "F1": fan_titles}, set(), set())
    assert [(c["id"], c["genre"]) for c in both] == [("B", "game"), ("F1", "fan")], both
    q = discover_queries(dt.date(2026, 9, 24))
    assert len(q) == 12 and sum(1 for _, g in q if g == "fan") == 6 and q[-1][0].endswith("ファン"), q
    old, old_s, old_here = DATA, SNAP, HERE
    DATA = tempfile.mkdtemp(); SNAP = os.path.join(DATA, "snap")
    try:
        live = [{"ch": "A", "viewers": 300}, {"ch": "B", "viewers": None}]
        assert take_snapshot(live, dt.datetime(2026, 9, 24, 21, 55, tzinfo=JST)) is None, "22時前は残さない"
        assert take_snapshot(live, dt.datetime(2026, 9, 24, 22, 3, tzinfo=JST)) == {"A": 300}
        assert take_snapshot([{"ch": "A", "viewers": 999}], dt.datetime(2026, 9, 24, 22, 13, tzinfo=JST)) is None, "22時台の最初の1回だけ"
        dump(os.path.join(SNAP, "2026-08-01.json"), {"A": 1})
        dump(os.path.join(DATA, "history", "2026-09-20.json"), {"A": 1})
        prune_old(today)
        assert sorted(os.listdir(SNAP)) == ["2026-09-24.json"], os.listdir(SNAP)
        assert not os.path.exists(os.path.join(DATA, "history")), "登録者数の履歴は残さない"
        dump(os.path.join(DATA, "channels_meta.json"), {"UC1": {"name": "a", "subs": 10}, "UC2": {"name": "b", "subs": 20, "tags_auto": ["応援"]}})
        dump(os.path.join(DATA, "full.json"), {"videos": []})
        build_latest([{"id": "UC1"}, {"id": "UC2", "genre": "fan"}])
        lj = load(os.path.join(DATA, "latest.json"), {})
        assert {c["id"]: c["genre"] for c in lj["channels"]} == {"UC1": "game", "UC2": "fan"}, lj["channels"]
        assert [c for c in lj["channels"] if c["id"] == "UC2"][0]["tags"] == ["応援"]
        assert lj["news"] == [] and [t["id"] for t in lj["topics"]] == [t["id"] for t in NEWS_TOPICS] + ["official"]
        assert lj["topics"][-1] == OFFICIAL_TOPIC, lj["topics"]
        assert set(lj["topics"][0]) == {"id", "world", "label"}
        # topics.json で話題を変える（形がおかしい行は使わない）
        HERE = DATA
        dump(os.path.join(HERE, "topics.json"), [{"id": "x", "world": "yakyu", "q": "q", "label": "L"}, {"id": "y", "world": "?", "q": "q", "label": "L"}, "z"])
        assert load_topics() == [{"id": "x", "world": "yakyu", "q": "q", "label": "L"}]
        dump(os.path.join(HERE, "topics.json"), [{"id": "", "world": "game"}])
        assert load_topics() == NEWS_TOPICS, "使える行がなければふつうの話題"
        os.remove(os.path.join(HERE, "topics.json"))
        # 速報を latest.json に入れる（7日以内・決まった項目だけ）
        nowu = dt.datetime.now(dt.timezone.utc)
        rec = (nowu - dt.timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        old8 = (nowu - dt.timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
        n1 = {"id": "v1", "title": "t", "ch": "C", "chName": "n", "published": rec, "topic": "ohtani", "topics": ["ohtani"], "world": "yakyu", "thumb": "u"}
        dump(os.path.join(DATA, "news.json"), {"items": [n1, dict(n1, id="v2", published=old8)]})
        nf = news_for_app()
        assert [v["id"] for v in nf] == ["v1"] and set(nf[0]) == {"id", "title", "ch", "chName", "published", "topic", "world", "thumb", "team"}, nf
        assert nf[0]["team"] == ""
        # 試合：昨日の結果・中止も出す。手入力が優先だが、同じ試合なら結果は残す
        td = today_jst()
        d = lambda n: (td + dt.timedelta(days=n)).isoformat()
        auto = [{"day": d(-2), "time": "18:00", "home": "阪神", "away": "巨人", "place": "", "off": False, "status": "end", "score": [1, 0]},
                {"day": d(-1), "time": "18:00", "home": "ロッテ", "away": "西武", "place": "", "off": False, "status": "end", "score": [3, 2]},
                {"day": d(0), "time": "18:00", "home": "広島", "away": "中日", "place": "", "off": True},
                {"day": d(0), "time": "18:00", "home": "楽天", "away": "オリックス", "place": "", "status": "end", "score": [4, 4]},
                {"day": d(1), "time": "18:00", "home": "DeNA", "away": "ヤクルト", "place": ""}]
        dump(os.path.join(DATA, "games_npb.json"), {"games": auto})
        with open(os.path.join(HERE, "games.csv"), "w", encoding="utf-8") as f:
            f.write(f"{d(0)},14:00,楽天,オリックス,楽天モバイル\n{d(1)},13:00,DeNA,巨人,横浜\n")
        ug = upcoming_games(td)
        assert [(x["day"], x["home"]) for x in ug] == [(d(-1), "ロッテ"), (d(0), "楽天"), (d(0), "広島"), (d(1), "DeNA")], ug
        assert ug[0]["status"] == "end" and ug[0]["score"] == [3, 2] and ug[2]["status"] == "off" and "score" not in ug[2]
        assert ug[1]["time"] == "14:00" and ug[1]["status"] == "end" and ug[1]["score"] == [4, 4], ug[1]
        assert ug[3]["away"] == "巨人" and ug[3]["status"] == "" and "score" not in ug[3], ug[3]
        assert all(set(x) <= {"day", "time", "home", "away", "place", "status", "score"} and "status" in x for x in ug), ug
        # 今日の試合の読み直し（通信はせず、日程ページの代わりに html を返す）
        old_get = get
        html_today = f"<table><tr><td>{td.month}/{td.day}</td><td>広島 2 - 1 中日</td><td>マツダ 18:00</td></tr></table>"
        get = lambda url, params=None, as_json=True: html_today.encode("utf-8")
        try:
            dump(os.path.join(DATA, "games_npb.json"), {"games": auto})
            t1 = dt.datetime.combine(td, dt.time(21, 0), JST)
            assert mode_today_games(t1) is True
            gj = load(os.path.join(DATA, "games_npb.json"), {})
            hs = [x for x in gj["games"] if x["home"] == "広島"]
            assert len(hs) == 1 and hs[0]["status"] == "end" and hs[0]["score"] == [2, 1] and len(gj["games"]) == len(auto), gj
            assert mode_today_games(t1 + dt.timedelta(minutes=10)) is False, "20分以内は読まない"
            get = lambda *a, **k: (_ for _ in ()).throw(OSError("x"))
            assert mode_today_games(t1 + dt.timedelta(minutes=30)) is True, "失敗しても止まらない"
            assert len(load(os.path.join(DATA, "games_npb.json"), {})["games"]) == len(auto)
            # ---- 試合速報（NPB の試合ページ。通信はしない） ----
            Y, MD = td.year, f"{td.month:02d}{td.day:02d}"
            top = (f'<a href="https://npb.jp/scores/{Y}/{MD}/m-f-23/">a</a><a href="/scores/{Y}/{MD}/e-b-25/">b</a>'
                   f'<a href="/scores/{Y}/{MD}/c-d-20/">c</a><a href="/scores/{Y}/{MD}/m-f-23/">同じ</a><a href="/scores/{Y - 1}/{MD}/g-t-1/">去年</a>')
            links = today_game_links(top, td)
            assert [(x["home"], x["away"]) for x in links] == [("ロッテ", "日本ハム"), ("楽天", "オリックス"), ("広島", "中日")], links
            assert links[0]["url"] == f"https://npb.jp/scores/{Y}/{MD}/m-f-23/index.html"
            assert today_game_links(top, td + dt.timedelta(days=1)) == [] and today_game_links(None, td) == []
            tr = lambda cells: "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"
            hdr = tr([""] + [str(i) for i in range(1, 10)] + ["計", "H", "E"])
            pg = lambda mark, rows: f"<title>試合速報 | NPB.jp 日本野球機構</title><div>{mark}</div><table>{hdr}{''.join(tr(r) for r in rows)}</table>"
            fin = pg("【試合終了】", [["北海道日本ハムファイターズ", 0, 0, 1, 0, 0, 0, 0, 0, 0, 1, 6, 1], ["千葉ロッテマリーンズ", 0, 1, 0, 2, 0, 3, 0, 0, "X", 6, 10, 0]])
            assert parse_game_page(fin, "ロッテ", "日本ハム") == {"status": "end", "inning": "", "score": [6, 1]}
            mid = [["オリックス・バファローズ", 0, 0, 1, 0, 0, 2, 0, "", "", 3, 7, 0], ["東北楽天ゴールデンイーグルス", 1, 0, 0, 0, 0, 0, "", "", "", 1, 4, 1]]
            assert parse_game_page(pg("【7回裏】", mid), "楽天", "オリックス") == {"status": "live", "inning": "7回裏", "score": [1, 3]}
            assert parse_game_page(pg("【７回表】", mid), "楽天", "オリックス")["inning"] == "7回表", "全角の数字も読む"
            assert parse_game_page(pg("", mid), "楽天", "オリックス") == {"status": "live", "inning": "", "score": [1, 3]}, "【】がなくても回が進んでいれば試合中"
            pre = [["オリックス・バファローズ"] + [""] * 9 + ["0", "0", "0"], ["東北楽天ゴールデンイーグルス"] + [""] * 9 + ["0", "0", "0"]]
            assert parse_game_page(pg("【試合前】", pre) + "<a>中止試合のお知らせ</a>", "楽天", "オリックス") == {"status": "", "inning": "", "score": [0, 0]}
            assert parse_game_page(pg("【中止】", []), "広島", "中日") == {"status": "off", "inning": "", "score": None}
            assert parse_game_page("<p>ノーゲーム</p>", "広島", "中日")["status"] == "off" and parse_game_page("", "a", "b")["status"] == ""
            assert team_in("横浜DeNAベイスターズ") == "DeNA" and team_in("広島東洋カープ") == "広島" and team_in("計") == ""
            pages = {NPB_TOP: top, links[0]["url"]: fin, links[1]["url"]: pg("【7回裏】", mid), links[2]["url"]: pg("【中止】", [])}
            calls = []

            def fake(url, params=None, as_json=True):
                calls.append(url)
                return pages.get(url, "").encode("utf-8")
            get = fake
            nosleep = lambda s: None
            t2 = dt.datetime.combine(td, dt.time(18, 0), JST)
            assert mode_live_scores(dt.datetime.combine(td, dt.time(12, 50), JST), nosleep) is False and calls == [], "13時前は読まない"
            assert mode_live_scores(t2, nosleep) is True and len(calls) == 4, calls
            gl = load(os.path.join(DATA, "games_live.json"), {})
            assert gl["day"] == td.isoformat() and set(gl["games"]) == {"ロッテ-日本ハム", "楽天-オリックス", "広島-中日"}, gl
            assert gl["games"]["ロッテ-日本ハム"]["status"] == "end" and gl["games"]["ロッテ-日本ハム"]["score"] == [6, 1]
            assert gl["games"]["楽天-オリックス"]["inning"] == "7回裏" and gl["games"]["広島-中日"]["status"] == "off"
            assert mode_live_scores(t2 + dt.timedelta(minutes=5), nosleep) is False and len(calls) == 4, "9分以内は読まない"
            assert mode_live_scores(t2 + dt.timedelta(minutes=10), nosleep) is True and calls[4:] == [NPB_TOP, links[1]["url"]], "終わった・中止の試合は読まない"
            # アプリの games に重ねる（今日の楽天-オリックスは試合中、広島-中日は中止）
            ug = upcoming_games(td)
            rk = [x for x in ug if x["home"] == "楽天"][0]
            assert rk["status"] == "live" and rk["inning"] == "7回裏" and rk["score"] == [1, 3] and rk["time"] == "14:00", rk
            assert [x for x in ug if x["home"] == "広島"][0]["status"] == "off" and not any(x["home"] == "ロッテ" and x["day"] == d(0) for x in ug)
            assert all("inning" not in x for x in ug if x is not rk) and ug[0]["score"] == [3, 2], "昨日の試合はそのまま"
            get = lambda *a, **k: (_ for _ in ()).throw(OSError("x"))
            assert mode_live_scores(t2 + dt.timedelta(minutes=20), nosleep) is True, "読めなくても止まらない"
            assert load(os.path.join(DATA, "games_live.json"), {})["games"]["楽天-オリックス"]["status"] == "live", "前の結果は残す"
            # 1回の実行で7回まで（トップページ＋試合6つ）。日が変わったら作り直す
            codes = ["m-f", "e-b", "c-d", "g-t", "s-db", "h-l", "f-m"]
            nd = td + dt.timedelta(days=1)
            pages = {NPB_TOP: "".join(f'<a href="/scores/{nd.year}/{nd.month:02d}{nd.day:02d}/{c}-1/">x</a>' for c in codes)}
            calls.clear()
            get = fake
            assert mode_live_scores(dt.datetime.combine(nd, dt.time(18, 0), JST), nosleep) is True and len(calls) == LIVE_MAX_REQ, calls
            assert load(os.path.join(DATA, "games_live.json"), {})["day"] == nd.isoformat() and upcoming_games(td)[1]["status"] == "end", "別の日の速報は重ねない"
            # ---- 球団・リーグ公式チャンネル（RSS。通信はしない） ----
            nowu2 = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
            iso = lambda t: t.isoformat()
            feed = ('<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015"'
                    ' xmlns:media="http://search.yahoo.com/mrss/"><title>ch</title>'
                    f'<entry><yt:videoId>new1</yt:videoId><title>試合ハイライト</title><published>{iso(nowu2 - dt.timedelta(hours=2))}</published>'
                    '<author><name>千葉ロッテマリーンズ</name></author><media:group><media:thumbnail url="https://i.ytimg.com/vi/new1/hqdefault.jpg"/></media:group></entry>'
                    f'<entry><yt:videoId>old1</yt:videoId><title>古い動画</title><published>{iso(nowu2 - dt.timedelta(hours=50))}</published></entry></feed>')
            lotte = [c for c in NEWS_CHANNELS if c["team"] == "ロッテ"][0]
            pf = parse_feed(feed, lotte, nowu2)
            assert [v["id"] for v in pf] == ["new1"], pf
            want = {"id": "new1", "title": "試合ハイライト", "ch": lotte["id"], "chName": "千葉ロッテマリーンズ", "topic": "official", "world": "yakyu",
                    "thumb": "https://i.ytimg.com/vi/new1/hqdefault.jpg", "team": "ロッテ", "published": (nowu2 - dt.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")}
            assert pf[0] == want, pf[0]
            assert len(NEWS_CHANNELS) == 13 and all(c["team"] in TEAM_WORDS or c["team"] == "" for c in NEWS_CHANNELS)
            calls.clear()

            def fake_rss(url, params=None, as_json=True):
                calls.append(url)
                if lotte["id"] in url:
                    return feed.encode("utf-8")
                raise OSError("404")  # ほかのチャンネルは読めない（飛ばす）
            get = fake_rss
            assert mode_rss_news(nowu2) is True and len(calls) == 13, calls
            nj = load(os.path.join(DATA, "news.json"), {})
            assert [v["id"] for v in nj["items"]] == ["new1", "v1"] and nj["items"][0]["team"] == "ロッテ" and nj.get("rssAt"), nj
            assert mode_rss_news(nowu2 + dt.timedelta(minutes=10)) is False and len(calls) == 13, "20分以内は読まない"
            assert mode_rss_news(nowu2 + dt.timedelta(minutes=20)) is True and len(calls) == 26
            get = lambda *a, **k: {"items": []}  # 話題さがし（4時間ごと）でも公式チャンネルの分と rssAt は消えない
            mode_news(nowu2)
            nj = load(os.path.join(DATA, "news.json"), {})
            assert [v["id"] for v in nj["items"]] == ["new1", "v1"] and nj["rssAt"] == (nowu2 + dt.timedelta(minutes=20)).isoformat(), nj
            assert [v["team"] for v in news_for_app()] == ["ロッテ", ""]
            # news_channels.json で置きかえ・追加・使わない
            dump(os.path.join(HERE, "news_channels.json"), [{"id": NEWS_CHANNELS[0]["id"], "off": True}, {"id": "UCnew", "name": "新しい", "team": "阪神"},
                                                             {"id": lotte["id"], "name": "ロッテ2", "team": "?"}, {"id": "bad"}, "x"])
            nc = {c["id"]: c for c in load_news_channels()}
            assert len(nc) == 13 and NEWS_CHANNELS[0]["id"] not in nc and nc["UCnew"] == {"id": "UCnew", "name": "新しい", "team": "阪神", "label": "新しい"}
            assert nc[lotte["id"]]["name"] == "ロッテ2" and nc[lotte["id"]]["team"] == "", nc[lotte["id"]]
            os.remove(os.path.join(HERE, "news_channels.json"))
            build_latest([{"id": "UC1"}])
            lj = load(os.path.join(DATA, "latest.json"), {})
            assert lj["news"][0]["team"] == "ロッテ" and lj["topics"][-1]["id"] == "official"
        finally:
            get = old_get
    finally:
        DATA, SNAP, HERE = old, old_s, old_here
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["live", "full", "discover", "seed", "wide", "schedule", "news"])
    ap.add_argument("--genre", choices=["game", "fan"], default="game", help="--mode wide で集めるジャンル（game＝ゲーム実況、fan＝プロ野球の球団ファン）")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--merge-proposed", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.merge_proposed:
        return merge_proposed()
    if not KEY:
        sys.exit("YT_API_KEY が設定されていません")
    if a.mode == "seed":
        return mode_seed(None)
    if a.mode == "schedule":
        return mode_schedule()
    seeds = [c for c in load(os.path.join(HERE, "channels.json"), []) if not str(c.get("id", "")).startswith(("UCxxxx", "UCyyyy"))]
    if a.mode == "wide":
        return mode_wide(seeds, a.genre)
    seeds = resolve_seeds(seeds)
    if a.mode == "news":  # 速報は配信者がいなくても動く
        mode_news()
        return build_latest(seeds)
    if not seeds:
        sys.exit("channels.json に配信者がいません（まず --mode seed → --merge-proposed）")
    {"live": mode_live, "full": mode_full, "discover": mode_discover}[a.mode](seeds)
    if a.mode != "discover":
        build_latest(seeds)


if __name__ == "__main__":
    main()
