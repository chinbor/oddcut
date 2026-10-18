"""MobileSAM (Apache-2.0, https://github.com/ChaoningZhang/MobileSAM): clean outlines for 抠像.

The weights live in assets/models/mobilesam/mobile_sam.pt (not in git). Without them everything still works
with the older cut (YOLO-seg + GrabCut, depth for the sky). One image is encoded once (~0.6 s on a desktop CPU),
then each prompt - a box, or points to keep / leave out - is ~0.05 s.
"""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager

import cv2
import numpy as np

_LOCK = threading.Lock()
_P: dict = {}


def _path():
    from .paths import MODEL_DIR
    return MODEL_DIR / "mobilesam" / "mobile_sam.pt"


def available() -> bool:
    return _path().exists() and _predictor() is not None


def _predictor():
    if "p" not in _P:
        try:
            os.environ.setdefault("YOLO_OFFLINE", "1")
            from ultralytics.models.sam import Predictor
            _P["p"] = Predictor(overrides=dict(conf=0.25, task="segment", mode="predict", imgsz=1024, model=str(_path()),
                                               save=False, verbose=False)) if _path().exists() else None
        except Exception:                                   # no ultralytics / torch, or a broken file
            _P["p"] = None
    return _P["p"]


class _Image:
    def __init__(self, p, shape):
        self.p, self.shape = p, shape

    def _run(self, **kw) -> np.ndarray | None:
        try:
            r = self.p(**kw)
            m = r[0].masks
            if m is None or not len(m.data):
                return None
            out = m.data[0].cpu().numpy() > 0.5
        except Exception:
            return None
        if out.shape != self.shape:
            out = cv2.resize(out.astype(np.uint8), (self.shape[1], self.shape[0]), interpolation=cv2.INTER_NEAREST) > 0
        return out

    def box(self, x0, y0, x1, y1, pos=None) -> np.ndarray | None:
        """The thing in this box (pixels); pos: a point on it, to be sure which thing."""
        kw = {"bboxes": [[float(x0), float(y0), float(x1), float(y1)]]}
        if pos is not None:
            kw.update(points=[[float(pos[0]), float(pos[1])]], labels=[1])
        return self._run(**kw)

    def points(self, pos, neg=()) -> np.ndarray | None:
        """What the points in `pos` are on and the ones in `neg` are not (pixels)."""
        pts = [[float(x), float(y)] for x, y in list(pos) + list(neg)]
        return self._run(points=[pts], labels=[[1] * len(pos) + [0] * len(neg)])


@contextmanager
def image(rgb: np.ndarray):
    """with sam.image(rgb) as im: im.box(...) / im.points(...) - None if MobileSAM isn't there."""
    with _LOCK:
        p = _predictor()
        if p is None:
            yield None
            return
        p.set_image(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
        try:
            yield _Image(p, rgb.shape[:2])
        finally:
            p.reset_image()
