"""Render a finished digest into a WeCom mpnews article (the benchmark look of wecom/build_mpnews_prose.js).

Digest shape: {"date_label", "window", "lead", "toc": {"hooks": {id: str}, "papers", "briefs"}, "items": [...],
"briefs": [...], "gloss": {term: text}, "cut": [str], "coverage"}.
The body runs in fixed sections: 头条 → 更多动态 → 论文 → 快讯; the lead's table of contents follows the same order.
Each item: who, group, handle, kind_label, ev, url, top, title, text, you, extra, quote, and optionally
image {file, caption}. Each brief: who, url, text.
In title/text/you: [[term]] marks a glossary term, ==phrase== marks the paragraph's focus.
"""
import html
import re

INK, TITLE, NOTE, RULE = "#3f3f3f", "#0f766e", "#8c8c8c", "#e5e5e5"
AVATARS = ["#3b6fd8", "#d9622b", "#2f8a6e", "#8a5cd6", "#c2416b", "#4f7b9a", "#b88a1e", "#5a6f3a"]
EV_LABEL = {"first": "一手发布", "third": "第三方评测", "comment": "观点评论", "claim": "未独立核验"}
TEXT = f"color:{INK};font-size:15px;line-height:1.8;letter-spacing:.5px;text-align:justify"
NOTE_STYLE = f"color:{NOTE};font-size:13px;line-height:1.7;letter-spacing:.3px;text-align:justify"

# Images go to WeCom as this placeholder plus the file name; the VPS swaps in the uploaded image's URL.
IMAGE_PLACEHOLDER = "xdaily-image:"

TERM_RE = re.compile(r"\[\[(.+?)\]\]")
FOCUS_RE = re.compile(r"==(.+?)==")


def esc(s):
    return html.escape(s, quote=False)


def marked(s):
    """Escape, then turn ==focus== and [[term]] into inline styles."""
    s = FOCUS_RE.sub(lambda m: f'<span style="font-weight:bold;color:#1f1f1f;background-color:#dcefe9;padding:0 2px">{m[1]}</span>', esc(s))
    return TERM_RE.sub(lambda m: f'<span style="border-bottom:1px dotted {TITLE}">{m[1]}</span>', s)


def plain(s):
    return TERM_RE.sub(r"\1", FOCUS_RE.sub(r"\1", s))


def terms_in(*texts):
    seen = []
    for t in TERM_RE.findall(" ".join(texts)):
        if t not in seen:
            seen.append(t)
    return seen


def avatar(who, color, size):
    radius, font, gap = round(size / 3.5), round(size * 0.55), 8 if size > 20 else 6
    return (f'<span style="display:inline-block;width:{size}px;height:{size}px;line-height:{size}px;text-align:center;'
            f'border-radius:{radius}px;background:{color};color:#fff;font-size:{font}px;font-weight:bold;'
            f'margin-right:{gap}px;vertical-align:middle">{esc(who[:1])}</span>')


def figure(item, image_src):
    """The post's result chart, inside the same card: picture, then a gray caption."""
    image = item.get("image")
    if not image or not image_src:
        return ""
    return (f'<!--image:{esc(image["file"])}--><section style="margin:14px 0 0">'
            f'<img src="{html.escape(image_src(image["file"]))}" style="display:block;width:100%;height:auto;border-radius:4px">'
            f'<p style="margin:6px 0 0;{NOTE_STYLE};font-size:12px">图｜{esc(image["caption"])}</p></section><!--/image-->')


def post(item, k, total, color, gloss, image_src=None):
    notes = [f"{marked(f'[[{t}]]')}：{esc(gloss[t])}" for t in terms_in(item["title"], item["text"], item.get("you", "")) if t in gloss]
    if item.get("extra"):
        notes.append(esc(item["extra"]))
    if item.get("quote"):
        notes.append(f"原文：“{esc(item['quote'])}”")
    byline = "" if k == 0 else (
        f'<p style="margin:0 0 10px;padding-top:16px;border-top:1px dashed {RULE};font-size:13px;color:{NOTE}">'
        f'{avatar(item["who"], color, 18)}<b style="color:{color}">{esc(item["who"])}</b> · 第 {k + 1} 条，共 {total} 条</p>')
    return (
        f'<section style="margin:0 0 28px">{byline}'
        + (f'<p style="margin:0 0 2px;font-size:12px;letter-spacing:2px;color:{TITLE}">头条</p>' if item.get("top") else "")
        + f'<p style="margin:0;font-size:18px;font-weight:bold;line-height:1.5;color:{TITLE}">{marked(item["title"])}</p>'
        + f'<section style="width:32px;height:3px;background:{TITLE};margin:8px 0 12px"></section>'
        + f'<p style="margin:0;{TEXT}">{marked(item["text"])}</p>'
        + figure(item, image_src)
        + (f'<p style="margin:12px 0 0;{TEXT};font-size:14px"><span style="color:{NOTE}">和你的研究｜</span>{marked(item["you"])}</p>' if item.get("you") else "")
        + f'<section style="margin-top:12px;{NOTE_STYLE}">'
        + "".join(f'<p style="margin:0 0 2px">{n}</p>' for n in notes)
        + f'<p style="margin:2px 0 0">{EV_LABEL[item["ev"]]} · {esc(item["kind_label"])} · '
          f'<a href="{html.escape(item["url"])}" style="color:{NOTE};text-decoration:underline">原文链接</a></p></section></section>')


def groups(items):
    """Group posts by source in first-appearance order; headlines' groups come first."""
    order = {}
    for item in sorted(items, key=lambda i: not i.get("top")):
        order.setdefault(item["group"], []).append(item)
    return list(order.values())


def briefs(digest):
    rows = "".join(
        f'<p style="margin:0 0 10px;{TEXT};font-size:14px"><b style="color:{INK}">{esc(b["who"])}</b>：{esc(b["text"])} '
        f'<a href="{html.escape(b["url"])}" style="color:{NOTE};font-size:12px;text-decoration:underline">原文</a></p>'
        for b in digest.get("briefs", []))
    return (f'<p style="{SECTION_LABEL};margin-bottom:10px">快讯</p>'
            f'<section style="padding-top:16px;margin-bottom:20px;border-top:1px solid {RULE}">{rows}</section>') if rows else ""


SECTION_LABEL = f"margin:26px 0 0;font-size:12px;letter-spacing:2px;color:{TITLE}"


def sections(items):
    """[(label, groups)] in reading order: headlines, other news, papers. Empty sections are left out."""
    gs = groups(items)
    tops = [g for g in gs if any(i.get("top") for i in g)]
    papers = [g for g in gs if g not in tops and g[0]["group"] == "论文"]
    rest = [g for g in gs if g not in tops and g not in papers]
    return [(label, g) for label, g in (("头条", tops), ("更多动态", rest), ("论文", papers)) if g]


def toc(digest, ordered, colors):
    """The lead's table of contents: one row per post for headlines and news, one line each for papers and briefs."""
    hooks = digest.get("toc", {}).get("hooks", {})
    heading = lambda text: f'<p style="margin:14px 0 6px;font-size:12px;color:{NOTE}">{text}</p>'
    line = lambda body: f'<p style="margin:0 0 8px;{TEXT};font-size:14px;line-height:1.6">{body}</p>'
    out = []
    for label, gs in ordered:
        posts = [p for g in gs for p in g]
        if label == "论文":
            summary = digest.get("toc", {}).get("papers") or "、".join(plain(p["title"]) for p in posts[:2])
            out.append(heading(f"论文 · {len(posts)} 篇") + line(esc(summary)))
            continue
        out.append(heading(label))
        for p in posts:
            dot = (f'<span style="display:inline-block;width:6px;height:6px;border-radius:2px;background:{colors[p["group"]]};'
                   f'margin:0 8px 2px 0;vertical-align:middle"></span>')
            out.append(line(f'{dot}<b style="color:{INK}">{esc(p["who"])}</b><span style="color:{NOTE}">｜</span>'
                            f'{esc(hooks.get(p["id"]) or plain(p["title"]))}'))
    if digest.get("briefs"):
        summary = digest.get("toc", {}).get("briefs") or "、".join(b["who"] for b in digest["briefs"][:3])
        out.append(heading(f"快讯 · {len(digest['briefs'])} 条") + line(esc(summary)))
    return "".join(out)


def render_content(digest, image_src=None):
    ordered = sections(digest["items"])
    colors, body = {}, []
    for label, gs in ordered:
        body.append(f'<p style="{SECTION_LABEL}">{label}</p>')
        for posts in gs:
            first = posts[0]
            color = colors.setdefault(first["group"], AVATARS[len(colors) % len(AVATARS)])
            count = f'<span style="color:{NOTE};font-weight:normal;font-size:13px;margin-left:6px">· {len(posts)} 条</span>' if len(posts) > 1 else ""
            body.append(
                f'<section style="margin:0 0 6px;padding-top:20px;border-top:1px solid {RULE}">'
                f'<p style="margin:0 0 14px;font-size:16px;font-weight:bold;color:{INK}">{avatar(first["who"], color, 28)}{esc(first["who"])}'
                f'<span style="color:{NOTE};font-weight:normal;font-size:13px;margin-left:6px">{esc(first.get("handle", ""))}</span>{count}</p>'
                f'<section style="border-left:2px solid {color};padding-left:12px;margin-left:13px">'
                + "".join(post(p, k, len(posts), color, digest["gloss"], image_src) for k, p in enumerate(posts))
                + "</section></section>")
    cut = "".join(f'<p style="margin:0 0 2px">{esc(c)}</p>' for c in digest.get("cut", []))
    opening = ""
    if digest.get("lead") or digest.get("toc"):
        opening = (f'<p style="margin:0 0 4px;font-size:12px;letter-spacing:2px;color:{TITLE}">今日导读</p>'
                   + (f'<p style="margin:0 0 6px;{TEXT};font-size:16px">{marked(digest["lead"])}</p>' if digest.get("lead") else "")
                   + (toc(digest, ordered, colors) if digest.get("toc") else "")
                   + '<p style="margin:0 0 4px"></p>')
    return (
        f'<section style="{TEXT}">'
        f'<p style="margin:0;text-align:center;font-size:12px;letter-spacing:3px;color:{TITLE}">CS / AI 科研雷达</p>'
        f'<p style="margin:2px 0 0;text-align:center;font-size:13px;color:{NOTE}">{esc(digest["date_label"])} · 入选 {len(digest["items"])} 条</p>'
        f'<p style="margin:0 0 22px;text-align:center;font-size:12px;color:{NOTE}">{esc(digest.get("window", ""))}</p>'
        + opening
        + "".join(body)
        + briefs(digest)
        + f'<section style="padding-top:20px;border-top:1px solid {RULE};{NOTE_STYLE}">'
        + (f'<p style="margin:0 0 4px;color:{INK};font-weight:bold">今天没选的</p>{cut}' if cut else "")
        + f'<p style="margin:12px 0 0;font-size:12px">{esc(digest.get("coverage", ""))}</p></section></section>')


def utf8_cut(s, limit):
    data = s.encode("utf-8")[:limit]
    return data.decode("utf-8", "ignore")


def render_article(digest, image_src=None):
    tops = [i for i in digest["items"] if i.get("top")] or digest["items"][:3]
    return {
        "title": utf8_cut(f"AI科研日报 · {digest['date_label']}", 128),
        "author": "CS/AI 科研雷达",
        "digest": utf8_cut("\n".join("· " + plain(i["title"]) for i in tops), 512),
        "content": render_content(digest, image_src),
    }


def render_page(article):
    """A standalone HTML copy for the local archive."""
    return ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>{esc(article['title'])}</title></head>"
            "<body style=\"margin:0;background:#fff\"><div style=\"max-width:640px;margin:0 auto;padding:20px 16px\">"
            f"{article['content']}</div></body></html>")
