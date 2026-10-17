"""Output aspect ratios. A template names one in output.aspect; the CLI / API can override it per run."""

# 1080 on the short side: what the phone apps upload at
ASPECTS: dict[str, tuple[int, int]] = {
    "3:4": (1080, 1440),
    "9:16": (1080, 1920),
    "4:5": (1080, 1350),
    "1:1": (1080, 1080),
    "16:9": (1920, 1080),
}


def parse_aspect(aspect: str) -> tuple[int, int]:
    """'9:16' -> (9, 16)."""
    try:
        a, b = (int(v) for v in aspect.replace("/", ":").split(":"))
    except ValueError:
        raise ValueError(f"aspect must look like '9:16', got {aspect!r}") from None
    if a <= 0 or b <= 0:
        raise ValueError(f"aspect must be positive, got {aspect!r}")
    return a, b


def resolution_for(aspect: str) -> tuple[int, int]:
    """Output size for an aspect: the preset if there is one, else 1080 on the short side (even numbers)."""
    key = "{}:{}".format(*parse_aspect(aspect))
    if key in ASPECTS:
        return ASPECTS[key]
    a, b = parse_aspect(aspect)
    if a <= b:
        return 1080, int(round(1080 * b / a / 2)) * 2
    return int(round(1080 * a / b / 2)) * 2, 1080


def apply_aspect(tpl: dict, aspect: str | None) -> dict:
    """Set output.aspect / output.resolution on a template (in place). With aspect=None the template's own
    values stay; a template with an aspect but no resolution gets the preset size."""
    out = tpl.setdefault("output", {})
    if aspect is not None:
        a, b = parse_aspect(aspect)
        out["aspect"] = f"{a}:{b}"
        out["resolution"] = list(resolution_for(aspect))
    elif "resolution" not in out:
        out["resolution"] = list(resolution_for(out.get("aspect", "3:4")))
    out.setdefault("aspect", "{}:{}".format(*out["resolution"]))
    return tpl


def output_size(tpl: dict) -> tuple[int, int]:
    w, h = tpl["output"]["resolution"]
    return int(w), int(h)
