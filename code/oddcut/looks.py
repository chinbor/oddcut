"""What the user can pick in the app's 「贴纸选择」 and 「手绘风格选择」, kept free of the drawing code (cv2, PIL)
so the planner and the server can import it cheaply. The pictures come from stickers.sticker_preview and
drawn_reveal.style_preview.

A sticker's `families` say what it can sit on: the tags sticker_stage.tag_clips puts on the footage
("object": cups, bowls, cars ...; "animal": cats, dogs ...), or "free": any shot, as a decoration in
the emptiest part of the frame. A placed cartoon sticker carries where it went: "plane@animal".
"""

STICKERS = [
    dict(id="eyes", name="眼睛", group="persona", families=("object",)),
    dict(id="anger", name="生气", group="persona", families=("animal",)),
    dict(id="question", name="问号", group="persona", families=("animal",)),
    dict(id="car", name="小汽车", group="cartoon", families=("object", "animal", "free")),
    dict(id="plane", name="小飞机", group="cartoon", families=("object", "animal", "free")),
    dict(id="camera", name="相机", group="cartoon", families=("object", "animal", "free")),
    dict(id="sparkle", name="星星闪光", group="cartoon", families=("object", "animal", "free")),
    dict(id="heart", name="爱心", group="cartoon", families=("object", "animal", "free")),
    dict(id="note", name="音符", group="cartoon", families=("object", "animal", "free")),
]
STICKER_GROUPS = {"persona": "拟人贴纸", "cartoon": "卡通小物件"}
LEGACY_STICKERS = ("eyes", "anger", "question")   # what a plan uses when no selection is given

DRAWN_STYLES = [
    dict(id="lineart", name="线稿上色", description="细线勾出主体，平涂上色，再渐变成实拍"),
]
DEFAULT_DRAWN_STYLE = "lineart"

_BY_ID = {s["id"]: s for s in STICKERS}


def check_stickers(ids) -> list[str]:
    """Known sticker ids in catalog order; ValueError on an unknown one."""
    bad = [i for i in ids if i not in _BY_ID]
    if bad:
        raise ValueError(f"unknown sticker {bad[0]!r}")
    return [s["id"] for s in STICKERS if s["id"] in ids]


def check_drawn_style(style: str | None) -> str:
    style = style or DEFAULT_DRAWN_STYLE
    if style not in {s["id"] for s in DRAWN_STYLES}:
        raise ValueError(f"unknown drawn style {style!r}")
    return style


def sticker_families(ids) -> set[str]:
    return {f for i in ids for f in _BY_ID[i]["families"]}


def sticker_id(sticker: str | None) -> str | None:
    """"plane@animal" -> "plane"."""
    return sticker.partition("@")[0] if sticker else None


def family_of(sticker: str | None) -> str | None:
    """The family a placed sticker went on ("plane@free" -> "free"; "eyes" -> its only family)."""
    sid = sticker_id(sticker)
    if sid not in _BY_ID:
        return None
    return sticker.partition("@")[2] or _BY_ID[sid]["families"][0]


def rule_for(family: str, n_so_far: int, allowed=None, used=None) -> str | None:
    """Sticker for a shot whose footage shows `family`; `n_so_far` = stickers already placed on that family.
    allowed=None: the original behaviour (eyes on objects, 💢 / ? alternating on animals). Otherwise the
    least used of the selected stickers that fit (`used`: id -> times placed so far; ties go in catalog
    order), so every selected sticker gets its turn; None if none of them fits."""
    if allowed is None:
        if family == "object":
            return "eyes"
        return "anger" if n_so_far % 2 == 0 else "question"
    fits = [i for i in check_stickers(allowed) if family in _BY_ID[i]["families"]]
    if not fits:
        return None
    used = used or {}
    pick = min(fits, key=lambda i: used.get(i, 0)) if used else fits[n_so_far % len(fits)]
    return f"{pick}@{family}" if _BY_ID[pick]["group"] == "cartoon" else pick
