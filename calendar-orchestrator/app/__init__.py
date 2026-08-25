"""Calendar orchestrator: watches a Google Calendar and triggers the meeting bot.

Deliberately a standalone service rather than code inside ``src/``: it never touches a
meeting itself, it only decides *when* to ask the existing bridge (``meeting-connectors``,
``POST /sessions``) to join one. That keeps the bridge platform-blind and this service
calendar-blind — neither needs to know how the other works internally.
"""
