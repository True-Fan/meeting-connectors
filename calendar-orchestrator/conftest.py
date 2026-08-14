"""Puts the project root on ``sys.path`` so tests can ``import app`` without installing.

Its presence at the root (rather than inside ``tests/``) is what makes pytest prepend this
directory, matching how the service itself is run: ``uvicorn app.main:app`` from here.
"""
