#!/usr/bin/env python3
"""
載せる配信者をえらぶ画面（Mac の中だけで動く。外には公開されない）

  python3 curate.py        → ブラウザで http://localhost:8765 が開く
  候補（channels.proposed.json）と、いま載せている人（channels.json）を並べて表示。
  🎮 ゲーム実況（プロスピ）と ⚾ 野球ファン（球団ファンのYouTuber）の両方を、タブで分けて選べる。
  チェックを付けた人だけを channels.json に保存し、そのまま latest.json を作り直す。
"""
import http.server, json, os, subprocess, sys, threading, webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
PORT = 8765
TEAMS = ["ロッテ", "ソフトバンク", "日本ハム", "楽天", "西武", "オリックス", "巨人", "阪神", "DeNA", "広島", "ヤクルト", "中日"]
GENRES = ["リアタイ", "ガチャ", "純正", "育成", "無課金", "解説", "初心者向け", "イベント周回", "エンジョイ", "査定"]
FAN_TAGS = ["現地観戦", "応援", "試合振り返り", "ニュース・考察", "ドラフト・二軍", "球場グルメ"]  # ⚾ 野球ファンのジャンル（fetch.py の FAN_GENRE_WORDS と同じ）


def genre_of(c):
    return "fan" if (c or {}).get("genre") == "fan" else "game"


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def rows():
    cur = [c for c in load(os.path.join(HERE, "channels.json"), []) if c.get("id") and not str(c["id"]).startswith(("UCxxxx", "UCyyyy"))]
    meta = load(os.path.join(DATA, "channels_meta.json"), {})
    prop = load(os.path.join(HERE, "channels.proposed.json"), [])
    pmap = {p["id"]: p for p in prop}
    out, seen = [], set()
    for c in cur:
        m, p = meta.get(c["id"], {}), pmap.get(c["id"], {})
        out.append({"id": c["id"], "genre": genre_of(c), "name": c.get("name") or m.get("name") or p.get("name", ""), "thumb": m.get("thumb") or p.get("thumb", ""),
                    "handle": m.get("handle") or p.get("handle", ""), "subs": m.get("subs", p.get("subs", 0)), "team_auto": m.get("team_auto", p.get("team_auto", "")),
                    "level": p.get("level", "掲載中"), "rel": p.get("rel"), "n": p.get("n"), "sample": p.get("sample", []),
                    "tags": c.get("tags", []), "team": c.get("team", ""), "streamer": c.get("streamer", False), "all": c.get("all", p.get("level") == "専門"),
                    "on": True, "listed": True})
        seen.add(c["id"])
    for p in prop:
        if p["id"] in seen:
            continue
        g = genre_of(p)  # 野球ファンの人は最初「全部の動画を出す」をOFF（野球の話題の動画だけ出す）
        out.append(dict(p, genre=g, on=p.get("level") == "専門", listed=False, all=p.get("level") == "専門" and g == "game"))
    return out


PAGE = r"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>載せる配信者をえらぶ</title><style>
:root{--ink:#1E1B18;--paper:#FFFBF0;--mustard:#FFC62E;--tomato:#FF5B45;--mint:#4ED1A5;--muted:#5C6670}
*{box-sizing:border-box}body{margin:0;font-family:-apple-system,"Hiragino Sans",sans-serif;background:#A6DBF2;color:var(--ink)}
header{position:sticky;top:0;z-index:5;background:var(--paper);border-bottom:3px solid var(--ink);padding:10px 16px;display:flex;flex-wrap:wrap;gap:8px;align-items:center}
h1{font-size:17px;margin:0 12px 0 0}.tabs button,.b{border:2.5px solid var(--ink);border-radius:99px;background:#fff;padding:4px 12px;font-size:13px;font-weight:700;cursor:pointer}
.tabs button.on{background:var(--ink);color:#fff}.b.save{background:var(--tomato);color:#fff;font-size:15px;padding:7px 18px;margin-left:auto}
input[type=search]{border:2.5px solid var(--ink);border-radius:10px;padding:5px 10px;font-size:14px;min-width:0}
main{max-width:980px;margin:0 auto;padding:14px 16px 80px;display:flex;flex-direction:column;gap:10px}
.row{display:grid;grid-template-columns:auto 56px 1fr;gap:12px;align-items:start;background:#fff;border:2.5px solid var(--ink);border-radius:12px;padding:10px 12px;box-shadow:3px 3px 0 var(--ink)}
.row.off{opacity:.55;box-shadow:none}.row input.pick{width:24px;height:24px;margin-top:14px;accent-color:var(--tomato)}
.row img{width:56px;height:56px;border-radius:50%;border:2.5px solid var(--ink);object-fit:cover;background:#ddd}
.nm{font-weight:800;font-size:15px}.nm a{color:inherit}.meta{font-size:12px;color:var(--muted);margin-top:2px}
.lv{display:inline-block;font-size:11px;border:2px solid var(--ink);border-radius:6px;padding:0 6px;margin-left:6px;font-weight:800}
.gb{display:inline-block;font-size:11px;border:2px solid var(--ink);border-radius:6px;padding:0 6px;margin-left:6px;font-weight:800;background:#E3DBFF}.gb.fan{background:#FFD9CF}
.lv.専門{background:var(--mint)}.lv.多め{background:var(--mustard)}.lv.ときどき{background:#eee}.lv.掲載中{background:#cfe}
.smp{font-size:12px;margin:6px 0 0;padding-left:1.1em;color:#333}.ed{display:flex;flex-wrap:wrap;gap:5px;margin-top:8px;align-items:center}
.ed button{border:2px solid var(--ink);border-radius:8px;background:#fff;font-size:12px;padding:1px 7px;cursor:pointer}.ed button.on{background:var(--mustard)}
.ed select{border:2px solid var(--ink);border-radius:8px;font-size:12px;padding:1px 4px}.ed label{font-size:12px}
#msg{position:fixed;left:50%;bottom:16px;transform:translateX(-50%);background:var(--ink);color:#fff;padding:12px 16px;border-radius:12px;max-width:92%;white-space:pre-wrap;font-size:13px;display:none}
.b.save:disabled{background:#999}.hint{font-size:12px;color:var(--ink);background:#FFF6D6;border:2px solid var(--ink);border-radius:10px;padding:8px 12px;line-height:1.7}
</style></head><body>
<header><h1>載せる配信者をえらぶ</h1><span class="tabs" id="gtabs"></span><span class="tabs" id="tabs"></span><input type="search" id="q" placeholder="名前で絞る"><span id="cnt" style="font-size:13px;font-weight:700"></span><button class="b save" id="save">この内容で保存</button></header>
<main><div class="hint">チェックを付けた人だけがアプリに載ります。載せるのは2種類：🎮「ゲーム」＝プロスピの実況・配信をする人、⚾「野球ファン」＝本物のプロ野球の球団ファンのYouTuber（現地観戦・応援・試合の振り返り・ニュース考察・ドラフト・球場グルメなど）。上のタブで分けて見られます。種類がちがう人は、行の中で切りかえられます。<br>「専門」＝最近の動画の7割以上がその種類の動画（ゲーム＝プロスピ、野球ファン＝ゲームでないプロ野球の話題）、「多め」＝4割以上、「ときどき」＝それより少ない。<br>ジャンル・球団・「配信する人」は、アプリの検索やおすすめに使われます（あとから変えてもOK）。球団は「自動」のままなら、チャンネル名・説明・動画タイトルから推定して付けます（はっきりしない人には付けません）。<br>アプリに出る動画・ライブは、ゲームの人はタイトルにプロスピが入ったものだけ、野球ファンの人はプロ野球の話題のタイトルだけ。「全部の動画を出す」をONにした人は、タイトルに関係なく全部出ます（最初はゲームの「専門」の人だけON）。<br>途中の選択は自動で残ります。保存が終わるまでターミナルは閉じないでください。</div><div id="list" style="display:flex;flex-direction:column;gap:10px"></div></main>
<div id="msg"></div><div id="down" style="display:none;position:fixed;top:0;left:0;right:0;z-index:9;background:#B3261E;color:#fff;padding:10px 16px;font-weight:700;font-size:14px">ターミナルが止まっているので保存できません。ターミナルで curate を動かし直してから、このタブで保存してください（選んだ内容は残っています）</div>
<script>
const R=__ROWS__,TEAMS=__TEAMS__,GENRES=__GENRES__,FAN_TAGS=__FAN_TAGS__;let tab="全部",gtab="両方";
const GN={game:"ゲーム",fan:"野球ファン"},tagsOf=g=>g==="fan"?FAN_TAGS:GENRES;
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fmt=n=>n>=10000?(Math.round(n/1000)/10)+"万":Number(n||0).toLocaleString();
function draw(){const q=document.getElementById("q").value.trim().toLowerCase();
 const GT=["両方","ゲーム","野球ファン"];document.getElementById("gtabs").innerHTML=GT.map(t=>`<button class="${t===gtab?"on":""}" data-gt="${t}">${t==="ゲーム"?"🎮 ":t==="野球ファン"?"⚾ ":""}${t}</button>`).join(" ")+" ｜";
 document.querySelectorAll("#gtabs button").forEach(b=>b.onclick=()=>{gtab=b.dataset.gt;draw()});
 const T=["全部","チェック中","掲載中","専門","多め","ときどき"];document.getElementById("tabs").innerHTML=T.map(t=>`<button class="${t===tab?"on":""}" data-t="${t}">${t}</button>`).join(" ");
 document.querySelectorAll("#tabs button").forEach(b=>b.onclick=()=>{tab=b.dataset.t;draw()});
 const list=R.filter(r=>gtab==="両方"||GN[r.genre||"game"]===gtab).filter(r=>tab==="全部"||(tab==="チェック中"?r.on:tab==="掲載中"?r.listed:r.level===tab)).filter(r=>!q||(r.name+r.handle).toLowerCase().includes(q));
 const on=R.filter(r=>r.on),nf=on.filter(r=>r.genre==="fan").length;
 document.getElementById("cnt").textContent=`載せる：${on.length}人（ゲーム${on.length-nf}・野球ファン${nf}／全${R.length}人）`;
 document.getElementById("list").innerHTML=list.map(r=>{const i=R.indexOf(r);return`<div class="row ${r.on?"":"off"}"><input type="checkbox" class="pick" data-i="${i}" ${r.on?"checked":""} aria-label="載せる">
  <img src="${esc(r.thumb)}" alt="" referrerpolicy="no-referrer" onerror="this.style.visibility='hidden'">
  <div><div class="nm"><a href="https://www.youtube.com/channel/${esc(r.id)}" target="_blank">${esc(r.name)}</a><span class="gb ${r.genre==="fan"?"fan":""}">${r.genre==="fan"?"⚾ 野球ファン":"🎮 ゲーム"}</span><span class="lv ${esc(r.level)}">${esc(r.level)}</span></div>
  <div class="meta">${r.handle?"@"+esc(r.handle)+"・":""}登録者 ${fmt(r.subs)}${r.n?`・最近${r.n}本のうち${r.genre==="fan"?"野球の話題":"プロスピ"} ${r.rel}本`:""}</div>
  ${r.sample&&r.sample.length?`<ul class="smp">${r.sample.map(t=>`<li>${esc(t)}</li>`).join("")}</ul>`:""}
  ${r.on?`<div class="ed"><select data-genre="${i}"><option value="game" ${r.genre!=="fan"?"selected":""}>🎮 ゲーム</option><option value="fan" ${r.genre==="fan"?"selected":""}>⚾ 野球ファン</option></select>${tagsOf(r.genre).map(g=>`<button data-g="${g}" data-i="${i}" class="${(r.tags||[]).includes(g)?"on":""}">${g}</button>`).join("")}
   <select data-team="${i}"><option value="" ${!r.team?"selected":""}>球団：自動${r.team_auto?"（"+esc(r.team_auto)+"）":"（なし）"}</option><option value="-" ${r.team==="-"?"selected":""}>球団：付けない</option>${TEAMS.map(t=>`<option ${r.team===t?"selected":""}>${t}</option>`).join("")}</select>
   <label><input type="checkbox" data-st="${i}" ${r.streamer?"checked":""}>配信する人</label>
   <label title="OFFのときは、ゲームの人はタイトルにプロスピが入った動画だけ、野球ファンの人はプロ野球の話題の動画だけ"><input type="checkbox" data-all="${i}" ${r.all?"checked":""}>全部の動画を出す</label></div>`:""}</div></div>`}).join("")||'<p>該当なし</p>';
 document.querySelectorAll(".pick").forEach(x=>x.onchange=()=>{R[x.dataset.i].on=x.checked;if(x.checked&&R[x.dataset.i].all==null)R[x.dataset.i].all=R[x.dataset.i].level==="専門";draw()});
 document.querySelectorAll("[data-g]").forEach(x=>x.onclick=()=>{const r=R[x.dataset.i];r.tags=r.tags||[];r.tags=r.tags.includes(x.dataset.g)?r.tags.filter(t=>t!==x.dataset.g):[...r.tags,x.dataset.g];draw()});
 document.querySelectorAll("[data-genre]").forEach(x=>x.onchange=()=>{const r=R[x.dataset.genre];r.genre=x.value;r.tags=(r.tags||[]).filter(t=>tagsOf(r.genre).includes(t));draw()});
 document.querySelectorAll("[data-team]").forEach(x=>x.onchange=()=>{R[x.dataset.team].team=x.value;keep()});
 document.querySelectorAll("[data-st]").forEach(x=>x.onchange=()=>{R[x.dataset.st].streamer=x.checked;keep()});
 document.querySelectorAll("[data-all]").forEach(x=>x.onchange=()=>{R[x.dataset.all].all=x.checked;keep()});keep()}
/* 途中の選択はブラウザに自動で保存（タブを閉じても消えない。保存が終わったら消す） */
const LS="curate-draft";function keep(){try{localStorage.setItem(LS,JSON.stringify(R.map(r=>({id:r.id,on:r.on,genre:r.genre,tags:r.tags,team:r.team,streamer:r.streamer,all:r.all}))))}catch(e){}}
try{const d=JSON.parse(localStorage.getItem(LS)||"null");if(d){const m=new Map(d.map(x=>[x.id,x]));R.forEach(r=>{const x=m.get(r.id);if(x)Object.assign(r,x)})}}catch(e){}
/* ターミナルが止まっていたら知らせる */
let alive=true;async function ping(){try{const r=await fetch("/ping",{cache:"no-store"});alive=r.ok}catch(e){alive=false}
 document.getElementById("down").style.display=alive?"none":"block";document.getElementById("save").disabled=!alive}
setInterval(ping,4000);
document.getElementById("q").oninput=draw;
function msg(t){const m=document.getElementById("msg");m.textContent=t;m.style.display="block"}
document.getElementById("save").onclick=async()=>{const pick=R.filter(r=>r.on).map(r=>({id:r.id,name:r.name,genre:r.genre==="fan"?"fan":"game",tags:r.tags||[],team:r.team||"",streamer:!!r.streamer,all:!!r.all}));
 if(!pick.length){msg("1人以上チェックしてください");return}
 msg(`${pick.length}人で保存して、データを作り直しています…（1〜3分）`);
 try{const res=await fetch("/save",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(pick)});const t=await res.text();msg(t);R.forEach(r=>r.listed=r.on);try{localStorage.removeItem(LS)}catch(e){}}catch(e){msg("保存できませんでした："+e)}};
draw();
</script></body></html>"""


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="text/plain; charset=utf-8"):
        b = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/ping":
            return self.send(200, "ok")
        if self.path not in ("/", "/index.html"):
            return self.send(404, "not found")
        page = PAGE.replace("__ROWS__", json.dumps(rows(), ensure_ascii=False)).replace("__TEAMS__", json.dumps(TEAMS, ensure_ascii=False)).replace("__GENRES__", json.dumps(GENRES, ensure_ascii=False)).replace("__FAN_TAGS__", json.dumps(FAN_TAGS, ensure_ascii=False))
        self.send(200, page, "text/html; charset=utf-8")

    def do_POST(self):
        if self.path != "/save" or self.headers.get("Origin", f"http://localhost:{PORT}") not in (f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"):
            return self.send(403, "forbidden")
        try:
            pick = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"[]")
            clean = [{"id": str(c["id"]), "name": str(c.get("name", ""))[:80], "genre": genre_of(c),
                      "tags": [t for t in c.get("tags", []) if t in (FAN_TAGS if genre_of(c) == "fan" else GENRES)],
                      "team": c.get("team") if c.get("team") in TEAMS or c.get("team") == "-" else "", "streamer": bool(c.get("streamer")), "all": bool(c.get("all"))}
                     for c in pick if str(c.get("id", "")).startswith("UC")]
        except Exception as e:
            return self.send(400, f"読み取れませんでした：{e}")
        # 前は載せていたのにチェックを外した人は blocked.json へ（自動の追加でまた入らないように）。チェックした人は blocked から外す
        before = {c.get("id") for c in load(os.path.join(HERE, "channels.json"), [])}
        now_ids = {c["id"] for c in clean}
        blocked = set(load(os.path.join(HERE, "blocked.json"), [])) | (before - now_ids)
        blocked -= now_ids
        with open(os.path.join(HERE, "blocked.json"), "w", encoding="utf-8") as f:
            json.dump(sorted(x for x in blocked if x), f, ensure_ascii=False, indent=1)
        with open(os.path.join(HERE, "channels.json"), "w", encoding="utf-8") as f:
            json.dump(clean, f, ensure_ascii=False, indent=1)
        nf = sum(1 for c in clean if c["genre"] == "fan")
        lines = [f"channels.json に {len(clean)}人（ゲーム {len(clean) - nf}・野球ファン {nf}）を保存しました（外した人は blocked.json に入れて、自動では二度と追加しません）。GitHub には channels.json と blocked.json を上げてください。"]
        if os.environ.get("YT_API_KEY"):
            for mode in ("full", "live"):
                r = subprocess.run([sys.executable, os.path.join(HERE, "fetch.py"), "--mode", mode], capture_output=True, text=True)
                lines.append((r.stdout or "").strip() or (r.stderr or "").strip()[-300:])
            lines.append(f"できました：{os.path.join(DATA, 'latest.json')}\nアプリの黒い帯から、このファイルを読み込み直してください。")
        else:
            lines.append("APIキーが無いので、データの作り直しはしていません。 bash start_mac.sh の 3/4・4/4 と同じく、fetch.py --mode full と --mode live を動かしてください。")
        self.send(200, "\n".join(lines))


if __name__ == "__main__":
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H)
    url = f"http://localhost:{PORT}"
    print(f"えらぶ画面：{url}（終わったらこのターミナルで control + C）")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n終了しました")
