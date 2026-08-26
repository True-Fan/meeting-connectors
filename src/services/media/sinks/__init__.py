"""Platform-agnostic media sinks.

Deliberately **not** inside any connector (doc 002 §1.2 D5): these are
general-purpose destinations, and trapping them in the Zoom package would mean tests
and non-Zoom use had to import a connector to get a test double.
"""

from src.services.media.sinks.file_sink import FileSink
from src.services.media.sinks.null_sink import NullSink

__all__ = ["FileSink", "NullSink"]
