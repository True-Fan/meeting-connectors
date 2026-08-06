"""Detecting the failures that report as healthy.

``watchdog.py`` watches *media* liveness rather than process liveness. Everything a crashed
browser breaks is already visible; this covers the case where the browser is fine and the
audio has quietly stopped.
"""
