"""Hand edits from the desktop app on the 主轨 shots: the shot inspector (edit_shot: sticker / 手绘 / 万物生长),
moving a cut, 分割, 修剪, reordering shots, putting the edit on another song (retime) and carrying hand edits over
to a new plan (carry_manual). Each edit returns the new plan and notes for the user (Chinese).
"""
from dataclasses import replace
from pathlib import Path

import numpy as np

from .looks import DEFAULT_DRAWN_STYLE, check_drawn_style, check_stickers
from .planner import Shot
from .plan_data import Analysis, MusicRef, Plan, _norm
from .overlays import _check_overlays, _resnap_overlays


# ----------------------------------------------------------------------------- hand edits (desktop app)

KEEP = object()          # edit_shot: leave this part of the shot as it is


def _frames_between(a: float, b: float, fps: float) -> int:
    return int(round(b * fps)) - int(round(a * fps))


def _check_place(place: dict) -> dict:
    keys = []
    for k in place.get("keys") or []:
        keys.append({"t": max(0.0, float(k["t"])), "x": min(1.0, max(0.0, float(k["x"]))),
                     "y": min(1.0, max(0.0, float(k["y"]))), "size": min(0.8, max(0.04, float(k.get("size", 0.22))))})
    if not keys:
        raise ValueError("a placed sticker needs at least one keyframe")
    mode = place.get("mode", "follow")
    if mode not in ("follow", "path", "still", "fixed"):
        raise ValueError(f"unknown sticker mode {mode!r}")
    out = {"mode": mode, "keys": sorted(keys, key=lambda k: k["t"]), "in": max(0.0, float(place.get("in") or 0.0))}
    if place.get("out") is not None:
        out["out"] = float(place["out"])
    return out


def check_grow(g: dict) -> dict:
    """万物生长 settings: layers 3 or 4, a layer every beat or half-beat, motion blur 0..1.5; source auto / manual
    (a manual choice wins over the automatic pick and survives a new plan); {"off": true} = turned off by hand."""
    source = g.get("source", "manual") if g.get("source") in ("auto", "manual") else "manual"
    if g.get("off"):
        return {"off": True, "source": "manual"}
    stagger = g.get("stagger", "beat")
    if stagger not in ("beat", "half"):
        raise ValueError(f"unknown stagger {stagger!r}")
    out = {"layers": 4 if int(g.get("layers", 3)) >= 4 else 3, "stagger": stagger,
           "blur": round(min(1.5, max(0.0, float(g.get("blur", 1.0)))), 2), "source": source}
    if g.get("why"):
        out["why"] = str(g["why"])[:80]
    return out


def grows(s: Shot) -> bool:
    """Does this shot grow in (万物生长 on and not turned off by hand)?"""
    return bool(s.grow) and not s.grow.get("off")


def grow_all(p: Plan, g: dict | None) -> Plan:
    """全片万物生长: every shot grows in (the first out of black), or none does (g=None)."""
    g = check_grow({**g, "source": "manual"}) if g else None
    return replace(p, shots=[replace(s, grow=g) for s in p.shots])


def grow_hint(analysis: Analysis, p: Plan, index: int, cache_file: Path | None = None) -> str | None:
    """When a shot is turned on by hand: a short warning if it doesn't suit growing in."""
    from .formats import output_size
    from .growth import AUTO_THRESHOLD, score_shot
    w, h = output_size(p.template)
    s = p.shots[index]
    sc = score_shot(s.clip.path, s.src_start, w / h)
    if sc["visual"] >= AUTO_THRESHOLD * 0.9 or not sc.get("why_not"):
        return None
    return f"这个镜头不太适合万物生长（{sc['why_not']}），效果可能不明显"


def edit_shot(analysis: Analysis, p: Plan, index: int, sticker=KEEP, drawn=KEEP, grow=KEEP) -> tuple[Plan, list[str]]:
    """Hand edits from the app's shot inspector. Returns the new plan and notes for the user (Chinese).
    sticker: None = no sticker; {"id", "mode", "keys", "in", "out"} = placed by hand (any catalog sticker).
    drawn:   None = not drawn; {"style", "box"?, "force"?} = draw this shot. A drawn shot needs time to draw,
             so a short one is lengthened to a strong beat; the following shot(s) give up that time (their
             cuts stay on the beat grid and the total length is unchanged)."""
    if not 0 <= index < len(p.shots):
        raise IndexError(f"shot {index} out of range (plan has {len(p.shots)})")
    tpl = p.template
    fps = float(tpl["output"]["fps"])
    shots = [replace(s) for s in p.shots]
    s = shots[index]
    notes: list[str] = []
    if grow is not KEEP:
        s.grow = check_grow(grow) if grow else None
        if s.grow and not s.grow.get("off"):
            hint = grow_hint(analysis, p, index)
            if hint:
                notes.append(hint)
    if sticker is not KEEP:
        if sticker is None:
            s.sticker, s.sticker_at = None, None
            if s.role == "sticker":
                s.role = "body"
        else:
            sid = check_stickers([sticker["id"]])[0]
            s.sticker, s.sticker_at = sid, _check_place(sticker)
            if s.drawn:                       # one effect per shot
                s.drawn = None
                s.role = "hook" if index == 0 else "body"
                notes.append("这个镜头原来的手绘已换成贴纸")
    if drawn is not KEEP:
        if drawn is None:
            if s.drawn:
                s.drawn = None
                s.role = "hook" if index == 0 else ("outro" if index == len(shots) - 1 else "body")
        else:
            style = check_drawn_style(drawn.get("style"))
            cfg = tpl.get("drawn_opening" if index == 0 else "drawn_mid") or tpl["drawn_opening"]
            lo, hi = cfg.get("reveal_window", (1.0, 1.7))
            rs = float(cfg.get("reveal_seconds", 0.4))
            tlo, thi = cfg.get("tail", (0.5, 1.1))
            grid, strength = analysis.music.grid, analysis.music.strength
            t0 = s.t_start
            m = (grid >= t0 + lo) & (grid <= t0 + hi)
            reveal = float(grid[m][np.argmax(strength[m])]) if m.any() else t0 + (lo + hi) / 2
            need = reveal + rs + tlo
            if s.t_end < need - 1e-6:
                if index == len(shots) - 1:
                    raise ValueError("最后一个镜头太短，放不下手绘（画线、上色、变回实拍至少要 1.9 秒）")
                em = (grid >= need) & (grid <= reveal + rs + thi)
                new_end = float(grid[em][np.argmax(strength[em])]) if em.any() else need
                clip_len = next((c.duration for c in analysis.clips if _norm(c.path) == _norm(s.clip.path)), None)
                if clip_len is not None and s.src_start + (new_end - t0) > clip_len:
                    raise ValueError(f"这段素材（{s.clip.name}）从这里开始不够长，放不下手绘")
                old_len = s.t_end - t0
                k = index + 1
                merged = 0
                while k < len(shots) and shots[k].t_start < new_end - 1e-6:
                    nxt = shots[k]
                    if nxt.t_end - new_end >= 0.4 or k == len(shots) - 1:
                        if nxt.t_end - new_end < 0.4:
                            raise ValueError("后面只剩结尾镜头，没有足够的时间拉长这个镜头")
                        cut = new_end - nxt.t_start
                        nxt.src_start += cut
                        nxt.t_start = new_end
                        nxt.frames = _frames_between(nxt.t_start, nxt.t_end, fps)
                        if nxt.sticker_at:
                            nxt.sticker_at = _shift_keys(nxt.sticker_at, -cut)
                        break
                    shots.pop(k)              # swallowed whole by the longer drawn shot
                    merged += 1
                s.t_end = new_end
                s.frames = _frames_between(t0, new_end, fps)
                notes.append(f"手绘需要时间，这个镜头从 {old_len:.1f} 秒拉长到 {new_end - t0:.1f} 秒（停在重拍上）"
                             + (f"，后面 {merged} 个镜头被合并掉了" if merged else "，后一个镜头相应缩短"))
            s.sticker, s.sticker_at = None, None
            s.role = "drawn"
            meta = {"reveal_at": round(reveal - t0, 3), "tail": round(s.t_end - reveal - rs, 3),
                    "label": drawn.get("label") or (s.drawn or {}).get("label") or "手绘", "manual": True}
            if style != DEFAULT_DRAWN_STYLE:
                meta["style"] = style
            if drawn.get("box"):
                meta["box"] = [round(min(1.0, max(0.0, float(v))), 4) for v in drawn["box"]]
            if drawn.get("force"):
                meta["force"] = True
            s.drawn = meta
    return replace(p, shots=shots), notes


def _shift_keys(place: dict, dt: float) -> dict:
    """Keyframe times after the shot start moved by dt (the shot now starts later / earlier in its clip)."""
    out = dict(place)
    out["keys"] = [{**k, "t": max(0.0, k["t"] + dt)} for k in place["keys"]]
    out["in"] = max(0.0, float(place.get("in") or 0.0) + dt)
    if place.get("out") is not None:
        out["out"] = max(0.0, float(place["out"]) + dt)
    return out


def has_manual(p: Plan) -> bool:
    return bool(p.looks.get("edited_cuts")) or any(s.sticker_at or (s.drawn and s.drawn.get("manual")) for s in p.shots)


MIN_SHOT = 0.4           # shortest shot a cut edit may leave


def _drawn_cfg(tpl: dict, index: int) -> tuple[float, float]:
    """(reveal seconds, shortest tail) of a drawn shot."""
    cfg = tpl.get("drawn_opening" if index == 0 else "drawn_mid") or tpl["drawn_opening"]
    return float(cfg.get("reveal_seconds", 0.4)), float(cfg.get("tail", (0.5, 1.1))[0])


def _retail(s: Shot, rs: float) -> None:
    """A drawn shot's tail after its length or reveal moved."""
    s.drawn = {**s.drawn, "tail": round(s.t_end - s.t_start - s.drawn["reveal_at"] - rs, 3)}


def _clip_len(analysis: Analysis, path: str) -> float | None:
    return next((c.duration for c in analysis.clips if _norm(c.path) == _norm(path)), None)


def move_cut(analysis: Analysis, p: Plan, k: int, t: float, snap: bool = True) -> tuple[Plan, list[str]]:
    """Move the cut between shot k-1 and shot k to timeline time `t` (snapped to the nearest beat unless
    snap=False). The shot before ends there, the one after starts there and shows the same footage it showed
    at that moment (its start in the clip moves with the cut), so nothing else moves and the total stays."""
    if not 1 <= k < len(p.shots):
        raise IndexError(f"cut {k} out of range (plan has {len(p.shots)} shots)")
    fps = float(p.template["output"]["fps"])
    shots = [replace(s) for s in p.shots]
    a, b = shots[k - 1], shots[k]
    lo, hi = a.t_start + MIN_SHOT, b.t_end - MIN_SHOT
    if a.drawn:                                   # the drawing has to finish before the cut
        rs, tail = _drawn_cfg(p.template, k - 1)
        lo = max(lo, a.t_start + a.drawn["reveal_at"] + rs + tail)
    if b.drawn:                                   # and a drawn shot after it needs its drawing time
        rs, tail = _drawn_cfg(p.template, k)
        hi = min(hi, b.t_start + b.drawn["reveal_at"] - 0.5, b.t_end - rs - tail)
    clip_a = _clip_len(analysis, a.clip.path)
    if clip_a is not None:                        # can't show more of a clip than there is
        hi = min(hi, a.t_start + (clip_a - a.src_start) - 0.05)
    lo = max(lo, b.t_start - b.src_start)         # nor start the next shot before its clip starts
    if lo > hi:
        raise ValueError("这两个镜头已经不能再调了（太短，或者手绘需要的时间不够）")
    if snap:
        grid = analysis.music.grid
        inside = grid[(grid >= lo - 1e-6) & (grid <= hi + 1e-6)]
        if len(inside):
            t = float(inside[np.argmin(np.abs(inside - t))])
    t = float(min(max(t, lo), hi))
    d = t - b.t_start
    a.t_end = t
    a.frames = _frames_between(a.t_start, a.t_end, fps)
    b.t_start = t
    b.src_start += d
    b.frames = _frames_between(b.t_start, b.t_end, fps)
    if b.sticker_at:
        b.sticker_at = _shift_keys(b.sticker_at, -d)
    if b.drawn:
        b.drawn = {**b.drawn, "reveal_at": round(b.drawn["reveal_at"] - d, 3)}
        _retail(b, _drawn_cfg(p.template, k)[0])
    if a.drawn:
        _retail(a, _drawn_cfg(p.template, k - 1)[0])
    return replace(p, shots=shots, looks={**p.looks, "edited_cuts": True}), []


def split_shot(analysis: Analysis, p: Plan, index: int, t: float) -> tuple[Plan, list[str]]:
    """分割: shot `index` becomes two at timeline time `t` (to the frame): the same footage carries on across
    the new cut. A placed sticker's keyframes go to the half they fall in; a drawing / 万物生长 stays with the
    first half."""
    if not 0 <= index < len(p.shots):
        raise IndexError(f"shot {index} out of range")
    fps = float(p.template["output"]["fps"])
    s = p.shots[index]
    t = round(t * fps) / fps
    if t - s.t_start < MIN_SHOT - 1e-6 or s.t_end - t < MIN_SHOT - 1e-6:
        raise ValueError("分割点离片段两头太近了（每段至少 0.4 秒）")
    d = t - s.t_start
    a = replace(s, t_end=t, frames=_frames_between(s.t_start, t, fps))
    b = replace(s, t_start=t, src_start=s.src_start + d, frames=_frames_between(t, s.t_end, fps), drawn=None, grow=None,
                role="body" if s.role in ("hook", "drawn") else s.role)
    if s.sticker_at:
        keys_a = [k for k in s.sticker_at["keys"] if k["t"] < d]
        keys_b = [{**k, "t": round(k["t"] - d, 4)} for k in s.sticker_at["keys"] if k["t"] >= d]
        a = replace(a, sticker_at={**s.sticker_at, "keys": keys_a} if keys_a else None, sticker=s.sticker if keys_a else None)
        b = replace(b, sticker_at=_shift_keys({**s.sticker_at, "keys": keys_b}, 0) if keys_b else None, sticker=s.sticker if keys_b else None)
        if b.sticker_at:
            b.sticker_at["in"] = max(0.0, float(s.sticker_at.get("in") or 0) - d)
    if s.drawn and s.drawn["reveal_at"] + _drawn_cfg(p.template, index)[0] > d:
        a = replace(a, drawn=None, role="hook" if index == 0 else "body")
    shots = list(p.shots[:index]) + [a, b] + list(p.shots[index + 1:])
    return replace(p, shots=shots, looks={**p.looks, "edited_cuts": True}), []


def trim_shot(analysis: Analysis, p: Plan, index: int, side: str, t: float, snap: bool = False) -> tuple[Plan, list[str]]:
    """修剪, as in 剪映: drag shot `index`'s start edge ('in') or end edge ('out') to timeline time `t`. The shot
    gets shorter or longer and everything after it moves up or back (the 画中画 layers and 万物生长 that start
    after its end too), so the video gets shorter or longer. 'in' starts the footage later (or earlier) in the
    clip; the shot itself stays where it starts. snap: the edge goes onto the nearest beat."""
    if not 0 <= index < len(p.shots):
        raise IndexError(f"shot {index} out of range")
    fps = float(p.template["output"]["fps"])
    s = p.shots[index]
    if snap:
        grid = analysis.music.grid
        if len(grid):
            t = float(grid[np.argmin(np.abs(grid - t))])
    clip_len = _clip_len(analysis, s.clip.path) or 1e9
    min_n = int(np.ceil(MIN_SHOT * fps))
    if side == "out":
        n = int(round((t - s.t_start) * fps))
        n = max(min_n, min(n, int((clip_len - s.src_start - 0.05) * fps)))
        dn, src = n - s.frames, s.src_start
    else:
        cut = int(round((t - s.t_start) * fps))           # frames taken off the front (negative: put back)
        cut = max(-int(s.src_start * fps), min(cut, s.frames - min_n))
        dn, src = -cut, s.src_start + cut / fps
    if dn == 0 and src == s.src_start:
        return p, []
    d = dn / fps
    if s.drawn:
        rs, tail = _drawn_cfg(p.template, index)
        if s.drawn["reveal_at"] + rs + tail > (s.frames + dn) / fps + 1e-6:
            raise ValueError("手绘需要时间，这个镜头不能再短了")
    edge = s.t_end
    first = replace(s, src_start=src, t_end=s.t_end + d, frames=s.frames + dn)
    if s.sticker_at and side == "in":
        first = replace(first, sticker_at=_shift_keys(s.sticker_at, -(src - s.src_start)))
    after = [replace(x, t_start=x.t_start + d, t_end=x.t_end + d) for x in p.shots[index + 1:]]
    shots = list(p.shots[:index]) + [first] + after
    looks = dict(p.looks)
    moved = []
    for o in looks.get("overlays") or []:
        if o["t_start"] >= edge - 1e-3:
            o = {**o, "t_start": o["t_start"] + d, "t_end": o["t_end"] + d}
        moved.append(o)
    looks["grow_presets"] = [({**g, "cut": round(g["cut"] + d, 4), "land": round(g["land"] + d, 4)} if g["cut"] >= edge - 1e-3 else g)
                             for g in looks.get("grow_presets") or []]
    looks["edited_cuts"] = True
    q = replace(p, shots=shots, looks=looks)
    looks["overlays"] = _check_overlays(analysis, q.duration, moved)
    return replace(q, looks=looks), []


def move_shot(analysis: Analysis, p: Plan, src: int, dst: int) -> tuple[Plan, list[str]]:
    """Move shot `src` to position `dst` (as in a timeline drag). Every shot keeps its footage (clip and start
    in it), its sticker and drawing; the shots are laid end to end in the new order and each cut then goes to
    the nearest beat (at least MIN_SHOT per shot, never past the end of a clip, drawn shots keep room to draw,
    the total length is unchanged). Drawn shots reveal on the strongest beat near where they did before."""
    n = len(p.shots)
    if not (0 <= src < n and 0 <= dst < n):
        raise IndexError(f"shot {src} -> {dst} out of range (plan has {n})")
    if src == dst:
        return p, []
    fps = float(p.template["output"]["fps"])
    grid, strength = analysis.music.grid, analysis.music.strength
    shots = [replace(s) for s in p.shots]
    s = shots.pop(src)
    shots.insert(dst, s)
    total = p.shots[-1].t_end
    lens = [x.t_end - x.t_start for x in shots]
    # cuts: nominal = shots end to end with their own lengths, then the nearest allowed beat
    cuts, prev, nominal = [], 0.0, 0.0
    for i in range(1, n):
        nominal += lens[i - 1]
        a = shots[i - 1]
        lo = prev + MIN_SHOT
        hi = total - MIN_SHOT * (n - i)
        if a.drawn:                                    # room to draw, reveal and show the tail
            rs, tail = _drawn_cfg(p.template, i - 1)
            lo = max(lo, prev + min(lens[i - 1], 0.6 + rs + tail))
        clip_a = _clip_len(analysis, a.clip.path)
        if clip_a is not None:                         # can't show more of a clip than there is
            hi = min(hi, prev + (clip_a - a.src_start) - 0.05)
        hi = max(hi, lo)
        cand = grid[(grid >= lo - 1e-6) & (grid <= hi + 1e-6)]
        t = float(cand[np.argmin(np.abs(cand - nominal))]) if len(cand) else min(max(nominal, lo), hi)
        cuts.append(t)
        prev = t
    bounds = [0.0] + cuts + [total]
    notes = []
    for i, x in enumerate(shots):
        x.t_start, x.t_end = bounds[i], bounds[i + 1]
        x.frames = _frames_between(x.t_start, x.t_end, fps)
        if x.role in ("hook", "outro", "body"):        # opening / closing roles belong to positions
            x.role = "hook" if i == 0 else ("outro" if i == n - 1 else "body")
        if x.drawn:
            rs, tail = _drawn_cfg(p.template, i)
            want = x.t_start + x.drawn["reveal_at"]
            m = (grid >= x.t_start + 0.6) & (grid <= x.t_end - rs - tail)
            reveal = float(grid[m][np.argmax(strength[m] - 0.3 * np.abs(grid[m] - want))]) if m.any() else None
            if reveal is None:
                x.drawn = None
                x.role = "hook" if i == 0 else ("outro" if i == n - 1 else "body")
                notes.append(f"镜头 {i + 1} 移动后太短，放不下手绘，已改成普通镜头")
            else:
                x.drawn = {**x.drawn, "reveal_at": round(reveal - x.t_start, 3)}
                _retail(x, rs)
        if x.sticker_at and x.sticker_at.get("out") is not None:
            x.sticker_at = {**x.sticker_at, "out": min(float(x.sticker_at["out"]), x.t_end - x.t_start)}
    return replace(p, shots=shots, looks={**p.looks, "edited_cuts": True}), notes


def retime(analysis: Analysis, p: Plan) -> tuple[Plan, list[str]]:
    """Keep this edit (shots, footage, stickers, drawings) but put it on another song: every cut snaps to the
    nearest beat of the new song, drawn shots reveal on its strongest beat, the music reference changes."""
    fps = float(p.template["output"]["fps"])
    grid, strength = analysis.music.grid, analysis.music.strength
    shots = [replace(s) for s in p.shots]
    n = len(shots)
    total = min(p.duration, float(analysis.music.duration) - 0.3)
    cuts, prev = [], 0.0
    for i, s in enumerate(shots[1:], start=1):
        room = total - MIN_SHOT * (n - i)
        cand = grid[(grid >= prev + MIN_SHOT) & (grid <= room)]
        t = float(cand[np.argmin(np.abs(cand - s.t_start))]) if len(cand) else min(max(s.t_start, prev + MIN_SHOT), room)
        cuts.append(t)
        prev = t
    ends = grid[(grid >= prev + 0.8) & (grid <= total + 0.3)]
    end = float(ends[np.argmin(np.abs(ends - total))]) if len(ends) else total
    bounds = [0.0] + cuts + [end]
    notes = []
    for i, s in enumerate(shots):
        old_start = s.t_start
        s.t_start, s.t_end = bounds[i], bounds[i + 1]
        s.frames = _frames_between(s.t_start, s.t_end, fps)
        if s.drawn:
            rs, tail = _drawn_cfg(p.template, i)
            want = old_start + s.drawn["reveal_at"]
            m = (grid >= s.t_start + 0.6) & (grid <= s.t_end - rs - tail)
            reveal = float(grid[m][np.argmax(strength[m] - 0.3 * np.abs(grid[m] - want))]) if m.any() else None
            if reveal is None:
                s.drawn = None
                s.role = "hook" if i == 0 else "body"
                notes.append(f"镜头 {i + 1} 在新歌里太短，放不下手绘，已改成普通镜头")
                continue
            s.drawn = {**s.drawn, "reveal_at": round(reveal - s.t_start, 3)}
            _retail(s, rs)
        if s.sticker_at and s.sticker_at.get("out") is not None:
            s.sticker_at = {**s.sticker_at, "out": min(float(s.sticker_at["out"]), s.t_end - s.t_start)}
    m = analysis.music
    music = MusicRef(m.path, float(m.start), float(m.tempo), float(m.beat_period))
    looks = {**p.looks, "edited_cuts": True}
    if p.looks.get("overlays"):
        looks["overlays"], more = _resnap_overlays(analysis, p.looks["overlays"], bounds[-1])
        notes += more
    return replace(p, shots=shots, music=music, looks=looks), notes


def _overlap(a: Shot, b: Shot) -> float:
    """Seconds of the same footage two shots share."""
    if _norm(a.clip.path) != _norm(b.clip.path):
        return 0.0
    return min(a.src_start + (a.t_end - a.t_start), b.src_start + (b.t_end - b.t_start)) - max(a.src_start, b.src_start)


def carry_manual(old: Plan, new: Plan, analysis: Analysis) -> tuple[Plan, list[str]]:
    """Hand edits of `old` re-applied to `new` (another song or plan): each goes to the new shot that shows the
    same footage (same clip, overlapping time in it). Ones that no longer have a home are dropped and listed."""
    notes, dropped = [], 0
    for s in old.shots:                           # 万物生长 turned on / off by hand: the shot with that footage
        if not (s.grow and s.grow.get("source") == "manual"):
            continue
        best = max(range(len(new.shots)), key=lambda j: _overlap(s, new.shots[j]), default=None)
        if best is not None and _overlap(s, new.shots[best]) > 0:
            new = replace(new, shots=[replace(n, grow=dict(s.grow)) if j == best else n for j, n in enumerate(new.shots)])
    for s in old.shots:
        manual_drawn = bool(s.drawn and s.drawn.get("manual"))
        if not (s.sticker_at or manual_drawn):
            continue
        best, overlap = None, 0.0
        for j, n in enumerate(new.shots):
            if _norm(n.clip.path) != _norm(s.clip.path):
                continue
            ov = min(s.src_start + (s.t_end - s.t_start), n.src_start + (n.t_end - n.t_start)) - max(s.src_start, n.src_start)
            if ov > overlap:
                best, overlap = j, ov
        if best is None:
            dropped += 1
            continue
        n = new.shots[best]
        try:
            if s.sticker_at:
                new, _ = edit_shot(analysis, new, best, sticker={"id": s.sticker.partition("@")[0],
                                                                 **_shift_keys(s.sticker_at, s.src_start - n.src_start)})
            else:
                new, more = edit_shot(analysis, new, best, drawn={**s.drawn, "style": s.drawn.get("style")})
                notes += more
        except ValueError:
            dropped += 1
    if dropped:
        notes.append(f"{dropped} 处手动加的贴纸 / 手绘在新方案里找不到对应镜头，没有保留")
    return new, notes
