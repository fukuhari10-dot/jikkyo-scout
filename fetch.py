#!/usr/bin/env python3
"""
チューバースカウト データ収集スクリプト（標準ライブラリのみ）

使い方:
  YT_API_KEY=xxxx python fetch.py --mode live       # 10分ごと: ライブ中・配信予定・新着動画
  YT_API_KEY=xxxx python fetch.py --mode full       # 1時間ごと: 登録者数・動画の再生数・トピック・大台達成
  YT_API_KEY=xxxx python fetch.py --mode discover   # 1日1回: 新しい配信者の候補をさがす（人が確認してから channels.json に追加）
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

channels.json には "id"（UC…）か "handle"（@…）のどちらかを書けばよい。
"genre" は "game"（ゲーム実況・プロスピ）か "fan"（プロ野球の球団ファン）。書かなければ "game"。

YouTube の規約（API Developer Policies）に合わせていること:
  - 登録者数・再生数などは YouTube の公開値をそのまま出す。増加数・伸び率などの独自の指標は作らない（III.E.4.h）
  - 複数チャンネルの数値を合計・集計しない（III.E.2）
  - 過去の登録者数の記録は持たない。予想用の記録も30日で消す（III.E.4）
"""
import argparse, datetime as dt, json, os, sys, urllib.parse, urllib.request
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


def parse_npb_schedule(html, year):
    """NPB の月別日程ページ（表）から [{day,time,home,away,place}] を取り出す。表の書き方が多少変わっても読めるよう、行ごとの文字で判断する"""
    import re
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

    p = T(); p.feed(html)
    out, cur = [], None
    for cells in p.rows:
        text = " ".join(cells)
        dm = re.search(r"(\d{1,2})/(\d{1,2})", cells[0] if cells else "")
        if dm:
            try: cur = dt.date(year, int(dm.group(1)), int(dm.group(2)))
            except ValueError: cur = None
        m = re.search(rf"({_TEAMS_RE})\s*(?:\d+\s*)?[-－―ー–]\s*(?:\d+\s*)?({_TEAMS_RE})", text)
        if not (cur and m) or m.group(1) == m.group(2):
            continue
        tm = re.search(r"([^\s|｜\d][^|｜]*?)\s*(\d{1,2}:\d{2})", text[m.end():])
        place, time = (tm.group(1).strip()[-12:], tm.group(2).zfill(5)) if tm else ("", "")
        out.append({"day": cur.isoformat(), "time": time, "home": m.group(1), "away": m.group(2), "place": place,
                    "off": "中止" in text or "延期" in text})
    return out


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


def upcoming_games(today, days=7):
    """自動の日程（NPB）に、games.csv の手入力を上書きで足す（同じ日・同じ球団は手入力を優先）"""
    auto = [g for g in load(os.path.join(DATA, "games_npb.json"), {}).get("games", []) if not g.get("off")]
    auto = [g for g in auto if 0 <= (dt.date.fromisoformat(g["day"]) - today).days < days]
    manual, _ = read_games(os.path.join(HERE, "games.csv"), today, days)
    key = lambda g: (g["day"], g["home"])
    merged = {key(g): {k: g[k] for k in ("day", "time", "home", "away", "place")} for g in auto}
    for g in manual:
        merged[key(g)] = g
    return sorted(merged.values(), key=lambda g: (g["day"], g["time"]))


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
        "games": upcoming_games(today_jst()),  # これから7日の試合日程（NPBの公開日程から自動＋games.csv の手入力）
    }
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
    import tempfile
    global DATA, SNAP
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
<tr><td>10/8（木）</td><td>セ・CSファーストS</td><td></td></tr></table>"""
    g = parse_npb_schedule(html, 2026)
    assert [(x["day"], x["home"], x["away"], x["time"]) for x in g] == [("2026-10-01", "阪神", "巨人", "18:00"), ("2026-10-01", "ロッテ", "日本ハム", "18:00"), ("2026-10-02", "ソフトバンク", "西武", "18:00"), ("2026-10-03", "DeNA", "中日", "14:00")], g
    assert g[0]["place"] == "甲子園" and g[1]["place"] == "ZOZOマリン" and g[2]["off"] is True
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
    old, old_s = DATA, SNAP
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
    finally:
        DATA, SNAP = old, old_s
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["live", "full", "discover", "seed", "wide", "schedule"])
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
    if not seeds:
        sys.exit("channels.json に配信者がいません（まず --mode seed → --merge-proposed）")
    {"live": mode_live, "full": mode_full, "discover": mode_discover}[a.mode](seeds)
    if a.mode != "discover":
        build_latest(seeds)


if __name__ == "__main__":
    main()
