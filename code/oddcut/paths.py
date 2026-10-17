"""Project layout, in one place. The package lives in <root>/code/oddcut; override the root with ODDCUT_ROOT.

    <root>/code/templates   style templates (JSON)
    <root>/assets/public    downloaded footage + music      <root>/assets/test     synthetic test footage
    <root>/assets/models    YOLO weights, text-embedding caches
    <root>/output/edits     finished edits                  <root>/output/<demo>   single-shot effect demos
"""
import os
from pathlib import Path

PROJECT_ROOT = Path(os.environ.get("ODDCUT_ROOT") or Path(__file__).resolve().parents[2])
CODE_DIR = PROJECT_ROOT / "code"
TEMPLATE_DIR = CODE_DIR / "templates"
DEFAULT_TEMPLATE = TEMPLATE_DIR / "template_odd_daily.json"
ASSETS_DIR = PROJECT_ROOT / "assets"
PUBLIC_DIR = ASSETS_DIR / "public"
TEST_DIR = ASSETS_DIR / "test"
MODEL_DIR = ASSETS_DIR / "models"
OUTPUT_DIR = PROJECT_ROOT / "output"
EDITS_DIR = OUTPUT_DIR / "edits"
DOCS_DIR = PROJECT_ROOT / "docs"
