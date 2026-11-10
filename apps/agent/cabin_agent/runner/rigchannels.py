"""按设备能力装配执行通道。本地执行服务与台架 Agent 共用。"""
from __future__ import annotations

from ..device.base import DeviceAdapter
from .actions.adb import AdbChannel
from .actions.audio import SimAudioChannel


def rig_channels(dev: DeviceAdapter) -> list:
    return [AdbChannel(dev)] + ([SimAudioChannel(dev)] if dev.voice_capable else [])


def device_capabilities(dev: DeviceAdapter) -> set[str]:
    """SimCar 模拟语音，所以带 audio；真实台架的音频通道接入后再上报（PF-AUD-03）。"""
    return {"adb", "audio"} if dev.voice_capable else {"adb"}
