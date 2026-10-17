"""oddcut: template-driven auto-editing (beat-synced cuts, loop bookends, film grade, any output aspect)."""
__version__ = "0.4.1"

import os as _os

# the vision models run fully offline: no update checks or usage events from the YOLO library
_os.environ.setdefault("YOLO_OFFLINE", "1")