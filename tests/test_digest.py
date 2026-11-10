import json
import sys
import tempfile
import unittest
from unittest import mock
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import digest  # noqa: E402
import render_mpnews as render  # noqa: E402

SOURCE = ("We're sharing new research. Guided by humans, our models uncovered two major weaknesses in legacy "
          "1024-bit RSA cryptosystems. Modern 2048-bit systems remain safe.")
TEXT = ("DeepMind 称，其模型在人类引导下发现了旧版 [[1024 位]] [[RSA]] 的两处重大弱点，现代 2048 位系统不受这两种攻击影响。"
        "官方借此主张，AI 时代不能再依赖历史上的困难性假设，密码标准需要持续接受压力测试。“AI 做科研”由此从数学、生物延伸到了密码分析；"
        "但推文没有给出方法，人类引导占多大比重、发现的是新数学还是已知弱点的工程组合，要等长文说明，目前只能先记下这个声称。")


def candidate(cid, source, kind, url, summary, title="t"):
    return {"id": cid, "source": source, "kind": kind, "title": title, "summary": summary, "original_url": url,
            "thread_parts": 2, "upvotes": 0, "published_at": "2026-11-06T17:20:31+00:00"}


BRIEF_SOURCE = "In AA-PersonalityBench, models that rank high on sincerity tend to rank low on slop across 30 traits."


def day_data():
    return {"sources": [{"source": "X: @GoogleDeepMind", "status": "ok_new", "reason": ""},
                        {"source": "METR", "status": "failed", "reason": "timeout"}],
            "candidates": [candidate("c1", "X: @GoogleDeepMind", "x", "https://x.com/GoogleDeepMind/status/1", SOURCE)
                           | {"images": ["https://pbs.twimg.com/media/AAA?format=png"]},
                           candidate("c2", "X: @someone", "x", "https://x.com/someone/status/2", "lol friday deploys"),
                           candidate("c3", "X: @ArtificialAnlys", "x", "https://x.com/ArtificialAnlys/status/3", BRIEF_SOURCE)]}


def written(**overrides):
    out = {"id": "c1", "title": "DeepMind：AI 找出旧版 [[RSA]] 的两处弱点", "text": TEXT, "you": "", "extra": "",
           "quote": "Guided by humans, our models uncovered two major weaknesses in legacy 1024-bit RSA cryptosystems.",
           "new_terms": []}
    return out | overrides


class FakeCodex:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, prompt, schema, log_path, model, images=()):
        label = log_path.name.split(".")[0]
        self.calls.append(label)
        self.models = getattr(self, "models", {}) | {label: model}
        self.images = getattr(self, "images", {}) | {label: list(images)}
        answer = self.answers[label]
        return answer.pop(0) if isinstance(answer, list) else answer


GLOSS = {"RSA": "常用公钥加密算法。", "1024 位": "RSA 的密钥长度。"}


class CheckTests(unittest.TestCase):
    item = {"id": "c1", "top": True, "source_text": SOURCE}

    def test_faithful_item_passes(self):
        self.assertEqual(digest.check_written(written(), self.item, GLOSS), [])

    def test_number_not_in_source_is_flagged(self):
        problems = digest.check_written(written(text=TEXT.replace("2048", "4096")), self.item, GLOSS)
        self.assertTrue(any("4096" in p for p in problems))

    def test_reworded_quote_is_flagged_and_dropped(self):
        warnings = []
        out = digest.finalize_written(written(quote="Our models found two weaknesses."), self.item, GLOSS, warnings)
        self.assertEqual(out["quote"], "")
        self.assertTrue(warnings)

    def test_curly_apostrophe_quote_still_matches(self):
        self.assertEqual(digest.check_written(written(quote="We’re sharing new research."), self.item, GLOSS), [])

    def test_term_without_definition_is_unwrapped(self):
        out = digest.finalize_written(written(title="关于 [[格密码]] 的消息"), self.item, GLOSS, [])
        self.assertEqual(out["title"], "关于 格密码 的消息")

    def test_highlight_spans_are_copied_onto_the_original(self):
        good = TEXT.replace("现代 2048 位系统不受这两种攻击影响", "==现代 2048 位系统不受这两种攻击影响==")
        self.assertEqual(digest.apply_highlight(TEXT, good), good)
        # The model dropped a space and changed a word outside the span: only the span is used.
        sloppy = good.replace("现代 2048", "现代2048").replace("官方借此", "官方据此")
        self.assertEqual(digest.apply_highlight(TEXT, sloppy), good)
        self.assertIsNone(digest.apply_highlight(TEXT, good.replace("两种", "两类")))  # span not in the text
        self.assertIsNone(digest.apply_highlight(TEXT, TEXT))  # no focus at all
        self.assertIsNone(digest.apply_highlight(TEXT, TEXT.replace("[[RSA]] 的两处", "[[==RSA]] 的两处==")))  # cuts into a term

    def test_space_between_chinese_and_latin(self):
        self.assertEqual(digest.space_cjk("Inkling 2 Small在ARC-AGI-2得81.7%，旧版==1024位[[RSA]]=="),
                         "Inkling 2 Small 在 ARC-AGI-2 得 81.7%，旧版 ==1024 位 [[RSA]]==")

    def test_number_words_and_scales_count_as_in_the_source(self):
        allowed = digest.source_numbers("covering four categories and a 16M-line formalization of 129,826 problems")
        self.assertTrue({"4", "1600", "129826"} <= allowed)
        item = {"id": "c1", "top": True, "source_text": SOURCE + " It spans four categories."}
        self.assertEqual(digest.check_written(written(text=TEXT.replace("密码分析", "4 类密码分析")), item, GLOSS), [])


class BuildTests(unittest.TestCase):
    def build(self, answers, profile=None):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "data").mkdir()
        (tmp / "data" / "2026-11-07.json").write_text(json.dumps(day_data()), encoding="utf-8")
        (tmp / "glossary.json").write_text(json.dumps(GLOSS, ensure_ascii=False), encoding="utf-8")
        fake = FakeCodex(answers)
        self.downloads = []

        def download(url, path):
            self.downloads.append(url)
            path.write_bytes(b"\x89PNG\r\n\x1a\n fake")
            return True
        patcher = mock.patch.object(digest, "download_image", download)
        patcher.start()
        self.addCleanup(patcher.stop)
        result = digest.build(date(2026, 11, 7), digest.Steps(tmp / "work", runner=fake), root=tmp, profile=profile or digest.load_profile("example"))
        return result, fake, tmp

    def answers(self, **overrides):
        marked = TEXT.replace("在人类引导下发现了旧版 [[1024 位]] [[RSA]] 的两处重大弱点", "==在人类引导下发现了旧版 [[1024 位]] [[RSA]] 的两处重大弱点==")
        base = {"select": {"items": [{"ids": ["c1", "nope"], "who": "Google DeepMind", "top": True, "ev": "claim"}],
                           "briefs": [{"ids": ["c3"], "who": "Artificial Analysis"}],
                           "cut": ["@someone：周五部署玩笑（c2）——无信息量"]},
                "write": {"items": [written()]},
                "highlight": {"items": [{"id": "c1", "text": marked}]},
                "lead": {"thread": "今天最值得注意的是 DeepMind 说模型找到了旧版 RSA 的弱点。",
                         "hooks": [{"id": "c1", "hook": "模型找出旧版 1024 位 RSA 的两处弱点"}, {"id": "zz", "hook": "不存在"}],
                         "papers": "", "briefs": "性格基准"},
                "brief": {"items": [{"id": "c3", "text": "性格基准覆盖 30 种特质，真诚度排名高的模型套话也更少。"}]},
                "images": {"items": [{"id": "c1", "image": "c1-1.png", "caption": "DeepMind 推文附图"}]}}
        return base | overrides

    def test_full_run_renders_without_markers(self):
        result, fake, _ = self.build(self.answers())
        self.assertEqual(fake.calls, ["select", "write", "highlight", "brief", "lead", "images"])
        self.assertEqual(fake.models["write"], digest.WRITER)
        self.assertEqual(fake.models["highlight"], digest.WRITER)
        self.assertEqual(fake.models["select"], (digest.MODEL, digest.EFFORT))
        item = result["items"][0]
        self.assertEqual((item["ids"], item["handle"], item["kind_label"], item["ev"]), (["c1"], "@GoogleDeepMind", "X · 2 段 thread", "claim"))
        self.assertIn("==", item["text"])
        self.assertEqual(result["warnings"], [])
        self.assertIn("1 个失败（METR）", result["coverage"])
        content = render.render_article(result)["content"]
        self.assertNotIn("==", content)
        self.assertNotIn("[[", content)
        self.assertIn("RSA</span>：常用公钥加密算法。", content)
        self.assertEqual(result["date_label"], "2026-11-08（周日）")  # the morning the 11-07 window ends
        self.assertIn("收录 11-07 08:00 至 11-08 08:00", content)
        self.assertEqual(result["cut"], ["@someone：周五部署玩笑——无信息量"])
        self.assertEqual(result["briefs"][0]["who"], "Artificial Analysis")
        self.assertIn("快讯", content)
        self.assertEqual(result["toc"]["hooks"], {"c1": "模型找出旧版 1024 位 RSA 的两处弱点"})
        self.assertIn("今日导读", content)
        self.assertIn("模型找出旧版 1024 位 RSA 的两处弱点", content)
        self.assertIn("快讯 · 1 条", content)
        self.assertLess(content.index("今日导读"), content.index(">头条<"))
        self.assertEqual(item["image"], {"file": "c1-1.png", "caption": "DeepMind 推文附图"})
        self.assertTrue(fake.images["images"][0].endswith("c1-1.png"))
        self.assertNotIn("<img", content)  # no image source given: the picture is left out
        with_images = render.render_article(result, image_src=lambda n: render.IMAGE_PLACEHOLDER + n)["content"]
        self.assertIn('src="xdaily-image:c1-1.png"', with_images)
        self.assertIn("图｜DeepMind 推文附图", with_images)

    def test_failing_item_is_retried_with_the_problem(self):
        bad = written(text=TEXT.replace("2048", "4096"))
        result, fake, tmp = self.build(self.answers(write={"items": [bad]}, **{"write-retry": {"items": [written()]}}))
        self.assertIn("write-retry", fake.calls)
        self.assertIn("4096", (tmp / "work" / "write-retry.prompt.md").read_text(encoding="utf-8"))
        self.assertNotIn("4096", result["items"][0]["text"])

    def test_bad_highlight_falls_back_to_plain_text(self):
        result, _, _ = self.build(self.answers(highlight={"items": [{"id": "c1", "text": "完全不同的段落"}]}))
        self.assertEqual(result["items"][0]["text"], TEXT)
        self.assertTrue(result["warnings"])

    def test_cached_steps_are_not_called_again(self):
        _, _, tmp = self.build(self.answers())
        fake = FakeCodex({})
        digest.build(date(2026, 11, 7), digest.Steps(tmp / "work", runner=fake), root=tmp)
        self.assertEqual(fake.calls, [])

    def test_new_terms_are_saved_to_glossary(self):
        out = written(title="DeepMind：[[格密码]] 之外的 [[RSA]]", new_terms=[{"term": "格密码", "definition": "一类基于格问题的密码。"}])
        result, _, tmp = self.build(self.answers(write={"items": [out]}))
        saved = json.loads((tmp / "glossary.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["格密码"], "一类基于格问题的密码。")
        self.assertIn("格密码", result["gloss"])

    def test_profile_feeds_the_prompts(self):
        research = {"summary": "代表工作：示例项目 Alpha。", "links": ["自动形式化的忠实性"], "max_items": 4}
        _, _, tmp = self.build(self.answers(), profile=digest.load_profile("example") | {"research": research})
        select = (tmp / "work" / "select.prompt.md").read_text(encoding="utf-8")
        write = (tmp / "work" / "write.prompt.md").read_text(encoding="utf-8")
        self.assertIn("多模态（约 20%）", select)
        self.assertIn("示例项目 Alpha", write)
        self.assertIn("一天最多 4 条", write)
        self.assertNotIn("{{", select + write)

    def test_profile_without_research_drops_you(self):
        public = {k: v for k, v in digest.load_profile("example").items() if k != "research"}
        result, _, tmp = self.build(self.answers(write={"items": [written(you="和你的自动形式化工作相关。")]}), profile=public)
        self.assertEqual(result["items"][0]["you"], "")
        self.assertNotIn("示例项目 Alpha", (tmp / "work" / "write.prompt.md").read_text(encoding="utf-8"))
        self.assertIn("固定留空", (tmp / "work" / "write.prompt.md").read_text(encoding="utf-8"))

    def test_brief_with_a_number_not_in_the_source_is_dropped(self):
        bad = {"items": [{"id": "c3", "text": "性格基准覆盖 45 种特质，真诚度排名高的模型套话也更少。"}]}
        result, _, _ = self.build(self.answers(brief=bad))
        self.assertEqual(result["briefs"], [])
        self.assertTrue(any("快讯" in w for w in result["warnings"]))

    def test_image_choice_outside_the_offer_or_with_new_numbers_is_ignored(self):
        result, _, _ = self.build(self.answers(images={"items": [{"id": "c1", "image": "other.png", "caption": ""}]}))
        self.assertNotIn("image", result["items"][0])
        result, _, _ = self.build(self.answers(images={"items": [{"id": "c1", "image": "c1-1.png", "caption": "准确率 97%"}]}))
        self.assertEqual(result["items"][0]["image"]["caption"], "图片来自原帖")


    def test_hook_with_a_new_number_falls_back_to_the_title(self):
        lead = {"thread": "今天的主线是 RSA。", "hooks": [{"id": "c1", "hook": "模型找出 3 处 RSA 弱点"}], "papers": "", "briefs": ""}
        result, _, _ = self.build(self.answers(lead=lead))
        self.assertEqual(result["toc"]["hooks"], {})
        self.assertTrue(any("目录导语" in w for w in result["warnings"]))
        self.assertIn("DeepMind：AI 找出旧版 RSA 的两处弱点", render.render_article(result)["content"])


if __name__ == "__main__":
    unittest.main()
