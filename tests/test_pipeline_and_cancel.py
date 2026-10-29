"""Unit tests for `CancelToken`, fallback chains, model verification, and end-to-end `pipeline.run`."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import Config
from app.core import model_manager, pipeline, transcriber
from app.core.cancel import CancelToken
from app.core.downloader import MediaInfo
from app.core.errors import CancelledError
from app.core.pipeline import Job
from app.core.transcriber import Segment, TranscribeOptions
from app.paths import AppPaths


class TestPipelineAndCancel(unittest.TestCase):
    def test_cancel_token_raises_cancelled_error(self) -> None:
        token = CancelToken()
        self.assertFalse(token.is_cancelled)
        token.check()
        token.cancel()
        self.assertTrue(token.is_cancelled)
        with self.assertRaises(CancelledError):
            token.check()

    def test_compute_type_and_fallback_chain(self) -> None:
        opts_high_vram = TranscribeOptions(device="cuda", vram_gb=8.0)
        chain_high = transcriber.build_fallback_chain(opts_high_vram)
        self.assertEqual(
            chain_high,
            [("cuda", "float16"), ("cuda", "int8_float16"), ("cpu", "int8")],
        )

        opts_low_vram = TranscribeOptions(device="cuda", vram_gb=4.0)
        chain_low = transcriber.build_fallback_chain(opts_low_vram)
        self.assertEqual(
            chain_low,
            [("cuda", "int8_float16"), ("cpu", "int8")],
        )

    def test_verify_model_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            m_dir = Path(tmp) / "small"
            m_dir.mkdir()
            ok, missing = model_manager.verify_model_dir(m_dir)
            self.assertFalse(ok)
            self.assertIn("model.bin", missing)

            (m_dir / "model.bin").write_bytes(b"MODEL")
            (m_dir / "config.json").write_text("{}", encoding="utf-8")
            (m_dir / "vocabulary.txt").write_text("a\nb\n", encoding="utf-8")
            ok2, missing2 = model_manager.verify_model_dir(m_dir)
            self.assertTrue(ok2)
            self.assertEqual(missing2, [])

    def test_pipeline_end_to_end_with_mocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = AppPaths(app_dir=Path(tmp))
            cfg = Config(model="small", use_gpu=False, write_srt=True)

            # Pre-create valid model dir so download_model is skipped
            model_dir = paths.models_dir / "small"
            model_dir.mkdir(parents=True)
            (model_dir / "model.bin").write_bytes(b"MODEL")
            (model_dir / "config.json").write_text("{}", encoding="utf-8")
            (model_dir / "tokenizer.json").write_text("{}", encoding="utf-8")

            fake_info = MediaInfo(
                url="https://example.com/watch?v=123",
                id="123",
                title="端到端流水线测试视频",
                uploader="MoonlightChannel",
                duration=60.0,
                extractor="Youtube",
            )

            def _fake_download(url, workdir, opts, on_progress=None, on_log=None, token=None, paths=None):
                audio_file = workdir / "123.m4a"
                audio_file.write_bytes(b"FAKE_AUDIO")
                if on_progress:
                    on_progress(1.0, "done")
                return audio_file

            def _fake_convert(input_path, output_wav, on_progress=None, on_log=None, token=None, paths=None):
                output_wav.write_bytes(b"RIFF_WAV")
                if on_progress:
                    on_progress(1.0)
                return output_wav

            def _fake_transcribe(wav_path, opts, on_progress=None, on_log=None, token=None, paths=None):
                segs = [
                    Segment(0.0, 4.0, "第一段转录文本。"),
                    Segment(7.0, 12.0, "第二段转录文本。"),
                ]
                meta = {
                    "model": opts.model,
                    "device": "cpu",
                    "compute_type": "int8",
                    "language": "zh",
                    "language_probability": 0.98,
                    "duration": 60.0,
                }
                if on_progress:
                    on_progress(1.0, "done")
                return segs, meta

            with patch("app.core.downloader.fetch_info", return_value=fake_info), patch(
                "app.core.downloader.download_audio", side_effect=_fake_download
            ), patch("app.core.audio.convert_to_wav", side_effect=_fake_convert), patch(
                "app.core.transcriber.transcribe", side_effect=_fake_transcribe
            ):
                job = Job(source="https://example.com/watch?v=123")
                res = pipeline.run(job=job, cfg=cfg, paths=paths)
                self.assertTrue(res.md_path.exists())
                self.assertIsNotNone(res.srt_path)
                self.assertTrue(res.srt_path.exists())
                self.assertIn("第一段转录文本", res.md_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
