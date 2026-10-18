"""画中画 on a plan: _check_overlays cleans and checks the overlay list the app sends (pip.py describes every
field), set_overlays stores it, _resnap_overlays moves overlays onto the beats of another song.
"""
from dataclasses import replace
from pathlib import Path

import numpy as np

from .looks import check_drawn_style
from .plan_data import Analysis, Plan, _norm


OVERLAY_TRACKS = 6       # 画中画 1..6 above the main track; a higher track is drawn on top
MIN_OVERLAY = 0.3


def _check_overlays(analysis: Analysis, total: float, overlays) -> list[dict]:
    """Picture-in-picture clips laid over the edit (see pip.py for every field). A 宫格 (grid) carries its
    clips in grid.cells instead of one clip; its first cell's clip stands in as the overlay's clip."""
    from .pip import ANIMS, BLENDS, EASES, LAYOUTS, MASKS
    clips = {_norm(c.path): c for c in analysis.clips}

    def clip_of(path: str):
        c = clips.get(_norm(path))
        if c is None:
            raise ValueError(f"画中画的素材不在这个项目里：{Path(path).name}")
        return c

    def num(v, lo, hi, default):
        try:
            return min(hi, max(lo, float(v)))
        except (TypeError, ValueError):
            return default

    out = []
    for n, o in enumerate(overlays or []):
        grid = o.get("grid")
        if grid:
            layout = grid.get("layout")
            if layout not in LAYOUTS:
                raise ValueError(f"unknown grid layout {layout!r}")
            k = LAYOUTS[layout][0] * LAYOUTS[layout][1]
            cells = []
            for cell in (grid.get("cells") or [])[:k]:
                c = clip_of(cell["clip"])
                cells.append({"clip": c.path, "name": c.name,
                              "src_start": round(num(cell.get("src_start"), 0, max(0.0, c.duration - MIN_OVERLAY), 0.0), 3)})
            if len(cells) < k:
                raise ValueError(f"这个宫格需要 {k} 段素材")
            c = clip_of(cells[0]["clip"])
            src = cells[0]["src_start"]
            room = min(clip_of(x["clip"]).duration - x["src_start"] for x in cells)
        else:
            c = clip_of(o["clip"])
            src = num(o.get("src_start"), 0, max(0.0, c.duration - MIN_OVERLAY), 0.0)
            room = c.duration - src
        t0 = num(o.get("t_start"), 0, total - MIN_OVERLAY, 0.0)
        t1 = min(num(o.get("t_end"), 0, total, total), t0 + room)
        if t1 - t0 < MIN_OVERLAY - 1e-6:
            raise ValueError("画中画片段太短（至少 0.3 秒）")
        w = num(o.get("w"), 0.05, 1.0, 0.5)
        h = num(o.get("h"), 0.05, 1.0, 0.5)
        item = {"id": str(o.get("id") or f"ov{n}"), "track": int(num(o.get("track"), 0, OVERLAY_TRACKS - 1, 0)),
                "clip": c.path, "name": c.name, "src_start": round(src, 3), "t_start": round(t0, 3), "t_end": round(t1, 3),
                "x": round(num(o.get("x"), 0.02 - w, 0.98, 0.0), 4), "y": round(num(o.get("y"), 0.02 - h, 0.98, 0.0), 4),
                "w": round(w, 4), "h": round(h, 4), "rot": round(num(o.get("rot"), -360, 360, 0.0), 2),
                "sc": round(num(o.get("sc"), 0.05, 5, 1.0), 4),
                "opacity": round(num(o.get("opacity"), 0.05, 1, 1.0), 3),
                "radius": round(num(o.get("radius"), 0, 0.5, 0.0), 3), "border": bool(o.get("border")),
                "blend": o.get("blend") if o.get("blend") in BLENDS else "normal"}
        if o.get("link") and not grid:
            item["link"] = True
        if grid:
            item["grid"] = {"layout": grid["layout"], "cells": cells, "gap": round(num(grid.get("gap"), 0, 0.08, 0.01), 3)}
            item["name"] = {"2v": "上下分屏", "2h": "左右分屏", "3v": "三宫格", "4": "四宫格", "9": "九宫格"}[grid["layout"]]
        m = o.get("mask") or {}
        if m.get("shape") == "cutout" and not grid:
            def cut_target(tg):
                if tg.get("kind") == "object":
                    target = {"kind": "object", "x": round(num(tg.get("x"), 0, 1, 0.5), 4), "y": round(num(tg.get("y"), 0, 1, 0.5), 4)}
                elif tg.get("kind") == "lasso":
                    target = {"kind": "lasso", "pts": [[round(num(q[0], 0, 1, 0), 4), round(num(q[1], 0, 1, 0), 4)]
                                                       for q in (tg.get("pts") or [])[:600]]}
                else:
                    from .cutout import PARTS
                    target = {"kind": "region", "parts": [p_ for p_ in (tg.get("parts") or []) if p_ in PARTS] or ["things"]}
                    if tg.get("band"):
                        target["band"] = True
                if tg.get("t") and target["kind"] != "region":
                    target["t"] = round(num(tg.get("t"), 0, 3600, 0), 3)
                return target
            target = cut_target(m.get("target") or {})
            brush = [{"op": "sub" if b.get("op") == "sub" else "add", "r": round(num(b.get("r"), 0.005, 0.3, 0.04), 4),
                      "pts": [[round(num(q[0], 0, 1, 0), 4), round(num(q[1], 0, 1, 0), 4)] for q in (b.get("pts") or [])[:400]]}
                     for b in (m.get("brush") or [])[:60]]
            item["mask"] = {"shape": "cutout", "target": target, "brush": brush,
                            "feather": round(num(m.get("feather"), 0, 0.1, 0.004), 4), "invert": bool(m.get("invert"))}
            extra = [{"op": "sub" if e.get("op") == "sub" else "add", "target": cut_target(e.get("target") or {})}
                     for e in (m.get("extra") or [])[:20] if isinstance(e, dict)]
            if extra:
                item["mask"]["extra"] = extra
            st = m.get("stroke")
            if isinstance(st, dict):
                col = str(st.get("color") or "#ffffff")
                if not (len(col) == 7 and col[0] == "#" and all(c in "0123456789abcdefABCDEF" for c in col[1:])):
                    col = "#ffffff"
                item["mask"]["stroke"] = {"color": col.lower(), "width": round(num(st.get("width"), 0.003, 0.08, 0.02), 4)}
            if m.get("hold"):
                item["mask"]["hold"] = True
            if m.get("dilate"):
                item["mask"]["dilate"] = round(num(m.get("dilate"), 0, 0.1, 0.02), 4)
        elif m.get("shape", "none") != "none" and not grid:
            if m["shape"] not in MASKS:
                raise ValueError(f"unknown mask {m['shape']!r}")
            item["mask"] = {"shape": m["shape"], "cx": round(num(m.get("cx"), -0.5, 1.5, 0.5), 4),
                            "cy": round(num(m.get("cy"), -0.5, 1.5, 0.5), 4), "size": round(num(m.get("size"), 0.05, 2, 0.8), 4),
                            "rot": round(num(m.get("rot"), -360, 360, 0), 2), "feather": round(num(m.get("feather"), 0, 0.5, 0), 3),
                            "invert": bool(m.get("invert"))}
        keys = []
        for k_ in o.get("keys") or []:
            key = {"t": round(num(k_.get("t"), 0, t1 - t0, 0), 3), "x": round(num(k_.get("x"), -1, 2, 0.5), 4),
                   "y": round(num(k_.get("y"), -1, 2, 0.5), 4), "s": round(num(k_.get("s"), 0.05, 5, 1), 4),
                   "r": round(num(k_.get("r"), -720, 720, 0), 2), "a": round(num(k_.get("a"), 0, 1, 1), 3)}
            if k_.get("e") in EASES and k_.get("e") != "smooth":
                key["e"] = k_["e"]
            keys.append(key)
        if keys:
            item["keys"] = sorted(keys, key=lambda k_: k_["t"])
        if o.get("mblur"):
            item["mblur"] = round(num(o.get("mblur"), 0, 1.5, 0), 2)
        fx = o.get("fx") or {}
        if fx.get("kind") == "drawn" and not grid:
            fk = sorted(({"t": round(num(k_.get("t"), 0, t1 - t0, 0), 3), "v": round(num(k_.get("v"), 0, 2, 0), 3)}
                         for k_ in (fx.get("keys") or [])[:40]), key=lambda k_: k_["t"])
            item["fx"] = {"kind": "drawn", "style": check_drawn_style(fx.get("style")), "keys": fk or [{"t": 0, "v": 2}]}
        if o.get("group"):                            # made by a preset (万物生长 ...): {id, kind, layer, of, label}
            g_ = o["group"]
            item["group"] = {"id": str(g_.get("id")), "kind": str(g_.get("kind")), "layer": int(g_.get("layer", 0)),
                             "of": int(g_.get("of", 1)), "label": str(g_.get("label", ""))[:20]}
        for which in ("anim_in", "anim_out"):
            a = o.get(which) or {}
            if a.get("type", "none") != "none":
                if a["type"] not in ANIMS:
                    raise ValueError(f"unknown animation {a['type']!r}")
                item[which] = {"type": a["type"], "beats": 0.5 if float(a.get("beats", 1)) < 0.75 else 1}
        out.append(item)
    return out


def set_overlays(analysis: Analysis, p: Plan, overlays) -> tuple[Plan, list[str]]:
    return replace(p, looks={**p.looks, "overlays": _check_overlays(analysis, p.duration, overlays)}), []


def _resnap_overlays(analysis: Analysis, overlays: list[dict], total: float) -> tuple[list[dict], list[str]]:
    """Overlays on another song: same times, their ends moved to the nearest beats of the new one."""
    grid = analysis.music.grid
    near = lambda t: float(grid[np.argmin(np.abs(grid - t))]) if len(grid) else t
    out, dropped = [], 0
    for o in overlays:
        if o["t_start"] >= total - MIN_OVERLAY:
            dropped += 1
            continue
        t0, t1 = near(o["t_start"]), near(o["t_end"])
        if t1 - t0 < MIN_OVERLAY:
            t0, t1 = o["t_start"], o["t_end"]
        out.append({**o, "t_start": t0, "t_end": min(t1, total)})
    try:
        out = _check_overlays(analysis, total, out)
    except ValueError:
        out = [o for o in out if o["t_end"] - o["t_start"] >= MIN_OVERLAY]
    return out, ([f"{dropped} 个画中画在新歌结束之后，已去掉"] if dropped else [])
