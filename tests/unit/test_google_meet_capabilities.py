"""The official-API findings that justify running a browser.

**This file exists to be a speed bump.** The whole connector's shape follows from one fact:
Google publishes no way to send media into a Meet conference. Anyone who concludes otherwise
and "simplifies" the connector into a sidecar has to delete assertions that quote the Google
sentence contradicting them — which is the right amount of friction for a decision that took
a documentation review to establish.
"""

from __future__ import annotations

from src.connectors.google_meet.capabilities import (
    MEET_CAPABILITIES,
    OFFICIAL_PUBLISH_CAPABILITIES,
    PUBLISH_AUDIO,
    PUBLISH_VIDEO,
    VERBATIM_NO_SEND_MEDIA,
    Capability,
    SupportLevel,
    describe,
    official_media_egress_available,
)


class TestPublishingIsUnsupported:
    """The premise. If any of these fail, the architecture should be revisited."""

    def test_publishing_video_is_unsupported(self) -> None:
        assert PUBLISH_VIDEO.level is SupportLevel.UNSUPPORTED
        assert PUBLISH_VIDEO.blocks_avatar

    def test_publishing_audio_is_unsupported(self) -> None:
        assert PUBLISH_AUDIO.level is SupportLevel.UNSUPPORTED
        assert PUBLISH_AUDIO.blocks_avatar

    def test_the_verbatim_google_sentence_is_recorded(self) -> None:
        """Quoted, not paraphrased, so the claim is checkable against the source."""
        assert "receive-only" in VERBATIM_NO_SEND_MEDIA
        assert "does not support sending of media" in VERBATIM_NO_SEND_MEDIA

    def test_both_publish_capabilities_cite_the_same_reference(self) -> None:
        references = {c.reference for c in OFFICIAL_PUBLISH_CAPABILITIES}
        assert len(references) == 1
        assert "developers.google.com" in references.pop()

    def test_no_official_media_egress_exists(self) -> None:
        """The condition under which the Chromium bridge could be replaced by a sidecar."""
        assert official_media_egress_available() is False


class TestPreviewCapabilitiesAreNotProductionUsable:
    def test_developer_preview_is_not_usable_in_production(self) -> None:
        """Every participant must be enrolled — an external candidate breaks the session."""
        assert not SupportLevel.DEVELOPER_PREVIEW.is_usable_in_production

    def test_generally_available_is_usable(self) -> None:
        assert SupportLevel.GENERALLY_AVAILABLE.is_usable_in_production

    def test_preview_capabilities_block_the_avatar(self) -> None:
        preview = [c for c in MEET_CAPABILITIES if c.level is SupportLevel.DEVELOPER_PREVIEW]
        assert preview, "the Media API entries should still be recorded"
        assert all(c.blocks_avatar for c in preview)


class TestRecordKeeping:
    def test_every_capability_is_checkable(self) -> None:
        """A claim with no URL is folklore, which is what this module exists to avoid."""
        for capability in MEET_CAPABILITIES:
            assert isinstance(capability, Capability)
            assert capability.reference.startswith("https://")
            assert capability.note, f"{capability.name} has no consequence recorded"

    def test_notes_state_the_consequence_not_just_the_fact(self) -> None:
        """A note that only restates the level teaches a future reader nothing."""
        for capability in MEET_CAPABILITIES:
            assert len(capability.note) > 80, capability.name

    def test_describe_lists_every_capability(self) -> None:
        rendered = describe()
        for capability in MEET_CAPABILITIES:
            assert capability.name in rendered
