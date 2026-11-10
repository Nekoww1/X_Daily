#!/usr/bin/env python3
"""Turn a day's candidates (data/DATE.json from radar.py) into the finished Chinese daily and deliver it.

Steps, each one `codex exec` call with a JSON schema (official ChatGPT login; luna max, sol high for write/highlight):
  1 select    pick 8-14 items, 3 headlines, evidence type, what was cut
  2 write     title + one paragraph + optional "和你的研究" + notes, glossary terms marked [[...]]
  3 highlight mark 1-2 focus phrases per paragraph with ==...==, nothing else may change
  4 brief     one-sentence briefs for items worth knowing but too small for a paragraph
  5 lead      the opening: one main-line sentence plus a table of contents (a hook per item)
  6 images    look at the selected X posts' photos and keep a result chart per item, with a caption
Code checks every step (numbers and quotes must come from the source, highlights must not change text)
and retries once with the problems listed; what still fails falls back to a safe version.
The reader profile (profiles/NAME.toml) sets the topics, the reader and "和你的研究".
Step outputs are cached in data/digest-work/DATE/, so a rerun only calls Codex for missing steps.

Usage: python digest.py [--date YYYY-MM-DD] [--send] [--fresh]
"""
import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from datetime import date, datetime, timedelta
from pathlib import Path

import radar
import render_mpnews as render

ROOT = Path(__file__).resolve().parent
PROMPTS = ROOT / "prompts"
PROFILE = os.environ.get("DIGEST_PROFILE", "personal")
MODEL = os.environ.get("DIGEST_MODEL", "gpt-6-luna")
EFFORT = os.environ.get("DIGEST_EFFORT", "max")
# Writing the paragraphs and marking their focus read better with sol; selection and the lead stay on luna.
WRITER = (os.environ.get("DIGEST_WRITER_MODEL", "gpt-6.1-sol"), os.environ.get("DIGEST_WRITER_EFFORT", "high"))
STEP_MODELS = {"write": WRITER, "write-retry": WRITER, "highlight": WRITER, "brief": WRITER, "images": WRITER}
MAX_DAY_IMAGES = 5
IMAGE_LIMIT_BYTES = 2_000_000  # WeCom's uploadimg limit
CODEX_TIMEOUT = 60 * 60  # sol high took 39 min to write 14 items
WEEKDAYS = "一二三四五六日"
EV = ["first", "third", "comment", "claim"]
SOURCE_CHARS = {"x": 4000, "paper": 2000, "blog": 2500}


def obj(required, **props):
    return {"type": "object", "additionalProperties": False, "required": list(required), "properties": props}


STR, BOOL = {"type": "string"}, {"type": "boolean"}
arr = lambda item: {"type": "array", "items": item}
SCHEMAS = {
    "select": obj(["items", "briefs", "cut"],
                  items=arr(obj(["ids", "who", "top", "ev"], ids=arr(STR), who=STR, top=BOOL, ev={"type": "string", "enum": EV})),
                  briefs=arr(obj(["ids", "who"], ids=arr(STR), who=STR)),
                  cut=arr(STR)),
    "write": obj(["items"], items=arr(obj(["id", "title", "text", "you", "extra", "quote", "new_terms"],
                                          id=STR, title=STR, text=STR, you=STR, extra=STR, quote=STR,
                                          new_terms=arr(obj(["term", "definition"], term=STR, definition=STR))))),
    "highlight": obj(["items"], items=arr(obj(["id", "text"], id=STR, text=STR))),
    "lead": obj(["thread", "hooks", "papers", "briefs"], thread=STR,
                hooks=arr(obj(["id", "hook"], id=STR, hook=STR)), papers=STR, briefs=STR),
    "brief": obj(["items"], items=arr(obj(["id", "text"], id=STR, text=STR))),
    "images": obj(["items"], items=arr(obj(["id", "image", "caption"], id=STR, image=STR, caption=STR))),
}


# ---------- Codex ----------

def codex_bin():
    return os.environ.get("CODEX_BIN") or shutil.which("codex") or str(Path.home() / ".local/bin/codex")


def run_codex(prompt, schema, log_path, model=(MODEL, EFFORT), images=(), search=False):
    """One non-interactive Codex call that must answer with JSON matching schema."""
    name, effort = model
    with tempfile.TemporaryDirectory(prefix="x-daily-codex-") as tmp:
        schema_path, out_path = Path(tmp) / "schema.json", Path(tmp) / "out.json"
        schema_path.write_text(json.dumps(schema), encoding="utf-8")
        cmd = [codex_bin(), *(["--search"] if search else []), "exec", *(f"--image={i}" for i in images), "--ephemeral", "--skip-git-repo-check",
               "--ignore-user-config",  # skip the third-party provider in config.toml; use the official login
               "-m", name, "-c", f'model_reasoning_effort="{effort}"', "-s", "read-only", "-C", tmp,
               "--output-schema", str(schema_path), "-o", str(out_path), "-"]
        result = subprocess.run(cmd, input=prompt, text=True, capture_output=True, timeout=CODEX_TIMEOUT, encoding="utf-8")
        log_path.write_text(f"exit {result.returncode}\n--- stdout\n{result.stdout[-20000:]}\n--- stderr\n{result.stderr[-20000:]}", encoding="utf-8")
        if result.returncode != 0 or not out_path.exists():
            raise RuntimeError(f"codex exited {result.returncode}; see {log_path.name}")
        return json.loads(out_path.read_text(encoding="utf-8"))


class Steps:
    """Runs and caches the Codex steps for one day."""

    def __init__(self, work_dir, fresh=False, runner=run_codex):
        self.dir, self.runner = work_dir, runner
        self.dir.mkdir(parents=True, exist_ok=True)
        if fresh:
            for f in self.dir.glob("*.json"):
                f.unlink()

    def __call__(self, label, prompt, schema, images=()):
        cached = self.dir / f"{label}.json"
        if cached.exists():
            return json.loads(cached.read_text(encoding="utf-8"))
        (self.dir / f"{label}.prompt.md").write_text(prompt, encoding="utf-8")
        last_error = None
        for attempt in (1, 2):
            try:
                model = STEP_MODELS.get(label, (MODEL, EFFORT))
                answer = self.runner(prompt, schema, self.dir / f"{label}.{attempt}.log", model, images)
                cached.write_text(json.dumps(answer, ensure_ascii=False, indent=1), encoding="utf-8")
                return answer
            except Exception as error:  # timeouts, bad JSON, non-zero exit: one retry
                last_error = error
        raise RuntimeError(f"step {label} failed twice: {last_error}")


def prompt(name, **parts):
    text = (PROMPTS / f"{name}.md").read_text(encoding="utf-8")
    for key, value in parts.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def load_profile(name=PROFILE):
    return tomllib.loads((ROOT / "profiles" / f"{name}.toml").read_text(encoding="utf-8"))


def reader_text(profile):
    a, research = profile["audience"], profile.get("research")
    return "".join([f"读者：{a['who']}", research["summary"] if research else "",
                    f"熟悉：{'、'.join(a['known'])}，这些不必解释。不熟悉：{'、'.join(a['unfamiliar'])}，这些需要注释。"])


def focus_text(profile):
    return "\n".join(f"- {f['name']}（约 {f['share']}%）：{f['topics']}" for f in profile["focus"])


def you_rule(profile):
    research = profile.get("research")
    if not research:
        return "这一版不写“和你的研究”，固定留空字符串。"
    return (f"“和你的研究”一两句，不超过 70 字，只在和读者的工作（{'、'.join(research['links'])}）有具体、非牵强的联系时才写，"
            f"说清联系在哪里。一天最多 {research['max_items']} 条有这一项，其余留空字符串。")


def as_json(value):
    return json.dumps(value, ensure_ascii=False, indent=1)


# ---------- checks ----------

def normalize(s):
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"').replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", s).strip()


NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
NUMBER_WORDS = {w: str(n) for n, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen twenty".split())} | {"thirty": "30", "forty": "40", "fifty": "50", "hundred": "100"}
SCALE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(m|million|b|billion|k|thousand)\b", re.I)
TO_CHINESE_UNIT = {"m": 100, "million": 100, "b": 10, "billion": 10, "k": 0.1, "thousand": 0.1}  # 16M = 1600 万, 2B = 20 亿, 50k = 5 万


def tidy(n):
    n = n.replace(",", "")
    return n.rstrip("0").rstrip(".") if "." in n else n


def numbers(s):
    return [tidy(n) for n in NUMBER_RE.findall(render.plain(s))]


def source_numbers(s):
    """Numbers a faithful Chinese text may use: digits as written, English number words, M/B/k in 万/亿."""
    found = set(numbers(s))
    found |= {NUMBER_WORDS[w] for w in re.findall(r"[a-z]+", s.lower()) if w in NUMBER_WORDS}
    for value, unit in SCALE_RE.findall(s):
        found.add(tidy(f"{float(value) * TO_CHINESE_UNIT[unit.lower()]:.4f}"))
    return found


def balanced(s):
    return s.count("[[") == s.count("]]")


def check_written(out, item, gloss):
    """Problems in one written item; empty means it passes."""
    problems = []
    source = normalize(item["source_text"])
    if not out["title"].strip() or not out["text"].strip():
        return ["title 或 text 为空"]
    allowed = source_numbers(source)
    missing = [n for n in numbers(" ".join(out[k] for k in ("title", "text", "you", "extra"))) if n not in allowed]
    if missing:
        problems.append(f"这些数字在原始材料里找不到：{'、'.join(sorted(set(missing)))}。只用材料里的数字，照原样写。")
    low, high = (170, 240) if item["top"] else (110, 190)
    size = len(render.plain(out["text"]))
    if not low <= size <= high:
        problems.append(f"text 现在 {size} 字，要求 {low + 10}–{high - 10} 字。")
    if out["quote"] and normalize(out["quote"]) not in source:
        problems.append("quote 不是原始材料里逐字一致的原话。要么逐字复制，要么留空。")
    known = set(gloss) | {t["term"] for t in out["new_terms"]}
    unknown = [t for t in render.terms_in(out["title"], out["text"], out["you"]) if t not in known]
    if unknown:
        problems.append(f"这些 [[术语]] 没有解释：{'、'.join(unknown)}。在 new_terms 里补上，或去掉标记。")
    if not all(balanced(out[k]) for k in ("title", "text", "you")):
        problems.append("[[ ]] 标记不成对。")
    return problems


CJK = r"[一-鿿]"
LATIN = r"[A-Za-z0-9$]"


def space_cjk(s):
    """Put a space between Chinese and Latin letters or digits ("在ARC-AGI-2得81.7%" -> "在 ARC-AGI-2 得 81.7%"),
    looking through [[ ]] and == == marks."""
    s = re.sub(rf"({CJK})((?:\[\[|==)?)({LATIN})", r"\1 \2\3", s)
    return re.sub(rf"([A-Za-z0-9%])((?:\]\]|==)?)({CJK})", r"\1\2 \3", s)


def unwrap_terms(s, keep):
    return render.TERM_RE.sub(lambda m: m[0] if m[1] in keep else m[1], s)


def finalize_written(out, item, gloss, warnings):
    """Last-resort fixes after the retry: drop what cannot be trusted, keep the rest."""
    source = normalize(item["source_text"])
    if out["quote"] and normalize(out["quote"]) not in source:
        warnings.append(f"{item['id']}: 摘句不是原文，已去掉")
        out["quote"] = ""
    known = set(gloss) | {t["term"] for t in out["new_terms"] if t["definition"].strip()}
    for key in ("title", "text", "you"):
        out[key] = unwrap_terms(out[key], known)
    for key in ("title", "text", "you", "extra"):
        out[key] = space_cjk(out[key])
    for problem in check_written(out, item, gloss):
        warnings.append(f"{item['id']}: {problem}")
    return out


def apply_highlight(original, marked):
    """Copy the model's ==focus== spans onto the original paragraph; None if they do not fit.

    Only the spans are taken from the model, so a dropped space or other small change elsewhere
    in its copy cannot alter the paragraph. A span may differ from the original only in whitespace.
    """
    spans = render.FOCUS_RE.findall(marked)
    if not 1 <= len(spans) <= 2:
        return None
    taken, result = [], original
    for span in spans:
        pattern = r"\s*".join(re.escape(ch) for ch in span if not ch.isspace())
        found = re.search(pattern, original)
        if not found or not 4 <= len(render.plain(found[0])) <= 40 or not balanced(found[0]):
            return None
        if any(found.start() < end and start < found.end() for start, end in taken):
            return None
        taken.append(found.span())
    for start, end in sorted(taken, reverse=True):  # matches start and end on non-space characters
        result = f"{result[:start]}=={result[start:end]}=={result[end:]}"
    # A span must not cut into a [[term]]: outside the spans the term marks must still pair up.
    if not balanced(render.FOCUS_RE.sub("", result)):
        return None
    return result


# ---------- building the digest ----------

def handle_of(c):
    if c["kind"] == "x":
        return "@" + c["source"].split("@", 1)[-1]
    if c["kind"] == "paper":
        found = re.search(r"(\d{4}\.\d{4,5})", c["original_url"])
        return found[1] if found else "论文"
    return "博客"


def kind_label(c):
    if c["kind"] == "x":
        return f"X · {c['thread_parts']} 段 thread" if c.get("thread_parts", 1) > 1 else "X"
    return {"paper": "论文", "blog": "博客"}.get(c["kind"], c["kind"])


def source_text(cands):
    return "\n\n".join(f"[{c['source']}] {c['title']}\n{c['summary'][:SOURCE_CHARS.get(c['kind'], 2000)]}" for c in cands)


def select_items(steps, data, profile):
    candidates = {c["id"]: c for c in data["candidates"]}
    payload = [{"id": c["id"], "source": c["source"], "kind": c["kind"], "title": c["title"],
                "text": c["summary"][:SOURCE_CHARS.get(c["kind"], 2000)], "thread_parts": c.get("thread_parts", 1),
                "upvotes": c.get("upvotes", 0)} for c in data["candidates"]]
    answer = steps("select", prompt("select", PROFILE=reader_text(profile), FOCUS=focus_text(profile), DATA=as_json(payload)), SCHEMAS["select"])
    items, used, tops = [], set(), 0
    for entry in answer["items"]:
        ids = [i for i in entry["ids"] if i in candidates and i not in used]
        if not ids:
            continue
        used.update(ids)
        cands = [candidates[i] for i in ids]
        first = cands[0]
        top = entry["top"] and tops < 3
        tops += top
        items.append({"id": ids[0], "ids": ids, "who": entry["who"].strip() or first["source"],
                      "group": "论文" if first["kind"] == "paper" else first["source"],
                      "handle": "" if first["kind"] == "paper" else handle_of(first),
                      "kind_label": kind_label(first), "ev": entry["ev"], "url": first["original_url"],
                      "top": top, "source_text": source_text(cands)})
    for item in items:  # paper groups show the arXiv id per post via kind_label
        if item["group"] == "论文":
            item["kind_label"] = f"论文 · {handle_of(candidates[item['id']])}"
    briefs = []
    for entry in answer["briefs"][:6]:
        ids = [i for i in entry["ids"] if i in candidates and i not in used]
        if ids:
            used.update(ids)
            cands = [candidates[i] for i in ids]
            briefs.append({"id": ids[0], "who": entry["who"].strip() or cands[0]["source"],
                           "url": cands[0]["original_url"], "source_text": source_text(cands)})
    cut = [CANDIDATE_IDS_RE.sub("", c).strip() for c in answer["cut"]]
    return items, briefs, [c for c in cut if c][:10]


CANDIDATE_IDS_RE = re.compile(r"\s*[（(]\s*c\d+(?:\s*[、,，–—~-]\s*c?\d+)*\s*[）)]")


def write_briefs(steps, briefs, warnings):
    """One checked sentence per brief; a brief that fails the checks is dropped, not retried."""
    if not briefs:
        return []
    rows = [{"id": b["id"], "who": b["who"], "source": b["source_text"]} for b in briefs]
    answer = steps("brief", prompt("brief", DATA=as_json(rows)), SCHEMAS["brief"])
    texts = {o["id"]: space_cjk(o["text"].strip()) for o in answer["items"]}
    kept = []
    for b in briefs:
        text = texts.get(b["id"], "")
        missing = [n for n in numbers(text) if n not in source_numbers(normalize(b["source_text"]))]
        if not 15 <= len(text) <= 110 or missing or "[[" in text or "==" in text:
            warnings.append(f"{b['id']}: 快讯没通过检查，已去掉")
            continue
        kept.append({"who": b["who"], "url": b["url"], "text": text})
    return kept


def write_items(steps, items, gloss, profile, warnings):
    def payload(subset, feedback=None):
        rows = [{"id": i["id"], "top": i["top"], "who": i["who"], "source": i["source_text"]} for i in subset]
        if feedback:
            for row in rows:
                row["上次的问题"] = feedback[row["id"]]
        return as_json(rows)

    glossary = "\n".join(f"- {t}" for t in gloss) or "（暂无）"
    by_id = {i["id"]: i for i in items}
    parts = {"PROFILE": reader_text(profile), "YOU_RULE": you_rule(profile), "GLOSSARY": glossary}
    answer = steps("write", prompt("write", **parts, DATA=payload(items)), SCHEMAS["write"])
    written = {o["id"]: o for o in answer["items"] if o["id"] in by_id}
    failing = {i["id"]: (["这一条上次漏写了"] if i["id"] not in written else check_written(written[i["id"]], i, gloss)) for i in items}
    failing = {k: v for k, v in failing.items() if v}
    if failing:
        retry_items = [by_id[k] for k in failing]
        again = steps("write-retry", prompt("write", **parts, DATA=payload(retry_items, failing)), SCHEMAS["write"])
        for o in again["items"]:
            if o["id"] in failing:
                written[o["id"]] = o
    result = []
    for item in items:
        if item["id"] not in written:
            warnings.append(f"{item['id']}: 两次都没写出来，已跳过")
            continue
        result.append((item, finalize_written(written[item["id"]], item, gloss, warnings)))
    return result


def highlight(steps, posts, warnings):
    rows = [{"id": item["id"], "top": item["top"], "text": out["text"]} for item, out in posts]
    answer = steps("highlight", prompt("highlight", DATA=as_json(rows)), SCHEMAS["highlight"])
    marked = {o["id"]: o["text"] for o in answer["items"]}
    for item, out in posts:
        result = apply_highlight(out["text"], marked.get(item["id"], ""))
        if result:
            out["text"] = result
        else:
            warnings.append(f"{item['id']}: 重点标记对不上原文或数量不对，这段不标重点")


def lead(steps, posts, briefs, warnings):
    """The opening: a main-line sentence and a hook per item for the table of contents.

    Every number must already appear in the items or their sources. A failing hook falls back to the
    item's title; a failing main line is dropped, the table of contents stays.
    """
    rows = [{"id": item["id"], "top": item["top"], "who": item["who"], "title": render.plain(out["title"]),
             "text": render.plain(out["text"])} for item, out in posts]
    brief_lines = "\n".join(f"- {b['who']}：{b['text']}" for b in briefs) or "（今天没有快讯）"
    answer = steps("lead", prompt("lead", DATA=as_json(rows), BRIEFS=brief_lines), SCHEMAS["lead"])
    allowed = source_numbers(" ".join(r["title"] + " " + r["text"] + " " + item["source_text"] for r, (item, _) in zip(rows, posts))
                             + " " + brief_lines)

    def checked(text, limit, what):
        text = space_cjk(text.strip())
        missing = [n for n in numbers(text) if n not in allowed]
        if missing or len(text) > limit or "[[" in text or "==" in text:
            warnings.append(f"导读的{what}没通过检查" + (f"（数字 {'、'.join(missing)} 不在条目中）" if missing else "") + "，已不用")
            return ""
        return text

    hooks = {}
    for o in answer["hooks"]:
        if any(item["id"] == o["id"] for item, _ in posts):
            hooks[o["id"]] = checked(o["hook"], 36, f" {o['id']} 目录导语")
    return {"thread": checked(answer["thread"], 90, "主线"), "hooks": {k: v for k, v in hooks.items() if v},
            "papers": checked(answer["papers"], 50, "论文概括"), "briefs": checked(answer["briefs"], 50, "快讯概括")}


def download_image(url, path):
    """Fetch an X photo at a phone-friendly size; fall back to the small size above WeCom's limit."""
    for size in ("medium", "small"):
        data = radar.fetch(f"{url}{'&' if '?' in url else '?'}name={size}", timeout=60, retries=1)
        if len(data) <= IMAGE_LIMIT_BYTES:
            path.write_bytes(data)
            return True
    return False


def image_suffix(data):
    return ".png" if data[:8] == b"\x89PNG\r\n\x1a\n" else ".jpg"


def pick_images(steps, posts, candidates, warnings):
    """Let the model look at each selected post's photos and keep at most one result chart per item."""
    folder = steps.dir / "images"
    folder.mkdir(exist_ok=True)
    offered, rows, files = {}, [], []
    for item, out in posts:
        urls = [u for i in item["ids"] for u in candidates[i].get("images", [])][:2]
        names = []
        for n, url in enumerate(urls, 1):
            stem = f"{item['id']}-{n}"
            existing = next(iter(folder.glob(stem + ".*")), None)
            try:
                if not existing:
                    raw = folder / stem
                    if not download_image(url, raw):
                        continue
                    existing = raw.rename(raw.with_suffix(image_suffix(raw.read_bytes())))
            except Exception as error:
                warnings.append(f"{item['id']}: 图片下载失败（{type(error).__name__}）")
                continue
            names.append(existing.name)
            files.append(existing)
        if names:
            offered[item["id"]] = names
            rows.append({"id": item["id"], "who": item["who"], "title": render.plain(out["title"]),
                         "text": render.plain(out["text"]), "images": names})
    if not rows:
        return
    answer = steps("images", prompt("images", DATA=as_json(rows)), SCHEMAS["images"], images=[str(f) for f in files])
    by_id = {item["id"]: (item, out) for item, out in posts}
    chosen = 0
    for o in answer["items"]:
        if o["id"] not in by_id or o["image"] not in offered.get(o["id"], []) or chosen >= MAX_DAY_IMAGES:
            continue
        item, out = by_id[o["id"]]
        caption = space_cjk(o["caption"].strip())
        if not set(numbers(caption)) <= source_numbers(normalize(item["source_text"])) or len(caption) > 60:
            caption = ""
        out["image"] = {"file": o["image"], "caption": caption or "图片来自原帖"}
        chosen += 1


def coverage(data, selected):
    sources = data["sources"]
    failed = [s["source"] for s in sources if s["status"] == "failed"]
    short = [s["source"].replace("X: ", "") for s in sources if radar.X_SHORT_REACH_NOTE in s.get("reason", "")]
    parts = [f"覆盖：{len(sources)} 个来源，{len(failed)} 个失败" + (f"（{'、'.join(failed)}）" if failed else "")]
    if short:
        parts.append(f"{'、'.join(short)} 可能漏帖")
    return "；".join(parts) + f"。X 经 RSSHub 获取（非官方接口）。{len(data['candidates'])} 条候选，入选 {selected} 条。"


def build(day, steps, root=ROOT, profile=None):
    data = json.loads((root / "data" / f"{day.isoformat()}.json").read_text(encoding="utf-8"))
    profile = profile or load_profile()
    gloss_path = root / "glossary.json"
    gloss = json.loads(gloss_path.read_text(encoding="utf-8")) if gloss_path.exists() else {}
    warnings = []
    if not data["candidates"]:
        raise RuntimeError("今天没有候选内容")

    items, briefs, cut = select_items(steps, data, profile)
    if not items:
        raise RuntimeError("筛选后没有入选条目")
    posts = write_items(steps, items, gloss, profile, warnings)
    limit = profile["research"]["max_items"] if profile.get("research") else 0
    for _, out in [p for p in posts if p[1]["you"]][limit:]:  # the profile caps "和你的研究"
        out["you"] = ""
    highlight(steps, posts, warnings)
    brief_items = write_briefs(steps, briefs, warnings)
    opening = lead(steps, posts, brief_items, warnings)
    pick_images(steps, posts, {c["id"]: c for c in data["candidates"]}, warnings)

    used_terms, final = {}, []
    for item, out in posts:
        new = {t["term"]: t["definition"].strip() for t in out["new_terms"] if t["definition"].strip()}
        for t in render.terms_in(out["title"], out["text"], out["you"]):
            used_terms[t] = gloss.get(t) or new[t]
        final.append({k: item[k] for k in ("id", "ids", "who", "group", "handle", "kind_label", "ev", "url", "top")}
                     | {k: out[k].strip() for k in ("title", "text", "you", "extra", "quote")}
                     | ({"image": out["image"]} if out.get("image") else {}))
    gloss_path.write_text(json.dumps(gloss | used_terms, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    start, end = radar.report_window(day)
    return {"date": day.isoformat(), "date_label": date_label(day),
            "window": f"收录 {start:%m-%d %H:%M} 至 {end:%m-%d %H:%M}（北京时间）",
            "lead": opening["thread"], "toc": {k: opening[k] for k in ("hooks", "papers", "briefs")}, "items": final, "briefs": brief_items, "gloss": used_terms, "cut": cut,
            "coverage": coverage(data, len(final)), "warnings": warnings, "profile": profile["name"], "model": f"{MODEL} {EFFORT}; write/highlight {WRITER[0]} {WRITER[1]}"}


def date_label(day):
    """Readers see the morning the report arrives, i.e. the end of its 08:00-08:00 window."""
    shown = radar.report_window(day)[1].date()
    return f"{shown.isoformat()}（周{WEEKDAYS[shown.weekday()]}）"


# ---------- delivery ----------

def deliver(payload):
    """Hand the article to the VPS; its SSH key may only run the WeCom sender (see wecom/receive.py)."""
    target = os.environ.get("WECOM_RELAY")  # user@host of the VPS that holds the WeCom secret
    if not target:
        raise RuntimeError("WECOM_RELAY is not set")
    key = os.environ.get("WECOM_RELAY_KEY", str(Path.home() / ".ssh/id_ed25519_xdaily_send"))
    result = subprocess.run(["ssh", "-i", key, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", target],
                            input=json.dumps(payload, ensure_ascii=False), text=True, capture_output=True, timeout=180, encoding="utf-8")
    if result.returncode != 0 or "sent" not in result.stdout:
        raise RuntimeError(f"delivery failed: {result.stdout[-500:]} {result.stderr[-500:]}")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", help="report date, YYYY-MM-DD; default is yesterday (Asia/Taipei), like radar.py")
    parser.add_argument("--send", action="store_true", help="push the article to WeCom through the VPS")
    parser.add_argument("--fresh", action="store_true", help="ignore cached step outputs and call Codex again")
    args = parser.parse_args()
    radar.load_dotenv(ROOT / ".env")
    day = date.fromisoformat(args.date) if args.date else (datetime.now(radar.TAIPEI) - timedelta(days=1)).date()

    try:
        digest = build(day, Steps(ROOT / "data" / "digest-work" / day.isoformat(), fresh=args.fresh))
    except Exception as error:
        print(f"digest failed: {error}")
        if args.send:
            deliver({"text": f"AI科研日报 {date_label(day)} 没有生成：{error}"})
        return 1

    work = ROOT / "data" / "digest-work" / day.isoformat() / "images"
    shown = [i["image"]["file"] for i in digest["items"] if i.get("image")]
    archive = ROOT / "reports" / "img" / day.isoformat()
    archive.mkdir(parents=True, exist_ok=True)
    for name in shown:
        shutil.copyfile(work / name, archive / name)
    (ROOT / "data" / f"{day.isoformat()}.digest.json").write_text(as_json(digest), encoding="utf-8")
    page = render.render_article(digest, image_src=lambda name: f"img/{day.isoformat()}/{name}")
    (ROOT / "reports" / f"{day.isoformat()}.html").write_text(render.render_page(page), encoding="utf-8")
    article = render.render_article(digest, image_src=lambda name: render.IMAGE_PLACEHOLDER + name)
    print(f"items: {len(digest['items'])}, warnings: {len(digest['warnings'])}")
    for w in digest["warnings"]:
        print("  warning:", w)
    if args.send:
        images = {name: base64.b64encode((work / name).read_bytes()).decode() for name in shown}
        deliver({"mpnews": article, "images": images})
        print("sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
