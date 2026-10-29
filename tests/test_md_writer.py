"""Unit tests for `app.core.md_writer`: paragraph merging, deduplication, filename sanitization, and SRT."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.core import md_writer
from app.core.downloader import MediaInfo
from app.core.transcriber import Segment


class TestMdWriter(unittest.TestCase):
    def test_sanitize_filename_and_truncation(self) -> None:
        dirty = '  Video: "How to <Use> Faster*Whisper?" | Part 1/2?  '
        cleaned = md_writer.sanitize_filename(dirty, max_len=80)
        self.assertNotIn(":", cleaned)
        self.assertNotIn("<", cleaned)
        self.assertNotIn("?", cleaned)
        self.assertNotIn("|", cleaned)

        long_title = "A" * 120
        self.assertEqual(len(md_writer.sanitize_filename(long_title, max_len=80)), 80)

    def test_resolve_unique_path_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            p0 = md_writer.resolve_unique_path(out_dir, "demo", ".md")
            p0.write_text("first", encoding="utf-8")

            p1 = md_writer.resolve_unique_path(out_dir, "demo", ".md")
            self.assertEqual(p1.name, "demo (1).md")
            p1.write_text("second", encoding="utf-8")

            p2 = md_writer.resolve_unique_path(out_dir, "demo", ".md")
            self.assertEqual(p2.name, "demo (2).md")

    def test_deduplicate_adjacent_hallucinations(self) -> None:
        segs = [
            Segment(0.0, 2.0, "感谢大家的收看。"),
            Segment(2.0, 4.0, "感谢大家的收看"),
            Segment(4.0, 6.0, "感谢大家的收看！"),
            Segment(6.5, 8.5, "我们下期再见。"),
        ]
        deduped = md_writer.deduplicate_segments(segs)
        self.assertEqual(len(deduped), 2)
        self.assertEqual(deduped[0].text, "感谢大家的收看。")
        self.assertEqual(deduped[1].text, "我们下期再见。")

    def test_merge_segments_gap_and_length_rules(self) -> None:
        segs = [
            Segment(0.0, 1.5, "第一小句"),
            Segment(1.8, 3.2, "第二小句。"),
            # Gap > 2.0s (6.0 - 3.2 = 2.8s) -> must start a new paragraph
            Segment(6.0, 8.0, "这是间隔超过两秒后的新段落。"),
        ]
        paras = md_writer.merge_segments_to_paragraphs(segs)
        self.assertEqual(len(paras), 2)
        self.assertAlmostEqual(paras[0][0], 0.0)
        self.assertIn("第一小句，第二小句。", paras[0][1])
        self.assertAlmostEqual(paras[1][0], 6.0)

    def test_write_markdown_and_srt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            info = MediaInfo(
                url="https://example.com/v1",
                id="v1",
                title="测试视频标题",
                uploader="测试UP主",
                duration=125.0,
                extractor="BiliBili",
            )
            segs = [
                Segment(0.0, 3.5, "大家好，欢迎来到测试视频。"),
                Segment(6.5, 10.2, "这是第二段内容。"),
            ]
            meta = {
                "model": "large-v3-turbo",
                "device": "cuda",
                "compute_type": "float16",
                "language": "zh",
                "language_probability": 0.99,
            }
            md_path = md_writer.write_markdown(
                output_dir=out_dir,
                filename_template="{title}",
                info=info,
                segments=segs,
                meta=meta,
                summary_text="> 核心摘要：这是一个测试视频。",
            )
            self.assertTrue(md_path.exists())
            md_text = md_path.read_text(encoding="utf-8")
            self.assertIn("# 测试视频标题", md_text)
            self.assertIn("large-v3-turbo（cuda/float16）", md_text)
            self.assertIn("> 核心摘要：这是一个测试视频。", md_text)
            self.assertIn("**[00:00:00]** 大家好，欢迎来到测试视频。", md_text)
            self.assertIn("**[00:00:06]** 这是第二段内容。", md_text)

            srt_path = md_writer.write_srt(md_path, segs)
            self.assertTrue(srt_path.exists())
            srt_text = srt_path.read_text(encoding="utf-8")
            self.assertIn("00:00:00,000 --> 00:00:03,500", srt_text)


if __name__ == "__main__":
    unittest.main()
