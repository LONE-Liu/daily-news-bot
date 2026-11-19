#!/usr/bin/env python3
"""每日简报：世界头条 + 外媒看中国 + 国内要闻，中文呈现 + AI 语音朗读。

流程（GitHub Actions）:
    抓 RSS → DeepSeek 翻译英文条目 → 生成中文 Markdown
    → edge-tts 合成语音 mp3 + 播放页 → 推送到个人微信（Server酱）

用法:
    python3 daily_news.py --dry-run                 # 抓取+翻译+打印，不合成不推送
    python3 daily_news.py --out brief.md --site site  # 产出 brief.md 与 site/index.html、site/latest.mp3
    python3 daily_news.py --send brief.md           # 读取 brief.md 并推送到微信

环境变量:
    SCT_KEY              Server酱 SendKey（SCT 开头）
    DEEPSEEK_API_KEY     DeepSeek API key（缺失则跳过翻译，保留英文）
    SITE_URL             语音播放页地址，默认 https://LONE-Liu.github.io/daily-news-bot
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

FEEDS = [
    {
        "section": "世界头条",
        "limit": 15,
        "sources": [
            ("BBC", "http://feeds.bbci.co.uk/news/world/rss.xml", 3),
            ("The Guardian", "https://www.theguardian.com/world/rss", 3),
            ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml", 3),
            ("DW", "https://rss.dw.com/rdf/rss-en-world", 3),
            ("France 24", "https://www.france24.com/en/rss", 3),
        ],
    },
    {
        "section": "外媒看中国",
        "limit": 5,
        "sources": [
            ("DW 中文", "https://rss.dw.com/rdf/rss-chi-all", 2),
            ("The Guardian", "https://www.theguardian.com/world/china/rss", 2),
            ("BBC 中文", "https://feeds.bbci.co.uk/zhongwen/simp/rss.xml", 1),
        ],
    },
    {
        "section": "国内 · 时政",
        "limit": 2,
        "sources": [("中新网", "https://www.chinanews.com.cn/rss/china.xml", 2)],
    },
    {
        "section": "国内 · 财经",
        "limit": 2,
        "sources": [("中新网", "https://www.chinanews.com.cn/rss/finance.xml", 2)],
    },
    {
        "section": "国内 · 科技",
        "limit": 2,
        "sources": [("IT之家", "https://www.ithome.com/rss/", 2)],
    },
    {
        "section": "国内 · 社会",
        "limit": 2,
        "sources": [("中新网", "https://www.chinanews.com.cn/rss/society.xml", 2)],
    },
]

MAX_ITEMS = 30
MAX_AGE_HOURS = 36
TITLE_MAX = 60
SUMMARY_MAX = 80
UA = "Mozilla/5.0 (compatible; daily-news-bot/3.0)"
VOICE = "zh-CN-XiaoxiaoNeural"
SITE_URL = os.environ.get("SITE_URL", "https://lone-liu.github.io/daily-news-bot")
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"

FOCUS = ("中国", "China", "BIM", "建筑", "工程", "EPC", "房地产")
NOISE = (
    "喜加一", "免费领取", "限时免费", "优惠券", "打折", "促销", "秒杀",
    "开奖", "中奖", "彩票", "招聘启事", "更正", "讣告",
)

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
PUNCT_RE = re.compile(r"[\s\u3000,，.。!！?？:：;；、\"'“”‘’（）()\[\]【】《》<>·—\-_|/\\]+")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def clean(text):
    if not text:
        return ""
    text = TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return WS_RE.sub(" ", text).strip()


def clip(text, limit):
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    head = text[:limit]
    for sep in ("。", "！", "？", ". ", "! ", "? "):
        idx = head.rfind(sep)
        if idx >= limit // 2:
            return head[: idx + len(sep)].strip()
    return head.rstrip("，、,;；:： ") + "…"


def norm_key(title):
    return PUNCT_RE.sub("", (title or "").lower())


def is_noise(title):
    return any(word in title for word in NOISE)


def is_cjk(text):
    return bool(CJK_RE.search(text or ""))


def parse_date(raw):
    if not raw:
        return None
    raw = raw.strip()
    try:
        return parsedate_to_datetime(raw)
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def normalize_dt(d):
    if d is None:
        return None
    if d.tzinfo is not None:
        return d.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return d


def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def parse_feed(raw):
    root = ET.fromstring(raw)
    items = []
    for node in root.iter():
        tag = node.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        title = summary = link = pub = None
        for child in node:
            name = child.tag.split("}")[-1]
            if name == "title":
                title = child.text
            elif name in ("description", "summary", "content") and summary is None:
                summary = child.text
            elif name == "link":
                href = child.attrib.get("href")
                if link is None or child.attrib.get("rel") in (None, "alternate"):
                    link = href or child.text
            elif name in ("pubDate", "date", "published", "updated"):
                pub = child.text
        items.append((clean(title), clean(summary), (link or "").strip(), parse_date(pub)))
    return items


def collect():
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    cutoff = now - dt.timedelta(hours=MAX_AGE_HOURS)
    seen = set()
    result = []
    errors = []

    for spec in FEEDS:
        picked = []
        for label, url, per_source in spec["sources"]:
            if len(picked) >= spec["limit"]:
                break
            try:
                entries = parse_feed(fetch(url))
            except Exception as e:
                errors.append("%s / %s: %s" % (spec["section"], label, type(e).__name__))
                continue

            dated = [(t, s, l, normalize_dt(d)) for (t, s, l, d) in entries]
            dated.sort(key=lambda x: x[3] or dt.datetime.min, reverse=True)
            dated = ([x for x in dated if any(k in x[0] for k in FOCUS)]
                     + [x for x in dated if not any(k in x[0] for k in FOCUS)])

            taken = 0
            for title, summary, link, when in dated:
                if taken >= per_source or len(picked) >= spec["limit"]:
                    break
                if not title or not link or is_noise(title):
                    continue
                if when is not None and when < cutoff:
                    continue
                key = norm_key(title)
                if not key or key in seen:
                    continue
                seen.add(key)
                taken += 1
                picked.append({
                    "source": label,
                    "title": clip(title, TITLE_MAX),
                    "summary": clip(summary, SUMMARY_MAX),
                    "link": link,
                })
        if picked:
            result.append({"section": spec["section"], "items": picked})

    total = sum(len(g["items"]) for g in result)
    for group in reversed(result):
        while total > MAX_ITEMS and group["items"]:
            group["items"].pop()
            total -= 1
        if total <= MAX_ITEMS:
            break
    result = [g for g in result if g["items"]]
    return result, errors


def translate(groups, key):
    """把英文条目翻译成简体中文，原地替换。失败则保留原文。"""
    flat = []
    for g in groups:
        for it in g["items"]:
            if not is_cjk(it["title"]):
                flat.append(it)
    if not flat or not key:
        return 0, 0, None  # 无需翻译

    payload = [{"title": it["title"], "summary": it["summary"]} for it in flat]
    prompt = (
        "把下面 JSON 数组里每个对象的 title 和 summary 翻译成简体中文，"
        "保留 JSON 结构，只输出 JSON，不要任何解释或额外文字：\n"
        + json.dumps(payload, ensure_ascii=False)
    )
    body = json.dumps({
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(DEEPSEEK_URL, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
        content = data["choices"][0]["message"]["content"].strip()
        content = re.sub(r"^```(json)?\s*|\s*```$", "", content).strip()
        translated = json.loads(content)
        for it, tr in zip(flat, translated):
            if tr.get("title"):
                it["title"] = clip(clean(tr["title"]), TITLE_MAX)
            if tr.get("summary"):
                it["summary"] = clip(clean(tr["summary"]), SUMMARY_MAX)
        return len(flat), len(translated), None
    except Exception as e:
        return len(flat), 0, "翻译失败，保留原文: %s" % type(e).__name__


def build_markdown(groups, today, with_voice=True):
    lines = ["## 每日简报 %s" % today]
    if with_voice:
        lines.append("")
        lines.append("> 🔊 [点击收听今日 AI 语音版](%s)" % SITE_URL)
    for group in groups:
        lines.append("")
        lines.append("### %s" % group["section"])
        last_source = None
        for it in group["items"]:
            if it["source"] != last_source:
                lines.append("**%s**" % it["source"])
                last_source = it["source"]
            entry = "- [%s](%s)" % (it["title"], it["link"])
            if it["summary"]:
                entry += " —— %s" % it["summary"]
            lines.append(entry)
    return "\n".join(lines).rstrip() + "\n"


def build_tts_text(groups):
    parts = ["每日简报。"]
    for g in groups:
        parts.append(g["section"] + "。")
        for it in g["items"]:
            parts.append(it["title"] + "。" + ((it["summary"] or "") + "。"))
    return "\n".join(parts)


def synth(text, out_mp3):
    fd, tf = tempfile.mkstemp(suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    try:
        subprocess.run(
            [sys.executable, "-m", "edge_tts", "--voice", VOICE, "--file", tf, "--write-media", out_mp3],
            check=True, capture_output=True,
        )
    finally:
        os.unlink(tf)


def build_site(groups, today, site_dir):
    os.makedirs(site_dir, exist_ok=True)
    tts_text = build_tts_text(groups)
    synth(tts_text, os.path.join(site_dir, "latest.mp3"))

    body = []
    for g in groups:
        body.append("<h2>%s</h2>" % html.escape(g["section"]))
        last_source = None
        for it in g["items"]:
            if it["source"] != last_source:
                body.append('<h3 class="src">%s</h3>' % html.escape(it["source"]))
                last_source = it["source"]
            body.append(
                '<p><a href="%s">%s</a> — %s</p>'
                % (html.escape(it["link"]), html.escape(it["title"]), html.escape(it["summary"] or ""))
            )
    page = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>每日简报 %(today)s</title>
<style>
body{font-family:-apple-system,system-ui,sans-serif;max-width:720px;margin:0 auto;padding:16px 20px 60px;color:#1a1a1a;line-height:1.7}
h1{font-size:20px} h2{font-size:16px;border-bottom:1px solid #eee;padding-bottom:6px;margin-top:22px}
h3.src{font-size:13px;color:#888;margin:12px 0 4px} p{font-size:14px;margin:6px 0}
.audio{position:sticky;top:0;background:#fff;padding:10px 0;border-bottom:1px solid #eee}
a{color:#0a66c2;text-decoration:none}
</style></head><body>
<div class="audio"><audio controls autoplay src="latest.mp3" style="width:100%%"></audio></div>
<h1>每日简报 %(today)s · AI 语音版</h1>
%(body)s
</body></html>""" % {"today": today, "body": "\n".join(body)}
    with open(os.path.join(site_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)


def push(markdown, title, key):
    url = "https://sctapi.ftqq.com/%s.send" % key
    body = urllib.parse.urlencode({"title": title[:32], "desp": markdown}).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out")
    ap.add_argument("--site")
    ap.add_argument("--send")
    args = ap.parse_args()

    tz = dt.timezone(dt.timedelta(hours=8))
    today = dt.datetime.now(tz).strftime("%Y-%m-%d")

    # —— 纯发送模式：只读已有文件并推送 ——
    if args.send:
        with open(args.send, encoding="utf-8") as f:
            markdown = f.read()
        key = os.environ.get("SCT_KEY", "").strip()
        if not key:
            print("缺少环境变量 SCT_KEY", file=sys.stderr)
            return 3
        res = push(markdown, "每日简报 %s" % today, key)
        print(json.dumps(res, ensure_ascii=False), file=sys.stderr)
        return 0 if res.get("code") in (0, "0") else 1

    groups, errors = collect()
    for e in errors:
        print("[warn] %s" % e, file=sys.stderr)

    total = sum(len(g["items"]) for g in groups)
    if total == 0:
        print("没有抓到任何条目，放弃本次推送", file=sys.stderr)
        return 2

    # 翻译
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        n_src, n_done, err = translate(groups, key)
        if err:
            print("[warn] %s" % err, file=sys.stderr)
        else:
            print("[info] 翻译 %d 条" % n_done, file=sys.stderr)
    else:
        print("[warn] 未设置 DEEPSEEK_API_KEY，跳过翻译，英文标题保留", file=sys.stderr)

    markdown = build_markdown(groups, today, with_voice=bool(args.site))
    print(markdown)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(markdown)

    if args.site:
        build_site(groups, today, args.site)
        print("[info] 语音与播放页已生成到 %s" % args.site, file=sys.stderr)

    if args.dry_run:
        print("[dry-run] 共 %d 条，未推送" % total, file=sys.stderr)
        return 0

    # 非 dry-run 且带 SCT_KEY 时顺带发送（本地完整测试用；Actions 里用 --send 分步）
    key = os.environ.get("SCT_KEY", "").strip()
    if key:
        res = push(markdown, "每日简报 %s" % today, key)
        print(json.dumps(res, ensure_ascii=False), file=sys.stderr)
        return 0 if res.get("code") in (0, "0") else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
