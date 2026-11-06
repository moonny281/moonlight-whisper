"""Language routing: decide which model transcribes a file, based on its (user-chosen or detected) language.

* The user can pick a language explicitly -> the rule table maps it to a model.
* With "auto" language, a small Whisper model (`route_detect_model`, default `base`) identifies the language first.
* Languages without a rule use `route_fallback_model`.
* If detection fails or is not confident, the model chosen in the main window is used with auto-detect.

A model is only picked when it actually supports the language (e.g. SenseVoiceSmall: zh/en/ja/ko/yue only).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional

from app.config import Config
from app.core import languages, model_manager, transcriber
from app.core.cancel import CancelToken
from app.paths import PATHS, AppPaths

LogCallback = Callable[[str], None]
StageCallback = Callable[[str], None]
ProgressCallback = Callable[[float, str], None]

# Below this language-ID probability the detection is not trusted.
MIN_DETECT_CONFIDENCE = 0.4


@dataclass(frozen=True)
class RouteDecision:
    model: str                              # model that will transcribe the file
    language: Optional[str]                 # language handed to the engine (None = engine auto-detect)
    routed: bool = False                    # True when routing was enabled and decided the model
    detected_language: Optional[str] = None
    detected_probability: Optional[float] = None


def pick_model_for_language(
    language: str,
    rules: Dict[str, str],
    fallback_model: str,
    default_model: str,
) -> str:
    """Rule for `language` -> else fallback -> else the main-window model; each candidate must support `language`."""
    for candidate in (rules.get(language), fallback_model, default_model):
        if candidate and languages.is_language_supported(candidate, language):
            return candidate
    return default_model


def resolve_route(
    cfg: Config,
    wav_path: Path,
    hf_mirror: Optional[bool] = None,
    proxy: Optional[str] = None,
    on_log: Optional[LogCallback] = None,
    on_stage: Optional[StageCallback] = None,
    on_progress: Optional[ProgressCallback] = None,
    token: Optional[CancelToken] = None,
    paths: AppPaths = PATHS,
) -> RouteDecision:
    """Choose `(model, language)` for `wav_path` according to `cfg`; never raises for detection problems."""

    def log(msg: str) -> None:
        if on_log:
            on_log(msg)

    default_model = cfg.model

    if not cfg.route_enabled:
        lang, note = languages.normalize_language(default_model, cfg.language)
        if note:
            log(f"[提示] {note}")
        return RouteDecision(model=default_model, language=lang)

    explicit = cfg.language
    detected: Optional[str] = None
    prob: Optional[float] = None

    if explicit:
        lang: Optional[str] = explicit
        log(f"[语言路由] 已指定语言：{languages.language_label(explicit)}")
    else:
        detect_model = cfg.route_detect_model
        if model_manager.get_model_engine(detect_model) != model_manager.ENGINE_WHISPER:
            detect_model = "base"
        if not model_manager.is_model_installed(detect_model, paths=paths):
            if on_stage:
                on_stage(f"下载语言检测模型 ({detect_model})")
            log(f"[语言路由] 本地未找到语言检测模型 {detect_model}，开始自动下载...")
            try:
                model_manager.download_model(
                    model_name=detect_model,
                    hf_mirror=cfg.hf_mirror if hf_mirror is None else hf_mirror,
                    proxy=cfg.proxy if proxy is None else proxy,
                    on_progress=(lambda ratio, msg: on_stage(f"下载语言检测模型 ({detect_model}) {int(ratio * 100)}%"))
                    if on_stage
                    else None,
                    on_log=on_log,
                    token=token,
                    paths=paths,
                )
            except Exception as exc:  # network trouble must not fail the job
                from app.core.errors import CancelledError

                if isinstance(exc, CancelledError):
                    raise
                log(f"[语言路由] 语言检测模型下载失败 ({exc})，改用主界面所选模型 {default_model}。")
                return RouteDecision(model=default_model, language=languages.normalize_language(default_model, None)[0])

        if on_stage:
            on_stage("检测语言")
        lang, p = transcriber.detect_language(
            wav_path,
            detect_model,
            models_dir=paths.models_dir,
            cpu_threads=cfg.cpu_threads,
            on_log=on_log,
            token=token,
            paths=paths,
        )
        prob = p
        detected = lang
        if not lang or p < MIN_DETECT_CONFIDENCE:
            shown = f"{languages.language_label(lang)}，置信度 {p:.2f}" if lang else "检测失败"
            log(f"[语言路由] 语言检测不可靠 ({shown})，改用主界面所选模型 {default_model} 并自动识别语言。")
            return RouteDecision(
                model=default_model,
                language=None,
                detected_language=detected,
                detected_probability=prob,
            )
        log(f"[语言路由] 检测到语言：{languages.language_label(lang)}（置信度 {p:.2f}）")

    assert lang is not None
    model = pick_model_for_language(lang, cfg.route_rules, cfg.route_fallback_model, default_model)
    if model_manager.get_model_engine(model) == model_manager.ENGINE_SENSEVOICE and not explicit:
        # Let SenseVoice decide itself between e.g. Mandarin and Cantonese instead of trusting Whisper's label.
        engine_lang: Optional[str] = None
    else:
        engine_lang, note = languages.normalize_language(model, lang)
        if note:
            log(f"[提示] {note}")

    log(f"[语言路由] {languages.language_label(lang)} → 使用模型 {model}")
    return RouteDecision(
        model=model,
        language=engine_lang,
        routed=True,
        detected_language=detected,
        detected_probability=prob,
    )
