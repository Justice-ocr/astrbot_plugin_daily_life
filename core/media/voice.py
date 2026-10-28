from __future__ import annotations

import asyncio
import shutil
import uuid
from pathlib import Path
from typing import Any

from ..config.options import VoiceGenerationSettings
from .base import GeneratedVoice, normalize_emotion_category, normalize_voice_style

MIMO_PLUGIN_NAME = "astrbot_plugin_mimo_tts_clone"
_MAX_AUDIO_BYTES = 32 * 1024 * 1024
_STYLE_CONTEXT = {
    "neutral": "自然平稳",
    "happy": "开心明快",
    "light": "轻松自然",
    "soft": "轻柔低声",
    "sad": "低缓柔和",
    "angry": "克制但明显地不高兴",
}


class MimoVoiceService:
    """Reuse MiMo synthesis without entering its independent delivery queue."""

    def __init__(self, settings: VoiceGenerationSettings, data_dir: Path, context: Any):
        self.settings = settings
        self.output_dir = data_dir / "generated" / "voices"
        self.context = context

    async def close(self) -> None:
        # The peer plugin owns its client, queue and lifecycle.
        pass

    def _plugin(self) -> Any:
        getter = getattr(self.context, "get_registered_star", None)
        metadata = getter(MIMO_PLUGIN_NAME) if callable(getter) else None
        if metadata is None or not getattr(metadata, "activated", False):
            raise RuntimeError("请先安装并启用 MiMo TTS 音色克隆插件")
        plugin = getattr(metadata, "star_cls", None)
        if not callable(getattr(plugin, "synthesize_text", None)):
            raise RuntimeError("MiMo TTS 插件尚未就绪，或缺少 synthesize_text 接口")
        return plugin

    async def synthesize(
        self,
        text: str,
        *,
        emotion: str = "",
        emotion_category: str = "",
        voice_style: str = "",
        target_umo: str = "",
        user_id: str = "",
    ) -> GeneratedVoice:
        if not self.settings.enabled:
            raise RuntimeError("语音生成未启用")
        text = str(text or "").strip()
        if not text:
            raise ValueError("缺少语音文本")
        plugin = self._plugin()
        style = normalize_voice_style(voice_style)
        category = (
            normalize_emotion_category(emotion_category)
            or normalize_emotion_category(voice_style)
            or "neutral"
        )
        scope = str(target_umo or "").strip()
        if not user_id and ":FriendMessage:" in scope:
            user_id = scope.split(":FriendMessage:", 1)[1]
        context = _STYLE_CONTEXT[style]
        if emotion:
            context += "；" + str(emotion).strip()[:300]
        # Resolve the live instance per call so peer reloads do not leave stale clients.
        output = await asyncio.wait_for(
            plugin.synthesize_text(
                text,
                voice_name=self.settings.mimo_voice or None,
                emotion=category,
                context=context,
                user_id=str(user_id or ""),
                group_id=scope,
            ),
            timeout=self.settings.mimo_timeout_seconds,
        )
        if not isinstance(output, (str, Path)) or not str(output).strip():
            raise RuntimeError("MiMo TTS 未返回有效的音频文件路径")
        # Copy into our own retention domain; never delete or close peer resources.
        path = await asyncio.to_thread(self._copy_audio, Path(output))
        return GeneratedVoice(path)

    def _copy_audio(self, source: Path) -> Path:
        if not source.is_file() or not 44 <= source.stat().st_size <= _MAX_AUDIO_BYTES:
            raise RuntimeError("MiMo TTS 音频不存在、为空或超过 32 MB")
        with source.open("rb") as stream:
            header = stream.read(12)
        if header[:4] != b"RIFF" or header[8:12] != b"WAVE":
            raise RuntimeError("MiMo TTS 返回的文件不是 WAV 音频")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        destination = self.output_dir / f"mimo_{uuid.uuid4().hex}.wav"
        partial = destination.with_suffix(".wav.part")
        try:
            shutil.copyfile(source, partial)
            partial.replace(destination)
        finally:
            partial.unlink(missing_ok=True)
        return destination


async def synthesize_voice(
    service: Any,
    text: str,
    *,
    target_umo: str = "",
    source_event: Any = None,
    **kwargs: Any,
) -> GeneratedVoice:
    """Pass conversation routing only to the backend that supports it."""
    if isinstance(service, MimoVoiceService):
        sender = ""
        if str(getattr(source_event, "unified_msg_origin", "") or "") == target_umo:
            getter = getattr(source_event, "get_sender_id", None)
            if callable(getter):
                sender = str(getter() or "")
        kwargs.update(target_umo=target_umo, user_id=sender)
    return await service.synthesize(text, **kwargs)
