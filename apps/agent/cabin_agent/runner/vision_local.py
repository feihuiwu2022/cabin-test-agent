"""本地视觉定位（台架端 PRD 触屏执行 4.2）：OCR 找文字、模板匹配找图标。都在台架 PC 本地跑，不调大模型。

依赖可选：OCR 用 rapidocr-onnxruntime（或 paddleocr），模板匹配用 opencv-python-headless。
没装时该层定位跳过并给出告警，不影响控件树与坐标定位。
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass
from typing import Protocol

RE_SPACE = re.compile(r"[\s:：·•,，。.]+")


@dataclass
class Found:
    bbox: list[int]  # [l, t, r, b]（屏幕像素）
    score: float
    text: str = ""

    @property
    def center(self) -> tuple[int, int]:
        l, t, r, b = self.bbox
        return (l + r) // 2, (t + b) // 2


class OcrEngine(Protocol):
    def read(self, png: bytes) -> list[tuple[str, list[int], float]]:
        """返回 [(文字, [l,t,r,b], 置信度)]。"""


def _norm(s: str) -> str:
    return RE_SPACE.sub("", s or "").lower()


def _img(png: bytes):
    from PIL import Image
    return Image.open(io.BytesIO(png)).convert("RGB")


class RapidOcr:
    name = "rapidocr"

    def __init__(self) -> None:
        from rapidocr_onnxruntime import RapidOCR
        self.eng = RapidOCR()

    def read(self, png: bytes) -> list[tuple[str, list[int], float]]:
        import numpy as np
        res, _ = self.eng(np.array(_img(png)))
        out = []
        for box, text, score in res or []:
            xs, ys = [p[0] for p in box], [p[1] for p in box]
            out.append((text, [int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))], float(score)))
        return out


class PaddleOcr:
    name = "paddleocr"

    def __init__(self) -> None:
        from paddleocr import PaddleOCR
        self.eng = PaddleOCR(use_angle_cls=False, lang="ch", show_log=False)

    def read(self, png: bytes) -> list[tuple[str, list[int], float]]:
        import numpy as np
        res = self.eng.ocr(np.array(_img(png)), cls=False) or []
        out = []
        for line in (res[0] or []) if res else []:
            box, (text, score) = line
            xs, ys = [p[0] for p in box], [p[1] for p in box]
            out.append((text, [int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))], float(score)))
        return out


_ocr: OcrEngine | None = None
_ocr_err = ""


def get_ocr() -> OcrEngine | None:
    global _ocr, _ocr_err
    if _ocr is None and not _ocr_err:
        for cls in (RapidOcr, PaddleOcr):
            try:
                _ocr = cls()
                break
            except Exception as e:  # 没装或模型加载失败：试下一个
                _ocr_err = f"{cls.name}: {type(e).__name__}"
        else:
            _ocr_err = _ocr_err or "未安装 OCR"
        if _ocr:
            _ocr_err = ""
    return _ocr


def set_ocr(engine: OcrEngine | None) -> None:
    """测试或台架替换 OCR 引擎。"""
    global _ocr, _ocr_err
    _ocr, _ocr_err = engine, ""


def ocr_unavailable() -> str:
    return "" if get_ocr() else (_ocr_err or "未安装 OCR")


def ocr_find(png: bytes, want: str, region: list[int] | None = None, min_score: float = 0.6) -> Found | None:
    """在截图里找文字：整行相等优先，其次包含（按字符位置估算子框）；region 为 [l,t,r,b] 屏幕像素。"""
    eng = get_ocr()
    if not eng or not want:
        return None
    w = _norm(want)
    best: tuple[int, float, Found] | None = None
    for text, bbox, score in eng.read(png):
        if score < min_score:
            continue
        if region and not (region[0] <= (bbox[0] + bbox[2]) / 2 <= region[2] and region[1] <= (bbox[1] + bbox[3]) / 2 <= region[3]):
            continue
        t = _norm(text)
        if not t or w not in t:
            continue
        rank = 2 if t == w else 1
        fb = bbox
        if rank == 1:  # 子串：按字符比例截出子框，点在文字上而不是整行中间
            i = t.index(w)
            l, top, r, b = bbox
            cw = (r - l) / max(len(t), 1)
            fb = [int(l + i * cw), top, int(l + (i + len(w)) * cw), b]
        cand = (rank, score, Found(bbox=fb, score=score, text=text))
        if best is None or cand[:2] > best[:2]:
            best = cand
    return best[2] if best else None


def template_unavailable() -> str:
    try:
        import cv2  # noqa: F401
        return ""
    except Exception as e:
        return f"未安装 OpenCV（{type(e).__name__}）"


SCALES = (1.0, 0.9, 1.1, 0.8, 1.25)


def template_find(png: bytes, tpl_png: bytes, threshold: float = 0.85,
                  region: list[int] | None = None) -> Found | None:
    """图标模板匹配（灰度、归一化相关，多尺度）；返回最佳且 ≥ threshold 的位置。"""
    if template_unavailable():
        return None
    import cv2
    import numpy as np
    scr = cv2.cvtColor(np.array(_img(png)), cv2.COLOR_RGB2GRAY)
    tpl0 = cv2.cvtColor(np.array(_img(tpl_png)), cv2.COLOR_RGB2GRAY)
    ox = oy = 0
    if region:
        l, t, r, b = [max(0, int(v)) for v in region]
        scr, ox, oy = scr[t:b, l:r], l, t
    best: Found | None = None
    for s in SCALES:
        tpl = tpl0 if s == 1.0 else cv2.resize(tpl0, None, fx=s, fy=s, interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR)
        th, tw = tpl.shape[:2]
        if th > scr.shape[0] or tw > scr.shape[1] or th < 4 or tw < 4:
            continue
        res = cv2.matchTemplate(scr, tpl, cv2.TM_CCOEFF_NORMED)
        _, score, _, (x, y) = cv2.minMaxLoc(res)
        if score >= threshold and (best is None or score > best.score):
            best = Found(bbox=[x + ox, y + oy, x + ox + tw, y + oy + th], score=float(score))
    return best


def crop(png: bytes, bbox: list[int], pad: int = 0) -> bytes:
    """按框裁图（UI 地图录制时存控件图标模板用）。"""
    im = _img(png)
    l, t, r, b = bbox
    box = (max(0, l - pad), max(0, t - pad), min(im.width, r + pad), min(im.height, b + pad))
    buf = io.BytesIO()
    im.crop(box).save(buf, "PNG")
    return buf.getvalue()
