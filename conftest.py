"""测试使用独立数据目录，不污染本地结果库。"""
import os
import shutil
import tempfile
from pathlib import Path

_src = Path(__file__).resolve().parent / "apps" / "cloud" / "data"
_tmp = Path(tempfile.mkdtemp(prefix="cabin-test-"))
for sub in ("cases",):
    shutil.copytree(_src / sub, _tmp / sub)
os.environ.setdefault("CABIN_DATA_DIR", str(_tmp))
# 测试绝不连真实飞书：环境变量优先于 .env，这里置空
for _k in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_BITABLE_URL", "FEISHU_WEBHOOK", "FEISHU_CASE_DOC"):
    os.environ[_k] = ""
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def fake_tts(monkeypatch):
    """测试绝不调用真实语音合成：按文字生成确定的短 WAV，并记录调用。"""
    import hashlib
    import io
    import wave

    from cabin_cloud import audioasset

    calls = []

    async def fake(text, voice, speed, session):
        calls.append((text, voice, speed))
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(audioasset.RATE)
            w.writeframes(hashlib.sha256(f"{text}{voice}{speed}".encode()).digest())
        return buf.getvalue()

    monkeypatch.setattr(audioasset, "synthesize", fake)
    return calls


for _k in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_BITABLE_URL", "FEISHU_WEBHOOK", "FEISHU_CASE_DOC"):
    os.environ[_k] = ""
