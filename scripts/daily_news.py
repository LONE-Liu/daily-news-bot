#!/usr/bin/env python3
"""每日热点简报：抓 RSS → 去重排序 → 生成 Markdown → 推送到个人微信（Server酱 Turbo）。

零第三方依赖，只用标准库，可直接在 GitHub Actions 上跑。

用法:
    python3 daily_news.py              # 抓取并推送
    python3 daily_news.py --dry-run    # 只打印，不推送
    python3 daily_news.py --out x.md   # 另存一份 Markdown

环境变量:
    SCT_KEY    Server酱 Turbo 的 SendKey（SCT 开头），--dry-run 时可省略
    MAX_ITEMS  总条数，默认 8
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

# ── 数据源 ────────────────────────────────────────────────────────────────
# 每个栏目下按顺序取，前面源不够再由后面的补。
FEEDS = {
    "时政": [
        "https://www.chinanews.com.cn/rss/china.xml",
        "https://www.chinanews.com.cn/rss/scroll-news.xml",
    ],
    "财经": [
        "https://www.chinanews.com.cn/rss/finance.xml",
    ],
    "科技": [
        "https://www.ithome.com/rss/",
        "https://sspai.com/feed",
    ],
    "社会": [
        "https://www.chinanews.com.cn/rss/society.xml",
    ],
}

MAX_AGE_HOURS = 36      # 超过这个时间的条目丢弃
MAX_ITEMS = 8           # 总条数上限（按栏目均摊）
PER_CATEGORY_MAX = max(1, -(-MAX_ITEMS // len(FEEDS)))   # 单栏上限=总量按栏目数向上取整
TITLE_MAX = 46          # 标题截断长度
SUMMARY_MAX = 72        # 摘要截断长度
UA = "Mozilla/5.0 (compatible; daily-news-bot/1.0)"

# 低信息量条目，直接丢
NOISE = (
    "喜加一", "免费领取", "限时免费", "优惠券", "打折", "促销", "秒杀",
    "开奖", "中奖", "彩票", "招聘启事", "声明", "更正", "讣告",
)

# 关注点加权：标题命中这些关键词的条目优先入选
FOCUS = ("BIM", "建筑", "工程", "EPC", "房地产", "人力资源")

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
PUNCT_RE = re.compile(r"[\s\u3000,，.。!！?？:：;；、\"'“”‘’（）()\[\]【】《》<>·—\-_|/\\]+")


def clean(text):
    """去 HTML、反转义、压空白。"""
    if not text:
        return ""
    text = TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return WS_RE.sub(" ", text).strip()


def norm_key(title):
    """用于去重的归一化键：去掉所有标点空白后比较。"""
    return PUNCT_RE.sub("", title or "").lower()


def clip(text, limit):
    """截断到 limit；摘要尽量在句号处收尾，避免半句加省略号。"""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    head = text[:limit]
    for sep in ("。", "！", "？"):
        idx = head.rfind(sep)
        if idx >= limit // 2:
            return head[: idx + 1]
    return head.rstrip("，、,;；:： ") + "…"


def is_noise(title):
    return any(word in title for word in NOISE)


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


def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def parse_feed(raw):
    """同时兼容 RSS 2.0 与 Atom，返回 [(title, summary, link, datetime|None)]。"""
    root = ET.fromstring(raw)
    items = []

    # RSS 2.0
    for node in root.iter():
        tag = node.tag.split("}")[-1]
        if tag != "item":
            continue
        title = summary = link = pub = None
        for child in node:
            name = child.tag.split("}")[-1]
            if name == "title":
                title = child.text
            elif name in ("description", "summary"):
                summary = child.text
            elif name == "link":
                link = child.text or child.attrib.get("href")
            elif name in ("pubDate", "date", "published"):
                pub = child.text
        items.append((clean(title), clean(summary), (link or "").strip(), parse_date(pub)))
    if items:
        return items

    # Atom
    for node in root.iter():
        if node.tag.split("}")[-1] != "entry":
            continue
        title = summary = link = pub = None
        for child in node:
            name = child.tag.split("}")[-1]
            if name == "title":
                title = child.text
            elif name in ("summary", "content"):
                if summary is None:
                    summary = child.text
            elif name == "link":
                if link is None or child.attrib.get("rel") in (None, "alternate"):
                    link = child.attrib.get("href") or child.text
            elif name in ("updated", "published"):
                pub = child.text
        items.append((clean(title), clean(summary), (link or "").strip(), parse_date(pub)))
    return items


def normalize_dt(d):
    """统一成 naive UTC，便于比较（feed 里时区各不相同）。"""
    if d is None:
        return None
    if d.tzinfo is not None:
        return d.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return d


def collect():
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    cutoff = now - dt.timedelta(hours=MAX_AGE_HOURS)
    seen = set()
    result = {}
    errors = []

    for category, urls in FEEDS.items():
        picked = []
        for url in urls:
            if len(picked) >= PER_CATEGORY_MAX:
                break
            try:
                raw = fetch(url)
                entries = parse_feed(raw)
            except Exception as e:
                errors.append("%s: %s" % (url, e))
                continue

            # 新的在前
            dated = [(t, s, l, normalize_dt(d)) for (t, s, l, d) in entries]
            dated.sort(key=lambda x: x[3] or dt.datetime.min, reverse=True)
            dated = ([x for x in dated if any(k in x[0] for k in FOCUS)]
                     + [x for x in dated if not any(k in x[0] for k in FOCUS)])

            for title, summary, link, when in dated:
                if len(picked) >= PER_CATEGORY_MAX:
                    break
                if not title or not link:
                    continue
                if is_noise(title):
                    continue
                if when is not None and when < cutoff:
                    continue
                key = norm_key(title)
                if not key or key in seen:
                    continue
                seen.add(key)
                picked.append(
                    {
                        "title": clip(title, TITLE_MAX),
                        "summary": clip(summary, SUMMARY_MAX),
                        "link": link,
                    }
                )
        if picked:
            result[category] = picked

    # 全局兜底：超量时从末尾栏目往回砍，保证总数不超 MAX_ITEMS
    total = sum(len(v) for v in result.values())
    for cat in reversed(list(result)):
        while total > MAX_ITEMS and result[cat]:
            result[cat].pop()
            total -= 1
        if not result[cat]:
            del result[cat]
        if total <= MAX_ITEMS:
            break

    return result, errors


def build_markdown(sections, today):
    lines = ["## 每日热点 %s" % today, ""]
    for category in FEEDS:
        items = sections.get(category)
        if not items:
            continue
        lines.append("### %s" % category)
        for it in items:
            entry = "- **[%s](%s)**" % (it["title"], it["link"])
            if it["summary"]:
                entry += " —— %s" % it["summary"]
            lines.append(entry)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


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
    args = ap.parse_args()

    tz = dt.timezone(dt.timedelta(hours=8))
    today = dt.datetime.now(tz).strftime("%Y-%m-%d")

    sections, errors = collect()
    for e in errors:
        print("[warn] %s" % e, file=sys.stderr)

    total = sum(len(v) for v in sections.values())
    if total == 0:
        print("没有抓到任何条目，放弃本次推送", file=sys.stderr)
        return 2

    markdown = build_markdown(sections, today)
    print(markdown)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(markdown)

    if args.dry_run:
        print("[dry-run] 未推送，共 %d 条" % total, file=sys.stderr)
        return 0

    key = os.environ.get("SCT_KEY", "").strip()
    if not key:
        print("缺少环境变量 SCT_KEY", file=sys.stderr)
        return 3

    res = push(markdown, "每日热点 %s" % today, key)
    print(json.dumps(res, ensure_ascii=False), file=sys.stderr)
    if res.get("code") not in (0, "0"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
