"""Turn template + music grid + analyzed clips into a frame-accurate timeline."""
from dataclasses import dataclass

import numpy as np

from .analyze import ClipAnalysis
from .music import Music
from .looks import sticker_families, sticker_id
from .sticker_stage import presence, rule_for

REUSE_PENALTY = 2.0   # a fresh clip should almost always beat reusing one


@dataclass
class Shot:
    role: str               # hook | drawn | body | breath | sticker | outro
    clip: ClipAnalysis
    src_start: float        # seconds into the source clip
    t_start: float          # seconds on the output timeline
    t_end: float
    frames: int
    sticker: str | None = None   # eyes | anger | question
    drawn: dict | None = None    # drawn-reveal opening: {"reveal_at": s, "tail": s, "label": ...}

    def to_dict(self) -> dict:
        return {"role": self.role, "clip": self.clip.name, "src_start": round(self.src_start, 3),
                "t_start": round(self.t_start, 3), "t_end": round(self.t_end, 3), "frames": self.frames,
                "sticker": self.sticker, "drawn": self.drawn}


def _section(tpl: dict, name: str) -> dict:
    return next(s for s in tpl["structure"] if s["section"] == name)


def _pick_cut(music: Music, t: float, lo: float, hi: float, target: float, rng) -> float | None:
    g = music.grid
    mask = (g >= t + lo - 1e-6) & (g <= t + hi + 1e-6)
    if not mask.any():
        return None
    cand, strength, on_beat = g[mask], music.strength[mask], music.on_beat[mask]
    score = (-np.abs(cand - (t + target)) / music.beat_period
             + 0.4 * strength + 0.1 * on_beat + rng.normal(0, 0.25, len(cand)))
    return float(cand[np.argmax(score)])


def plan_cuts(music: Music, tpl: dict, rng, max_stickers: int = 0, drawn: bool = False,
              drawn_mid: bool = False) -> list[tuple]:
    """Returns [(role, t_start, t_end)] with every boundary on the music grid. Drawn shots are
    ("drawn", t0, t1, reveal_at) with reveal_at relative to t0: the real footage shows on a strong beat.
    `drawn` makes the opening a drawn shot; `drawn_mid` turns the first breath slot after
    drawn_mid.after_seconds into one."""
    hook, body, outro = _section(tpl, "hook"), _section(tpl, "body"), _section(tpl, "outro")
    total = min(tpl["output"]["duration"]["target"], music.duration - 0.3)
    shot_cfg = body["shot_seconds"]
    breath_cfg = body["breathing_shot"]
    sticker_cfg = body.get("sticker_shot")

    segments = []
    opening = _plan_drawn_at(music, tpl["drawn_opening"], 0.0) if drawn else None
    if opening is not None:
        t, reveal_at = opening
        segments.append(("drawn", 0.0, t, reveal_at))
    else:
        lo, hi = hook["seconds"]
        t = _pick_cut(music, 0.0, lo, hi, (lo + hi) / 2, rng)
        if t is None:                             # no beat in range: nearest grid point, else plain time
            near = music.grid[(music.grid >= lo * 0.5) & (music.grid <= hi * 1.5)]
            t = float(near[np.argmin(np.abs(near - (lo + hi) / 2))]) if len(near) else hi
        segments.append(("hook", 0.0, t))

    outro_lo, outro_hi = outro["seconds"]
    mid_cfg = tpl.get("drawn_mid", {})
    mid_done = not drawn_mid
    since_breath, next_breath = 0, int(rng.integers(*breath_cfg["every_n_shots"], endpoint=True))
    # sticker shots: a little longer than the fast cuts so the pop-in can be read
    n_stickers, since_sticker = 0, 0
    next_sticker = int(rng.integers(*sticker_cfg["every_n_shots"], endpoint=True)) - 2 if sticker_cfg else 0
    while t < total - outro_lo - shot_cfg["min"]:
        if (sticker_cfg and n_stickers < max_stickers and since_sticker >= next_sticker
                and since_breath < next_breath):
            role, (lo, hi) = "sticker", sticker_cfg["seconds"]
            target = (lo + hi) / 2
            n_stickers, since_sticker = n_stickers + 1, 0
            next_sticker = int(rng.integers(*sticker_cfg["every_n_shots"], endpoint=True))
            since_breath += 1
        elif since_breath >= next_breath and not mid_done and t >= mid_cfg["after_seconds"]:
            mid = _plan_drawn_at(music, mid_cfg, t)
            if mid is not None and mid[0] <= total - outro_lo:
                segments.append(("drawn", t, mid[0], mid[1]))
                t, mid_done = mid[0], True
                since_breath, next_breath = 0, int(rng.integers(*breath_cfg["every_n_shots"], endpoint=True))
                continue
            mid_done = True
            role, (lo, hi) = "breath", breath_cfg["seconds"]
            target = (lo + hi) / 2
            since_breath, next_breath = 0, int(rng.integers(*breath_cfg["every_n_shots"], endpoint=True))
        elif since_breath >= next_breath:
            role, (lo, hi) = "breath", breath_cfg["seconds"]
            target = (lo + hi) / 2
            since_breath, next_breath = 0, int(rng.integers(*breath_cfg["every_n_shots"], endpoint=True))
        else:
            role, lo, hi = "body", shot_cfg["min"], shot_cfg["max"]
            target = float(np.clip(rng.lognormal(np.log(shot_cfg["median"]), 0.3), lo, hi))
            since_breath += 1
            since_sticker += 1
        nxt = _pick_cut(music, t, lo, hi, target, rng)
        if nxt is None:                           # sparse grid: take the next grid point
            later = music.grid[music.grid > t + 0.3]
            if not len(later):
                break
            nxt = float(later[0])
        segments.append((role, t, nxt))
        t = nxt

    end = _pick_cut(music, t, outro_lo, outro_hi, total - t, rng)
    if end is None or end > music.duration:
        end = min(t + (outro_lo + outro_hi) / 2, music.duration)
    segments.append(("outro", t, end))
    return segments


def _plan_drawn_at(music: Music, cfg: dict, t0: float) -> tuple[float, float] | None:
    """(end, reveal_at - t0) for a drawn shot starting at t0: reveal on the strongest beat in the
    reveal window (the drawing takes ~1.1s), then the dissolve plus some real playback before the cut."""
    lo, hi = t0 + cfg["reveal_window"][0], t0 + cfg["reveal_window"][1]
    m = (music.grid >= lo) & (music.grid <= hi)
    if not m.any():
        return None
    score = music.strength[m] + 0.3 * music.on_beat[m]
    reveal_at = float(music.grid[m][np.argmax(score)])
    t_lo = cfg["reveal_seconds"] + cfg["tail"][0]
    t_hi = cfg["reveal_seconds"] + cfg["tail"][1]
    em = (music.grid >= reveal_at + t_lo) & (music.grid <= reveal_at + t_hi)
    if not em.any():
        return None
    return float(music.grid[em][np.argmax(music.strength[em])]), reveal_at - t0


def _color_dist(a: np.ndarray, b: np.ndarray) -> float:
    return 0.5 * float(np.abs(a[:12] - b[:12]).sum()) + float(np.abs(a[12:] - b[12:]).sum())


def _choose_anchor(clips: list[ClipAnalysis], hook_len: float, outro_len: float) -> ClipAnalysis:
    def score(c: ClipAnalysis) -> float:
        fits = c.duration >= hook_len + outro_len + 2 * 0.25
        return (c.frame_score.mean() + 1.0 * c.stability(0, c.duration)
                + 0.1 * min(c.duration, 8.0) + (2.0 if fits else 0.0))
    return max(clips, key=score)


def _place_bookends(anchor: ClipAnalysis, hook_len: float, outro_len: float) -> tuple[float, float]:
    """Hook from the earlier part of the anchor clip, outro from a later, non-overlapping part."""
    best = None
    for s in anchor.candidate_starts(hook_len):
        anchor.used = [(s, s + hook_len)]
        for o in anchor.candidate_starts(outro_len):
            if o < s + hook_len:
                continue
            sc = anchor.window_score(s, hook_len) + anchor.window_score(o, outro_len)
            if best is None or sc > best[0]:
                best = (sc, s, o)
    anchor.used = []
    if best is None:                      # anchor too short for two windows: exact echo
        s = (anchor.best_window(hook_len) or anchor.best_window(min(hook_len, anchor.duration)) or (0.0, 0))[0]
        return s, s
    return best[1], best[2]


def _sticker_candidate(usable, recent, prev_feat, length, lam, usage, rng, last_family=None, stickered=(),
                       families=None):
    """Best (clip, start, family) whose window keeps a sticker-able object in view the whole time.
    Alternates families (eyes on objects / doodles on animals) so the stickers don't repeat.
    `families`: only these (the ones the selected stickers can sit on); None = any."""
    cands = []
    free = families is not None and "free" in families
    for c in usable:
        if any(c is r for r in recent) or (not c.tags and not free) or id(c) in stickered:   # no repeats
            continue
        for s in c.candidate_starts(length):
            hit = presence(c, s, length)
            if hit is not None and families is not None and hit[0] not in families:
                hit = None
            if hit is None and free:              # nothing to follow: a free-floating decoration
                hit = ("free", None)
            if hit is None:
                continue
            col, subj = c.feature_at(s)
            d = _color_dist(prev_feat[0], col) + 0.5 * float(np.linalg.norm(prev_feat[1] - subj))
            sc = (c.window_score(s, length) + 0.5 * c.stability(s, length)
                  - 0.5 * lam * d - REUSE_PENALTY * usage[id(c)]
                  - (2.0 if hit[0] == last_family else 0.0) - (1.0 if hit[0] == "free" else 0.0))
            cands.append((sc, c, s, hit[0]))
    if not cands:
        return None
    best = {}
    for sc, c, s, fam in cands:                   # best window per clip, then sample among top clips
        if id(c) not in best or sc > best[id(c)][0]:
            best[id(c)] = (sc, c, s, fam)
    top = sorted(best.values(), key=lambda x: -x[0])[:3]
    p = np.exp(np.array([x[0] for x in top]) / 0.3)
    _, c, s, fam = top[int(rng.choice(len(top), p=p / p.sum()))]
    return c, s, fam


def _choose_drawn(usable, seg, cfg: dict, prefer: str, exclude=(), outro_len: float | None = None):
    """Clip + start for a drawn shot: the preferred family first (animal / object), then bigger
    subjects; never a clip in `exclude`. With `outro_len`, also a later, non-overlapping window of the
    same clip for a loop outro (the drawn subject comes back for real at the end)."""
    _, t0, t1, reveal_at = seg
    tail = (t1 - t0) - reveal_at - cfg["reveal_seconds"]
    best = None
    for c in usable:
        if id(c) in exclude:
            continue
        for start, label, family, area, conf in c.drawable:
            o, o_score = None, 0.0
            if outro_len is not None:
                used_until = start + tail + 0.2
                outros = [o for o in c.candidate_starts(outro_len) if o >= used_until or o + outro_len <= start]
                if not outros:
                    continue
                o = max(outros, key=lambda o: c.window_score(o, outro_len))
                o_score = c.window_score(o, outro_len)
            # confidence matters: a glass dripper read as "cup" at ~0.5 draws badly, a mug at 0.9 well
            sc = (1.0 if family == prefer else 0.0) + 0.5 * area + 1.0 * conf + 0.3 * o_score
            if best is None or sc > best[0]:
                best = (sc, c, start, o, label)
    if best is None:
        return None
    _, c, start, o, label = best
    return c, start, o, {"reveal_at": round(reveal_at, 3), "tail": round(tail, 3), "label": label}


def _choose_outro(usable, outro_len: float, exclude=()):
    """A calm, clearly visible closing shot from a clip that isn't reserved for anything else."""
    best = None
    for c in usable:
        if id(c) in exclude:
            continue
        w = c.best_window(outro_len)
        if w is None:
            continue
        s, q = w
        sc = q + 1.0 * c.stability(s, outro_len) - 3.0 * max(0.0, 0.15 - c.brightness(s, outro_len)) / 0.15
        if best is None or sc > best[0]:
            best = (sc, c, s)
    return (best[1], best[2]) if best else None


def assign_clips(clips: list[ClipAnalysis], segments, tpl: dict, fps: float, rng, stickers=None) -> list[Shot]:
    """`stickers`: the selected sticker ids (None = the original eyes / 💢 / ? choice)."""
    usable = [c for c in clips if not c.rejected]
    if not usable:
        raise RuntimeError("all clips were rejected; nothing to edit")
    match_cut = _section(tpl, "body").get("ordering", {}).get("strategy") == "match_cut_chain"
    lam = 0.8 if match_cut else 0.0

    hook_len = segments[0][2] - segments[0][1]
    outro_len = segments[-1][2] - segments[-1][1]
    drawn = None
    reserved: set[int] = set()                    # clips kept for the drawn shots / outro
    open_cfg = tpl.get("drawn_opening", {})
    loop_outro = open_cfg.get("loop_outro", True)
    outro_clip = None
    if segments[0][0] == "drawn":
        drawn = _choose_drawn(usable, segments[0], open_cfg, open_cfg.get("prefer_family", "animal"),
                              outro_len=outro_len if loop_outro else None)
    if drawn is not None:
        anchor, hook_src, outro_src, drawn_meta = drawn
        hook_used = drawn_meta["tail"] + 0.2      # the drawing itself runs on a frozen start frame
        reserved.add(id(anchor))
        if not loop_outro:                         # end on something else, not the opening subject
            outro_clip = anchor
    else:
        if segments[0][0] == "drawn":
            segments[0] = ("hook", segments[0][1], segments[0][2])
        anchor = _choose_anchor(usable, hook_len, outro_len)
        hook_src, outro_src = _place_bookends(anchor, hook_len, outro_len)
        drawn_meta, hook_used = None, hook_len
    # mid drawn shots: picked up front so no other shot takes their clip
    mid_picks = {}
    mid_cfg = tpl.get("drawn_mid", {})
    for k, seg in enumerate(segments[1:-1], start=1):
        if seg[0] != "drawn":
            continue
        pick = _choose_drawn(usable, seg, mid_cfg, mid_cfg.get("prefer_family", "object"), reserved)
        if pick is None:
            segments[k] = ("breath", seg[1], seg[2])
            continue
        c, s, _, meta = pick
        c.used.append((s, s + meta["tail"] + 0.2))
        reserved.add(id(c))
        mid_picks[k] = (c, s, meta)
    if outro_clip is not None:
        pick = _choose_outro(usable, outro_len, reserved)
        if pick is not None:
            outro_clip, outro_src = pick
            outro_clip.used.append((outro_src, outro_src + outro_len))
            reserved.add(id(outro_clip))
        else:                                      # nothing else fits: fall back to the opening clip
            outro_clip = None
            if outro_src is None:
                outro_src = (anchor.best_window(outro_len) or (hook_src, 0))[0]
    anchor.used.append((hook_src, hook_src + hook_used))
    if outro_clip is None:
        outro_clip = anchor
        anchor.used.append((outro_src, outro_src + outro_len))

    def frames_between(a: float, b: float) -> int:
        return int(round(b * fps)) - int(round(a * fps))

    shots = [Shot(segments[0][0], anchor, hook_src, segments[0][1], segments[0][2],
                  frames_between(segments[0][1], segments[0][2]), drawn=drawn_meta)]
    prev_clip = anchor
    prev_feat = anchor.feature_at(hook_src + hook_used)
    usage = {id(c): 0 for c in usable}
    usage[id(anchor)] = 2
    for c in usable:
        if id(c) in reserved:
            usage[id(c)] = max(usage[id(c)], 1)
    placed, last_family = {"object": 0, "animal": 0, "free": 0}, None    # stickers so far, per family
    families = sticker_families(stickers) if stickers is not None else None

    for k, seg in enumerate(segments[1:-1], start=1):
        role, t0, t1 = seg[:3]
        length = t1 - t0
        if k in mid_picks:
            c, s, meta = mid_picks[k]
            shots.append(Shot("drawn", c, s, t0, t1, frames_between(t0, t1), drawn=meta))
            prev_clip, prev_feat = c, c.feature_at(s + meta["tail"])
            continue
        if role == "sticker":
            recent = [sh.clip for sh in shots[-3:]]
            pick = _sticker_candidate(usable, recent, prev_feat, length, lam, usage, rng, last_family,
                                      {id(sh.clip) for sh in shots if sh.sticker} | reserved, families)
            if pick is not None:
                c, s, family = pick
                rule = rule_for(family, placed[family], stickers,
                                {k: sum(1 for sh in shots if sticker_id(sh.sticker) == k) for k in stickers or ()})
                placed[family] += 1
                last_family = family
                c.used.append((s, s + length))
                usage[id(c)] += 1
                shots.append(Shot(role, c, s, t0, t1, frames_between(t0, t1), sticker=rule))
                prev_clip, prev_feat = c, c.feature_at(s + length)
                continue
            role = "body"                          # nothing sticker-able left: plain shot
        cands = []
        for allow_repeat in (False, True):
            for c in usable:
                if c is prev_clip and not allow_repeat:
                    continue
                if (c is anchor or id(c) in reserved) and len(usable) >= 4 and not allow_repeat:
                    continue                      # keep reserved clips for the drawn shots / bookends
                w = c.best_window(length)
                if w is None:
                    continue
                s, q = w
                col, subj = c.feature_at(s)
                d = _color_dist(prev_feat[0], col) + 0.5 * float(np.linalg.norm(prev_feat[1] - subj))
                sc = q - lam * d - REUSE_PENALTY * usage[id(c)]
                if role == "breath":                # long, calm shot: steady and clearly visible
                    sc += 1.0 * c.stability(s, length) - 3.0 * max(0.0, 0.15 - c.brightness(s, length)) / 0.15
                cands.append((sc, c, s))
            if cands:
                break
        if not cands:
            raise RuntimeError(f"not enough footage to fill a {length:.2f}s shot at {t0:.2f}s")
        cands.sort(key=lambda x: -x[0])
        top = cands[:3]
        p = np.exp(np.array([x[0] for x in top]) / 0.3)
        sc, c, s = top[int(rng.choice(len(top), p=p / p.sum()))]
        c.used.append((s, s + length))
        usage[id(c)] += 1
        shots.append(Shot(role, c, s, t0, t1, frames_between(t0, t1)))
        prev_clip, prev_feat = c, c.feature_at(s + length)

    t0, t1 = segments[-1][1], segments[-1][2]
    shots.append(Shot("outro", outro_clip, outro_src, t0, t1, frames_between(t0, t1)))
    return shots
