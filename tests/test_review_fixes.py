"""Regression unit tests verifying all P1, P2, and P3 code review fixes."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core import downloader, gpu_runtime, http_client, md_writer, model_manager
from app.core.cancel import CancelToken
from app.core.errors import CancelledError
from app.core.transcriber import Segment
from app.paths import AppPaths


class TestReviewFixes(unittest.TestCase):
    def test_p1_2_youtube_flat_id_and_bilibili_multipart_url_normalization(self) -> None:
        # 1. YouTube flat entry with only 11-char ID in `url`
        yt_entry = {"id": "dQw4w9WgXcQ", "url": "dQw4w9WgXcQ", "ie_key": "Youtube", "title": "YT Demo"}
        norm_yt = downloader.normalize_entry_url(
            yt_entry,
            fallback_url="https://www.youtube.com/playlist?list=PL123",
            playlist_index=1,
        )
        self.assertEqual(norm_yt, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")

        # 2. Bilibili multi-part video where flat entry lacks ?p=N
        bili_entry = {"id": "BV1xx411c7mD_p3", "title": "第三集"}
        norm_bili = downloader.normalize_entry_url(
            bili_entry,
            fallback_url="https://www.bilibili.com/video/BV1xx411c7mD",
            playlist_index=3,
        )
        self.assertEqual(norm_bili, "https://www.bilibili.com/video/BV1xx411c7mD?p=3")

    def test_p1_3_pep503_simple_index_html_parsing(self) -> None:
        sample_html = """
        <html><body>
        <a href="../../packages/nvidia_cublas_cu12-12.1.3.1-py3-none-manylinux1_x86_64.whl#sha256=aaaa">nvidia_cublas_cu12-12.1.3.1-py3-none-manylinux1_x86_64.whl</a>
        <a href="../../packages/nvidia_cublas_cu12-12.1.3.1-py3-none-win_amd64.whl#sha256=1111">nvidia_cublas_cu12-12.1.3.1-py3-none-win_amd64.whl</a>
        <a href="../../packages/nvidia_cublas_cu12-12.4.5.8-py3-none-win_amd64.whl#sha256=2222">nvidia_cublas_cu12-12.4.5.8-py3-none-win_amd64.whl</a>
        </body></html>
        """
        wheels = http_client.parse_pep503_simple_index(
            sample_html,
            base_url="https://mirrors.aliyun.com/pypi/simple/nvidia-cublas-cu12/",
            win_amd64_only=True,
        )
        self.assertEqual(len(wheels), 2)
        self.assertEqual(wheels[0]["filename"], "nvidia_cublas_cu12-12.4.5.8-py3-none-win_amd64.whl")
        self.assertEqual(wheels[0]["version"], "12.4.5.8")
        self.assertEqual(wheels[0]["digests"]["sha256"], "2222")
        self.assertTrue(wheels[0]["url"].startswith("https://mirrors.aliyun.com/pypi/packages/"))

    def test_p1_1_streaming_model_download_supports_instant_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = AppPaths(app_dir=Path(tmp))
            token = CancelToken()

            def _fake_stream_dl(url, dest_file, proxy="", expected_sha256="", on_chunk_progress=None, token=None, timeout=60):
                if on_chunk_progress:
                    on_chunk_progress(512 * 1024, 10 * 1024 * 1024)
                # Simulate user clicking Cancel mid-stream
                token.cancel()
                token.check()
                return dest_file

            with patch(
                "app.core.model_manager._fetch_repo_file_list",
                return_value=[("model.bin", 10 * 1024 * 1024)],
            ), patch("app.core.http_client.stream_download_with_resume", side_effect=_fake_stream_dl):
                with self.assertRaises(CancelledError):
                    model_manager.download_model("small", token=token, paths=paths)

    def test_p2_1_full_cudnn9_dll_list_and_handle_deduplication(self) -> None:
        required = gpu_runtime.required_files()
        self.assertIn("cudnn_engines_precompiled64_9.dll", required)
        self.assertIn("cudnn_engines_runtime_compiled64_9.dll", required)
        self.assertIn("cudnn_heuristic64_9.dll", required)
        self.assertIn("cudnn_adv64_9.dll", required)
        self.assertEqual(len(required), 10)

        with tempfile.TemporaryDirectory() as tmp:
            gpu_dir = Path(tmp) / "gpu_runtime"
            gpu_dir.mkdir()
            gpu_runtime.clear_dll_directory_handles()
            try:
                gpu_runtime.register_dll_directory(gpu_dir)
                gpu_runtime.register_dll_directory(gpu_dir)
                self.assertEqual(len(gpu_runtime._DLL_DIR_HANDLES), 1)
            finally:
                gpu_runtime.clear_dll_directory_handles()

    def test_p2_3_chinese_closing_quote_punctuation_merging(self) -> None:
        segs = [
            Segment(0.0, 1.5, "他说：“你好。”"),
            Segment(1.8, 3.2, "随后离开了房间。"),
        ]
        paras = md_writer.merge_segments_to_paragraphs(segs)
        self.assertEqual(len(paras), 1)
        # Must NOT insert an extra comma after `。”`
        self.assertEqual(paras[0][1], "他说：“你好。”随后离开了房间。")


if __name__ == "__main__":
    unittest.main()
