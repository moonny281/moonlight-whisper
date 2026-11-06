"""Tests for the language catalog, language routing, SenseVoice GPU support and the pipeline wiring (v1.4.4)."""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

from app.config import DEFAULT_ROUTE_RULES, Config
from app.core import gpu_runtime, languages, model_manager, pipeline, router, sensevoice_engine as sv, transcriber
from app.core.downloader import MediaInfo
from app.core.errors import GpuError, TranscribeError
from app.core.gpu_runtime import GpuInfo, GpuStatus
from app.core.pipeline import Job
from app.core.transcriber import Segment, TranscribeOptions
from app.paths import AppPaths


def _write_whisper_dir(paths: AppPaths, name: str) -> None:
    d = paths.models_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "model.bin").write_bytes(b"MODEL_BINARY_DATA")
    (d / "config.json").write_text("{}", encoding="utf-8")
    (d / "tokenizer.json").write_text("{}", encoding="utf-8")
    (d / "vocabulary.json").write_text("{}", encoding="utf-8")


def _write_sensevoice_dir(paths: AppPaths, fp32: bool = False) -> Path:
    d = paths.models_dir / "SenseVoiceSmall"
    d.mkdir(parents=True, exist_ok=True)
    (d / "model.int8.onnx").write_bytes(b"INT8_WEIGHTS_BYTES")
    (d / "tokens.txt").write_text("<unk> 0\n", encoding="utf-8")
    if fp32:
        (d / "model.onnx").write_bytes(b"FP32_WEIGHTS_BYTES")
    return d


class LanguageCatalogTests(unittest.TestCase):
    def test_codes_match_faster_whisper(self) -> None:
        try:
            from faster_whisper.tokenizer import _LANGUAGE_CODES
        except Exception:
            self.skipTest("faster-whisper not available")
        self.assertEqual(set(_LANGUAGE_CODES), set(languages.ALL_CODES))

    def test_options_and_labels(self) -> None:
        opts = languages.language_options()
        self.assertEqual(opts[0], (languages.AUTO_LABEL, None))
        self.assertEqual(len(opts), 1 + len(languages.ALL_CODES))
        self.assertEqual(len({code for _, code in opts}), len(opts))      # no duplicates
        self.assertEqual(opts[1][1], "zh")
        self.assertEqual(languages.language_label("yue"), "粤语 (yue)")
        self.assertEqual(languages.language_label(None), languages.AUTO_LABEL)

    def test_supported_languages_per_model(self) -> None:
        sv_langs = languages.supported_languages("SenseVoiceSmall")
        self.assertEqual(sv_langs, frozenset({"zh", "en", "ja", "ko", "yue"}))
        self.assertIn("yue", languages.supported_languages("large-v3-turbo"))
        self.assertIn("yue", languages.supported_languages("large-v3"))
        self.assertNotIn("yue", languages.supported_languages("small"))
        self.assertIn("fr", languages.supported_languages("small"))
        self.assertIsNone(languages.supported_languages("someone/custom-ct2-repo"))

    def test_is_supported_and_normalize(self) -> None:
        self.assertTrue(languages.is_language_supported("SenseVoiceSmall", None))
        self.assertFalse(languages.is_language_supported("SenseVoiceSmall", "fr"))
        self.assertTrue(languages.is_language_supported("custom/repo", "yue"))
        self.assertEqual(languages.normalize_language("large-v3", "yue"), ("yue", ""))
        self.assertEqual(languages.normalize_language("small", "auto"), (None, ""))
        lang, note = languages.normalize_language("small", "yue")
        self.assertIsNone(lang)
        self.assertIn("large-v3", note)
        lang, note = languages.normalize_language("SenseVoiceSmall", "fr")
        self.assertIsNone(lang)
        self.assertIn("zh/en/ja/ko/yue", note)


class ConfigRoutingTests(unittest.TestCase):
    def test_defaults_and_roundtrip(self) -> None:
        cfg = Config()
        self.assertFalse(cfg.route_enabled)
        self.assertEqual(cfg.route_rules, DEFAULT_ROUTE_RULES)
        self.assertIsNot(cfg.route_rules, DEFAULT_ROUTE_RULES)           # per-instance copy
        cfg.route_enabled = True
        cfg.route_rules = {"ja": "SenseVoiceSmall", "fr": "large-v3"}
        cfg.route_fallback_model = "small"
        cfg.route_detect_model = "tiny"
        loaded = Config.from_dict(cfg.to_dict())
        self.assertTrue(loaded.route_enabled)
        self.assertEqual(loaded.route_rules, {"ja": "SenseVoiceSmall", "fr": "large-v3"})
        self.assertEqual((loaded.route_fallback_model, loaded.route_detect_model), ("small", "tiny"))

    def test_invalid_rules_are_dropped_and_old_configs_get_defaults(self) -> None:
        cfg = Config.from_dict({"route_rules": {"ZH": " SenseVoiceSmall ", "xx": "small", "fr": "", 5: "small"}})
        self.assertEqual(cfg.route_rules, {"zh": "SenseVoiceSmall"})
        old = Config.from_dict({"model": "small"})                        # config.json written by v1.4.3
        self.assertFalse(old.route_enabled)
        self.assertEqual(old.route_rules, DEFAULT_ROUTE_RULES)
        self.assertEqual(Config.from_dict({"route_rules": "garbage"}).route_rules, DEFAULT_ROUTE_RULES)
        self.assertEqual(Config.from_dict({"route_fallback_model": "  "}).route_fallback_model, "large-v3-turbo")


class RouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.paths = AppPaths(app_dir=Path(self._tmp.name))
        _write_whisper_dir(self.paths, "base")
        self.wav = Path(self._tmp.name) / "a.wav"
        self.wav.write_bytes(b"RIFF")
        self.logs: list = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _cfg(self, **kw) -> Config:
        base = dict(model="small", route_enabled=True, route_fallback_model="large-v3-turbo")
        base.update(kw)
        return Config(**base)

    def _route(self, cfg: Config, detect=None):
        ctx = patch.object(transcriber, "detect_language", return_value=detect) if detect is not None else patch.object(
            transcriber, "detect_language", side_effect=AssertionError("detection must not run")
        )
        with ctx as m:
            decision = router.resolve_route(cfg, self.wav, on_log=self.logs.append, paths=self.paths)
        return decision, m

    def test_disabled_keeps_selected_model_and_normalizes_language(self) -> None:
        cfg = self._cfg(route_enabled=False, language="yue")              # "small" has no Cantonese
        d, _ = self._route(cfg)
        self.assertEqual((d.model, d.language, d.routed), ("small", None, False))
        self.assertTrue(any("large-v3" in m for m in self.logs))

    def test_explicit_language_uses_rules_without_detection(self) -> None:
        d, _ = self._route(self._cfg(language="zh"))
        self.assertEqual((d.model, d.language, d.routed), ("SenseVoiceSmall", "zh", True))
        d, _ = self._route(self._cfg(language="fr"))
        self.assertEqual((d.model, d.language), ("large-v3-turbo", "fr"))

    def test_auto_detection_routes_by_detected_language(self) -> None:
        d, m = self._route(self._cfg(), detect=("zh", 0.97))
        # SenseVoice decides Mandarin vs Cantonese itself -> engine language stays auto
        self.assertEqual((d.model, d.language, d.detected_language), ("SenseVoiceSmall", None, "zh"))
        m.assert_called_once()
        d, _ = self._route(self._cfg(), detect=("de", 0.9))
        self.assertEqual((d.model, d.language, d.detected_probability), ("large-v3-turbo", "de", 0.9))

    def test_low_confidence_or_failed_detection_uses_selected_model(self) -> None:
        d, _ = self._route(self._cfg(), detect=("fr", 0.2))
        self.assertEqual((d.model, d.language, d.routed), ("small", None, False))
        d, _ = self._route(self._cfg(), detect=(None, 0.0))
        self.assertEqual((d.model, d.language, d.routed), ("small", None, False))

    def test_rule_pointing_to_model_without_language_support_falls_back(self) -> None:
        cfg = self._cfg(language="fr", route_rules={"fr": "SenseVoiceSmall"})
        d, _ = self._route(cfg)
        self.assertEqual(d.model, "large-v3-turbo")                       # fallback model supports French
        self.assertEqual(
            router.pick_model_for_language("yue", {}, "small", "large-v3"), "large-v3"
        )                                                                  # fallback "small" cannot do Cantonese

    def test_detect_model_downloaded_when_missing_and_failure_is_not_fatal(self) -> None:
        shutil_target = self.paths.models_dir / "base"
        import shutil

        shutil.rmtree(shutil_target)
        with patch.object(model_manager, "download_model", side_effect=RuntimeError("offline")) as dl, patch.object(
            transcriber, "detect_language", side_effect=AssertionError("must not detect")
        ):
            d = router.resolve_route(self._cfg(), self.wav, on_log=self.logs.append, paths=self.paths)
        dl.assert_called_once()
        self.assertEqual(dl.call_args.kwargs["model_name"], "base")
        self.assertEqual((d.model, d.routed), ("small", False))

    def test_non_whisper_detect_model_is_replaced_by_base(self) -> None:
        cfg = self._cfg(route_detect_model="SenseVoiceSmall")
        with patch.object(transcriber, "detect_language", return_value=("en", 0.9)) as m:
            router.resolve_route(cfg, self.wav, paths=self.paths)
        self.assertEqual(m.call_args.args[1], "base")


class DetectLanguageTests(unittest.TestCase):
    def _wav(self, tmp: str) -> Path:
        import wave

        p = Path(tmp) / "a.wav"
        with wave.open(str(p), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(np.zeros(16000 * 2, dtype=np.int16).tobytes())
        return p

    def test_uses_vad_and_multiple_segments_when_supported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            model = MagicMock()
            model.detect_language = MagicMock(return_value=("ja", 0.93, []))
            import inspect

            def detect(audio=None, features=None, vad_filter=False, vad_parameters=None, language_detection_segments=1):
                return ("ja", 0.93, [])

            model.detect_language = MagicMock(side_effect=detect)
            model.detect_language.__signature__ = inspect.signature(detect)  # type: ignore[attr-defined]
            with patch.object(transcriber.MODEL_CACHE, "get_or_create", return_value=model):
                lang, prob = transcriber.detect_language(self._wav(tmp), "base", models_dir=Path(tmp))
            self.assertEqual((lang, prob), ("ja", 0.93))
            kwargs = model.detect_language.call_args.kwargs
            self.assertTrue(kwargs["vad_filter"])
            self.assertEqual(kwargs["language_detection_segments"], 3)

    def test_retries_without_vad_when_vad_finds_no_speech(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            calls = []

            def detect(audio=None, features=None, vad_filter=False, language_detection_segments=1):
                calls.append(vad_filter)
                if vad_filter:
                    raise ValueError("need at least one array to concatenate")
                return ("en", 0.8, [])

            model = MagicMock()
            model.detect_language = detect
            with patch.object(transcriber.MODEL_CACHE, "get_or_create", return_value=model):
                lang, prob = transcriber.detect_language(self._wav(tmp), "base", models_dir=Path(tmp))
            self.assertEqual((lang, prob), ("en", 0.8))
            self.assertEqual(calls, [True, False])

    def test_failures_return_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logs: list = []
            with patch.object(transcriber.MODEL_CACHE, "get_or_create", side_effect=RuntimeError("no model")):
                self.assertEqual(
                    transcriber.detect_language(self._wav(tmp), "base", models_dir=Path(tmp), on_log=logs.append),
                    (None, 0.0),
                )
            self.assertTrue(any("语言检测" in m for m in logs))


class SenseVoiceGpuTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.paths = AppPaths(app_dir=Path(self._tmp.name))
        self.logs: list = []
        sv.SENSEVOICE_CACHE.clear()

    def tearDown(self) -> None:
        sv.SENSEVOICE_CACHE.clear()
        self._tmp.cleanup()

    def _try_cuda(self, model_dir: Path):
        return sv._SenseVoiceCache._try_load_cuda(model_dir, model_dir / "tokens.txt", 4, self.logs.append)

    def test_cuda_falls_back_when_onnxruntime_is_cpu_build(self) -> None:
        d = _write_sensevoice_dir(self.paths, fp32=True)
        with patch.object(sv, "_gpu_runtime_has_cuda_provider", return_value=False):
            self.assertIsNone(self._try_cuda(d))
        self.assertTrue(any("onnxruntime-gpu" in m for m in self.logs))

    def test_cuda_falls_back_without_fp32_weights(self) -> None:
        d = _write_sensevoice_dir(self.paths, fp32=False)
        with patch.object(sv, "_gpu_runtime_has_cuda_provider", return_value=True):
            self.assertIsNone(self._try_cuda(d))
        self.assertTrue(any("model.onnx" in m for m in self.logs))

    def test_cuda_session_failure_falls_back(self) -> None:
        d = _write_sensevoice_dir(self.paths, fp32=True)
        with patch.object(sv, "_gpu_runtime_has_cuda_provider", return_value=True), patch.object(
            sv, "_make_session", side_effect=RuntimeError("LoadLibrary failed with error 126 cudnn64_9.dll")
        ):
            self.assertIsNone(self._try_cuda(d))
        self.assertTrue(any("GPU 回退" in m and "126" in m for m in self.logs))

    def test_cuda_loads_and_is_warmed_up(self) -> None:
        d = _write_sensevoice_dir(self.paths, fp32=True)

        class _Sess:
            ran = 0

            def get_modelmeta(self):
                from tests.test_sensevoice import _FakeSession

                return _FakeSession().get_modelmeta()

            def run(self, *_a, **_k):
                _Sess.ran += 1
                return [np.zeros((1, 4, 3), dtype=np.float32)]

        with patch.object(sv, "_gpu_runtime_has_cuda_provider", return_value=True), patch.object(
            sv, "_make_session", return_value=_Sess()
        ) as mk, patch.object(sv, "load_tokens", return_value=["<unk>", "a", "b"]):
            try:
                model = self._try_cuda(d)
            except ImportError:
                self.skipTest("kaldi-native-fbank missing")
        self.assertEqual(mk.call_args.args[2], "cuda")
        self.assertEqual(mk.call_args.args[0].name, "model.onnx")
        self.assertIsNotNone(model)
        self.assertEqual(model.device, "cuda")
        self.assertEqual(_Sess.ran, 1)                                    # warm-up inference happened

    def test_real_cpu_session_options_for_cuda_request_are_built_with_cuda_provider(self) -> None:
        captured = {}

        class _FakeOrt:
            class SessionOptions:
                pass

            @staticmethod
            def InferenceSession(source, sess_options=None, providers=None):
                captured["providers"] = providers

                class S:
                    @staticmethod
                    def get_providers():
                        return ["CPUExecutionProvider"]

                return S()

        with tempfile.TemporaryDirectory() as tmp:
            w = Path(tmp) / "model.onnx"
            w.write_bytes(b"x")
            with patch.dict("sys.modules", {"onnxruntime": _FakeOrt}):
                with self.assertRaises(RuntimeError):                      # CUDA EP not in the session's providers
                    sv._make_session(w, 4, "cuda")
        first = captured["providers"][0]
        self.assertEqual(first[0], "CUDAExecutionProvider")
        self.assertEqual(first[1]["cudnn_conv_algo_search"], "HEURISTIC")
        self.assertEqual(captured["providers"][-1], "CPUExecutionProvider")

    def test_runtime_cuda_error_retries_on_cpu(self) -> None:
        d = _write_sensevoice_dir(self.paths, fp32=True)
        wav = Path(self._tmp.name) / "a.wav"
        import wave

        with wave.open(str(wav), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(np.zeros(16000 * 4, dtype=np.int16).tobytes())

        class _Gpu:
            is_int8, weights_name, device = False, "model.onnx", "cuda"

            def recognize(self, *_a, **_k):
                raise RuntimeError("CUDA error: out of memory")

        class _Cpu:
            is_int8, weights_name, device = True, "model.int8.onnx", "cpu"

            def recognize(self, *_a, **_k):
                return "你好", "zh"

        def fake_get(model_dir, threads, device="cpu", on_log=None):
            return _Gpu() if device == "cuda" else _Cpu()

        with patch.object(sv.SENSEVOICE_CACHE, "get_or_create", side_effect=fake_get):
            segs, meta = sv.transcribe_sensevoice(
                wav,
                TranscribeOptions(model="SenseVoiceSmall", device="cuda", vad=False, models_dir=self.paths.models_dir),
                on_log=self.logs.append,
                paths=self.paths,
            )
        self.assertEqual([s.text for s in segs], ["你好"])
        self.assertEqual((meta["device"], meta["compute_type"], meta["fallback_occurred"]), ("cpu", "int8", True))
        self.assertTrue(any("切换为 CPU" in m for m in self.logs))
        del d

    def test_gpu_meta_reports_cuda_float32(self) -> None:
        _write_sensevoice_dir(self.paths, fp32=True)
        wav = Path(self._tmp.name) / "a.wav"
        import wave

        with wave.open(str(wav), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(np.zeros(16000 * 2, dtype=np.int16).tobytes())

        class _Gpu:
            is_int8, weights_name, device = False, "model.onnx", "cuda"

            def recognize(self, *_a, **_k):
                return "hello", "en"

        with patch.object(sv.SENSEVOICE_CACHE, "get_or_create", return_value=_Gpu()):
            _, meta = sv.transcribe_sensevoice(
                wav,
                TranscribeOptions(model="SenseVoiceSmall", device="cuda", vad=False, models_dir=self.paths.models_dir),
                paths=self.paths,
            )
        self.assertEqual((meta["device"], meta["compute_type"], meta["fallback_occurred"]), ("cuda", "float32", False))

    # --- ensure_gpu_assets -----------------------------------------------------------------
    def _assets(self, **kw) -> bool:
        return sv.ensure_gpu_assets(
            model_name="SenseVoiceSmall",
            hf_mirror=True,
            pypi_mirror="tuna",
            proxy="",
            on_log=self.logs.append,
            paths=self.paths,
            **kw,
        )

    def test_assets_false_without_cuda_onnxruntime(self) -> None:
        with patch.object(gpu_runtime, "onnxruntime_has_cuda", return_value=False):
            self.assertFalse(self._assets())
        self.assertTrue(any("onnxruntime-gpu" in m for m in self.logs))

    def test_assets_downloads_missing_extras_and_fp32(self) -> None:
        _write_sensevoice_dir(self.paths, fp32=False)
        with patch.object(gpu_runtime, "onnxruntime_has_cuda", return_value=True), patch.object(
            gpu_runtime, "check_onnx_extra_files", return_value=["cudart64_12.dll", "cufft64_11.dll"]
        ), patch.object(gpu_runtime, "install_onnx_extras") as extras, patch.object(
            gpu_runtime, "register_dll_directory"
        ), patch.object(model_manager, "download_sensevoice_fp32") as fp32:
            self.assertTrue(self._assets())
        extras.assert_called_once()
        fp32.assert_called_once()

    def test_assets_skips_downloads_when_present(self) -> None:
        _write_sensevoice_dir(self.paths, fp32=True)
        with patch.object(gpu_runtime, "onnxruntime_has_cuda", return_value=True), patch.object(
            gpu_runtime, "check_onnx_extra_files", return_value=[]
        ), patch.object(gpu_runtime, "install_onnx_extras") as extras, patch.object(
            gpu_runtime, "register_dll_directory"
        ), patch.object(model_manager, "download_sensevoice_fp32") as fp32:
            self.assertTrue(self._assets())
        extras.assert_not_called()
        fp32.assert_not_called()

    def test_assets_failure_means_cpu_not_error(self) -> None:
        _write_sensevoice_dir(self.paths, fp32=True)
        with patch.object(gpu_runtime, "onnxruntime_has_cuda", return_value=True), patch.object(
            gpu_runtime, "check_onnx_extra_files", return_value=["cudart64_12.dll"]
        ), patch.object(gpu_runtime, "install_onnx_extras", side_effect=GpuError("下载失败")):
            self.assertFalse(self._assets())
        self.assertTrue(any("GPU 回退" in m for m in self.logs))


class GpuRuntimeExtrasTests(unittest.TestCase):
    def test_extra_dll_list_and_check(self) -> None:
        self.assertEqual(set(gpu_runtime.ONNX_EXTRA_DLLS), {"cudart64_12.dll", "cufft64_11.dll"})
        self.assertEqual(len(gpu_runtime.required_files()), 10)           # Whisper requirement is unchanged
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            self.assertEqual(gpu_runtime.check_onnx_extra_files(d), list(gpu_runtime.ONNX_EXTRA_DLLS))
            (d / "CUDART64_12.dll").write_bytes(b"x")
            self.assertEqual(gpu_runtime.check_onnx_extra_files(d), ["cufft64_11.dll"])
            self.assertEqual(gpu_runtime.check_onnx_extra_files(d / "missing"), list(gpu_runtime.ONNX_EXTRA_DLLS))

    def test_install_onnx_extras_copies_dlls_next_to_existing_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = AppPaths(app_dir=Path(tmp))
            gpu_dir = paths.gpu_runtime_dir
            gpu_dir.mkdir(parents=True, exist_ok=True)
            (gpu_dir / "cublas64_12.dll").write_bytes(b"EXISTING")

            wheels = {
                "nvidia-cuda-runtime-cu12": ("nvidia_cuda_runtime_cu12-12.9-py3-none-win_amd64.whl", "nvidia/cuda_runtime/bin/cudart64_12.dll"),
                "nvidia-cufft-cu12": ("nvidia_cufft_cu12-11.4-py3-none-win_amd64.whl", "nvidia/cufft/bin/cufft64_11.dll"),
            }

            def resolve(pkg, mirror="tuna", proxy="", win_amd64_only=False):
                fname, member = wheels[pkg]
                return {"filename": fname, "url": f"https://x/{fname}", "size": 10, "digests": {"sha256": ""}, "member": member}

            def stream(url, dest_file, proxy="", expected_sha256="", on_chunk_progress=None, token=None, timeout=60, **_):
                member = next(m for f, m in wheels.values() if f in url)
                with zipfile.ZipFile(dest_file, "w") as zf:
                    zf.writestr(member, b"DLL_BYTES")
                return dest_file

            with patch("app.core.http_client.resolve_pypi_wheel", side_effect=resolve), patch(
                "app.core.http_client.stream_download_with_resume", side_effect=stream
            ), patch.object(gpu_runtime, "register_dll_directory"):
                gpu_runtime.install_onnx_extras(paths=paths)

            self.assertEqual(gpu_runtime.check_onnx_extra_files(gpu_dir), [])
            self.assertEqual((gpu_dir / "cublas64_12.dll").read_bytes(), b"EXISTING")   # untouched

    def test_install_onnx_extras_fails_cleanly_on_resolve_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = AppPaths(app_dir=Path(tmp))
            with patch("app.core.http_client.resolve_pypi_wheel", side_effect=RuntimeError("offline")):
                with self.assertRaises(GpuError):
                    gpu_runtime.install_onnx_extras(paths=paths)

    def test_onnxruntime_has_cuda_reflects_providers(self) -> None:
        fake = MagicMock()
        fake.get_available_providers.return_value = ["CPUExecutionProvider"]
        with patch.dict("sys.modules", {"onnxruntime": fake}):
            self.assertFalse(gpu_runtime.onnxruntime_has_cuda())
            fake.get_available_providers.return_value = ["CUDAExecutionProvider", "CPUExecutionProvider"]
            self.assertTrue(gpu_runtime.onnxruntime_has_cuda())


class Fp32DownloadTests(unittest.TestCase):
    def test_fp32_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            self.assertIsNone(model_manager.find_sensevoice_fp32(d))
            (d / "model.onnx").write_bytes(b"version https://git-lfs.github.com/spec/v1")
            self.assertIsNone(model_manager.find_sensevoice_fp32(d))       # LFS pointer is not a model
            (d / "model.onnx").write_bytes(b"REAL_FP32")
            self.assertEqual(model_manager.find_sensevoice_fp32(d).name, "model.onnx")

    def test_only_files_downloads_just_fp32(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = AppPaths(app_dir=Path(tmp))
            _write_sensevoice_dir(paths, fp32=False)
            fetched = {}

            def fake_fetch(repo_id, endpoint, proxy, engine="whisper", names=None):
                fetched["names"] = names
                return [("model.onnx", 937 * 1024 * 1024)]

            downloaded = []

            def fake_stream(url, dest_file, proxy="", expected_sha256="", on_chunk_progress=None, token=None, timeout=60, expected_size=0):
                downloaded.append(Path(url).name)
                dest_file.write_bytes(b"FP32_WEIGHTS")
                return dest_file

            with patch("app.core.model_manager._fetch_repo_file_list", side_effect=fake_fetch), patch(
                "app.core.http_client.stream_download_with_resume", side_effect=fake_stream
            ):
                out = model_manager.download_sensevoice_fp32(paths=paths)
            self.assertEqual(fetched["names"], ("model.onnx",))
            self.assertEqual(downloaded, ["model.onnx"])                   # int8 + tokens.txt not re-downloaded
            self.assertEqual(out, paths.models_dir / "SenseVoiceSmall")
            self.assertIsNotNone(model_manager.find_sensevoice_fp32(out))

    def test_fetch_repo_file_list_names_filter_and_fallback(self) -> None:
        meta = {"siblings": [{"rfilename": "model.onnx", "size": 5}, {"rfilename": "tokens.txt", "size": 1}]}
        with patch("app.core.http_client.fetch_json", return_value=meta):
            files = model_manager._fetch_repo_file_list("r/x", "https://hf-mirror.com", "", engine="sensevoice", names=("model.onnx",))
        self.assertEqual(files, [("model.onnx", 5)])
        with patch("app.core.http_client.fetch_json", side_effect=RuntimeError("offline")):
            files = model_manager._fetch_repo_file_list("r/x", "https://hf-mirror.com", "", engine="sensevoice", names=("model.onnx",))
        self.assertEqual(files, [("model.onnx", 0)])


class PipelineRoutingTests(unittest.TestCase):
    def _run(self, cfg: Config, setup=None, gpu_ready: bool = False, assets_ok: bool = True, detect=("fr", 0.95)):
        captured: dict = {}
        with tempfile.TemporaryDirectory() as tmp:
            paths = AppPaths(app_dir=Path(tmp))
            _write_whisper_dir(paths, "base")
            _write_whisper_dir(paths, "large-v3-turbo")
            _write_whisper_dir(paths, "small")
            _write_sensevoice_dir(paths)
            if setup:
                setup(paths)

            info = MediaInfo(url="https://example.com/v", id="1", title="路由测试", uploader="u", duration=30.0, extractor="Local")

            def fake_download(url, workdir, opts, on_progress=None, on_log=None, token=None, paths=None):
                f = workdir / "1.m4a"
                f.write_bytes(b"AUDIO")
                return f

            def fake_convert(input_path, output_wav, on_progress=None, on_log=None, token=None, paths=None):
                output_wav.write_bytes(b"RIFF_WAV")
                return output_wav

            def fake_transcribe(wav_path, opts, on_progress=None, on_log=None, token=None, paths=None):
                captured["opts"] = opts
                meta = {"model": opts.model, "device": opts.device, "compute_type": "int8", "language": "xx",
                        "language_probability": None, "duration": 30.0}
                return [Segment(0.0, 2.0, "测试文本。")], meta

            gpu_info = GpuInfo(status=GpuStatus.READY if gpu_ready else GpuStatus.NO_DRIVER, device_name="RTX", vram_gb=8.0,
                               message="x")
            with patch("app.core.downloader.fetch_info", return_value=info), patch(
                "app.core.downloader.download_audio", side_effect=fake_download
            ), patch("app.core.audio.convert_to_wav", side_effect=fake_convert), patch(
                "app.core.transcriber.transcribe", side_effect=fake_transcribe
            ), patch("app.core.transcriber.detect_language", return_value=detect) as det, patch(
                "app.core.gpu_runtime.detect", return_value=gpu_info
            ), patch("app.core.sensevoice_engine.ensure_gpu_assets", return_value=assets_ok) as assets:
                res = pipeline.run(job=Job(source="https://example.com/v"), cfg=cfg, paths=paths)
            captured["result"] = res
            captured["detect"] = det
            captured["assets"] = assets
            captured["md"] = res.md_path.read_text(encoding="utf-8")
        return captured

    def test_routing_disabled_uses_selected_model(self) -> None:
        cap = self._run(Config(model="small", use_gpu=False, route_enabled=False, language="fr"))
        self.assertEqual((cap["opts"].model, cap["opts"].language), ("small", "fr"))
        cap["detect"].assert_not_called()

    def test_auto_detected_french_routes_to_whisper_fallback(self) -> None:
        cfg = Config(model="SenseVoiceSmall", use_gpu=False, route_enabled=True, route_fallback_model="large-v3-turbo")
        cap = self._run(cfg, detect=("fr", 0.95))
        self.assertEqual((cap["opts"].model, cap["opts"].language), ("large-v3-turbo", "fr"))
        self.assertIn("large-v3-turbo", cap["md"])

    def test_auto_detected_chinese_routes_to_sensevoice(self) -> None:
        cfg = Config(model="large-v3-turbo", use_gpu=False, route_enabled=True)
        cap = self._run(cfg, detect=("zh", 0.99))
        self.assertEqual((cap["opts"].model, cap["opts"].language), ("SenseVoiceSmall", None))

    def test_explicit_language_skips_detection(self) -> None:
        cfg = Config(model="large-v3-turbo", use_gpu=False, route_enabled=True, language="ja",
                     route_rules={"ja": "SenseVoiceSmall"})
        cap = self._run(cfg)
        self.assertEqual((cap["opts"].model, cap["opts"].language), ("SenseVoiceSmall", "ja"))
        cap["detect"].assert_not_called()

    def test_sensevoice_uses_gpu_when_ready_and_assets_available(self) -> None:
        cap = self._run(Config(model="SenseVoiceSmall", use_gpu=True), gpu_ready=True, assets_ok=True)
        self.assertEqual(cap["opts"].device, "cuda")
        cap["assets"].assert_called_once()

    def test_sensevoice_falls_back_to_cpu_when_assets_unavailable(self) -> None:
        cap = self._run(Config(model="SenseVoiceSmall", use_gpu=True), gpu_ready=True, assets_ok=False)
        self.assertEqual(cap["opts"].device, "cpu")

    def test_sensevoice_cpu_when_gpu_not_ready_or_disabled(self) -> None:
        cap = self._run(Config(model="SenseVoiceSmall", use_gpu=True), gpu_ready=False)
        self.assertEqual(cap["opts"].device, "cpu")
        cap["assets"].assert_not_called()
        cap = self._run(Config(model="SenseVoiceSmall", use_gpu=False), gpu_ready=True)
        self.assertEqual(cap["opts"].device, "cpu")

    def test_whisper_gpu_path_does_not_touch_sensevoice_assets(self) -> None:
        cap = self._run(Config(model="small", use_gpu=True), gpu_ready=True)
        self.assertEqual(cap["opts"].device, "cuda")
        cap["assets"].assert_not_called()


class ReleaseMetaTests(unittest.TestCase):
    def test_version(self) -> None:
        import app

        self.assertEqual(app.__version__, "1.4.4")

    def test_requirements_pin_cuda12_onnxruntime_gpu(self) -> None:
        text = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text(encoding="utf-8")
        lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
        self.assertTrue(any(ln.startswith("onnxruntime-gpu") and "<1.25" in ln for ln in lines))
        self.assertFalse(any(ln.startswith("onnxruntime>") or ln == "onnxruntime" for ln in lines))
        self.assertTrue(any(ln.startswith("kaldi-native-fbank") for ln in lines))


if __name__ == "__main__":
    unittest.main()
