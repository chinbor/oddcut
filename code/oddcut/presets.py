"""万物生长 and 手绘 as presets made of ordinary editable things (画中画 layers + 抠像 + keyframes): the automatic
pick of the cuts that suit growing in (auto_grow), adding / removing one (grow_preset, remove_grow_preset), and a
手绘 layer (drawn_layer; drawn_to_layers turns the planner's drawn shots into such layers).
"""
import json
import time
from dataclasses import replace
from pathlib import Path

import numpy as np

from .plan_data import Analysis, Plan, _norm
from .edits import MIN_SHOT, _clip_len, edit_shot, move_cut
from .overlays import OVERLAY_TRACKS, _check_overlays


def _downbeats(m) -> np.ndarray:
    """True at grid points that are a bar's first beat (4/4; the phase whose beats hit hardest, as the app's
    timeline shows them)."""
    on = np.asarray(m.on_beat, bool)
    full = np.nonzero(on)[0]
    out = np.zeros(len(on), bool)
    if not len(full):
        return out
    st = np.asarray(m.strength, float)
    phase = max(range(4), key=lambda q: st[full[q::4]].mean() if len(full[q::4]) else 0)
    out[full[phase::4]] = True
    return out


def auto_grow(analysis: Analysis, p: Plan, cache_file: Path | None = None, progress=None
              ) -> tuple[Plan, list[str], list[dict]]:
    """万物生长 only where it fits: every cut is scored (growth.score_shot: layers, a whole subject, a steady
    camera, contrast between the layers; plus the beat it falls on) and the best 1-3 (about one per 9 s) above
    the bar get it, never two cuts in a row. Choices made by hand (on or off) stay as they are. Returns the plan,
    notes and a report per shot (score, picked, why / why not)."""
    from .formats import output_size
    from .growth import AUTO_EVERY, AUTO_MAX, AUTO_THRESHOLD, score_shot
    from .render import effect_rev
    w, h = output_size(p.template)
    aspect = w / h
    cache = {}
    if cache_file and cache_file.exists():
        try:
            cache = json.loads(cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cache = {}
    # earlier automatic picks go (their cuts move back); hand-made ones stay
    for g in [g for g in p.looks.get("grow_presets") or [] if g.get("source") == "auto"]:
        p, _ = remove_grow_preset(analysis, p, g["id"])
    rev = effect_rev("grow")
    m = analysis.music
    grid, strength, down = m.grid, np.asarray(m.strength, float), _downbeats(m)
    report = []
    for i, s in enumerate(p.shots):
        key = f"{_norm(s.clip.path)}|{s.src_start:.2f}|{aspect:.4f}|{rev}"
        if key not in cache:
            cache[key] = score_shot(s.clip.path, s.src_start, aspect)
        if progress:
            progress(i + 1, len(p.shots))
        sc = cache[key]
        if i == 0:
            beat, beat_why = 1.0, "开场"
        else:
            g = int(np.argmin(np.abs(grid - s.t_start)))
            beat = float(0.6 * strength[g] + 0.4 * down[g])
            beat_why = "正好在重拍上" if down[g] else ("踩在强拍上" if strength[g] > 0.6 else "")
        total = 0.75 * sc["visual"] + 0.25 * beat
        report.append({"index": i, "score": round(total, 3), "visual": sc["visual"], "beat": round(beat, 3),
                       "parts": {k: sc.get(k) for k in ("layers", "whole", "steady", "contrast", "penalty")},
                       "why": "，".join(x for x in (sc.get("why"), beat_why) if x), "why_not": sc.get("why_not", ""),
                       "picked": False})
    if cache_file:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    manual = [g for g in p.looks.get("grow_presets") or []]
    near_manual = {i for i, s in enumerate(p.shots) for g in manual if i and abs(s.t_start - g["land"]) < 0.05}
    off = {i for i, s in enumerate(p.shots) if s.grow and s.grow.get("off")}
    k = min(AUTO_MAX, max(1, round(p.duration / AUTO_EVERY))) - len(manual)
    picked: list[int] = []
    for r in sorted(report, key=lambda r: -r["score"]):
        i = r["index"]
        if len(picked) >= k or r["score"] < AUTO_THRESHOLD:
            break
        if i == 0:
            r["why_not"] = (r["why_not"] + "，" if r["why_not"] else "") + "开场没有上一个镜头"
            continue
        if i in off or p.shots[i].drawn or any(abs(i - j) <= 1 for j in picked + sorted(near_manual)):
            continue
        try:
            p, _ = grow_preset(analysis, p, i, 3, "beat" if float(m.beat_period) >= 0.38 else "half", "auto", strict=True)
        except ValueError as e:
            r["why_not"] = (r["why_not"] + "，" if r["why_not"] else "") + str(e)
            continue
        picked.append(i)
        r["picked"] = True
    for g, i in zip([g for g in p.looks.get("grow_presets") or [] if g.get("source") == "auto"], picked):
        g["why"] = report[i]["why"][:80]
    notes = [f"自动给 {len(picked)} 个切点加了万物生长（都是画中画层 + 抠像 + 关键帧，可以直接改）" if picked
             else "没有特别适合万物生长的切点，这次没有自动加"]
    return p, notes, report


GROW_STEPS = ["复制下一个镜头的画面，叠在画中画轨道上", "每一层抠出一个完整的部分：前景物体 → 加上地面 → 加上中景，最后一层是整个画面",
              "每层加两个关键帧：从画面下方升到原位，前快后慢，带动感模糊", "各层按节拍错开（一拍或半拍），最后一层落定时切到下一个镜头"]


def _grow_subject(info: dict, rgb: np.ndarray | None = None) -> dict | None:
    """The one whole thing a 万物生长 can open with (a person, a car, an animal standing in the lower part of the
    picture, whole and in focus - not a passer-by smeared across the lens or cut by the side of the frame), as an
    抠像 point target - or None."""
    import cv2
    from .cutout import _clean, wholeness
    sharp = None
    if rgb is not None:
        sharp = np.abs(cv2.Laplacian(cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY), cv2.CV_32F))
    best = None
    for m in info["things"]:
        m = _clean(m)
        a = float(m.mean())
        if not 0.04 <= a <= 0.45:
            continue
        wh = wholeness(m)
        ys = np.nonzero(m.any(1))[0]
        if wh["pieces"] != 1 or wh["solidity"] < 0.6 or ys[-1] < 0.55 * m.shape[0]:
            continue
        xs = np.nonzero(m.any(0))[0]
        if xs[0] <= 1 or xs[-1] >= m.shape[1] - 2:        # cut by the side of the frame
            continue
        if sharp is not None and sharp[m].mean() < 0.6 * sharp.mean():
            continue                                       # out of focus / motion-blurred
        if best is None or a > best[0]:
            best = (a, m)
    if best is None:
        return None
    d = cv2.distanceTransform(best[1].astype(np.uint8), cv2.DIST_L2, 5)
    y, x = np.unravel_index(int(np.argmax(d)), d.shape)      # well inside it, so the click lands on it
    return {"kind": "object", "x": round(x / d.shape[1], 4), "y": round(y / d.shape[0], 4)}


def _grow_specs(info: dict, n: int, rgb: np.ndarray | None = None) -> list[tuple[dict, str]]:
    """What each layer of a 万物生长 shows (an 抠像 target and a name), in rising order, as in the reference
    videos: the one whole subject if there is one, then solid bands up from the bottom of the picture - the
    ground with the loose things on it, then up to the middle / far part (each band holds everything below its
    top edge) - and last the whole picture, sky and all."""
    from .cutout import PART_NAMES
    area = {k: float(v.mean()) for k, v in info["regions"].items()}
    bands = [p for p in ("ground", "mid", "far") if area.get(p, 0) >= 0.03]
    if area.get("sky", 0) < 0.03 and len(bands) > 1:
        bands = bands[:-1]                        # no sky: the whole picture is the last piece anyway
    steps: list[tuple[dict, str]] = []
    subj = _grow_subject(info, rgb)
    if subj is not None:
        steps.append((subj, "主体"))
    cum = ["things"]                              # small loose things go with the ground they stand on
    for p in bands:
        cum = cum + [p]
        steps.append(({"kind": "region", "parts": list(cum), "band": True}, ("加上" if steps else "") + PART_NAMES[p]))
    want = n - 1
    if len(steps) > want:                         # the front piece, then evenly up to the last
        idx = [0] + [round(j * (len(steps) - 1) / max(1, want - 1)) for j in range(1, want)] if want > 1 else [0]
        steps = [steps[i] for i in idx]
    return steps[:n - 1] + [([], "整个画面")]


def grow_preset(analysis: Analysis, p: Plan, index: int, layers: int = 3, stagger: str = "beat", source: str = "manual",
                strict: bool = False) -> tuple[Plan, list[str]]:
    """万物生长 on the cut into shot `index`, made only of ordinary editable things: copies of the shot on 画中画
    tracks, each with a 抠像 mask of one complete part (cumulative from the front), two position keyframes (from
    below the frame to its place, ease-out) with motion blur, started a beat or half-beat apart; the main-track
    cut moves to the moment the last layer lands, so the previous shot plays on underneath. strict: refuse
    when the cut pieces don't look whole (for the automatic pick)."""
    from .cutout import _clean, _frames, analyse, first_mask, wholeness
    from .formats import output_size
    if not 1 <= index < len(p.shots):
        raise ValueError("万物生长要从上一个镜头长出来，开场镜头不能用")
    s = p.shots[index]
    fps = float(p.template["output"]["fps"])
    w, h = output_size(p.template)
    frame0 = _frames(s.clip.path, s.src_start, 1, fps, w / h)[0]
    info = analyse(frame0)
    specs = _grow_specs(info, max(2, min(4, layers)), frame0)
    if len(specs) < 2:
        raise ValueError("这个镜头分不出层次，做不了万物生长")
    beat = float(p.music.beat_period)
    # as in the references: each layer rises for 0.3-0.5 s, the whole growth takes 2-3 beats (1-1.6 s)
    rise = min(0.5, max(0.3, 0.9 * beat))
    span = min(1.6, max(1.0, 2.5 * beat))
    step = max(0.2, min(beat, (span - rise) / max(1, len(specs) - 1)))
    if stagger != "beat":
        step = max(0.15, min(step, beat / 2))
    n = len(specs)
    t_cut = s.t_start
    room = (s.t_end - s.t_start) - MIN_SHOT - 0.05     # the shot must keep its minimum length after the moved cut
    notes = []
    want = (n - 1) * step + rise
    if want > room and index + 1 < len(p.shots):
        # one click should just work: make the shot longer - its end goes to the next beat with room (the
        # next shot gives up the time); only the timing changes, so it's undone like any cut move
        need = t_cut + want + MIN_SHOT + 0.05
        grid = analysis.music.grid
        later = grid[grid >= need - 1e-6]
        try:
            q, _ = move_cut(analysis, p, index + 1, float(later[0]) if len(later) else need, snap=False)
            if q.shots[index].t_end > s.t_end + 1e-3:
                notes.append(f"万物生长需要时间，这个镜头从 {s.t_end - s.t_start:.1f} 秒拉长到 {q.shots[index].t_end - t_cut:.1f} 秒")
                p, s = q, q.shots[index]
                room = (s.t_end - s.t_start) - MIN_SHOT - 0.05
        except ValueError:
            pass
    lead = 0.0
    want = (n - 1) * step + rise
    if want > room and not strict:
        # still short (the next shot can't give more): start rising over the end of the shot before - it plays
        # on underneath anyway; the layers then show this shot's footage from that much earlier
        prev = p.shots[index - 1]
        lead = max(0.0, min(want - room, prev.t_end - prev.t_start - MIN_SHOT - 0.05, s.src_start))
        if lead < 0.02:
            lead = 0.0
        t_cut -= lead
        room += lead
    if (n - 1) * step + rise > room:
        rise = max(0.3, min(rise, room - 0.15 * (n - 1)))
        step = max(0.15, (room - rise) / max(1, n - 1))
    land = (n - 1) * step + rise
    if land > room:
        raise ValueError("这个镜头太短，放不下万物生长")
    prev = p.shots[index - 1]
    clip_a = _clip_len(analysis, prev.clip.path)
    if clip_a is not None and not strict:
        # the shot before can't play on past the end of its clip: start that much earlier instead
        over = t_cut + land - (prev.t_start + clip_a - prev.src_start - 0.05)
        if over > 1e-3:
            more = max(0.0, min(over, prev.t_end - prev.t_start - MIN_SHOT - 0.05 - lead, s.src_start - lead))
            t_cut -= more
            lead += more
            room_a = prev.t_start + clip_a - prev.src_start - 0.05 - t_cut      # and if still not enough, faster
            if land > room_a >= 0.3 + 0.15 * (n - 1):
                rise = max(0.3, min(rise, room_a - 0.15 * (n - 1)))
                step = (room_a - rise) / max(1, n - 1)
                land = (n - 1) * step + rise
    for k, (target, label) in enumerate(specs[:-1]):
        wh = wholeness(_clean(first_mask(info, target, frame0.shape[:2])))
        if wh["area"] < 0.02 or wh["pieces"] > 4 or (k == 0 and wh["solidity"] < 0.35):
            if strict:
                raise ValueError(f"「{label}」抠出来不完整")
            notes.append(f"「{label}」这一层抠得不太完整，可以在画中画面板里改抠像")
    # tracks: the group needs n free lanes over [cut, land]; the first layer to rise goes on top
    others = [o for o in p.looks.get("overlays") or []]
    busy = lambda k: any(o["track"] == k and o["t_start"] < t_cut + land and t_cut < o["t_end"] for o in others)
    base = next((b for b in range(0, OVERLAY_TRACKS - n + 1) if not any(busy(b + j) for j in range(n))), None)
    if base is None:
        raise ValueError("这里的画中画轨道不够用了，先挪开一些画中画")
    gid = f"grow{int(time.time() * 1000) % 10**9}"
    new = []
    for k, (target, label) in enumerate(specs):
        t0 = t_cut + k * step
        o = {"id": f"{gid}_{k + 1}", "track": base + (n - 1 - k), "clip": s.clip.path, "src_start": s.src_start - lead + k * step,
             "t_start": t0, "t_end": t_cut + land + 1.0 / fps, "x": 0, "y": 0, "w": 1, "h": 1, "opacity": 1, "radius": 0,
             "mblur": 1.0, "keys": [{"t": 0, "x": 0.5, "y": 1.5, "s": 1, "r": 0, "e": "out"}, {"t": round(rise, 3), "x": 0.5, "y": 0.5, "s": 1, "r": 0}],
             "group": {"id": gid, "kind": "grow", "layer": k + 1, "of": n, "label": label}}
        if target:
            o["mask"] = {"shape": "cutout", "target": target, "feather": 0.004}
        new.append(o)
    q, more = move_cut(analysis, p, index, t_cut + land, snap=False)
    if abs(q.shots[index].t_start - (t_cut + land)) > 1.5 / fps:
        raise ValueError("上一个镜头的素材不够长，放不下万物生长")
    looks = dict(q.looks)
    looks["overlays"] = _check_overlays(analysis, q.duration, others + new)
    looks["grow_presets"] = [g for g in (looks.get("grow_presets") or [])] + [
        {"id": gid, "cut": round(t_cut + lead, 4), "land": round(t_cut + land, 4), "layers": n, "stagger": stagger, "source": source}]
    return replace(q, looks=looks), notes + more


def remove_grow_preset(analysis: Analysis, p: Plan, gid: str, remember: bool = False) -> tuple[Plan, list[str]]:
    """Take a 万物生长 away again: its layers go and the cut moves back to the beat it was on. remember: the
    user took it away - the automatic pick won't put one on that cut again."""
    g = next((x for x in p.looks.get("grow_presets") or [] if x["id"] == gid), None)
    if g is None:
        return p, []
    k = next((i for i, s in enumerate(p.shots) if i and abs(s.t_start - g["land"]) < 0.05), None)
    q = p
    if k is not None:
        q, _ = move_cut(analysis, p, k, g["cut"], snap=False)
    if remember and k is not None:
        q = replace(q, shots=[replace(s, grow={"off": True, "source": "manual"}) if i == k else s for i, s in enumerate(q.shots)])
    looks = dict(q.looks)
    looks["overlays"] = [o for o in looks.get("overlays") or [] if (o.get("group") or {}).get("id") != gid]
    looks["grow_presets"] = [x for x in looks.get("grow_presets") or [] if x["id"] != gid]
    return replace(q, looks=looks), []


def drawn_layer(analysis: Analysis, p: Plan, index: int, x: float | None = None, y: float | None = None,
                style: str = "lineart", reveal_at: float | None = None) -> tuple[Plan, list[str]]:
    """手绘 made of ordinary editable things: a 画中画 layer of shot `index`'s own footage with a 抠像 of one object
    (the one at x, y - or the biggest whole one), held from the first frame and a little wider, carrying the
    手绘 effect in `style` with its 绘制进度 keyframes: 0 blank -> 1 drawn (by ~1.1 s, waiting for the beat) ->
    2 the real picture again, ending on the strongest beat in the shot's reveal window (or `reveal_at`)."""
    from .cutout import _clean, _frames, analyse, first_mask
    from .formats import output_size
    if not 0 <= index < len(p.shots):
        raise IndexError(f"shot {index} out of range")
    s = p.shots[index]
    fps = float(p.template["output"]["fps"])
    w, h = output_size(p.template)
    frame0 = _frames(s.clip.path, s.src_start, 1, fps, w / h)[0]
    info = analyse(frame0)
    if x is None or y is None:
        whole = [m for m in info["things"] if 0.04 <= m.mean() <= 0.6]
        if not whole:
            raise ValueError("这个镜头里没找到能画的完整物体，可以点「点选物体」自己选一个")
        m = max(whole, key=lambda m: m.sum())
        ys, xs = np.nonzero(m)
        x, y = float(xs.mean() / m.shape[1]), float(ys.mean() / m.shape[0])
    target = {"kind": "object", "x": round(float(x), 4), "y": round(float(y), 4)}
    m = _clean(first_mask(info, target, frame0.shape[:2]))
    if m.mean() < 0.01:
        raise ValueError("点的地方没有能画的物体，换个位置点一下")
    notes: list[str] = []
    if reveal_at is None:
        # the timing of the drawn shots (edit_shot): reveal on the strongest beat of the window, the shot made
        # longer up to a strong beat when it's too short (the next shots give up the time)
        q, notes = edit_shot(analysis, p, index, drawn={"style": style})
        s = q.shots[index]
        reveal_at = s.t_start + float(s.drawn["reveal_at"])
        p = replace(q, shots=[replace(x, drawn=None, role="drawn" if j == index else x.role) if j == index else x
                              for j, x in enumerate(q.shots)])
        s = p.shots[index]
    t0 = s.t_start
    R = reveal_at - t0
    if R < 0.4 or R + 0.4 > s.t_end - t0 + 1e-6:
        raise ValueError("这个镜头太短，放不下手绘（画线、上色、变回实拍至少要 0.8 秒）")
    drawn_by = min(1.1, R)
    keys = [{"t": 0, "v": 0}, {"t": round(drawn_by, 3), "v": 1}]
    if R > drawn_by + 1e-3:
        keys.append({"t": round(R, 3), "v": 1})
    keys.append({"t": round(R + 0.4, 3), "v": 2})
    others = list(p.looks.get("overlays") or [])
    t1 = t0 + R + 0.4 + 2.0 / fps
    track = next((k for k in range(OVERLAY_TRACKS) if not any(o["track"] == k and o["t_start"] < t1 and t0 < o["t_end"] for o in others)), None)
    if track is None:
        raise ValueError("这里的画中画轨道不够用了")
    from .looks import DRAWN_STYLES
    name = next((d["name"] for d in DRAWN_STYLES if d["id"] == style), "手绘")
    gid = f"drawn{int(time.time() * 1000) % 10**9}"
    o = {"id": gid, "track": track, "clip": s.clip.path, "src_start": s.src_start, "t_start": t0, "t_end": min(t1, s.t_end),
         "x": 0, "y": 0, "w": 1, "h": 1, "opacity": 1, "radius": 0,
         "mask": {"shape": "cutout", "target": target, "hold": True, "dilate": 0.025, "feather": 0.004},
         "fx": {"kind": "drawn", "style": style, "keys": keys},
         "group": {"id": gid, "kind": "drawn", "layer": 1, "of": 1, "label": name}}
    looks = {**p.looks, "overlays": _check_overlays(analysis, p.duration, others + [o])}
    return replace(p, looks=looks), notes


def drawn_to_layers(analysis: Analysis, p: Plan) -> tuple[Plan, list[str]]:
    """The planner's 手绘开场 shots as editable 手绘 layers (the app's plans; the CLI keeps rendering them as before)."""
    notes = []
    for i, s in enumerate(p.shots):
        if not s.drawn:
            continue
        meta = s.drawn
        try:
            p, _ = drawn_layer(analysis, p, i, style=meta.get("style", "lineart"), reveal_at=s.t_start + float(meta["reveal_at"]))
        except ValueError as e:
            notes.append(f"镜头 {i + 1} 的手绘没做成：{e}")
        p = replace(p, shots=[replace(x, drawn=None) if j == i else x for j, x in enumerate(p.shots)])
    return p, notes
