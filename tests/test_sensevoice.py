"""Tests for SenseVoiceSmall support (no real model required)."""

from __future__ import annotations

import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np

from app.core import md_writer, model_manager, sensevoice_engine as sv, transcriber
from app.core.errors import TranscribeError
from app.core.transcriber import Segment, TranscribeOptions
from app.paths import AppPaths


class _FakeSession:
    """Minimal stand-in for `onnxruntime.InferenceSession` carrying SenseVoice metadata."""

    def __init__(self, logits: np.ndarray | None = None) -> None:
        self._logits = logits
        self.last_feed = None

    def get_modelmeta(self):
        dim = 560

        class _Meta:
            custom_metadata_map = {
                "neg_mean": ",".join(["-1.0"] * dim),
                "inv_stddev": ",".join(["0.5"] * dim),
                "lfr_window_size": "7",
                "lfr_window_shift": "6",
                "normalize_samples": "0",
                "lang_auto": "0",
                "lang_zh": "3",
                "lang_en": "4",
                "lang_yue": "7",
                "lang_ja": "11",
                "lang_ko": "12",
                "lang_nospeech": "13",
                "with_itn": "14",
                "without_itn": "15",
            }

        return _Meta()

    def run(self, _names, feed):
        self.last_feed = feed
        return [self._logits]


def _make_model(tokens, logits=None) -> sv.SenseVoiceModel:
    return sv.SenseVoiceModel(_FakeSession(logits), tokens, "model.int8.onnx")


def _write_valid_sensevoice_dir(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "model.int8.onnx").write_bytes(b"\x08\x07ONNX_MODEL_WEIGHTS")
    (directory / "tokens.txt").write_text("<unk> 0\n<s> 1\n", encoding="utf-8")


class SenseVoiceModelManagerTests(unittest.TestCase):
    def test_spec_registered_with_sensevoice_engine(self) -> None:
        spec = model_manager.MODEL_SPEC_MAP["SenseVoiceSmall"]
        self.assertEqual(spec.engine, model_manager.ENGINE_SENSEVOICE)
        self.assertEqual(model_manager.get_model_engine("SenseVoiceSmall"), model_manager.ENGINE_SENSEVOICE)
        self.assertEqual(model_manager.get_model_engine("small"), model_manager.ENGINE_WHISPER)
        self.assertEqual(model_manager.get_model_engine("someone/custom-repo"), model_manager.ENGINE_WHISPER)

    def test_verify_sensevoice_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "SenseVoiceSmall"
            ok, missing = model_manager.verify_model_dir(d, engine="sensevoice")
            self.assertFalse(ok)
            self.assertEqual(missing, ["model.int8.onnx", "tokens.txt"])

            d.mkdir()
            (d / "model.int8.onnx").write_bytes(b"version https://git-lfs.github.com/spec/v1\noid sha256:x")
            (d / "tokens.txt").write_text("<!DOCTYPE html><html></html>", encoding="utf-8")
            ok, missing = model_manager.verify_model_dir(d, engine="sensevoice")
            self.assertFalse(ok)
            self.assertEqual(missing, ["model.int8.onnx", "tokens.txt"])

            _write_valid_sensevoice_dir(d)
            ok, missing = model_manager.verify_model_dir(d, engine="sensevoice")
            self.assertTrue(ok)
            self.assertEqual(missing, [])

    def test_fp32_weights_accepted_but_int8_preferred(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "model.onnx").write_bytes(b"FP32_WEIGHTS_BYTES")
            (d / "tokens.txt").write_text("<unk> 0\n", encoding="utf-8")
            self.assertEqual(model_manager.find_sensevoice_weights(d).name, "model.onnx")
            (d / "model.int8.onnx").write_bytes(b"INT8_WEIGHTS_BYTES")
            self.assertEqual(model_manager.find_sensevoice_weights(d).name, "model.int8.onnx")

    def test_whisper_verification_is_unchanged_and_not_confused_by_sensevoice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "small"
            _write_valid_sensevoice_dir(d)
            ok, missing = model_manager.verify_model_dir(d)  # default engine = whisper
            self.assertFalse(ok)
            self.assertIn("model.bin", missing)

    def test_is_model_installed_and_list_models(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = AppPaths(app_dir=Path(tmp))
            self.assertFalse(model_manager.is_model_installed("SenseVoiceSmall", paths=paths))
            _write_valid_sensevoice_dir(paths.models_dir / "SenseVoiceSmall")
            self.assertTrue(model_manager.is_model_installed("SenseVoiceSmall", paths=paths))
            statuses = {st.spec.name: st for st in model_manager.list_models(paths)}
            self.assertTrue(statuses["SenseVoiceSmall"].installed)
            self.assertFalse(statuses["small"].installed)
            # No Whisper-only artefact may be written into a SenseVoice directory.
            self.assertFalse((paths.models_dir / "SenseVoiceSmall" / "preprocessor_config.json").exists())

    def test_import_model_directory_validates_by_engine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = AppPaths(app_dir=Path(tmp) / "app")
            src = Path(tmp) / "downloaded"
            _write_valid_sensevoice_dir(src)
            target = model_manager.import_model_directory("SenseVoiceSmall", src, paths=paths)
            self.assertTrue((target / "model.int8.onnx").exists())
            self.assertTrue((target / "tokens.txt").exists())
            self.assertFalse((target / "preprocessor_config.json").exists())

            # A SenseVoice folder is not a valid Whisper model, and vice versa.
            from app.core.errors import ModelError
            with self.assertRaises(ModelError):
                model_manager.import_model_directory("small", src, paths=paths)

    def test_fetch_repo_file_list_fallback_for_sensevoice(self) -> None:
        with patch("app.core.http_client.fetch_json", side_effect=RuntimeError("offline")):
            files = model_manager._fetch_repo_file_list("r/x", "https://hf-mirror.com", "", engine="sensevoice")
        self.assertEqual([f[0] for f in files], ["model.int8.onnx", "tokens.txt"])

    def test_fetch_repo_file_list_filters_sensevoice_repo(self) -> None:
        meta = {
            "siblings": [
                {"rfilename": "model.onnx", "size": 937617178},
                {"rfilename": "model.int8.onnx", "size": 239233841},
                {"rfilename": "tokens.txt", "size": 315894},
                {"rfilename": "export-onnx.py", "size": 5905},
            ]
        }
        with patch("app.core.http_client.fetch_json", return_value=meta):
            files = model_manager._fetch_repo_file_list("r/x", "https://hf-mirror.com", "", engine="sensevoice")
        # fp32 weights (~900 MB) must not be downloaded.
        self.assertEqual(sorted(f[0] for f in files), ["model.int8.onnx", "tokens.txt"])


class SenseVoiceFrontendAndDecodeTests(unittest.TestCase):
    def test_load_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fp = Path(tmp) / "tokens.txt"
            fp.write_text("<unk> 0\n<s> 1\n\u2581the 2\n<|zh|> 3\n", encoding="utf-8")
            toks = sv.load_tokens(fp)
            self.assertEqual(toks, ["<unk>", "<s>", "\u2581the", "<|zh|>"])
            fp.write_text("\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                sv.load_tokens(fp)

    def test_lfr_cmvn_shape_and_values(self) -> None:
        model = _make_model(["<unk>"])
        frames = np.arange(100 * 80, dtype=np.float32).reshape(100, 80)
        out = model._apply_lfr_cmvn(frames)
        self.assertEqual(out.shape, (int(np.ceil(100 / 6)), 560))
        # First LFR frame = 3 copies of frame 0 (left pad) + frames 0..3, then (x - 1.0) * 0.5.
        expected_first = np.concatenate([frames[0]] * 3 + [frames[i] for i in range(4)])
        np.testing.assert_allclose(out[0], (expected_first - 1.0) * 0.5, rtol=1e-5)
        # Last window is completed by repeating the final frame (no audio is dropped).
        self.assertTrue(np.isfinite(out).all())

    def test_extract_features_matches_expected_frame_count(self) -> None:
        try:
            import kaldi_native_fbank  # noqa: F401
        except ImportError:
            self.skipTest("kaldi-native-fbank not installed")
        model = _make_model(["<unk>"])
        samples = (np.random.RandomState(0).randn(16000 * 3) * 0.1).astype(np.float32)
        feats = model.extract_features(samples)
        n_fbank = 1 + (len(samples) - 400) // 160           # snip_edges=True
        self.assertEqual(feats.shape, (int(np.ceil(n_fbank / 6)), 560))
        self.assertEqual(feats.dtype, np.float32)

    def test_decode_logits_collapses_repeats_strips_tags_and_detects_language(self) -> None:
        tokens = ["<unk>", "<|zh|>", "<|NEUTRAL|>", "<|Speech|>", "<|withitn|>", "\u2581hello", "\u4f60", "\u597d", "\u2581world"]
        model = _make_model(tokens)
        seq = [1, 2, 3, 4, 0, 5, 5, 0, 6, 7, 7, 8, 0]  # blank-separated repeats collapse; 0 = blank
        logits = np.full((len(seq), len(tokens)), -10.0, dtype=np.float32)
        for t, tid in enumerate(seq):
            logits[t, tid] = 5.0
        text, lang = model.decode_logits(logits)
        self.assertEqual(lang, "zh")
        self.assertEqual(text, "hello\u4f60\u597d world")

    def test_decode_logits_empty_and_blank_only(self) -> None:
        model = _make_model(["<unk>", "a"])
        self.assertEqual(model.decode_logits(np.zeros((0, 2), dtype=np.float32)), ("", ""))
        blanks = np.zeros((5, 2), dtype=np.float32)
        blanks[:, 0] = 1.0
        self.assertEqual(model.decode_logits(blanks), ("", ""))

    def test_recognize_feeds_language_and_itn_ids(self) -> None:
        try:
            import kaldi_native_fbank  # noqa: F401
        except ImportError:
            self.skipTest("kaldi-native-fbank not installed")
        logits = np.zeros((1, 10, 3), dtype=np.float32)
        model = _make_model(["<unk>", "a", "b"], logits=logits)
        samples = np.zeros(16000, dtype=np.float32)
        model.recognize(samples, "ja", use_itn=True)
        feed = model.session.last_feed
        self.assertEqual(int(feed["language"][0]), 11)
        self.assertEqual(int(feed["text_norm"][0]), 14)
        model.recognize(samples, "bogus", use_itn=False)
        feed = model.session.last_feed
        self.assertEqual(int(feed["language"][0]), 0)      # unknown -> auto
        self.assertEqual(int(feed["text_norm"][0]), 15)
        self.assertEqual(feed["x"].shape[0], 1)
        self.assertEqual(feed["x"].shape[2], 560)

    def test_invalid_onnx_metadata_is_rejected(self) -> None:
        class _Bad(_FakeSession):
            def get_modelmeta(self):
                class _M:
                    custom_metadata_map = {}
                return _M()

        with self.assertRaises(ValueError):
            sv.SenseVoiceModel(_Bad(), ["<unk>"], "model.int8.onnx")


class SenseVoiceChunkingAndDispatchTests(unittest.TestCase):
    def test_fixed_windows(self) -> None:
        self.assertEqual(sv.fixed_windows(0), [])
        sr = sv.SAMPLE_RATE
        spans = sv.fixed_windows(int(40 * sr))
        self.assertEqual(spans[0], (0, int(15 * sr)))
        self.assertEqual(spans[-1][1], int(40 * sr))
        self.assertEqual(len(spans), 3)
        # A tail shorter than 1 s is merged into the previous window.
        spans = sv.fixed_windows(int(30.5 * sr))
        self.assertEqual(len(spans), 2)
        self.assertEqual(spans[-1][1], int(30.5 * sr))

    def test_plan_chunks_falls_back_to_fixed_windows_on_vad_error(self) -> None:
        audio = np.zeros(int(20 * sv.SAMPLE_RATE), dtype=np.float32)
        logs = []
        with patch.object(sv, "vad_chunks", side_effect=RuntimeError("onnx boom")):
            spans = sv._plan_chunks(audio, TranscribeOptions(model="SenseVoiceSmall", vad=True), logs.append)
        self.assertEqual(spans, sv.fixed_windows(len(audio)))
        self.assertTrue(any("VAD 回退" in m for m in logs))

    def test_plan_chunks_without_vad_uses_fixed_windows(self) -> None:
        audio = np.zeros(int(20 * sv.SAMPLE_RATE), dtype=np.float32)
        with patch.object(sv, "vad_chunks") as vad:
            spans = sv._plan_chunks(audio, TranscribeOptions(model="SenseVoiceSmall", vad=False), None)
        vad.assert_not_called()
        self.assertEqual(spans, sv.fixed_windows(len(audio)))

    def _write_wav(self, path: Path, seconds: float = 2.0) -> None:
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(np.zeros(int(16000 * seconds), dtype=np.int16).tobytes())

    def test_transcribe_dispatches_to_sensevoice_and_builds_segments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = AppPaths(app_dir=Path(tmp))
            _write_valid_sensevoice_dir(paths.models_dir / "SenseVoiceSmall")
            wav = Path(tmp) / "a.wav"
            self._write_wav(wav, 20.0)

            class _FakeModel:
                is_int8 = True
                weights_name = "model.int8.onnx"
                device = "cpu"

                def __init__(self) -> None:
                    self.calls = []

                def recognize(self, samples, language, use_itn=True):
                    self.calls.append((len(samples), language, use_itn))
                    return ("\u7e41\u9ad4\u4e2d\u6587", "zh") if len(self.calls) == 1 else ("hello", "en")

            fake = _FakeModel()
            progress = []
            with patch.object(sv.SENSEVOICE_CACHE, "get_or_create", return_value=fake):
                opts = TranscribeOptions(model="SenseVoiceSmall", vad=False, language="fr", models_dir=paths.models_dir)
                logs = []
                segs, meta = transcriber.transcribe(
                    wav, opts, on_progress=lambda r, m: progress.append(r), on_log=logs.append, paths=paths
                )

            self.assertEqual(len(segs), 2)
            self.assertEqual(segs[0].text, "\u7e41\u4f53\u4e2d\u6587")      # zh output is simplified
            self.assertEqual(segs[1].text, "hello")
            self.assertTrue(all(c[1] == "auto" for c in fake.calls))       # unsupported "fr" -> auto
            self.assertTrue(any("仅支持" in m for m in logs))
            self.assertEqual(meta["model"], "SenseVoiceSmall")
            self.assertEqual(meta["device"], "cpu")
            self.assertEqual(meta["compute_type"], "int8")
            self.assertIsNone(meta["language_probability"])
            self.assertEqual(progress, sorted(progress))
            self.assertAlmostEqual(progress[-1], 1.0, places=2)

    def test_japanese_kanji_is_not_converted_to_simplified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = AppPaths(app_dir=Path(tmp))
            _write_valid_sensevoice_dir(paths.models_dir / "SenseVoiceSmall")
            wav = Path(tmp) / "a.wav"
            self._write_wav(wav, 3.0)

            class _JaModel:
                is_int8 = True
                weights_name = "model.int8.onnx"
                device = "cpu"

                def recognize(self, samples, language, use_itn=True):
                    return "\u8ca9\u58f2\u306e\u30d1\u30f3", "ja"

            with patch.object(sv.SENSEVOICE_CACHE, "get_or_create", return_value=_JaModel()), patch.object(
                transcriber, "_convert_to_simplified", side_effect=lambda t: "CONVERTED"
            ):
                segs, meta = transcriber.transcribe(
                    wav,
                    TranscribeOptions(model="SenseVoiceSmall", vad=False, models_dir=paths.models_dir),
                    paths=paths,
                )
            self.assertEqual(segs[0].text, "\u8ca9\u58f2\u306e\u30d1\u30f3")
            self.assertEqual(meta["language"], "ja")

    def test_missing_model_raises_actionable_error_matching_self_heal_keywords(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = AppPaths(app_dir=Path(tmp))
            wav = Path(tmp) / "a.wav"
            self._write_wav(wav)
            with self.assertRaises(TranscribeError) as cm:
                transcriber.transcribe(
                    wav, TranscribeOptions(model="SenseVoiceSmall", models_dir=paths.models_dir), paths=paths
                )
            text = f"{cm.exception.user_message} {cm.exception.detail}".lower()
            self.assertIn("model.int8.onnx", text)       # pipeline auto-repair keyword

    def test_cache_clear_helpers(self) -> None:
        sv.SENSEVOICE_CACHE._model = object()  # type: ignore[assignment]
        sv.SENSEVOICE_CACHE._key = ("x", 1)
        transcriber.clear_all_model_caches()
        self.assertIsNone(sv.SENSEVOICE_CACHE._model)
        self.assertIsNone(transcriber.MODEL_CACHE._model)


class MarkdownLanguageCellTests(unittest.TestCase):
    def test_language_probability_none_omits_confidence(self) -> None:
        from app.core.downloader import MediaInfo

        info = MediaInfo(url="https://x", id="1", title="t", uploader="u", duration=10.0, extractor="Local")
        segs = [Segment(0.0, 1.0, "你好")]
        meta = {
            "model": "SenseVoiceSmall",
            "device": "cpu",
            "compute_type": "int8",
            "language": "zh",
            "language_probability": None,
        }
        out = md_writer.render_markdown(info, segs, meta)
        self.assertIn("| 语言 | zh |", out)
        self.assertNotIn("置信度", out)
        self.assertIn("SenseVoiceSmall（cpu/int8）", out)

        out2 = md_writer.render_markdown(info, segs, dict(meta, language_probability=0.97))
        self.assertIn("置信度 0.97", out2)


if __name__ == "__main__":
    unittest.main()
