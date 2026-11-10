"""cabin-audioasset：测试音频合成与缓存。

TaskPlan 下发前按“文字 + 音色 + 语速 + 模型”合成一次，文件按内容哈希存放，回放只播放缓存（R-1）。
相同参数不重复合成；所有用例共用同一份唤醒词音频。
当前经模型网关调 MiniMax 语音合成；切到私有云语音服务时只需改 VOICES 与网关实现。
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import time
import wave
from pathlib import Path

from cabin_common import audio
from cabin_common.audio import AssetError, duration_ms  # noqa: F401

from .config import get_settings
from .modelgw import get_gateway
from .store import get_store

RATE = 16000
VOICES = {"female_a": "female-shaonv", "male_b": "male-qn-qingse"}  # 测试音色 → 语音服务音色
_locks: dict[str, asyncio.Lock] = {}


def assets_dir() -> Path:
    d = get_settings().data_dir / "assets"
    d.mkdir(parents=True, exist_ok=True)
    return d


def asset_path(sha: str) -> Path:
    return audio.asset_file(assets_dir(), sha)


def cache_key(text: str, voice: str, speed: float, model: str) -> str:
    return hashlib.sha256(f"{text}\0{voice}\0{speed}\0{model}".encode()).hexdigest()


async def synthesize(text: str, voice: str, speed: float, session: str) -> bytes:
    if voice not in VOICES:
        raise AssetError(f"未知测试音色 {voice}（可用：{', '.join(VOICES)}）")
    return await get_gateway().tts(text, session=session, fmt="wav", voice=VOICES[voice], speed=speed, sample_rate=RATE)


async def ensure(text: str, voice: str, speed: float = 1.0, session: str = "") -> dict:
    """取缓存，没有才合成。返回 {sha256, size, duration_ms}。"""
    model = get_settings().tts_model
    key = cache_key(text, voice, speed, model)
    st = get_store()
    async with _locks.setdefault(key, asyncio.Lock()):  # 同一句话并发时只合成一次
        r = st.q("SELECT sha256, size, duration_ms FROM audio_asset WHERE key=?", (key,))
        if r and asset_path(r[0]["sha256"]).is_file():
            return dict(r[0])
        try:
            data = await synthesize(text, voice, speed, session)
            ms = duration_ms(data)
        except AssetError:
            raise
        except Exception as e:
            raise AssetError(f"音频合成失败“{text[:20]}”：{type(e).__name__} {e}") from e
        sha = hashlib.sha256(data).hexdigest()
        asset_path(sha).write_bytes(data)
        st.x("INSERT OR REPLACE INTO audio_asset(key, sha256, text, voice, speed, model, size, duration_ms, created) "
             "VALUES(?,?,?,?,?,?,?,?,?)", (key, sha, text, voice, speed, model, len(data), ms, time.time()))
        st.append(session or "system", "asset.synth", {"text": text, "voice": voice, "speed": speed, "model": model,
                                                       "sha256": sha, "size": len(data), "duration_ms": ms})
        return {"sha256": sha, "size": len(data), "duration_ms": ms}


async def fill_plan(plan: dict, session: str = "") -> None:
    """合成 TaskPlan 引用的全部音频，把哈希写回 assets。
    文字含车型参数或被车型覆盖改写时，按各适用车型的实际文字各合成一份，写入 variants {文字: 哈希}。"""
    from .planvars import RE_PARAM, asset_texts
    ov_utt = any((v or {}).get("utterance") for v in (plan.get("overrides") or {}).values())
    for name, a in (plan.get("assets") or {}).items():
        voice, speed = a.get("voice", "female_a"), float(a.get("speed", 1.0))
        if RE_PARAM.search(a["text"]) or (name == "utt.wav" and ov_utt):
            variants = {}
            for text in sorted(set(asset_texts(plan, name).values())):
                variants[text] = (await ensure(text, voice, speed, session))["sha256"]
            a.pop("size", None), a.pop("duration_ms", None)
            a["sha256"], a["variants"] = None, variants
        else:
            a.update(await ensure(a["text"], voice, speed, session))


def missing(plan: dict) -> list[str]:
    return audio.missing(plan, assets_dir())


def read_verified(sha: str) -> bytes:
    return audio.read_verified(assets_dir(), sha)
