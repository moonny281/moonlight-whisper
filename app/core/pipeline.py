"""End-to-end transcription pipeline orchestrating download, transcode, GPU/model readiness, transcribe, summary, and output."""

from __future__ import annotations

import inspect
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from app.config import Config
from app.core import audio, downloader, gpu_runtime, md_writer, model_manager, summarizer, transcriber
from app.core.cancel import CancelToken
from app.core.downloader import DownloadOptions, MediaInfo
from app.core.errors import CancelledError, MoonlightError
from app.core.gpu_runtime import GpuStatus
from app.core.transcriber import TranscribeOptions
from app.paths import PATHS, AppPaths


@dataclass
class Job:
    source: str
    job_id: str = ""
    title_hint: str = ""

    def __post_init__(self) -> None:
        if not self.job_id:
            self.job_id = uuid.uuid4().hex[:10]

    @property
    def is_local_file(self) -> bool:
        s = self.source.strip()
        if s.startswith(("http://", "https://", "www.")):
            return False
        return Path(s).expanduser().exists()


@dataclass
class Callbacks:
    on_stage: Optional[Callable[[str], None]] = None
    on_progress: Optional[Callable[[int], None]] = None
    on_log: Optional[Callable[[str], None]] = None
    on_title: Optional[Callable[[str], None]] = None


@dataclass
class PipelineResult:
    job_id: str
    title: str
    md_path: Path
    srt_path: Optional[Path]
    kept_audio_path: Optional[Path]
    duration: float
    language: str
    device_used: str
    compute_type_used: str


def _check_disk_space(directory: Path, min_free_mb: int = 300) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(directory)
    free_mb = usage.free / (1024 * 1024)
    if free_mb < min_free_mb:
        raise MoonlightError(
            f"磁盘可用空间不足（当前剩余 {free_mb:.0f} MB，至少需要 {min_free_mb} MB）。",
            detail=str(directory),
        )


def run(
    job: Job,
    cfg: Config,
    cb: Optional[Callbacks] = None,
    token: Optional[CancelToken] = None,
    paths: AppPaths = PATHS,
) -> PipelineResult:
    """Execute the 9-step transcription pipeline with unified 0–100% progress mapping."""
    cb = cb or Callbacks()
    token = token or CancelToken()
    cfg.apply_to_paths(paths)
    paths.ensure_runtime_dirs()

    def _emit_stage(stage: str) -> None:
        if cb.on_stage:
            cb.on_stage(stage)

    def _emit_progress(pct: int) -> None:
        if cb.on_progress:
            cb.on_progress(min(100, max(0, int(pct))))

    def _emit_log(msg: str) -> None:
        if cb.on_log:
            cb.on_log(msg)

    _check_disk_space(paths.temp_dir)
    _check_disk_space(paths.output_dir)

    workdir = paths.temp_dir / f"job_{job.job_id}"
    workdir.mkdir(parents=True, exist_ok=True)

    try:
        token.check()

        # Step 1 & 2 & 3: Input resolution, metadata fetch, and audio acquisition (0% - 30%)
        if job.is_local_file:
            local_path = Path(job.source.strip()).expanduser().resolve()
            title = job.title_hint or local_path.stem
            info = MediaInfo(
                url=str(local_path),
                id=local_path.stem,
                title=title,
                uploader="本地文件",
                duration=0.0,
                extractor="local",
                webpage_url=str(local_path),
            )
            if cb.on_title:
                cb.on_title(info.title)
            _emit_stage("读取本地文件")
            _emit_log(f"输入源为本地音视频文件：{local_path}")
            raw_audio_path = local_path
            _emit_progress(30)
        else:
            dl_opts = DownloadOptions(
                proxy=cfg.proxy,
                cookies_file=cfg.cookies_file,
                cookies_from_browser=cfg.cookies_from_browser,
                allow_playlist=False,
                ffmpeg_dir=paths.bin_dir,
            )
            _emit_stage("获取视频信息")
            _emit_log(f"正在解析链接元数据：{job.source}")
            info = downloader.fetch_info(
                job.source,
                dl_opts,
                token=token,
                on_log=_emit_log,
                paths=paths,
            )
            if job.title_hint and (not info.title or info.title == info.id):
                info.title = job.title_hint
            if cb.on_title:
                cb.on_title(info.title)
            _emit_log(f"已解析标题：《{info.title}》 (作者：{info.uploader})")
            _emit_progress(5)

            _emit_stage("下载音频")
            raw_audio_path = downloader.download_audio(
                job.source,
                workdir=workdir,
                opts=dl_opts,
                on_progress=lambda ratio, detail: _emit_progress(5 + int(ratio * 25)),
                on_log=_emit_log,
                token=token,
                paths=paths,
            )
            _emit_progress(30)

        token.check()

        # Step 4: Convert audio to 16kHz mono WAV (30% - 35%) and backfill duration if unknown (Fixes P2-3)
        _emit_stage("转码音频")
        wav_path = workdir / "audio_16k.wav"

        def _on_probed_duration(dur: float) -> None:
            if info.duration <= 0.0 and dur > 0.0:
                info.duration = dur

        convert_kwargs = {
            "input_path": raw_audio_path,
            "output_wav": wav_path,
            "on_progress": lambda ratio: _emit_progress(30 + int(ratio * 5)),
            "on_log": _emit_log,
            "token": token,
            "paths": paths,
        }
        sig = inspect.signature(audio.convert_to_wav)
        if "on_duration" in sig.parameters:
            convert_kwargs["on_duration"] = _on_probed_duration

        audio.convert_to_wav(**convert_kwargs)
        _emit_progress(35)

        token.check()

        # Step 5: Ensure GPU or fallback to CPU
        target_device = "cpu"
        vram_gb = 8.0
        if cfg.use_gpu:
            gpu_info = gpu_runtime.detect(paths)
            if gpu_info.status == GpuStatus.READY:
                target_device = "cuda"
                vram_gb = gpu_info.vram_gb or 8.0
                _emit_log(f"GPU 加速已启用：{gpu_info.device_name} ({vram_gb:.1f}GB)")
            else:
                _emit_log(f"[GPU 回退] {gpu_info.message}，本次任务自动使用 CPU 模式。")

        # Step 6: Ensure model is downloaded locally
        if not model_manager.is_model_installed(cfg.model, paths=paths):
            _emit_stage(f"下载模型 ({cfg.model})")
            _emit_log(f"本地未找到模型 {cfg.model}，开始自动下载...")
            model_manager.download_model(
                model_name=cfg.model,
                hf_mirror=cfg.hf_mirror,
                proxy=cfg.proxy,
                on_progress=lambda ratio, msg: _emit_log(msg),
                on_log=_emit_log,
                token=token,
                paths=paths,
            )

        token.check()

        # Step 7: Transcribe with faster-whisper (35% - 95%)
        _emit_stage("加载模型与转录中")
        tr_opts = TranscribeOptions(
            model=cfg.model,
            language=cfg.language,
            device=target_device,
            compute_type=cfg.compute_type,
            cpu_threads=cfg.cpu_threads,
            vad=cfg.vad,
            min_silence_duration_ms=cfg.min_silence_duration_ms,
            to_simplified=cfg.to_simplified,
            models_dir=paths.models_dir,
            vram_gb=vram_gb,
        )
        segments, meta = transcriber.transcribe(
            wav_path=wav_path,
            opts=tr_opts,
            on_progress=lambda ratio, detail: _emit_progress(35 + int(ratio * 60)),
            on_log=_emit_log,
            token=token,
            paths=paths,
        )
        _emit_progress(95)

        if not segments:
            _emit_log(
                "[提示] 转录结果为空：音频可能不包含人声，或 VAD 静音过滤过于严格（可在设置中关闭 VAD 后重试）。"
            )

        if info.duration <= 0.0 and meta.get("duration"):
            info.duration = float(meta["duration"])

        token.check()

        # Step 8: Optional LLM Summary + Write Markdown & SRT (95% - 100%)
        summary_text = summarizer.PLACEHOLDER_SUMMARY
        if cfg.llm.enabled and cfg.llm.api_key.strip() and segments:
            _emit_stage("生成摘要")
            summary_text = summarizer.generate_summary(
                segments=segments,
                title=info.title,
                llm_cfg=cfg.llm,
                proxy=cfg.proxy,
                on_log=_emit_log,
                token=token,
            )

        _emit_stage("写入文档")
        md_path = md_writer.write_markdown(
            output_dir=paths.output_dir,
            filename_template=cfg.filename_template,
            info=info,
            segments=segments,
            meta=meta,
            summary_text=summary_text,
        )
        _emit_log(f"已生成 Markdown 文档：{md_path}")

        srt_path: Optional[Path] = None
        if cfg.write_srt:
            srt_path = md_writer.write_srt(md_path, segments)
            _emit_log(f"已生成 SRT 字幕文件：{srt_path}")

        kept_audio_path: Optional[Path] = None
        if cfg.keep_audio and not job.is_local_file and raw_audio_path.exists():
            kept_audio_path = md_writer.resolve_unique_path(
                paths.output_dir,
                md_path.stem,
                raw_audio_path.suffix,
            )
            shutil.copy2(raw_audio_path, kept_audio_path)
            _emit_log(f"已保留原始音频文件：{kept_audio_path}")

        _emit_progress(100)
        _emit_stage("完成")

        return PipelineResult(
            job_id=job.job_id,
            title=info.title,
            md_path=md_path,
            srt_path=srt_path,
            kept_audio_path=kept_audio_path,
            duration=info.duration,
            language=str(meta.get("language") or "unknown"),
            device_used=str(meta.get("device") or target_device),
            compute_type_used=str(meta.get("compute_type") or "int8"),
        )
    except CancelledError:
        _emit_stage("已取消")
        _emit_log("任务已取消，正在清理临时文件...")
        raise
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
