"""Regression unit tests verifying all P1, P2, P3, v1.2.1, v1.2.2, v1.2.3, and v1.2.4 fixes."""

from __future__ import annotations

import contextlib
import http.cookiejar
import json
import math
import struct
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch

import numpy as np  # Pre-import C-extension at module load so sys.modules never reloads it

from app.core import downloader, gpu_runtime, http_client, md_writer, model_manager
from app.core.cancel import CancelToken
from app.core.downloader import DownloadOptions
from app.core.errors import CancelledError
from app.core.transcriber import Segment, TranscribeOptions
from app.paths import AppPaths


@contextlib.contextmanager
def _patch_sys_module(name: str, fake_mod: Any) -> Iterator[None]:
    """Patch a single entry in `sys.modules` without clearing other C-extension modules on exit."""
    had_prev = name in sys.modules
    prev_mod = sys.modules.get(name)
    sys.modules[name] = fake_mod
    try:
        yield
    finally:
        if had_prev and prev_mod is not None:
            sys.modules[name] = prev_mod
        else:
            sys.modules.pop(name, None)


class TestReviewFixes(unittest.TestCase):
    def test_p1_2_youtube_flat_id_and_bilibili_multipart_url_normalization(self) -> None:
        yt_entry = {"id": "dQw4w9WgXcQ", "url": "dQw4w9WgXcQ", "ie_key": "Youtube", "title": "YT Demo"}
        norm_yt = downloader.normalize_entry_url(
            yt_entry,
            fallback_url="https://www.youtube.com/playlist?list=PL123",
            playlist_index=1,
        )
        self.assertEqual(norm_yt, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")

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

            def _fake_stream_dl(url, dest_file, proxy="", expected_sha256="", on_chunk_progress=None, token=None, timeout=60, expected_size=0):
                if on_chunk_progress:
                    on_chunk_progress(512 * 1024, 10 * 1024 * 1024)
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
        self.assertEqual(paras[0][1], "他说：“你好。”随后离开了房间。")

    def test_v1_1_0_version_and_llm_connection_helper(self) -> None:
        import app
        from app.config import LlmConfig
        from app.core import summarizer
        self.assertEqual(app.__version__, "1.2.8")

        empty_cfg = LlmConfig(enabled=True, api_key="")
        ok, msg = summarizer.test_llm_connection(empty_cfg)
        self.assertFalse(ok)
        self.assertIn("API Key", msg)

        valid_cfg = LlmConfig(enabled=True, api_key="sk-test", model="gpt-4o-mini")
        with patch("app.core.http_client.post_json", return_value={"choices": [{"message": {"content": "OK"}}]}):
            ok2, msg2 = summarizer.test_llm_connection(valid_cfg)
            self.assertTrue(ok2)
            self.assertIn("OK", msg2)

    def test_v1_1_1_strict_vocab_check_and_chinese_path_model_loading(self) -> None:
        import os
        from app.core import transcriber
        with tempfile.TemporaryDirectory() as tmp:
            incomplete_dir = Path(tmp) / "中文路径_模型测试" / "large-v3-turbo"
            incomplete_dir.mkdir(parents=True)
            (incomplete_dir / "model.bin").write_bytes(b"REAL_MODEL_WEIGHTS_HEADER")
            (incomplete_dir / "config.json").write_text("{}", encoding="utf-8")
            (incomplete_dir / "tokenizer.json").write_text("{}", encoding="utf-8")

            ok, missing = model_manager.verify_model_dir(incomplete_dir)
            self.assertFalse(ok)
            self.assertIn("vocabulary.json / vocabulary.txt", missing)

            (incomplete_dir / "vocabulary.json").write_text("{}", encoding="utf-8")
            paths = AppPaths(app_dir=Path(tmp) / "中文路径_模型测试", custom_models_dir=str(incomplete_dir.parent))
            self.assertTrue(model_manager.is_model_installed("large-v3-turbo", paths=paths))
            self.assertTrue((incomplete_dir / "preprocessor_config.json").exists())

            recorded_args = []

            class DummyWhisperModel:
                def __init__(self, model_size_or_path, **kwargs):
                    recorded_args.append((model_size_or_path, os.getcwd(), kwargs))

            fake_fw = type(sys)("faster_whisper")
            fake_fw.WhisperModel = DummyWhisperModel  # type: ignore[attr-defined]
            with _patch_sys_module("faster_whisper", fake_fw):
                transcriber.MODEL_CACHE.clear()
                transcriber.MODEL_CACHE.get_or_create(
                    model_path_or_size="large-v3-turbo",
                    device="cpu",
                    compute_type="int8",
                    cpu_threads=0,
                    download_root=incomplete_dir.parent,
                )
                self.assertEqual(len(recorded_args), 1)
                passed_arg, cwd_during_init, kw = recorded_args[0]
                self.assertTrue(passed_arg.isascii())
                self.assertTrue(kw.get("local_files_only"))
                if passed_arg == ".":
                    self.assertEqual(Path(cwd_during_init).resolve(), incomplete_dir.resolve())
                else:
                    self.assertTrue(Path(passed_arg).exists())

    def test_v1_2_1_pyav19_metadata_errors_compat_and_pure_python_wav_loader(self) -> None:
        from app.core import transcriber

        fake_av = type(sys)("av")
        called_kwargs = []

        def _strict_pyav19_open(file_arg, mode="r"):
            called_kwargs.append((file_arg, mode))
            return "CONTAINER_OK"

        fake_av.open = _strict_pyav19_open  # type: ignore[attr-defined]
        with _patch_sys_module("av", fake_av):
            transcriber._patch_runtime_compat()
            res = fake_av.open("test.wav", mode="r", metadata_errors="ignore")
            self.assertEqual(res, "CONTAINER_OK")
            self.assertEqual(called_kwargs, [("test.wav", "r")])

        with tempfile.TemporaryDirectory() as tmp:
            wav_file = Path(tmp) / "中文音频_16k.wav"
            with wave.open(str(wav_file), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                samples = [int(16000 * math.sin(2 * math.pi * 440 * i / 16000)) for i in range(1600)]
                wf.writeframes(struct.pack("<" + "h" * len(samples), *samples))

            arr = transcriber.load_wav_as_float32_array(wav_file)
            self.assertIsNotNone(arr)
            self.assertEqual(arr.shape, (1600,))
            self.assertEqual(str(arr.dtype), "float32")

            model_dir = Path(tmp) / "models" / "small"
            model_dir.mkdir(parents=True)
            (model_dir / "model.bin").write_bytes(b"VALID_BIN_WEIGHTS")
            (model_dir / "config.json").write_text("{}", encoding="utf-8")
            (model_dir / "tokenizer.json").write_text("{}", encoding="utf-8")
            (model_dir / "vocabulary.json").write_text("{}", encoding="utf-8")

            vad_calls = []

            class DummyInfo:
                duration = 2.5
                language = "zh"
                language_probability = 0.99

            class DummySeg:
                start = 0.0
                end = 2.5
                text = "测试语音转录成功"

            class DummyModelWithBrokenVad:
                def __init__(self, *args, **kwargs):
                    pass

                def transcribe(self, audio_in, **kwargs):
                    vad_calls.append(kwargs.get("vad_filter"))
                    if kwargs.get("vad_filter"):
                        raise RuntimeError("onnxruntime SileroVAD failed")
                    return iter([DummySeg()]), DummyInfo()

            fake_fw = type(sys)("faster_whisper")
            fake_fw.WhisperModel = DummyModelWithBrokenVad  # type: ignore[attr-defined]
            with _patch_sys_module("faster_whisper", fake_fw):
                transcriber.MODEL_CACHE.clear()
                opts = TranscribeOptions(model="small", device="cpu", vad=True, models_dir=model_dir.parent)
                segs, meta = transcriber.transcribe(wav_file, opts)
                self.assertEqual(vad_calls, [True, False])
                self.assertEqual(len(segs), 1)
                self.assertEqual(segs[0].text, "测试语音转录成功")

    def test_v1_2_3_tiktok_direct_fallback_and_multi_format_isolated_cookies(self) -> None:
        tk_exc = RuntimeError(
            "ERROR: [TikTok] 7696957163670998294: Unexpected response from webpage request"
        )
        translated = downloader._translate_ydl_exception(tk_exc)
        self.assertNotIn("年龄验证", translated.user_message)

        with tempfile.TemporaryDirectory() as tmp:
            temp_dir = Path(tmp) / "temp"

            json_cookie_file = Path(tmp) / "tiktok_cookies.json"
            json_cookie_file.write_text(
                json.dumps(
                    [
                        {
                            "domain": ".tiktok.com",
                            "name": "sessionid",
                            "value": "abc123xyz",
                            "path": "/",
                            "secure": True,
                            "expirationDate": 0,
                        }
                    ]
                ),
                encoding="utf-8",
            )
            netscape_path = downloader.prepare_cookie_file(
                json_cookie_file,
                temp_dir,
                target_url="https://vt.tiktok.com/ZSgwwQnPH/",
            )
            self.assertIsNotNone(netscape_path)
            self.assertNotEqual(netscape_path, json_cookie_file)
            content = netscape_path.read_text(encoding="utf-8")
            self.assertTrue(content.startswith("# Netscape HTTP Cookie File"))
            self.assertIn(".tiktok.com\tTRUE\t/\tTRUE\t", content)
            self.assertNotIn("\t0\tsessionid", content)

            raw_hdr_file = Path(tmp) / "header_cookie.txt"
            raw_hdr_file.write_text("Cookie: sessionid=tok999; ttwid=tw888", encoding="utf-8")
            hdr_out = downloader.prepare_cookie_file(
                raw_hdr_file,
                temp_dir,
                target_url="https://vt.tiktok.com/ZSgwwQnPH/",
            )
            self.assertIsNotNone(hdr_out)
            hdr_text = hdr_out.read_text(encoding="utf-8")
            self.assertIn(".tiktok.com\tTRUE\t/\tTRUE\t", hdr_text)
            self.assertIn("sessionid\ttok999", hdr_text)

            class BrokenYoutubeDL:
                def __init__(self, opts):
                    pass

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    pass

                def extract_info(self, url, download=False):
                    raise RuntimeError("ERROR: [TikTok] 7696957163670998294: Unexpected response from webpage request")

            fake_ytdlp = type(sys)("yt_dlp")
            fake_ytdlp.YoutubeDL = BrokenYoutubeDL  # type: ignore[attr-defined]

            fake_tikwm_resp = {
                "code": 0,
                "data": {
                    "id": "7696957163670998294",
                    "title": "TikTok 备用通道测试视频",
                    "duration": 18.5,
                    "play": "https://cdn.example.com/tiktok_7696957163670998294.mp4",
                    "author": {"nickname": "hetawoodard"},
                },
            }

            def _fake_dl_stream(url, dest_file, proxy="", expected_sha256="", on_chunk_progress=None, token=None, timeout=60, expected_size=0):
                dest_file.write_bytes(b"FAKE_MP4_AUDIO_STREAM")
                if on_chunk_progress:
                    on_chunk_progress(100, 100)
                return dest_file

            downloader._DIRECT_MEDIA_CACHE.clear()
            paths = AppPaths(app_dir=Path(tmp))
            dl_opts = DownloadOptions(cookies_file=str(raw_hdr_file))

            with _patch_sys_module("yt_dlp", fake_ytdlp), patch(
                "app.core.http_client.fetch_json", return_value=fake_tikwm_resp
            ), patch("app.core.http_client.stream_download_with_resume", side_effect=_fake_dl_stream):
                info = downloader.fetch_info("https://vt.tiktok.com/ZSgwwQnPH/", dl_opts, paths=paths)
                self.assertEqual(info.id, "7696957163670998294")
                self.assertEqual(info.title, "TikTok 备用通道测试视频")

                audio_file = downloader.download_audio(
                    "https://vt.tiktok.com/ZSgwwQnPH/",
                    workdir=Path(tmp) / "work",
                    opts=dl_opts,
                    paths=paths,
                )
                self.assertTrue(audio_file.exists())
                self.assertEqual(audio_file.name, "7696957163670998294.mp4")

    def test_v1_2_4_youtube_cookie_sabr_fallback_and_mozilla_cookiejar_strict_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = AppPaths(app_dir=Path(tmp))
            # 1. Test Netscape file with empty-value trailing tab (`GPS\t`) & mismatched domain dot (`www.youtube.com\tTRUE`)
            raw_youtube_cookies = (
                "# Netscape HTTP Cookie File\n"
                "www.youtube.com\tTRUE\t/\tTRUE\t0\tGPS\t\n"
                ".youtube.com\tFALSE\t/\tTRUE\t1800000000\tSAPISID\tsapi_val_123\n"
            )
            saved_path, count, sites = downloader.import_cookies_from_text(raw_youtube_cookies, paths=paths)
            self.assertEqual(count, 2)
            self.assertIn("YouTube/Google", sites)

            # Verify Python's standard library MozillaCookieJar loads the normalized file with zero AssertionError!
            jar = http.cookiejar.MozillaCookieJar(str(saved_path))
            jar.load(ignore_discard=True, ignore_expires=True)
            self.assertEqual(len(jar), 2)

            # 2. Verify YouTube fallback to cookie-free `android_vr` when logged-in cookies fail (e.g. rotated cookies / SABR)
            calls = []

            class YouTubeMockYDL:
                def __init__(self, opts):
                    self.opts = opts

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    pass

                def extract_info(self, url, download=False):
                    has_cookie = "cookiefile" in self.opts
                    clients = (self.opts.get("extractor_args") or {}).get("youtube", {}).get("player_client")
                    calls.append((has_cookie, clients))
                    if has_cookie:
                        raise RuntimeError("ERROR: [youtube] mp28JPs25ek: Requested format is not available")
                    return {
                        "id": "mp28JPs25ek",
                        "title": "YouTube 免 Cookie 降级成功测试",
                        "uploader": "YTChannel",
                        "duration": 120.0,
                        "extractor_key": "Youtube",
                        "webpage_url": url,
                    }

            fake_ytdlp = type(sys)("yt_dlp")
            fake_ytdlp.YoutubeDL = YouTubeMockYDL  # type: ignore[attr-defined]

            dl_opts = DownloadOptions(cookies_file=str(saved_path))
            with _patch_sys_module("yt_dlp", fake_ytdlp):
                info = downloader.fetch_info("https://www.youtube.com/watch?v=mp28JPs25ek", dl_opts, paths=paths)
                self.assertEqual(info.title, "YouTube 免 Cookie 降级成功测试")
                # Must have tried with cookies first, then multi-client with cookies, then succeeded without cookies on android_vr!
                self.assertEqual(len(calls), 3)
                self.assertTrue(calls[0][0])
                self.assertTrue(calls[1][0])
                self.assertFalse(calls[2][0])
                self.assertIn("android_vr", calls[2][1])


if __name__ == "__main__":
    unittest.main()
