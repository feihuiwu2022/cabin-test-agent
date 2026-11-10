"""cabin-voice / TTS：按句合成，首句先出声。"""
from __future__ import annotations

import re

SENT_END = re.compile(r"([。！？!?；;\n]|\.(?=\s|$))")


class SentenceSplitter:
    """流式文本 → 完整句子。过短的句子与下一句合并，减少 TTS 请求次数。"""

    def __init__(self, min_len: int = 6):
        self.buf = ""
        self.min_len = min_len

    def feed(self, delta: str) -> list[str]:
        self.buf += delta
        out = []
        while True:
            m = SENT_END.search(self.buf)
            if not m:
                break
            cut = m.end()
            sent = self.buf[:cut].strip()
            if len(sent) < self.min_len and cut < len(self.buf):
                # 太短：尝试并入下一句
                nxt = SENT_END.search(self.buf, cut)
                if not nxt:
                    break
                cut = nxt.end()
                sent = self.buf[:cut].strip()
            self.buf = self.buf[cut:]
            if sent:
                out.append(sent)
        return out

    def flush(self) -> list[str]:
        s, self.buf = self.buf.strip(), ""
        return [s] if s else []


def speakable(text: str) -> str:
    """去掉 markdown 与链接，读得顺。"""
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"[*#`>_|]", "", t)
    t = re.sub(r"【来源[^】]*】|\[来源[^\]]*\]", "", t)
    return t.strip()
