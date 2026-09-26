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
DISCOVER_QUERIES = ["プロスピA", "プロ野球スピリッツA", "プロスピA リアタイ"]
# 広く集める（--mode wide）：プロスピ専門でなくても、最近プロスピの動画を出しているチャンネルを候補にする。載せるかは人が選ぶ
WIDE_QUERIES = ["プロスピA", "プロスピ リアタイ", "プロスピ ガチャ", "プロスピ 無課金", "プロスピ 純正", "プロスピ 育成",
                "プロスピ 生配信", "プロ野球スピリッツA", "プロスピ 初心者", "プロスピ オーダー", "プロスピ イベント", "プロスピ OB"]
WIDE_PAGES = 2          # 1つの言葉につき何ページ（50件ずつ）さがすか。search.list は1回100ユニット
WIDE_MIN_RELATED = 2    # 最近15本のうち、プロスピの動画が何本以上あれば候補にするか
WIDE_MIN_SUBS = 100     # 登録者がこれより少ないチャンネルは候補にしない


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


def shown(v, all_ids):
    """アプリに出す動画か：プロスピのタイトル、または「全部の動画を出す」にした配信者の動画"""
    return v["ch"] in all_ids or is_related(v["title"])


def all_ids_of(seeds):
    return {c["id"] for c in seeds if c.get("all")}


def senmon_ratio(titles):
    """タイトルのうちプロスピ関連の割合"""
    return sum(1 for t in titles if is_related(t)) / len(titles) if titles else 0.0


def review_list(all_vids, meta):
    """掲載中なのに、最近プロスピの動画を出していないチャンネル（人が確認して外すか決める）"""
    by = {}
    for v in all_vids:
        by.setdefault(v["ch"], []).append(v["title"])
    out = []
    for cid, titles in by.items():
        r = senmon_ratio(titles)
        if len(titles) >= 3 and r == 0:  # 最近プロスピの動画を出していない
            out.append({"id": cid, "name": meta.get(cid, {}).get("name", ""), "ratio": round(r, 2), "sample": titles[:3]})
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
        chans.append(dict(m, id=cid, tags=s.get("tags", []), team=s.get("team", ""), streamer=s.get("streamer", False),
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
    al = all_ids_of(seeds)
    live = sorted([v for v in vids.values() if v["state"] == "live" and shown(v, al)], key=lambda v: -(v["viewers"] or 0))
    upcoming = []
    for v in vids.values():
        if v["state"] == "upcoming" and v["start"] and shown(v, al):
            st = dt.datetime.fromisoformat(v["start"].replace("Z", "+00:00"))
            if now - dt.timedelta(hours=1) <= st <= now + dt.timedelta(hours=24):
                upcoming.append(v)
    upcoming.sort(key=lambda v: v["start"])
    recent = [v for v in vids.values() if v["state"] == "none" and shown(v, al)]
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
    dump(os.path.join(DATA, "channels_meta.json"), meta)
    ids = []
    for c in seeds:
        ids += rss_recent(c["id"], 5)
    all_vids = list(api_videos(list(dict.fromkeys(ids))).values())
    dump(os.path.join(DATA, "review.json"), review_list(all_vids, meta))
    al = all_ids_of(seeds)
    dump(os.path.join(DATA, "full.json"), {"videos": [v for v in all_vids if shown(v, al)]})
    prune_old(today)


def mode_discover(seeds):
    known = {c["id"] for c in seeds}
    cands = load(os.path.join(DATA, "candidates.json"), {})
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for q in DISCOVER_QUERIES:  # search.list は1回100ユニット
        res = get(API + "search", {"part": "snippet", "q": q, "type": "video", "order": "date", "regionCode": "JP",
                                   "relevanceLanguage": "ja", "publishedAfter": since, "maxResults": 50})
        for it in res.get("items", []):
            cid = it["snippet"]["channelId"]
            if cid in known:
                continue
            c = cands.setdefault(cid, {"name": it["snippet"]["channelTitle"], "hits": 0, "sample": it["snippet"]["title"]})
            c["hits"] += 1
    for cid, c in cands.items():
        if c["hits"] >= 2 and "ratio" not in c:
            c["ratio"] = round(related_ratio(cid)[0], 2)  # RSSなので割り当てを使わない
            c["senmon"] = c["ratio"] >= SENMON_RATIO
    dump(os.path.join(DATA, "candidates.json"), cands)
    print(f"candidates: {len(cands)}（channels.json に追加するかは人が確認）")


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


def mode_wide(seeds):
    """プロスピの動画を出しているチャンネルを広く集めて channels.proposed.json に入れる（載せるかは curate で人が選ぶ）"""
    known = {c["id"] for c in seeds}
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=180)).strftime("%Y-%m-%dT%H:%M:%SZ")
    found = {}
    for i, q in enumerate(WIDE_QUERIES):
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
        found.setdefault(cid, c.get("name", ""))
    meta = api_channels([c for c in found if c not in known])
    prop_path = os.path.join(HERE, "channels.proposed.json")
    old = {c["id"]: c for c in load(prop_path, [])}
    out = []
    for k, (cid, m) in enumerate(meta.items()):
        if m["subs"] < WIDE_MIN_SUBS:
            continue
        titles = recent_titles(cid)
        rel = [t for t in titles if is_related(t)]
        if len(rel) < WIDE_MIN_RELATED:
            continue
        o = old.get(cid, {})
        out.append({"id": cid, "name": m["name"], "handle": m["handle"], "thumb": m["thumb"], "subs": m["subs"],
                    "rel": len(rel), "n": len(titles), "level": level_of(len(rel), len(titles)), "sample": rel[:3],
                    "tags": o.get("tags", []), "team": o.get("team", ""), "streamer": o.get("streamer", False),
                    "check": "OK" if level_of(len(rel), len(titles)) == "専門" else ""})
        if k % 25 == 24:
            print(f"  確認中… {k + 1}/{len(meta)}", flush=True)
    out.sort(key=lambda c: ({"専門": 0, "多め": 1, "ときどき": 2}[c["level"]], -c["subs"]))
    dump(prop_path, out)
    cnt = {lv: sum(1 for c in out if c["level"] == lv) for lv in ("専門", "多め", "ときどき")}
    print(f"候補 {len(out)}件（専門 {cnt['専門']}・多め {cnt['多め']}・ときどき {cnt['ときどき']}）。次に curate で選んでください")


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
        cur.append({"id": c["id"], "name": c["name"], "tags": c.get("tags", []), "team": c.get("team", ""), "streamer": c.get("streamer", False)})
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
    assert level_of(12, 15) == "専門" and level_of(7, 15) == "多め" and level_of(2, 15) == "ときどき"
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
    finally:
        DATA, SNAP = old, old_s
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["live", "full", "discover", "seed", "wide"])
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
    seeds = [c for c in load(os.path.join(HERE, "channels.json"), []) if not str(c.get("id", "")).startswith(("UCxxxx", "UCyyyy"))]
    if a.mode == "wide":
        return mode_wide(seeds)
    seeds = resolve_seeds(seeds)
    if not seeds:
        sys.exit("channels.json に配信者がいません（まず --mode seed → --merge-proposed）")
    {"live": mode_live, "full": mode_full, "discover": mode_discover}[a.mode](seeds)
    if a.mode != "discover":
        build_latest(seeds)


if __name__ == "__main__":
    main()
