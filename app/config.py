"""Configuration management (`config.json`) with validation and schema migration."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from app.paths import PATHS, AppPaths

CURRENT_CONFIG_VERSION = 1

DEFAULT_LLM_PROMPT = (
    "请阅读以下视频转录文本，用简洁清晰的中文提炼：\n"
    "1. 核心内容摘要（150字以内）\n"
    "2. 关键要点列表（3~6条）\n"
    "只输出摘要内容本身，不要添加多余寒暄。"
)


@dataclass
class UiConfig:
    theme: str = "system"          # "system" | "dark" | "light"
    log_expanded: bool = False


@dataclass
class LlmConfig:
    """OpenAI-compatible API settings for optional automatic Markdown summary generation."""
    enabled: bool = False
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o-mini"
    system_prompt: str = DEFAULT_LLM_PROMPT
    timeout_sec: int = 45


@dataclass
class Config:
    version: int = CURRENT_CONFIG_VERSION
    output_dir: str = "output"
    data_dir: str = ""
    gpu_runtime_dir: str = ""
    models_dir: str = ""
    temp_dir: str = ""
    keep_audio: bool = False
    write_srt: bool = False
    to_simplified: bool = True
    allow_playlist: bool = False
    model: str = "large-v3-turbo"
    language: Optional[str] = None
    use_gpu: bool = True
    compute_type: Optional[str] = None
    cpu_threads: int = 0
    vad: bool = True
    min_silence_duration_ms: int = 500
    proxy: str = ""
    cookies_file: str = ""
    cookies_from_browser: str = ""
    hf_mirror: bool = True
    pypi_mirror: str = "tuna"      # "tuna" | "aliyun" | "official"
    filename_template: str = "{title}"
    log_level: str = "INFO"
    ui: UiConfig = field(default_factory=UiConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Config":
        """Validate, migrate, and fill defaults from a raw JSON dictionary."""
        data = dict(raw) if isinstance(raw, dict) else {}
        data = cls._migrate(data)

        ui_raw = data.get("ui")
        ui_cfg = UiConfig()
        if isinstance(ui_raw, dict):
            ui_cfg.theme = str(ui_raw.get("theme", ui_cfg.theme))
            if ui_cfg.theme not in ("system", "dark", "light"):
                ui_cfg.theme = "system"
            ui_cfg.log_expanded = bool(ui_raw.get("log_expanded", ui_cfg.log_expanded))

        llm_raw = data.get("llm")
        llm_cfg = LlmConfig()
        if isinstance(llm_raw, dict):
            llm_cfg.enabled = bool(llm_raw.get("enabled", llm_cfg.enabled))
            llm_cfg.base_url = str(llm_raw.get("base_url", llm_cfg.base_url)).strip() or "https://api.openai.com/v1"
            llm_cfg.api_key = str(llm_raw.get("api_key", llm_cfg.api_key)).strip()
            llm_cfg.model = str(llm_raw.get("model", llm_cfg.model)).strip() or "gpt-4o-mini"
            llm_cfg.system_prompt = str(llm_raw.get("system_prompt", llm_cfg.system_prompt)) or DEFAULT_LLM_PROMPT
            try:
                llm_cfg.timeout_sec = max(5, int(llm_raw.get("timeout_sec", llm_cfg.timeout_sec)))
            except (TypeError, ValueError):
                llm_cfg.timeout_sec = 45

        defaults = cls()

        def _str(key: str, default: str) -> str:
            val = data.get(key, default)
            return str(val) if val is not None else default

        def _opt_str(key: str, default: Optional[str]) -> Optional[str]:
            val = data.get(key, default)
            if val is None or str(val).strip() in ("", "auto", "None"):
                return None
            return str(val).strip()

        def _int(key: str, default: int, minimum: int = 0) -> int:
            try:
                return max(minimum, int(data.get(key, default)))
            except (TypeError, ValueError):
                return default

        pypi_mirror = _str("pypi_mirror", defaults.pypi_mirror)
        if pypi_mirror not in ("tuna", "aliyun", "official"):
            pypi_mirror = "tuna"

        filename_template = _str("filename_template", defaults.filename_template).strip() or "{title}"

        return cls(
            version=CURRENT_CONFIG_VERSION,
            output_dir=_str("output_dir", defaults.output_dir),
            data_dir=_str("data_dir", defaults.data_dir),
            gpu_runtime_dir=_str("gpu_runtime_dir", defaults.gpu_runtime_dir),
            models_dir=_str("models_dir", defaults.models_dir),
            temp_dir=_str("temp_dir", defaults.temp_dir),
            keep_audio=bool(data.get("keep_audio", defaults.keep_audio)),
            write_srt=bool(data.get("write_srt", defaults.write_srt)),
            to_simplified=bool(data.get("to_simplified", defaults.to_simplified)),
            allow_playlist=bool(data.get("allow_playlist", defaults.allow_playlist)),
            model=_str("model", defaults.model).strip() or defaults.model,
            language=_opt_str("language", defaults.language),
            use_gpu=bool(data.get("use_gpu", defaults.use_gpu)),
            compute_type=_opt_str("compute_type", defaults.compute_type),
            cpu_threads=_int("cpu_threads", defaults.cpu_threads, minimum=0),
            vad=bool(data.get("vad", defaults.vad)),
            min_silence_duration_ms=_int("min_silence_duration_ms", defaults.min_silence_duration_ms, minimum=100),
            proxy=_str("proxy", defaults.proxy).strip(),
            cookies_file=_str("cookies_file", defaults.cookies_file).strip(),
            cookies_from_browser=_str("cookies_from_browser", defaults.cookies_from_browser).strip(),
            hf_mirror=bool(data.get("hf_mirror", defaults.hf_mirror)),
            pypi_mirror=pypi_mirror,
            filename_template=filename_template,
            log_level=_str("log_level", defaults.log_level).upper() or "INFO",
            ui=ui_cfg,
            llm=llm_cfg,
        )

    @classmethod
    def _migrate(cls, data: Dict[str, Any]) -> Dict[str, Any]:
        """Migrate older config schemas forward to `CURRENT_CONFIG_VERSION`."""
        migrated = dict(data)
        version = migrated.get("version", 0)
        try:
            version = int(version)
        except (TypeError, ValueError):
            version = 0

        if version < 1:
            # v0 -> v1 migration: normalize legacy keys if present
            if "simplify_chinese" in migrated and "to_simplified" not in migrated:
                migrated["to_simplified"] = bool(migrated.pop("simplify_chinese"))
            if "export_srt" in migrated and "write_srt" not in migrated:
                migrated["write_srt"] = bool(migrated.pop("export_srt"))
            migrated["version"] = 1

        return migrated

    def apply_to_paths(self, paths: AppPaths = PATHS) -> None:
        """Sync custom directory settings from this Config into `AppPaths`."""
        paths.configure_overrides(
            custom_data_dir=self.data_dir,
            custom_gpu_dir=self.gpu_runtime_dir,
            custom_models_dir=self.models_dir,
            custom_output_dir=self.output_dir,
            custom_temp_dir=self.temp_dir,
        )

    def save(self, path: Optional[Path] = None, paths: AppPaths = PATHS) -> Path:
        """Persist configuration to disk as formatted JSON."""
        self.apply_to_paths(paths)
        target = path or paths.config_path
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = target.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(target)
        return target

    @classmethod
    def load(cls, path: Optional[Path] = None, paths: AppPaths = PATHS) -> "Config":
        """Load configuration from disk, creating a default file if missing or corrupted."""
        target = path or paths.bootstrap_config_path
        if not target.exists():
            cfg = cls()
            cfg.apply_to_paths(paths)
            return cfg

        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
            cfg = cls.from_dict(raw)
        except (OSError, json.JSONDecodeError):
            cfg = cls()

        cfg.apply_to_paths(paths)
        # If custom `data_dir` redirects to another `config.json`, load from there if present
        redirected = paths.config_path
        if path is None and redirected != target and redirected.exists():
            try:
                raw_redirected = json.loads(redirected.read_text(encoding="utf-8"))
                cfg = cls.from_dict(raw_redirected)
                cfg.apply_to_paths(paths)
            except (OSError, json.JSONDecodeError):
                pass
        return cfg
