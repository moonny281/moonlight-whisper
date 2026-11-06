"""Language catalog and per-model language support (Qt-free).

* `ALL_LANGUAGES`   – every language Whisper can transcribe (99 + Cantonese `yue`) with a Chinese display name.
* `supported_languages(model)` – which of them a given model can be asked for.
    - Whisper `tiny/base/small/medium` have the 99-language vocabulary (no `yue`);
      `large-v3` / `large-v3-turbo` add `yue`.
    - SenseVoiceSmall only knows `zh / en / ja / ko / yue`.
* `normalize_language(model, lang)` – never hand a model a language it would reject.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional, Tuple

from app.core import model_manager


@dataclass(frozen=True)
class Language:
    code: str
    zh_name: str

    @property
    def label(self) -> str:
        return f"{self.zh_name} ({self.code})"


# Shown first in every language list, in this order.
_COMMON_ORDER: Tuple[str, ...] = ("zh", "yue", "en", "ja", "ko", "fr", "de", "es", "ru", "pt", "it", "ar", "hi", "th", "vi", "id")

_NAMES: Dict[str, str] = {
    "af": "南非荷兰语", "am": "阿姆哈拉语", "ar": "阿拉伯语", "as": "阿萨姆语", "az": "阿塞拜疆语",
    "ba": "巴什基尔语", "be": "白俄罗斯语", "bg": "保加利亚语", "bn": "孟加拉语", "bo": "藏语",
    "br": "布列塔尼语", "bs": "波斯尼亚语", "ca": "加泰罗尼亚语", "cs": "捷克语", "cy": "威尔士语",
    "da": "丹麦语", "de": "德语", "el": "希腊语", "en": "英语", "es": "西班牙语",
    "et": "爱沙尼亚语", "eu": "巴斯克语", "fa": "波斯语", "fi": "芬兰语", "fo": "法罗语",
    "fr": "法语", "gl": "加利西亚语", "gu": "古吉拉特语", "ha": "豪萨语", "haw": "夏威夷语",
    "he": "希伯来语", "hi": "印地语", "hr": "克罗地亚语", "ht": "海地克里奥尔语", "hu": "匈牙利语",
    "hy": "亚美尼亚语", "id": "印度尼西亚语", "is": "冰岛语", "it": "意大利语", "ja": "日语",
    "jw": "爪哇语", "ka": "格鲁吉亚语", "kk": "哈萨克语", "km": "高棉语", "kn": "卡纳达语",
    "ko": "韩语", "la": "拉丁语", "lb": "卢森堡语", "ln": "林加拉语", "lo": "老挝语",
    "lt": "立陶宛语", "lv": "拉脱维亚语", "mg": "马达加斯加语", "mi": "毛利语", "mk": "马其顿语",
    "ml": "马拉雅拉姆语", "mn": "蒙古语", "mr": "马拉地语", "ms": "马来语", "mt": "马耳他语",
    "my": "缅甸语", "ne": "尼泊尔语", "nl": "荷兰语", "nn": "新挪威语", "no": "挪威语",
    "oc": "奥克语", "pa": "旁遮普语", "pl": "波兰语", "ps": "普什图语", "pt": "葡萄牙语",
    "ro": "罗马尼亚语", "ru": "俄语", "sa": "梵语", "sd": "信德语", "si": "僧伽罗语",
    "sk": "斯洛伐克语", "sl": "斯洛文尼亚语", "sn": "绍纳语", "so": "索马里语", "sq": "阿尔巴尼亚语",
    "sr": "塞尔维亚语", "su": "巽他语", "sv": "瑞典语", "sw": "斯瓦希里语", "ta": "泰米尔语",
    "te": "泰卢固语", "tg": "塔吉克语", "th": "泰语", "tk": "土库曼语", "tl": "他加禄语",
    "tr": "土耳其语", "tt": "鞑靼语", "uk": "乌克兰语", "ur": "乌尔都语", "uz": "乌兹别克语",
    "vi": "越南语", "yi": "意第绪语", "yo": "约鲁巴语", "zh": "中文", "yue": "粤语",
}


def _build_all() -> Tuple[Language, ...]:
    common = [Language(c, _NAMES[c]) for c in _COMMON_ORDER]
    rest = [Language(c, n) for c, n in sorted(_NAMES.items()) if c not in _COMMON_ORDER]
    return tuple(common + rest)


ALL_LANGUAGES: Tuple[Language, ...] = _build_all()
LANGUAGE_NAMES: Dict[str, str] = dict(_NAMES)
ALL_CODES: FrozenSet[str] = frozenset(_NAMES)

AUTO_LABEL = "自动检测 (Auto)"

SENSEVOICE_LANGUAGES: FrozenSet[str] = frozenset({"zh", "en", "ja", "ko", "yue"})
# Whisper checkpoints whose multilingual vocabulary contains the Cantonese token.
WHISPER_YUE_MODELS: FrozenSet[str] = frozenset({"large-v3", "large-v3-turbo"})


def language_label(code: Optional[str]) -> str:
    if not code:
        return AUTO_LABEL
    name = LANGUAGE_NAMES.get(code)
    return f"{name} ({code})" if name else str(code)


def language_options() -> List[Tuple[str, Optional[str]]]:
    """`(label, code)` pairs for a language combo box; auto-detect first, then every language."""
    return [(AUTO_LABEL, None)] + [(lang.label, lang.code) for lang in ALL_LANGUAGES]


def supported_languages(model_name: str) -> Optional[FrozenSet[str]]:
    """Languages `model_name` accepts, or `None` when unknown (custom Whisper repo: assume all)."""
    engine = model_manager.get_model_engine(model_name)
    if engine == model_manager.ENGINE_SENSEVOICE:
        return SENSEVOICE_LANGUAGES
    if model_name in model_manager.MODEL_SPEC_MAP:
        if model_name in WHISPER_YUE_MODELS:
            return ALL_CODES
        return ALL_CODES - {"yue"}
    return None


def is_language_supported(model_name: str, language: Optional[str]) -> bool:
    """`None` / auto is always supported."""
    if not language:
        return True
    supported = supported_languages(model_name)
    return supported is None or language in supported


def normalize_language(model_name: str, language: Optional[str]) -> Tuple[Optional[str], str]:
    """Return `(language_to_use, note)`; unsupported languages fall back to auto-detect with an explanation."""
    if not language or language == "auto":
        return None, ""
    if is_language_supported(model_name, language):
        return language, ""
    supported = supported_languages(model_name) or frozenset()
    if model_manager.get_model_engine(model_name) == model_manager.ENGINE_SENSEVOICE:
        why = "SenseVoiceSmall 仅支持 zh/en/ja/ko/yue"
    elif language == "yue":
        why = f"Whisper 模型 {model_name} 不支持粤语 (yue)，需要 large-v3 / large-v3-turbo"
    else:
        why = f"模型 {model_name} 不支持该语言"
    del supported
    return None, f"{why}，语言“{language_label(language)}”改用自动检测。"
