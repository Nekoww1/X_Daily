import io
import json
import random
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import radar  # noqa: E402

DAY = date(2026, 11, 6)
NOW = datetime(2026, 11, 7, 9, 0, tzinfo=radar.TAIPEI)
SOURCE = {"name": "S", "type": "feed", "kind": "blog", "coverage": "论文或博客补充", "url": "http://x/feed"}


def rss(*entries):
    body = "".join(
        f"<item><title>{title}</title><link>http://x/{title}</link>"
        + (f"<pubDate>{when}</pubDate>" if when else "")
        + "<description>text</description></item>"
        for title, when in entries
    )
    return f"<rss version='2.0'><channel><title>t</title>{body}</channel></rss>".encode()


def collect(payload):
    start, end = radar.report_window(DAY)
    with mock.patch.object(radar, "fetch", return_value=payload):
        return radar.collect_feed(SOURCE, start, end, NOW)


class XSourceTests(unittest.TestCase):
    source = {"name": "X: @a", "type": "x", "kind": "x", "coverage": radar.X_COVERAGE, "handle": "a"}
    older = "Sun, 01 Nov 2026 10:00:00 +0000"

    def setUp(self):
        self.elapsed = 0.0
        for patcher in (mock.patch.object(radar.time, "sleep", side_effect=self.advance),
                        mock.patch.object(radar.time, "monotonic", side_effect=lambda: self.elapsed)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def advance(self, seconds):
        self.elapsed += seconds

    def http_error(self, code, retry_after=None, body=b""):
        headers = {"Retry-After": retry_after} if retry_after else {}
        return HTTPError("http://rsshub/error", code, "RSSHub error", headers, io.BytesIO(body))

    def collect(self, source=None, **kwargs):
        start, end = radar.report_window(DAY)
        with mock.patch.object(radar, "fetch", **kwargs) as fetch:
            return radar.collect_x(source or self.source, start, end, NOW), fetch

    def post(self, handle, post_id, text, when="Fri, 06 Nov 2026 10:00:00 +0000"):
        title = text if text.startswith(("Re ", "[Subscribers")) else f"t{post_id}"
        return (f"<item><title>{title}</title><link>https://x.com/{handle}/status/{post_id}</link>"
                f"<pubDate>{when}</pubDate><description>{text}</description></item>")

    def feed(self, *posts):
        return f"<rss version='2.0'><channel><title>t</title>{''.join(posts)}</channel></rss>".encode()

    def timeline(self, *posts):
        """A timeline that reaches before the window, so no second page is needed."""
        return self.feed(*posts, self.post("a", 50, "older", self.older), self.post("a", 49, "older2", self.older))

    def test_old_tweets_are_flagged_as_possible_stale_data(self):
        old = "Mon, 01 Jun 2026 10:00:00 +0000"
        got, _ = self.collect(return_value=self.feed(self.post("a", 1, "x", old), self.post("a", 2, "y", old)))
        self.assertEqual(got["status"], "partial")
        self.assertIn("旧数据", got["reason"])
        self.assertEqual(got["items"], [])

    def test_recent_quiet_account_uses_one_request_and_is_not_flagged(self):
        got, fetch = self.collect(return_value=self.timeline())
        self.assertEqual(got["status"], "ok_empty")
        self.assertEqual(fetch.call_count, 1)
        self.assertIn("/twitter/user/a/count=20&includeRts=false&includeReplies=false", fetch.call_args.args[0])

    def test_plain_post_needs_no_detail_request(self):
        got, fetch = self.collect(return_value=self.timeline(self.post("a", 100, "just a post")))
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(got["items"][0]["summary"], "just a post")

    def test_long_post_without_detail_request_is_not_cut_at_blog_limit(self):
        got, _ = self.collect(return_value=self.timeline(self.post("a", 100, "word " * 400)))
        self.assertGreater(len(got["items"][0]["summary"]), 1900)

    def test_short_page_reads_second_page_once(self):
        short = self.feed(self.post("a", 100, "x"))
        got, fetch = self.collect(side_effect=[short, self.timeline(self.post("a", 100, "x"))])
        self.assertEqual(fetch.call_count, 2)
        self.assertIn("count=40", fetch.call_args.args[0])
        self.assertEqual(got["status"], "ok_new")

    def test_still_short_after_second_page_is_partial(self):
        short = self.feed(self.post("a", 1, "pinned", "Fri, 10 Jul 2020 10:00:00 +0000"), self.post("a", 100, "x"))
        got, _ = self.collect(side_effect=[short, short])
        self.assertEqual(got["status"], "partial")
        self.assertIn("漏帖", got["reason"])
        self.assertEqual(len(got["items"]), 1)

    def test_failure_does_not_retry_against_x(self):
        got, fetch = self.collect(side_effect=OSError("503"))
        self.assertEqual(got["status"], "failed")
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fetch.call_args.kwargs["retries"], 0)

    def test_subscriber_only_posts_are_skipped(self):
        got, _ = self.collect(return_value=self.timeline(self.post("a", 100, "[Subscribers Only] x")))
        self.assertEqual(got["items"], [])
        self.assertEqual(got["status"], "ok_empty")
        self.assertIn("跳过 1 条付费", got["reason"])

    def test_thread_keeps_only_authors_continuations_in_order(self):
        timeline = self.timeline(self.post("a", 100, "root 🧵"))
        detail = self.feed(
            self.post("a", 102, "Re second"),
            self.post("someone", 103, "Re @a nice thread"),
            self.post("a", 104, "Re @someone thanks"),
            self.post("a", 101, "Re first"),
            self.post("a", 100, "root 🧵"),
            self.post("advertiser", 90, "buy this"),
        )
        got, fetch = self.collect(side_effect=[timeline, detail])
        self.assertEqual(got["status"], "ok_new")
        self.assertEqual(got["items"][0]["summary"], "root 🧵 first second")
        self.assertEqual(got["items"][0]["thread_parts"], 3)
        self.assertIn("/twitter/tweet/a/status/100", fetch.call_args.args[0])

    def test_self_reply_merges_into_root_with_one_detail_request(self):
        timeline = self.timeline(self.post("a", 100, "root"), self.post("a", 101, "Re @b self reply"))
        detail = self.feed(self.post("a", 100, "root"), self.post("a", 101, "Re @b self reply"))
        got, fetch = self.collect(side_effect=[timeline, detail])
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(len(got["items"]), 1)
        self.assertEqual(got["items"][0]["original_url"], "https://x.com/a/status/100")
        self.assertEqual(got["items"][0]["summary"], "root @b self reply")
        self.assertEqual(got["items"][0]["thread_parts"], 2)

    def test_root_missing_from_timeline_but_in_window_becomes_the_candidate(self):
        timeline = self.timeline(self.post("a", 101, "Re later"))
        detail = self.feed(self.post("a", 100, "root"), self.post("a", 101, "Re later"))
        got, _ = self.collect(side_effect=[timeline, detail])
        self.assertEqual(got["items"][0]["original_url"], "https://x.com/a/status/100")
        self.assertEqual(got["items"][0]["summary"], "root later")

    def test_late_continuation_of_older_thread_keeps_its_own_link(self):
        timeline = self.timeline(self.post("a", 101, "Re new detail"))
        detail = self.feed(self.post("a", 100, "root", self.older), self.post("a", 101, "Re new detail"))
        got, _ = self.collect(side_effect=[timeline, detail])
        item = got["items"][0]
        self.assertEqual(item["original_url"], "https://x.com/a/status/101")
        self.assertEqual(item["summary"], "（接续 11-01 开始的 thread）root new detail")
        self.assertEqual(item["title"], "new detail")

    def test_thread_failure_keeps_posts_and_stops_requests(self):
        timeline = self.timeline(self.post("a", 100, "Re x"), self.post("a", 99, "Re other"))
        got, fetch = self.collect(side_effect=[timeline, OSError("503")])
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(got["status"], "partial")
        self.assertEqual(len(got["items"]), 2)
        self.assertIn("2 条贴文的 thread 未能读取", got["reason"])

    def test_request_budget_stops_detail_reads(self):
        source = dict(self.source, x_pacer=radar.XPacer(max_requests=1))
        got, fetch = self.collect(source=source, return_value=self.timeline(self.post("a", 100, "Re x")))
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(got["status"], "partial")
        self.assertIn("额度已用完", got["reason"])
        self.assertEqual(len(got["items"]), 1)

    def test_consecutive_failures_stop_all_x_requests(self):
        start, end = radar.report_window(DAY)
        sources = [dict(self.source, name=f"X{i}", handle=f"a{i}") for i in range(6)]
        with mock.patch.object(radar, "fetch", side_effect=OSError("401")) as fetch:
            results = radar.collect_all(sources, start, end, NOW)
        self.assertEqual(fetch.call_count, radar.X_MAX_CONSECUTIVE_FAILURES)
        self.assertEqual(len(results), 6)
        self.assertTrue(all(r["status"] == "failed" for r in results))
        self.assertIn("已停止", results[-1]["reason"])

    def test_request_budget_stops_timelines_across_accounts(self):
        start, end = radar.report_window(DAY)
        sources = [dict(self.source, name=f"X{i}", handle=f"a{i}") for i in range(6)]
        with mock.patch.object(radar, "X_MAX_REQUESTS", 2), \
                mock.patch.object(radar, "fetch", return_value=self.timeline()) as fetch:
            results = radar.collect_all(sources, start, end, NOW)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(len(results), 6)
        self.assertIn("额度已用完", results[-1]["reason"])

    def test_request_jitter_stays_within_bounds_and_varies(self):
        pacer = radar.XPacer(seed=7)
        times = []
        with mock.patch.object(radar, "fetch", side_effect=lambda *a, **kw: times.append(self.elapsed) or b"ok"):
            for _ in range(20):
                pacer.fetch("a", "http://rsshub/timeline")
        gaps = [later - earlier for earlier, later in zip(times, times[1:])]
        self.assertEqual(times[0], 0)
        self.assertTrue(all(radar.X_REQUEST_DELAY_SECONDS[0] <= gap <= radar.X_REQUEST_DELAY_SECONDS[1]
                            for gap in gaps))
        self.assertGreater(len(set(gaps)), 1)

    def test_gap_is_longer_when_moving_to_another_account(self):
        pacer = radar.XPacer(seed=11)
        times = []
        with mock.patch.object(radar, "fetch", side_effect=lambda *a, **kw: times.append(self.elapsed) or b"ok"):
            for account in ("a", "a", "b"):
                pacer.fetch(account, "http://rsshub/timeline")
        same, different = times[1] - times[0], times[2] - times[1]
        self.assertTrue(radar.X_ACCOUNT_DELAY_SECONDS[0] <= different <= radar.X_ACCOUNT_DELAY_SECONDS[1])
        self.assertGreater(different, same)

    def test_account_shuffle_is_reproducible_with_fixed_seed(self):
        start, end = radar.report_window(DAY)
        sources = [dict(self.source, name=f"X{i}", handle=f"a{i}") for i in range(8)]
        original = [s["name"] for s in sources]
        expected = original.copy()
        random.Random(7).shuffle(expected)
        for _ in range(2):
            with mock.patch.object(radar, "fetch", return_value=self.timeline()):
                results = radar.collect_all(sources, start, end, NOW, x_seed=7)
            self.assertEqual([r["source"] for r in results], expected)
        self.assertNotEqual(expected, original)
        self.assertEqual([s["name"] for s in sources], original)

    def test_second_page_and_details_share_the_same_pacer(self):
        short = self.feed(self.post("a", 100, "root 🧵"))
        deeper = self.timeline(self.post("a", 100, "root 🧵"))
        detail = self.feed(self.post("a", 100, "root 🧵"), self.post("a", 101, "Re more"))
        payloads, times = iter([short, deeper, detail]), []

        def fetch(url, **kwargs):
            times.append(self.elapsed)
            return next(payloads)

        got, fetch_mock = self.collect(side_effect=fetch)
        self.assertEqual(fetch_mock.call_count, 3)
        self.assertEqual(got["items"][0]["summary"], "root 🧵 more")
        self.assertTrue(all(radar.X_REQUEST_DELAY_SECONDS[0] <= b - a <= radar.X_REQUEST_DELAY_SECONDS[1]
                            for a, b in zip(times, times[1:])))
        self.assertTrue(all(call.kwargs["retries"] == 0 for call in fetch_mock.call_args_list))

    def test_rate_limit_backs_off_then_stops_even_with_success_between_signals(self):
        start, end = radar.report_window(DAY)
        sources = [dict(self.source, name=f"X{i}", handle=f"a{i}") for i in range(6)]
        responses = iter([self.http_error(429, "240"), self.timeline(), self.http_error(503, body=b"Rate limit exceeded")])
        times = []

        def fetch(url, **kwargs):
            times.append(self.elapsed)
            response = next(responses)
            if isinstance(response, Exception):
                raise response
            return response

        metrics = {}
        with mock.patch.object(radar, "fetch", side_effect=fetch) as fetch_mock:
            results = radar.collect_all(sources, start, end, NOW, x_seed=7, x_metrics=metrics)
        self.assertEqual(fetch_mock.call_count, 3)
        self.assertEqual(times[1] - times[0], 240)
        self.assertIn("退避 240 秒", results[0]["reason"])
        self.assertEqual(results[1]["status"], "ok_empty")
        self.assertTrue(all("两次" in r["reason"] and "已停止" in r["reason"] for r in results[2:]))
        self.assertEqual(len({call.args[0] for call in fetch_mock.call_args_list}), 3)
        self.assertEqual(metrics["requests"], 3)
        self.assertEqual(metrics["duration_seconds"], round(self.elapsed, 3))

    def test_rsshub_error_text_triggers_backoff_and_stop(self):
        start, end = radar.report_window(DAY)
        sources = [dict(self.source, name=f"X{i}", handle=f"a{i}") for i in range(4)]
        with mock.patch.object(radar, "fetch", side_effect=[b"<html>Rate limit exceeded</html>",
                                                          b'{"error":"TooManyRequests"}']) as fetch:
            results = radar.collect_all(sources, start, end, NOW)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(self.elapsed, radar.X_BACKOFF_SECONDS)
        self.assertIn("退避", results[0]["reason"])
        self.assertTrue(all("两次" in r["reason"] for r in results[1:]))

    def test_retry_after_http_date_and_invalid_header(self):
        for header, expected in (("60", radar.X_BACKOFF_SECONDS),
                                 (format_datetime(NOW + timedelta(seconds=300)), 300),
                                 ("invalid", radar.X_BACKOFF_SECONDS)):
            with self.subTest(header=header):
                pacer = radar.XPacer(seed=7)
                with mock.patch.object(radar.time, "time", return_value=NOW.timestamp()), \
                        mock.patch.object(radar, "fetch", side_effect=[self.http_error(503, header, b"Rate limit exceeded"), b"ok"]):
                    with self.assertRaisesRegex(RuntimeError, "退避"):
                        pacer.fetch("a", "http://rsshub/a")
                    before = self.elapsed
                    pacer.fetch("b", "http://rsshub/b")
                self.assertEqual(self.elapsed - before, expected)

    def test_plain_rsshub_503_is_an_ordinary_failure(self):
        with mock.patch.object(radar, "fetch", side_effect=[self.http_error(503, body=b"Error: tweet not found")]):
            pacer = radar.XPacer(seed=1)
            with self.assertRaises(HTTPError):
                pacer.fetch("a", "http://rsshub/x")
        self.assertEqual(pacer.trouble_signals, 0)

    def test_rate_limit_text_in_http_error_body_is_detected(self):
        pacer = radar.XPacer(seed=7)
        with mock.patch.object(radar, "fetch", side_effect=self.http_error(400, body=b"RateLimitExceeded")):
            with self.assertRaisesRegex(RuntimeError, "退避"):
                pacer.fetch("a", "http://rsshub/a")
        self.assertEqual(pacer.trouble_signals, 1)

    def test_rate_limit_words_in_real_posts_do_not_trigger_backoff(self):
        pacer = radar.XPacer(seed=7)
        got, _ = self.collect(source=dict(self.source, x_pacer=pacer),
                              return_value=self.timeline(self.post("a", 100, "rate limit exceeded")))
        self.assertEqual(got["status"], "ok_new")
        self.assertEqual(pacer.trouble_signals, 0)

    def test_detail_trouble_is_shared_with_the_next_accounts(self):
        start, end = radar.report_window(DAY)
        sources = [dict(self.source, name=f"X{i}", handle=f"a{i}") for i in range(4)]
        timeline = self.timeline(self.post("a", 100, "root 🧵"))
        with mock.patch.object(radar, "fetch", side_effect=[timeline, self.http_error(503, body=b"Rate limit exceeded"),
                                                          self.http_error(429)]) as fetch:
            results = radar.collect_all(sources, start, end, NOW)
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(results[0]["status"], "partial")
        self.assertIn("thread 未能读取", results[0]["reason"])
        self.assertIn("退避", results[0]["reason"])
        self.assertGreaterEqual(self.elapsed, radar.X_BACKOFF_SECONDS + radar.X_REQUEST_DELAY_SECONDS[0])
        self.assertTrue(all("两次" in r["reason"] for r in results[1:]))

    def test_second_page_trouble_is_reported_and_backed_off(self):
        start, end = radar.report_window(DAY)
        sources = [dict(self.source, name=f"X{i}", handle=f"a{i}") for i in range(2)]
        short = self.feed(self.post("a", 100, "plain post"))
        with mock.patch.object(radar, "fetch", side_effect=[short, self.http_error(503, body=b"Rate limit exceeded"), self.timeline()]) as fetch:
            results = radar.collect_all(sources, start, end, NOW)
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(results[0]["status"], "partial")
        self.assertIn("第 2 页未能读取", results[0]["reason"])
        self.assertIn("退避", results[0]["reason"])
        self.assertGreaterEqual(self.elapsed, radar.X_BACKOFF_SECONDS + radar.X_REQUEST_DELAY_SECONDS[0])

    def test_long_retry_after_stops_instead_of_exceeding_phase_limit(self):
        start, end = radar.report_window(DAY)
        sources = [dict(self.source, name=f"X{i}", handle=f"a{i}") for i in range(4)]
        with mock.patch.object(radar, "fetch", side_effect=self.http_error(429, str(radar.X_PHASE_SECONDS + 60))) as fetch:
            results = radar.collect_all(sources, start, end, NOW)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(self.elapsed, 0)
        self.assertIn(f"退避 {radar.X_PHASE_SECONDS + 60} 秒", results[0]["reason"])
        self.assertTrue(all("时间上限" in r["reason"] and "已停止" in r["reason"] for r in results[1:]))

    def test_request_timeout_is_limited_to_remaining_phase_time(self):
        pacer = radar.XPacer(seed=7)
        with mock.patch.object(radar, "fetch", return_value=b"ok") as fetch:
            pacer.fetch("a", "http://rsshub/a")
            self.advance(radar.X_PHASE_SECONDS - 30)
            pacer.fetch("a", "http://rsshub/detail", timeout=90)
        self.assertGreater(fetch.call_args.kwargs["timeout"], 0)
        self.assertLess(fetch.call_args.kwargs["timeout"], 30)
        self.advance(30)
        with mock.patch.object(radar, "fetch") as fetch:
            with self.assertRaisesRegex(RuntimeError, "时间上限"):
                pacer.fetch("b", "http://rsshub/b")
        fetch.assert_not_called()

    def test_mass_failure_warns_that_x_content_is_untrustworthy(self):
        bad = {"coverage": radar.X_COVERAGE, "status": "failed"}
        good = dict(bad, status="ok_new")
        self.assertIn("不可信", radar.x_coverage_note([bad, bad, good]))
        self.assertNotIn("不可信", radar.x_coverage_note([good, good, bad]))
        self.assertIn("没有接入", radar.x_coverage_note([]))


class WindowTests(unittest.TestCase):
    def test_taipei_boundary_is_applied_to_utc_entries(self):
        got = collect(rss(
            ("before", "Thu, 05 Nov 2026 23:59:00 +0000"),  # the day runs 08:00-08:00 Beijing = 00:00-00:00 UTC
            ("first", "Fri, 06 Nov 2026 00:00:00 +0000"),
            ("last", "Fri, 06 Nov 2026 23:59:00 +0000"),
            ("after", "Sat, 07 Nov 2026 00:00:00 +0000"),
        ))
        self.assertEqual([i["title"] for i in got["items"]], ["first", "last"])
        self.assertEqual(got["status"], "ok_new")



class ImageTests(unittest.TestCase):
    def test_post_photos_are_kept_without_avatars_or_duplicates(self):
        raw = ('<p>Results</p><img src="https://pbs.twimg.com/media/HWs55?format=png&amp;name=orig">'
               '<img src="https://pbs.twimg.com/profile_images/1/a.jpg"><img src="https://pbs.twimg.com/media/HWs55?format=png">')
        self.assertEqual(radar.images_in(raw), ["https://pbs.twimg.com/media/HWs55?format=png"])
        source = {"name": "X: @a", "kind": "x", "coverage": "x"}
        item = radar.make_item(source, "t", "https://x.com/a/status/1", NOW, raw, NOW)
        self.assertEqual(item["images"], ["https://pbs.twimg.com/media/HWs55?format=png"])
        self.assertNotIn("images", radar.make_item(dict(source, kind="blog"), "t", "u", NOW, raw, NOW))

class StatusTests(unittest.TestCase):
    def test_old_entries_mean_no_update_and_are_not_reported_as_today(self):
        got = collect(rss(("old", "Mon, 01 Jun 2026 10:00:00 +0000")))
        self.assertEqual(got["status"], "ok_empty")
        self.assertEqual(got["items"], [])
        self.assertTrue(got["latest_published_at"].startswith("2026-06-01"))

    def test_fetch_error_is_failed_not_empty(self):
        start, end = radar.report_window(DAY)
        with mock.patch.object(radar, "fetch", side_effect=OSError("boom")):
            got = radar.collect_feed(SOURCE, start, end, NOW)
        self.assertEqual(got["status"], "failed")
        self.assertIn("boom", got["reason"])

    def test_garbage_response_is_failed(self):
        self.assertEqual(collect(b"<html>login required</html>")["status"], "failed")

    def test_undated_entries_are_excluded_and_flagged(self):
        got = collect(rss(("nodate", None), ("ok", "Fri, 06 Nov 2026 01:00:00 +0000")))
        self.assertEqual(got["status"], "partial")
        self.assertEqual([i["title"] for i in got["items"]], ["ok"])

    def test_pre_1970_date_does_not_crash(self):
        got = collect(rss(("ancient", "Sat, 01 Jan 1955 00:00:00 +0000"), ("ok", "Fri, 06 Nov 2026 01:00:00 +0000")))
        self.assertEqual([i["title"] for i in got["items"]], ["ok"])

    def test_unexpected_source_error_only_fails_that_source(self):
        start, end = radar.report_window(DAY)
        hf = dict(SOURCE, name="HF", type="hf_daily")
        with mock.patch.object(radar, "fetch", return_value=b'[{"paper": {}}]'):
            got = radar.collect_all([hf], start, end, NOW)
        self.assertEqual(got[0]["status"], "failed")

    def test_feed_that_no_longer_reaches_window_is_partial(self):
        self.assertEqual(collect(rss(("new", "Mon, 09 Nov 2026 01:00:00 +0000")))["status"], "partial")


def item(url, source="S", **extra):
    base = {"source": source, "kind": "blog", "coverage": "c", "title": "An agent paper", "original_url": url,
            "published_at": "2026-11-06T01:00:00+00:00", "fetched_at": "x", "summary": "text"}
    base.update(extra)
    return base


class CandidateTests(unittest.TestCase):
    def results(self, *items):
        return [{"source": "S", "coverage": "c", "status": "ok_new", "reason": "",
                 "latest_published_at": None, "items": list(items)}]

    def test_same_arxiv_paper_from_two_sources_merges(self):
        candidates, _ = radar.merge_candidates(self.results(
            item("http://arxiv.org/abs/2611.08277v1", "arXiv"),
            item("https://arxiv.org/abs/2611.08277", "HF", upvotes=9),
        ), {}, DAY)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["also_from"], ["HF"])
        self.assertEqual(candidates[0]["upvotes"], 9)

    def test_rerun_same_day_keeps_items_but_next_day_drops_them(self):
        results = self.results(item("http://x/a"))
        first, _ = radar.merge_candidates(results, {}, DAY)
        seen = radar.update_seen({}, first, DAY)
        rerun, _ = radar.merge_candidates(results, seen, DAY)
        self.assertEqual(len(rerun), 1)
        nextday, dropped = radar.merge_candidates(results, seen, date(2026, 11, 7))
        self.assertEqual(nextday, [])
        self.assertEqual(dropped, 1)


class SectionTests(unittest.TestCase):
    def section(self, title, summary="text"):
        return radar.section_of({"title": title, "summary": summary})

    def test_generic_evaluation_or_formalize_in_abstract_stays_broad(self):
        self.assertEqual(self.section("An image model", "Our evaluation shows we formalize mathematics of colour."),
                         radar.SECTIONS[2])

    def test_specific_topics_are_routed(self):
        self.assertEqual(self.section("Futures of Maths Launch"), radar.SECTIONS[1])
        self.assertEqual(self.section("A prover", "We use a proof assistant."), radar.SECTIONS[1])
        self.assertEqual(self.section("VisSearch-Bench for Multimodal Agents"), radar.SECTIONS[0])


class SelectionTests(unittest.TestCase):
    def test_invented_ids_are_rejected_and_links_come_from_candidates(self):
        candidates = [dict(item("http://x/real"), id="c1", section=radar.SECTIONS[0], also_from=[], key="k")]
        raw = {"items": [
            {"ids": ["c99"], "title_zh": "假", "what": "w", "why": "y"},
            {"ids": ["c1"], "title_zh": "真", "what": "w", "why": "y", "evidence": "瞎编", "section": "nope"},
        ]}
        selected, rejected = radar.validate_selection(raw, candidates)
        self.assertEqual(rejected, 1)
        self.assertEqual(selected[0]["items"][0]["original_url"], "http://x/real")
        self.assertEqual(selected[0]["evidence"], "未独立核验的声称")
        self.assertEqual(selected[0]["section"], radar.SECTIONS[0])


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(radar, "load_dotenv")  # Tests must never read a real .env.
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "sources.json").write_text(json.dumps({
            "not_covered": ["X 原帖"],
            "sources": [SOURCE, dict(SOURCE, name="Broken", url="http://broken/feed")],
        }), encoding="utf-8")

    def fake_fetch(self, url, **_):
        if "broken" in url:
            raise OSError("down")
        return rss(("agent-news", "Fri, 06 Nov 2026 01:00:00 +0000"))

    def run_report(self, use_llm=False):
        with mock.patch.object(radar, "fetch", self.fake_fetch):
            path, *_ = radar.run(DAY, use_llm, root=self.root, now=NOW)
        return path.read_text(encoding="utf-8")

    def test_partial_failure_still_reports_and_names_the_failure(self):
        report = self.run_report()
        self.assertIn("http://x/agent-news", report)
        self.assertIn("失败 1 个", report)
        self.assertIn("| Broken |", report)
        self.assertIn("抓取失败", report)
        self.assertIn("没有接入 X 原帖", report)

    def test_json_records_x_request_count_and_phase_duration(self):
        sources = [SOURCE] + [dict(XSourceTests.source, name=f"X{i}", handle=f"a{i}") for i in range(2)]
        (self.root / "sources.json").write_text(json.dumps({"sources": sources}), encoding="utf-8")
        clock = [0.0]

        def sleep(seconds):
            clock[0] += seconds

        def fetch(url, **kwargs):
            if "/twitter/" in url:
                clock[0] += 2  # Include response time, as well as pacing, in the X phase.
                return rss(("old1", XSourceTests.older), ("old2", XSourceTests.older))
            clock[0] += 100  # Non-X collection must not count towards the X phase.
            return self.fake_fetch(url)

        with mock.patch.object(radar, "fetch", side_effect=fetch), \
                mock.patch.object(radar.time, "sleep", side_effect=sleep), \
                mock.patch.object(radar.time, "monotonic", side_effect=lambda: clock[0]):
            radar.run(DAY, False, root=self.root, now=NOW)
        data = json.loads((self.root / "data" / f"{DAY.isoformat()}.json").read_text(encoding="utf-8"))
        self.assertEqual(data["x_metrics"]["requests"], 2)
        self.assertEqual(data["x_metrics"]["duration_seconds"], round(clock[0] - 100, 3))
        self.assertTrue(24 <= data["x_metrics"]["duration_seconds"] <= 38)

    def test_json_records_zero_x_metrics_without_x_sources(self):
        self.run_report()
        data = json.loads((self.root / "data" / f"{DAY.isoformat()}.json").read_text(encoding="utf-8"))
        self.assertEqual(data["x_metrics"], {"requests": 0, "duration_seconds": 0.0})

    def test_rerun_produces_identical_report(self):
        self.assertEqual(self.run_report(), self.run_report())

    def test_all_failed_does_not_claim_a_quiet_day(self):
        with mock.patch.object(radar, "fetch", side_effect=OSError("down")):
            path, *_ = radar.run(DAY, False, root=self.root, now=NOW)
        report = path.read_text(encoding="utf-8")
        self.assertIn("不能判断当天是否真的没有动态", report)
        self.assertNotIn("统计窗口内没有新内容", report)

    def test_llm_failure_falls_back_to_rule_list(self):
        with mock.patch.dict("os.environ", {"LLM_API_KEY": "test"}), \
                mock.patch.object(radar, "call_llm", side_effect=TimeoutError()):
            report = self.run_report(use_llm=True)
        self.assertIn("LLM 调用失败", report)
        self.assertIn("候选清单", report)
        self.assertIn("http://x/agent-news", report)

    def test_llm_selection_is_rendered(self):
        raw = {"items": [{"ids": ["c1"], "title_zh": "中文标题", "what": "发生了", "why": "因为",
                          "evidence": "一手发布", "section": radar.SECTIONS[0], "must_read": True}]}
        with mock.patch.dict("os.environ", {"LLM_API_KEY": "test"}), \
                mock.patch.object(radar, "call_llm", return_value=(raw, {})):
            report = self.run_report(use_llm=True)
        self.assertIn("## 今日必读", report)
        self.assertIn("### 1. 中文标题", report)
        self.assertIn("(http://x/agent-news)", report)


if __name__ == "__main__":
    unittest.main()
