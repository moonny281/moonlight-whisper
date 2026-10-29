"""Unit tests for `app.core.summarizer` OpenAI-compatible summary hook."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.config import LlmConfig
from app.core import summarizer
from app.core.transcriber import Segment


class TestSummarizer(unittest.TestCase):
    def test_disabled_returns_placeholder(self) -> None:
        cfg = LlmConfig(enabled=False, api_key="sk-123")
        segs = [Segment(0.0, 5.0, "测试句子")]
        res = summarizer.generate_summary(segs, "标题", cfg)
        self.assertEqual(res, summarizer.PLACEHOLDER_SUMMARY)

    def test_successful_openai_compatible_response(self) -> None:
        cfg = LlmConfig(
            enabled=True,
            base_url="https://api.openai.com/v1",
            api_key="sk-test",
            model="gpt-4o-mini",
        )
        segs = [Segment(0.0, 5.0, "今天我们讲解 faster-whisper 的使用方法。")]
        mock_response = {
            "choices": [
                {
                    "message": {
                        "content": "核心摘要：本视频介绍了 faster-whisper 的使用方法。\n- 要点一：配置简单"
                    }
                }
            ]
        }

        with patch("app.core.http_client.post_json", return_value=mock_response) as mock_post:
            out = summarizer.generate_summary(segs, "教程", cfg, proxy="socks5://127.0.0.1:1080")
            self.assertIn("> 核心摘要：本视频介绍了 faster-whisper 的使用方法。", out)
            self.assertIn("> - 要点一：配置简单", out)
            mock_post.assert_called_once()
            self.assertEqual(mock_post.call_args.kwargs["proxy"], "socks5://127.0.0.1:1080")

    def test_network_failure_falls_back_to_placeholder(self) -> None:
        cfg = LlmConfig(enabled=True, api_key="sk-test")
        segs = [Segment(0.0, 5.0, "测试内容")]
        with patch("app.core.http_client.post_json", side_effect=OSError("Connection refused")):
            out = summarizer.generate_summary(segs, "教程", cfg)
            self.assertEqual(out, summarizer.PLACEHOLDER_SUMMARY)


if __name__ == "__main__":
    unittest.main()
