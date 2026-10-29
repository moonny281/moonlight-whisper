"""Markdown and SRT writer with paragraph merging, deduplication, and filename sanitization."""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core.downloader import MediaInfo
from app.core.summarizer import PLACEHOLDER_SUMMARY
from app.core.transcriber import Segment

_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_CJK_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")
# Recognizes sentence-ending punctuation even when followed by closing quotes/brackets (Fixes P2-3)
_SENTENCE_END_RE = re.compile(r"[。！？.!?…]+[”’\"')）】』]*$")
_CLAUSE_END_CHARS = ("，", "、", "；", "：", ",", ";", ":")


def sanitize_filename(name: str, max_len: int = 80) -> str:
    """Replace illegal Windows filename characters with `_` and truncate to `max_len`."""
    cleaned = _INVALID_FILENAME_CHARS.sub("_", name or "").strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        cleaned = "untitled"
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip().rstrip(".")
    return cleaned or "untitled"


def resolve_unique_path(directory: Path, stem: str, suffix: str) -> Path:
    """Return a non-colliding path in `directory`, appending ` (1)`, ` (2)` if needed."""
    directory.mkdir(parents=True, exist_ok=True)
    if not suffix.startswith("."):
        suffix = f".{suffix}"
    candidate = directory / f"{stem}{suffix}"
    if not candidate.exists():
        return candidate
    counter = 1
    while True:
        candidate = directory / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def format_timestamp(seconds: float, srt: bool = False) -> str:
    """Format seconds into `HH:MM:SS` (Markdown) or `HH:MM:SS,mmm` (SRT)."""
    sec_total = max(0.0, float(seconds))
    hours = int(sec_total // 3600)
    minutes = int((sec_total % 3600) // 60)
    secs = int(sec_total % 60)
    if srt:
        millis = int(round((sec_total - int(sec_total)) * 1000))
        if millis >= 1000:
            millis = 999
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def normalize_chinese_punctuation(text: str) -> str:
    """Normalize spacing and punctuation for Chinese sentences while keeping English intact."""
    s = text.strip()
    if not s:
        return s
    if _CJK_CHAR_RE.search(s):
        s = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", s)
    return s


def deduplicate_segments(segments: List[Segment]) -> List[Segment]:
    """Remove adjacent duplicate segments caused by Whisper hallucination loops."""
    result: List[Segment] = []
    prev_norm: Optional[str] = None
    for seg in segments:
        txt = normalize_chinese_punctuation(seg.text)
        if not txt:
            continue
        norm_key = re.sub(r"[\s\W_]+", "", txt.lower())
        if prev_norm and norm_key and norm_key == prev_norm:
            continue
        result.append(Segment(start=seg.start, end=seg.end, text=txt))
        if norm_key:
            prev_norm = norm_key
    return result


def _is_primarily_cjk(text: str) -> bool:
    cjk_count = len(_CJK_CHAR_RE.findall(text))
    return cjk_count >= max(1, len(text) // 4)


def _join_segment_texts(prev_text: str, next_text: str) -> str:
    if not prev_text:
        return next_text
    if _is_primarily_cjk(prev_text) or _is_primarily_cjk(next_text):
        if _SENTENCE_END_RE.search(prev_text) or prev_text.endswith(_CLAUSE_END_CHARS):
            return f"{prev_text}{next_text}"
        return f"{prev_text}，{next_text}"
    return f"{prev_text} {next_text}"


def merge_segments_to_paragraphs(segments: List[Segment]) -> List[Tuple[float, str]]:
    """Merge adjacent segments into paragraphs according to §5.6 rules."""
    cleaned = deduplicate_segments(segments)
    if not cleaned:
        return []

    paragraphs: List[Tuple[float, str]] = []
    cur_start = cleaned[0].start
    cur_text = cleaned[0].text
    prev_end = cleaned[0].end
    prev_seg_text = cleaned[0].text

    for seg in cleaned[1:]:
        gap = seg.start - prev_end
        is_cjk = _is_primarily_cjk(cur_text)
        word_count = len(cur_text.split())
        char_count = len(cur_text)

        exceeds_max = (char_count > 300) if is_cjk else (word_count > 80)
        prev_ended_sentence = bool(_SENTENCE_END_RE.search(prev_seg_text))
        exceeds_mid = (char_count > 150) if is_cjk else (word_count > 40)

        if gap > 2.0 or exceeds_max or (prev_ended_sentence and exceeds_mid):
            paragraphs.append((cur_start, cur_text))
            cur_start = seg.start
            cur_text = seg.text
        else:
            cur_text = _join_segment_texts(cur_text, seg.text)

        prev_end = seg.end
        prev_seg_text = seg.text

    if cur_text:
        paragraphs.append((cur_start, cur_text))

    return paragraphs


def render_markdown(
    info: MediaInfo,
    segments: List[Segment],
    meta: Dict[str, Any],
    summary_text: str = PLACEHOLDER_SUMMARY,
    transcribed_at: Optional[dt.datetime] = None,
) -> str:
    """Render the complete Markdown document according to §5.6 template."""
    now_str = (transcribed_at or dt.datetime.now()).strftime("%Y-%m-%d %H:%M")
    duration_val = float(info.duration or meta.get("duration") or 0.0)
    duration_str = format_timestamp(duration_val, srt=False)
    model_name = meta.get("model", "unknown")
    device = meta.get("device", "cpu")
    compute_type = meta.get("compute_type", "int8")
    language = meta.get("language", "unknown")
    prob = float(meta.get("language_probability", 1.0))

    paragraphs = merge_segments_to_paragraphs(segments)
    if paragraphs:
        body_lines = [
            f"**[{format_timestamp(ts)}]** {para_text}"
            for ts, para_text in paragraphs
        ]
        body_str = "\n\n".join(body_lines)
    else:
        body_str = "*（未检测到有效语音内容）*"

    summary_block = (summary_text or PLACEHOLDER_SUMMARY).strip()

    return (
        f"# {info.title}\n\n"
        f"| 项目 | 内容 |\n"
        f"|---|---|\n"
        f"| 来源 | {info.url} |\n"
        f"| 作者 | {info.uploader} |\n"
        f"| 时长 | {duration_str} |\n"
        f"| 模型 | {model_name}（{device}/{compute_type}） |\n"
        f"| 语言 | {language}（置信度 {prob:.2f}） |\n"
        f"| 转录时间 | {now_str} |\n\n"
        f"## 摘要\n\n"
        f"{summary_block}\n\n"
        f"## 转录正文\n\n"
        f"{body_str}\n"
    )


def format_filename_from_template(template: str, info: MediaInfo, meta: Dict[str, Any]) -> str:
    """Expand `{title}`, `{uploader}`, `{id}`, `{date}`, `{language}` in filename template."""
    today = dt.date.today().strftime("%Y-%m-%d")
    mapping = {
        "title": info.title or "untitled",
        "uploader": info.uploader or "unknown",
        "id": info.id or "local",
        "date": today,
        "language": str(meta.get("language") or "auto"),
        "model": str(meta.get("model") or ""),
    }
    try:
        rendered = (template or "{title}").format(**mapping)
    except Exception:
        rendered = info.title or "untitled"
    return sanitize_filename(rendered, max_len=80)


def write_markdown(
    output_dir: Path,
    filename_template: str,
    info: MediaInfo,
    segments: List[Segment],
    meta: Dict[str, Any],
    summary_text: str = PLACEHOLDER_SUMMARY,
) -> Path:
    """Write the Markdown file to `output_dir` with automatic collision avoidance."""
    stem = format_filename_from_template(filename_template, info, meta)
    md_path = resolve_unique_path(output_dir, stem, ".md")
    content = render_markdown(info=info, segments=segments, meta=meta, summary_text=summary_text)
    md_path.write_text(content, encoding="utf-8")
    return md_path


def render_srt(segments: List[Segment]) -> str:
    """Format raw segments as standard SubRip (`.srt`) subtitle content."""
    cleaned = deduplicate_segments(segments)
    blocks: List[str] = []
    for idx, seg in enumerate(cleaned, start=1):
        start_ts = format_timestamp(seg.start, srt=True)
        end_ts = format_timestamp(max(seg.start + 0.1, seg.end), srt=True)
        blocks.append(f"{idx}\n{start_ts} --> {end_ts}\n{seg.text}")
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def write_srt(md_path: Path, segments: List[Segment]) -> Path:
    """Write an `.srt` file alongside `md_path`."""
    srt_path = md_path.with_suffix(".srt")
    srt_path.write_text(render_srt(segments), encoding="utf-8")
    return srt_path
