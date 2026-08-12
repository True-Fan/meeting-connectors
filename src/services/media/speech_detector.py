"""SpeechDetector — notices that somebody in the meeting started talking.

**A trigger, not a behaviour.** What happens next is not this module's business and
deliberately not new: ``MediaRouter`` runs the same handover a raised hand runs, so the
avatar stops mid-sentence and the agent says "ok, go ahead". This file only answers *when*.

**Deliberately not a voice activity detector**, in the same sense ``EchoGuard`` is not: the
bridge runs no AI and makes no speech decisions. This measures RMS energy over the inbound
mix and applies three rules, each of which exists because of a way the naive version fails:

* **A learned noise floor.** A fixed threshold is wrong in every room but the one it was
  tuned in, and being too high fails *intermittently* — the same person heard on one sentence
  and not the next, which is indistinguishable from the feature being broken. So the trigger
  is ``max(floor, noise * NOISE_MULTIPLE)``, learned from the meeting while nobody is talking.
* **A minimum duration.** A cough, a door and a dropped pen are as loud as speech but do not
  persist. This is the false-positive defence, and it is a duration rather than a louder
  threshold for exactly that reason.
* **Hysteresis.** Speech dips between syllables and on unvoiced consonants. With one
  threshold those dips read as silence and the detector flaps mid-sentence, handing the floor
  back inside somebody's question. Starting takes the full trigger; continuing takes a
  fraction of it.

**One caveat worth knowing before tuning anything.** A participant listening on *speakers*
has their own echo canceller, and while the avatar is talking that canceller suppresses their
voice too — measured at 20-100x quieter than the same person speaking into silence. No
threshold here can recover audio that never arrived. A headset removes the problem entirely.
"""

from __future__ import annotations

import math

from src.domain.media import AudioFrame

DEFAULT_RMS_THRESHOLD = 350
"""The floor under the trigger, in int16 RMS amplitude — not the trigger itself.

The trigger is ``max(this, noise_floor * NOISE_MULTIPLE)``, so this only decides how quiet a
room has to be before the learned floor stops mattering. It keeps a near-silent meeting from
setting a bar low enough that line noise takes the floor."""

NOISE_MULTIPLE = 3.0
"""How far above the room's own noise floor speech has to be. About ten decibels, which is
what ordinary speech is over room tone in any room."""

NOISE_ADAPT = 0.02
"""How fast the learned floor follows the room, per 20 ms frame while nobody holds the floor.

Slow — a couple of seconds to settle — because it must not chase speech. Updated only while
the detector is *not* holding the floor, so an utterance cannot raise the bar for its own
continuation."""

SUSTAIN_RATIO = 0.55
"""Fraction of the trigger that keeps an utterance alive once it has started."""

MIN_SPEECH_MS = 160
"""How long the energy must persist before the floor changes hands.

Eight 20 ms frames: long enough to reject transients, short enough that the avatar stops
inside the speaker's first word."""

RELEASE_MS = 400
"""How long the quiet must persist before the utterance is considered over. Longer than the
pauses inside a sentence, shorter than the pause after one."""

_ENERGY_STRIDE = 8
"""Sample every eighth sample, mirroring ``pacer._is_audible``. Speech is periodic over far
longer than eight samples at any conference rate, and this runs on every inbound frame."""


def rms(pcm: bytes, *, stride: int = _ENERGY_STRIDE) -> float:
    """Root-mean-square amplitude of ``pcm``, as native-endian int16.

    Separate from ``pacer._is_audible`` rather than shared with it, because the two answer
    different questions. That one asks "did we emit anything at all" and returns on the first
    sample above a floor. This needs a *level*: a room is never digitally silent, so "any
    energy" would fire on room tone.
    """
    usable = len(pcm) - len(pcm) % 2
    if not usable:
        return 0.0
    samples = memoryview(pcm)[:usable].cast("h")
    total = 0
    counted = 0
    for index in range(0, len(samples), stride):
        sample = samples[index]
        total += sample * sample
        counted += 1
    return math.sqrt(total / counted) if counted else 0.0


class SpeechDetector:
    """Reports the moment a participant starts speaking."""

    __slots__ = (
        "_last_rms",
        "_noise",
        "_quiet_us",
        "_speaking",
        "_speech_us",
        "_threshold",
    )

    def __init__(self, *, rms_threshold: int = DEFAULT_RMS_THRESHOLD) -> None:
        self._threshold = max(rms_threshold, 0)
        self._speech_us = 0
        self._quiet_us = 0
        self._speaking = False
        self._last_rms = 0.0
        # Starts at zero rather than at the threshold, so the first sentence of a session in
        # a quiet room triggers instead of waiting for the floor to fall.
        self._noise = 0.0

    @property
    def is_speaking(self) -> bool:
        """True while an utterance is in progress."""
        return self._speaking

    @property
    def last_rms(self) -> float:
        """Energy of the most recent frame, for the caller's log line."""
        return self._last_rms

    @property
    def noise_floor(self) -> float:
        """What this room sounds like when nobody is speaking, as learned so far."""
        return self._noise

    @property
    def trigger_level(self) -> float:
        """What a frame has to reach right now to take the floor."""
        return max(float(self._threshold), self._noise * NOISE_MULTIPLE)

    def observe(self, frame: AudioFrame) -> bool:
        """Feed one inbound frame. True **once**, on the frame speech begins.

        A rising edge rather than a level, because the caller acts on it: one handover per
        utterance, not fifty a second. ``is_speaking`` stays True for the rest of it.

        Timed by the frames themselves rather than by a clock — the audio carries its own
        duration, so a stalled ingest cannot age the detector into thinking somebody stopped.
        """
        duration_us = (frame.sample_count * 1_000_000) // frame.format.sample_rate_hz
        level = rms(frame.pcm)
        self._last_rms = level
        trigger = self.trigger_level
        floor = trigger * SUSTAIN_RATIO if self._speaking else trigger

        if level >= floor:
            self._quiet_us = 0
            self._speech_us += duration_us
            if not self._speaking and self._speech_us >= MIN_SPEECH_MS * 1_000:
                self._speaking = True
                return True
            return False

        if not self._speaking:
            self._noise += (level - self._noise) * NOISE_ADAPT

        self._quiet_us += duration_us
        if self._quiet_us >= RELEASE_MS * 1_000:
            # The turn is over. The speech accumulator is only reset here — inside the release
            # window it stands, so a sentence spoken with normal gaps still reaches
            # ``MIN_SPEECH_MS`` rather than restarting at every breath.
            self._speech_us = 0
            self._speaking = False
        return False
