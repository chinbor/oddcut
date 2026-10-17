"""Progress reporting for the analyze / plan / render steps.

Every step takes `progress`, a callable that receives a ProgressEvent. The desktop app passes one that
forwards events to the UI; the CLI prints them (or emits JSON lines with --progress-json). A callback
may raise to stop the run (e.g. the user pressed cancel); the exception propagates out of the step.
"""
import json
import sys
import threading
from dataclasses import asdict, dataclass
from typing import Callable


@dataclass
class ProgressEvent:
    step: str        # analyze | plan | render
    stage: str       # clips | music | stickers | drawn | cuts | effects | shots | mux | done
    done: int        # items finished in this stage
    total: int       # items in this stage (1 for single-shot stages)
    message: str = ""

    @property
    def fraction(self) -> float:
        return self.done / self.total if self.total else 1.0


Progress = Callable[[ProgressEvent], None]


def silent(_: ProgressEvent) -> None:
    pass


class Reporter:
    """Thread-safe wrapper the steps use: report(step, stage, done, total, message)."""

    def __init__(self, callback: Progress | None):
        self._cb = callback or silent
        self._lock = threading.Lock()

    def __call__(self, step: str, stage: str, done: int, total: int, message: str = "") -> None:
        with self._lock:
            self._cb(ProgressEvent(step, stage, done, total, message))


def console(event: ProgressEvent) -> None:
    """Human-readable: only stage starts / ends and messages, so the log stays short."""
    if event.message:
        print(f"      [{event.step}/{event.stage} {event.done}/{event.total}] {event.message}", flush=True)


def json_lines(event: ProgressEvent) -> None:
    """One JSON object per line on stdout, for a parent process to parse."""
    sys.stdout.write(json.dumps({"type": "progress", **asdict(event), "fraction": round(event.fraction, 4)},
                                ensure_ascii=False) + "\n")
    sys.stdout.flush()
