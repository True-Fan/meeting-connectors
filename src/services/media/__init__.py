"""The media data plane.

Direct calls over bounded queues rather than an event bus: a fan-out bus has no
coherent backpressure semantics, and the per-stage drop policy this pipeline depends on
cannot be expressed as bus semantics (doc 003 §0.1). Events describe; queues carry.
"""
