#!/usr/bin/env python3
"""男子体操競技の大会結果を3ソースから収集し data/ に保存する (標準ライブラリのみ)。

  FIG (gymnastics.sport 公開API)  -> data/fig/<id>.json  (順位表をそのまま保存)
  日本体操協会 (jpn-gym.or.jp)    -> 結果PDFへのリンク
  全日本ジュニア体操クラブ連盟 (jr-gym.or.jp) -> 結果PDFへのリンク

何度実行しても安全 (差分更新)。1ソースが失敗しても他ソースは続行する。
"""
import datetime as dt, html, json, re, subprocess, ssl, sys, time, urllib.parse, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
UA = "gym-results-aggregator/1.0 (personal results archive; low-frequency)"
FIG_FROM = "2023-01-01"        # 初回に遡る開始日
REFETCH_DAYS = 30              # 直近この日数以内の大会は結果を再取得
SLEEP = 0.6
TODAY = dt.date.today()


def get(url, binary=False):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(3):
        try:
            try:
                with urllib.request.urlopen(req, timeout=40) as r:
                    body = r.read()
            except urllib.error.URLError as e:
                if not isinstance(e.reason, ssl.SSLCertVerificationError):
                    raise
                # ローカルmacのPythonに証明書が無い場合のみ curl (システム証明書) で取得
                body = subprocess.run(["curl", "-sSfL", "--max-time", "40", "-A", UA, url],
                                      check=True, capture_output=True).stdout
            time.sleep(SLEEP)
            return body if binary else body.decode("utf-8", "replace")
        except Exception:
            if attempt == 2:
                raise
            time.sleep(3 * (attempt + 1))


def jget(url):
    return json.loads(get(url))


def strip(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def load(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def save(path, obj):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def jdate(s):
    m = re.search(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日", s or "")
    return f"{int(m[1]):04d}-{int(m[2]):02d}-{int(m[3]):02d}" if m else None


# ---------------------------------------------------------------- FIG
FIG_API = "https://www.gymnastics.sport/api/sportevents"


def fig_level(agecats):
    n = " ".join(agecats).lower()
    if "junior" in n or "youth" in n:
        return "junior"
    return "senior"


def fig_results(eid):
    cats = jget(f"{FIG_API}/{eid}/agecategories/")["data"]
    out = {}
    for ac in cats:
        if ac["iddiscipline"] != 2:      # 2 = MAG
            continue
        label = ac["name"].replace("Men's Artistic Gymnastics", "").strip() or "Senior"
        for c in jget(f"{FIG_API}/{eid}/categories/?idDiscipline=2")["data"]:
            rows = jget(f"{FIG_API}/{eid}/results/?ageCategory={ac['id']}&category={c['id']}")["data"]
            res = []
            for r in rows:
                names = ", ".join(
                    f"{g['lastname']} {g['firstname']}".strip() for g in r.get("gymnasts", []))
                res.append({"rank": r.get("rankfinal"), "name": names, "country": r.get("country"),
                            "score": r.get("scorefinal"), "q_rank": r.get("rankqualif"),
                            "q_score": r.get("scorequalif")})
            res = [x for x in res if x["rank"] is not None or x["q_rank"] is not None]
            res.sort(key=lambda x: (x["rank"] is None, x["rank"] or 0, x["q_rank"] or 0))
            if res:
                out.setdefault(label, {})[c["name"]] = res
    return out


def scrape_fig(events):
    first = not any(e["source"] == "fig" for e in events.values())
    frm = FIG_FROM if first else (TODAY - dt.timedelta(days=120)).isoformat()
    lst = jget(f"{FIG_API}/?from={frm}&to={TODAY.isoformat()}&discipline=2")["data"]
    n = 0
    for e in lst:
        key = f"fig-{e['id']}"
        old = events.get(key)
        end = e["endevent"]
        recent = (TODAY - dt.date.fromisoformat(end)).days <= REFETCH_DAYS
        meta = {
            "id": key, "source": "fig", "name": e["title"].strip(),
            "start": e["startevent"], "end": end,
            "place": f"{e['city']['name'].title()} ({e['city']['country']['code']})",
            "url": f"https://www.gymnastics.sport/site/events/detail.php?id={e['id']}",
            "has_results": bool(e.get("hasresults")),
            "level": old["level"] if old else "senior", "files": [],
        }
        if e.get("hasresults") and (not old or not old.get("fetched") or recent):
            res = fig_results(e["id"])
            if res:
                save(DATA / "fig" / f"{e['id']}.json", res)
                meta["level"] = fig_level(list(res))
                meta["fetched"] = True
                meta["results_url"] = f"https://www.gymnastics.sport/site/events/results.php?idEvent={e['id']}"
                n += 1
        elif old:
            meta["fetched"] = old.get("fetched", False)
            meta["results_url"] = old.get("results_url")
        events[key] = meta
    print(f"[fig] {len(lst)} events listed, {n} results (re)fetched")


# ---------------------------------------------------------------- JGA
JGA_BASE = "https://jpn-gym.or.jp"
JUNIOR_RE = re.compile(r"ジュニア|U12|U15|U18|小学生|中学|高等学校|高校|ユース|テーブルマーク")
WOMEN_RE = re.compile(r"女子|WAG|women", re.I)
MEN_RE = re.compile(r"男子|MAG|men", re.I)


def is_women_only(text):
    return bool(WOMEN_RE.search(text)) and not MEN_RE.search(text.replace("women", "").replace("Women", ""))


def scrape_jga(events):
    sm = get(f"{JGA_BASE}/event-sitemap.xml")
    urls = sorted(set(re.findall(r"https://jpn-gym\.or\.jp/event/[^\]<\s]+", sm)))
    n = 0
    for u in urls:
        key = "jga-" + u.rstrip("/").split("/")[-1]
        old = events.get(key)
        if old and old.get("settled"):
            continue
        h = get(u)
        cat = strip(re.search(r'class="teaser_type[^"]*"[^>]*>(.*?)</p>', h, re.S).group(1)) \
            if re.search(r'class="teaser_type', h) else ""
        if cat != "体操競技":
            continue
        title = strip(re.search(r'<h1 class="teaser_hd">(.*?)</h1>', h, re.S).group(1))
        if is_women_only(title):
            continue
        per = re.search(r"<th>期間</th>\s*<td>(.*?)</td>", h, re.S)
        per = strip(per.group(1)) if per else ""
        start = jdate(per)
        end = start
        m = re.search(r"[～〜~]\s*(?:(\d{4})年)?\s*(\d{1,2})月\s*(\d{1,2})日", per)
        if m and start:
            end = f"{m[1] or start[:4]}-{int(m[2]):02d}-{int(m[3]):02d}"
        place = re.search(r"<th>場所</th>\s*<td>(.*?)(?:<div|</td>)", h, re.S)
        files = []
        box = re.search(r'<div class="box-result">(.*?)(?=<div class="box01"|</section>)', h, re.S)
        if box:
            for row in re.finditer(r"<tr>\s*<td>(.*?)</td>\s*<td>(.*?)</td>\s*</tr>", box.group(1), re.S):
                label = strip(row.group(1))
                for a in re.finditer(r'href="([^"]+)"', row.group(2)):
                    if is_women_only(label + a.group(1)):
                        continue
                    files.append({"label": label, "url": a.group(1)})
            for a in re.finditer(r'<a href="([^"]+)"[^>]*>(★?大会リザルト速報)</a>', box.group(1)):
                files.append({"label": "大会リザルト速報 (外部)", "url": a.group(1)})
        files = [dict(t) for t in {tuple(f.items()) for f in files}]
        files.sort(key=lambda f: f["label"])
        events[key] = {
            "id": key, "source": "jga", "name": title, "start": start, "end": end,
            "place": strip(place.group(1)) if place else "", "url": u,
            "level": "junior" if JUNIOR_RE.search(title) else "senior", "files": files,
            "has_results": bool(files),
            # 終了後60日たったら再取得しない
            "settled": bool(end and (TODAY - dt.date.fromisoformat(end)).days > 60 and files),
        }
        n += 1
    print(f"[jga] {len(urls)} event pages seen, {n} artistic events (re)parsed")


# ---------------------------------------------------------------- 全日本ジュニア体操クラブ連盟
JR_BASE = "https://www.jr-gym.or.jp"


def scrape_jr(events):
    lst = get(f"{JR_BASE}/tournament/")
    urls = sorted(set(re.findall(r'href="(https://www\.jr-gym\.or\.jp/tournament/[a-z0-9\-]+/)"', lst)))
    n = 0
    for u in urls:
        key = "jr-" + u.rstrip("/").split("/")[-1]
        old = events.get(key)
        h = get(u)
        title = strip(re.search(r"<title>(.*?)\|", h, re.S).group(1))
        per = re.search(r"(\d{4}年\d{1,2}月\d{1,2}日)[^～]*～\s*(\d{4}年\d{1,2}月\d{1,2}日)", strip(h))
        start, end = (jdate(per[1]), jdate(per[2])) if per else (None, None)
        body = re.search(r'id="tab-score_table"(.*?)(?=id="tab-|</main>)', h, re.S)
        files = []
        if body:
            klass = ""
            for tok in re.finditer(r"<h3[^>]*>(.*?)</h3>|<tr[^>]*>(.*?)</tr>", body.group(1), re.S):
                if tok.group(1) is not None:
                    klass = strip(tok.group(1))
                    continue
                tds = re.findall(r"<td[^>]*>(.*?)</td>", tok.group(2), re.S)
                if not tds:
                    continue
                kind = strip(tds[0])
                for td in tds[1:]:
                    for a in re.finditer(r'<a [^>]*href="([^"]+)"[^>]*>(.*?)</a>', td, re.S):
                        if strip(a.group(2)) == "男子":
                            files.append({"label": f"{klass} {kind}".strip(), "url": a.group(1)})
        events[key] = {
            "id": key, "source": "jr", "name": title, "start": start, "end": end,
            "place": "", "url": u, "level": "junior", "files": files,
            "has_results": bool(files),
        }
        n += 1
    print(f"[jr] {n} tournaments parsed")


# ---------------------------------------------------------------- main
def main():
    events = {e["id"]: e for e in load(DATA / "events.json", [])}
    status = load(DATA / "meta.json", {"sources": {}})
    for name, fn in (("fig", scrape_fig), ("jga", scrape_jga), ("jr", scrape_jr)):
        try:
            fn(events)
            status["sources"][name] = {"ok": True, "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
        except Exception as e:  # 1ソースの失敗で全体を止めない
            print(f"[{name}] FAILED: {e!r}", file=sys.stderr)
            status["sources"][name] = {**status["sources"].get(name, {}), "ok": False, "error": repr(e)[:200]}
        save(DATA / "events.json", sorted(events.values(), key=lambda e: e.get("start") or "", reverse=True))
    status["updated"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    save(DATA / "meta.json", status)
    if not any(s.get("ok") for s in status["sources"].values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
