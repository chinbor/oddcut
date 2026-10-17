"""Generate synthetic test footage + beat music so the pipeline can run without real material.

Usage: python code/tools/make_test_assets.py [out_dir]   (default: assets/test)
Creates ~18 labeled clips (mixed portrait/landscape, one blurry, one shaky) and two drum loops.
"""
import subprocess
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

FONT = "C\\:/Windows/Fonts/arial.ttf"


def label(text: str) -> str:
    return (f"drawtext=fontfile='{FONT}':text='{text}':fontsize=h/14:fontcolor=white:"
            "box=1:boxcolor=black@0.5:boxborderw=20:x=(w-tw)/2:y=h*0.12")


def moving_shape(bg: str, fg: str, w: int, h: int, speed: float, name: str) -> str:
    s = min(w, h) // 3
    return (f"color=c={bg}:s={w}x{h}:r=30,noise=alls=30:allf=u[bg];"
            f"color=c={fg}:s={s}x{s}:r=30[fg];"
            f"[bg][fg]overlay=x='(W-w)/2+W*0.25*sin(t*{speed})':y='(H-h)/2+H*0.2*cos(t*{speed * 0.7})',"
            f"{label(name)}[out]")


def clips(w_p=1080, h_p=1920):
    yield "c01_anchor_sky", 10, (
        f"gradients=s={w_p}x{h_p}:c0=0x1b3a6b:c1=0xf0a35e:c2=0x6aa8d8:speed=0.004:r=30,noise=alls=12:allf=u,"
        f"{label('c01 anchor sky')}[out]")
    palette = [
        ("c02_red", "0x2a1010", "0xe8322b"), ("c03_orange", "0x2a1d10", "0xff8a1e"),
        ("c04_yellow", "0x26240f", "0xf2d43a"), ("c05_green", "0x10241a", "0x3fbf6a"),
        ("c06_cyan", "0x0f2326", "0x2fd0d8"), ("c07_blue", "0x101a2e", "0x3c6cf0"),
        ("c08_purple", "0x1e1029", "0x9b4fe0"), ("c09_pink", "0x2a1020", "0xf06aa8"),
        ("c10_red2", "0x301818", "0xc0202a"), ("c11_teal", "0x0d2420", "0x1fa58c"),
    ]
    for i, (name, bg, fg) in enumerate(palette):
        landscape = i % 3 == 2
        w, h = (1920, 1080) if landscape else (w_p, h_p)
        yield name, 3 + (i * 7) % 6, moving_shape(bg, fg, w, h, 0.8 + 0.2 * i, name.replace("_", " "))
    yield "c12_mandelbrot", 6, f"mandelbrot=s={w_p}x{h_p}:r=30,{label('c12 mandelbrot')}[out]"
    yield "c13_life", 5, (f"life=s=270x480:mold=10:r=30:ratio=0.1:death_color=0x203040:life_color=0xd8e060,"
                          f"scale={w_p}:{h_p}:flags=neighbor,{label('c13 life')}[out]")
    yield "c14_testsrc", 4, f"testsrc2=s=1440x1080:r=30,{label('c14 testsrc')}[out]"
    yield "c15_cellauto", 5, (f"cellauto=s=540x960:rule=30:r=30,scale={w_p}:{h_p}:flags=neighbor,"
                              f"colorchannelmixer=rr=0.9:gg=0.6:bb=0.3,{label('c15 cellauto')}[out]")
    yield "c16_gradient_warm", 7, (
        f"gradients=s={w_p}x{h_p}:c0=0xff7b39:c1=0x7a1f3d:speed=0.02:r=30,noise=alls=25:allf=u,"
        f"{label('c16 warm')}[out]")
    yield "c17_blurry", 6, (moving_shape("0x202020", "0xffffff", w_p, h_p, 1.0, "c17 blurry")
                            .replace("[out]", ",boxblur=40:6[out]"))
    yield "c18_shaky", 6, (moving_shape("0x203020", "0x80ff80", w_p, h_p, 1.0, "c18 shaky")
                           .replace("[out]", ",crop=iw*0.7:ih*0.7:"
                                    "x='(iw-ow)/2+iw*0.14*sin(t*37)':y='(ih-oh)/2+ih*0.14*cos(t*29)'[out]"))


def make_drums(path: Path, bpm: float, seconds: float, sr: int = 44100) -> None:
    rng = np.random.default_rng(0)
    n = int(seconds * sr)
    y = np.zeros(n)
    beat = 60.0 / bpm

    def add(sig, t):
        i = int(t * sr)
        j = min(n, i + len(sig))
        y[i:j] += sig[: j - i]

    tk = np.arange(int(0.35 * sr)) / sr
    kick = np.sin(2 * np.pi * (50 + 90 * np.exp(-tk * 25)) * tk) * np.exp(-tk * 9)
    ts = np.arange(int(0.2 * sr)) / sr
    snare = rng.normal(0, 1, len(ts)) * np.exp(-ts * 22) * 0.5
    th = np.arange(int(0.05 * sr)) / sr
    hat = np.diff(rng.normal(0, 1, len(th) + 1)) * np.exp(-th * 80) * 0.15

    t, k = 0.5, 0
    while t < seconds - 0.5:
        add(kick, t)
        if k % 2 == 1:
            add(snare, t)
        add(hat, t + beat / 2)
        t += beat
        k += 1
    tt = np.arange(n) / sr                     # soft chord pad so it is not just percussion
    pad = sum(np.sin(2 * np.pi * f * tt) for f in (220.0, 277.18, 329.63)) * 0.04
    y = y + pad
    y = 0.9 * y / np.max(np.abs(y))
    sf.write(str(path), y.astype(np.float32), sr)


def main() -> None:
    from oddcut.paths import TEST_DIR
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else TEST_DIR
    (out / "clips").mkdir(parents=True, exist_ok=True)
    (out / "music").mkdir(parents=True, exist_ok=True)
    for name, dur, graph in clips():
        dst = out / "clips" / f"{name}.mp4"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-filter_complex", graph, "-map", "[out]",
                        "-t", str(dur), "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                        "-pix_fmt", "yuv420p", str(dst)], check=True)
        print("clip ", dst)
    make_drums(out / "music" / "drums_120bpm.wav", 120, 30)
    make_drums(out / "music" / "drums_76bpm.wav", 76, 30)
    print("music", out / "music")


if __name__ == "__main__":
    main()
