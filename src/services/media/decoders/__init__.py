"""Media decoders. ``FfmpegDecoder`` ships; PyAV is documented as the optimisation path."""

from src.services.media.decoders.ffmpeg import FfmpegDecoder, FfmpegDecoderError

__all__ = ["FfmpegDecoder", "FfmpegDecoderError"]
