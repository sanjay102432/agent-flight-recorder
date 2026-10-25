"""agent-flight-recorder (AFR) -- record, replay, diff, and speed up agents.

Public API (everything else is an implementation detail)::

    from afr import Recorder, TraceStore, make_tools

    store = TraceStore("./trace_store")
    rec   = Recorder(store, make_tools(workspace))
    rec.tool_call(1, "fs.read", {"rel_path": "notes.txt"})

CLI lives at ``afr`` (after pip install) or ``python -m afr``.
"""

from .demo_agent import DemoAgent, MockProvider
from .prefetch import NGramPredictor, PrefetchEngine, SpeculationRefused
from .recorder import Recorder, ToolExecutionError
from .report import build_markdown_report, build_report, build_terminal_report
from .replay import ReplayReport, extract_plan, replay
from .schema import SCHEMA_VERSION, Event, SchemaError, read_events
from .store import TraceStore
from .tools import SPECULATIVE_CLASSES, ToolRegistry, make_tools

__version__ = "0.1.0"

__all__ = [
    "Recorder", "TraceStore", "ToolRegistry", "make_tools",
    "DemoAgent", "MockProvider",
    "NGramPredictor", "PrefetchEngine", "SpeculationRefused",
    "replay", "extract_plan", "ReplayReport",
    "build_report", "build_markdown_report", "build_terminal_report",
    "Event", "SchemaError", "read_events", "SCHEMA_VERSION",
    "SPECULATIVE_CLASSES", "ToolExecutionError",
    "__version__",
]
