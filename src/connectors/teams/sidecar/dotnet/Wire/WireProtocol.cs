// Teams sidecar IPC codec — wire version 1.
//
// The C# counterpart of src/connectors/teams/sidecar/protocol.py, and the two must
// agree byte for byte. The Python side owns the conformance vector
// (tests/unit/test_teams_sidecar_protocol.py); this file is the implementation that
// has to match it.
//
// Everything is big-endian ("network order"), which .NET does not do natively — hence
// the explicit byte shuffling rather than BitConverter, which is little-endian on
// every platform we run on and would silently produce a mirror-image header.
//
// Layout (24-byte header, then payload):
//
//     offset size field
//     0      4    magic        0x544D4331 'TMC1'
//     4      1    version      1
//     5      1    type         WireMessageType
//     6      1    flags        WireFlags
//     7      1    reserved     0
//     8      4    seq
//     12     8    ptsUs        int64, microseconds on the sender's clock
//     20     4    payloadLen

using System;
using System.IO;
using System.Text;

namespace MeetingConnectors.Teams.Sidecar.Wire
{
    public enum WireMessageType : byte
    {
        VideoI420 = 0x01,
        AudioPcm = 0x02,
        ControlJoin = 0x03,
        ControlLeave = 0x04,
        Heartbeat = 0x05,
        Ready = 0x06,
        Error = 0x07,
        Roster = 0x08,
        CallState = 0x09,
    }

    [Flags]
    public enum WireFlags : byte
    {
        None = 0x00,
        Keyframe = 0x01,
        Unmixed = 0x02,
        Silence = 0x04,
    }

    public enum WireCallState
    {
        Establishing = 1,
        Established = 2,
        Terminating = 3,
        Terminated = 4,
    }

    /// <summary>Thrown when the byte stream violates the wire contract.</summary>
    public sealed class WireProtocolException : Exception
    {
        public WireProtocolException(string message) : base(message) { }
    }

    public static class WireProtocol
    {
        public const uint Magic = 0x544D4331;
        public const byte Version = 1;
        public const int HeaderSize = 24;
        public const int AudioHeaderSize = 12;
        public const int VideoHeaderSize = 12;
        public const int MaxPayloadBytes = 8 * 1024 * 1024;

        /// <summary>source_msi sentinel for "mixed, or not attributable".</summary>
        public const uint MixedSource = 0;

        public const byte SampleFormatS16Le = 1;

        public static byte[] EncodeHeader(
            WireMessageType type, int payloadLength, uint seq, long ptsUs, WireFlags flags)
        {
            if (payloadLength < 0 || payloadLength > MaxPayloadBytes)
            {
                throw new WireProtocolException(
                    "payload of " + payloadLength + " bytes exceeds " + MaxPayloadBytes);
            }

            var header = new byte[HeaderSize];
            WriteUInt32(header, 0, Magic);
            header[4] = Version;
            header[5] = (byte)type;
            header[6] = (byte)flags;
            header[7] = 0; // reserved
            WriteUInt32(header, 8, seq);
            WriteInt64(header, 12, ptsUs);
            WriteUInt32(header, 20, (uint)payloadLength);
            return header;
        }

        public static byte[] EncodeJson(WireMessageType type, string json, uint seq = 0, long ptsUs = 0)
        {
            var payload = Encoding.UTF8.GetBytes(json);
            var header = EncodeHeader(type, payload.Length, seq, ptsUs, WireFlags.None);
            var frame = new byte[header.Length + payload.Length];
            Buffer.BlockCopy(header, 0, frame, 0, header.Length);
            Buffer.BlockCopy(payload, 0, frame, header.Length, payload.Length);
            return frame;
        }

        /// <summary>
        /// Encode one PCM frame for the bridge. <paramref name="offset"/>/<paramref name="count"/>
        /// let a caller hand us a slice of a larger receive buffer without copying it first —
        /// which matters, because this runs per 20 ms audio buffer per speaker.
        /// </summary>
        public static byte[] EncodeAudio(
            byte[] pcm,
            int offset,
            int count,
            int sampleRateHz,
            int channels,
            int frameMs,
            uint sourceMsi,
            long ptsUs,
            uint seq,
            WireFlags flags)
        {
            var payloadLength = AudioHeaderSize + count;
            var header = EncodeHeader(WireMessageType.AudioPcm, payloadLength, seq, ptsUs, flags);

            var frame = new byte[HeaderSize + payloadLength];
            Buffer.BlockCopy(header, 0, frame, 0, HeaderSize);

            var p = HeaderSize;
            WriteUInt32(frame, p, (uint)sampleRateHz);
            frame[p + 4] = (byte)channels;
            frame[p + 5] = SampleFormatS16Le;
            WriteUInt16(frame, p + 6, (ushort)Math.Min(frameMs, ushort.MaxValue));
            WriteUInt32(frame, p + 8, sourceMsi);

            Buffer.BlockCopy(pcm, offset, frame, HeaderSize + AudioHeaderSize, count);
            return frame;
        }

        /// <summary>Decoded prologue of a VIDEO_I420 payload.</summary>
        public struct VideoHeader
        {
            public int Width;
            public int Height;
            public int StrideY;
            public int StrideUv;
            public int Fps;
        }

        /// <summary>Decoded prologue of an AUDIO_PCM payload.</summary>
        public struct AudioHeader
        {
            public int SampleRateHz;
            public int Channels;
            public byte SampleFormat;
            public int FrameMs;
            public uint SourceMsi;
        }

        public static VideoHeader DecodeVideoHeader(byte[] payload)
        {
            if (payload.Length < VideoHeaderSize)
            {
                throw new WireProtocolException(
                    "video payload of " + payload.Length + " bytes is shorter than its header");
            }

            return new VideoHeader
            {
                Width = ReadUInt16(payload, 0),
                Height = ReadUInt16(payload, 2),
                StrideY = ReadUInt16(payload, 4),
                StrideUv = ReadUInt16(payload, 6),
                Fps = ReadUInt16(payload, 8),
            };
        }

        public static AudioHeader DecodeAudioHeader(byte[] payload)
        {
            if (payload.Length < AudioHeaderSize)
            {
                throw new WireProtocolException(
                    "audio payload of " + payload.Length + " bytes is shorter than its header");
            }

            return new AudioHeader
            {
                SampleRateHz = (int)ReadUInt32(payload, 0),
                Channels = payload[4],
                SampleFormat = payload[5],
                FrameMs = ReadUInt16(payload, 6),
                SourceMsi = ReadUInt32(payload, 8),
            };
        }

        // -- primitive big-endian access ------------------------------------

        public static void WriteUInt16(byte[] buffer, int offset, ushort value)
        {
            buffer[offset] = (byte)(value >> 8);
            buffer[offset + 1] = (byte)value;
        }

        public static void WriteUInt32(byte[] buffer, int offset, uint value)
        {
            buffer[offset] = (byte)(value >> 24);
            buffer[offset + 1] = (byte)(value >> 16);
            buffer[offset + 2] = (byte)(value >> 8);
            buffer[offset + 3] = (byte)value;
        }

        public static void WriteInt64(byte[] buffer, int offset, long value)
        {
            for (var i = 0; i < 8; i++)
            {
                buffer[offset + i] = (byte)(value >> (8 * (7 - i)));
            }
        }

        public static ushort ReadUInt16(byte[] buffer, int offset)
        {
            return (ushort)((buffer[offset] << 8) | buffer[offset + 1]);
        }

        public static uint ReadUInt32(byte[] buffer, int offset)
        {
            return ((uint)buffer[offset] << 24)
                 | ((uint)buffer[offset + 1] << 16)
                 | ((uint)buffer[offset + 2] << 8)
                 | buffer[offset + 3];
        }

        public static long ReadInt64(byte[] buffer, int offset)
        {
            long value = 0;
            for (var i = 0; i < 8; i++)
            {
                value = (value << 8) | buffer[offset + i];
            }
            return value;
        }
    }

    /// <summary>One decoded frame off the wire.</summary>
    public sealed class WireMessage
    {
        public WireMessageType Type { get; set; }
        public WireFlags Flags { get; set; }
        public uint Seq { get; set; }
        public long PtsUs { get; set; }
        public byte[] Payload { get; set; }

        public string Text()
        {
            return Payload == null || Payload.Length == 0
                ? "{}"
                : Encoding.UTF8.GetString(Payload);
        }
    }

    /// <summary>
    /// Incremental framing decoder. TCP gives no message boundaries, so bytes
    /// accumulate here until a whole message is available.
    ///
    /// It never attempts to resynchronise on a bad magic: a desynced binary stream
    /// cannot be realigned with confidence, and guessing would surface as corrupt
    /// audio in a live meeting rather than as an error. The link is torn down instead.
    /// </summary>
    public sealed class WireFrameDecoder
    {
        private readonly MemoryStream buffer = new MemoryStream();

        public void Reset()
        {
            buffer.SetLength(0);
            buffer.Position = 0;
        }

        public int Buffered
        {
            get { return (int)buffer.Length; }
        }

        public void Feed(byte[] data, int count)
        {
            buffer.Position = buffer.Length;
            buffer.Write(data, 0, count);
        }

        /// <summary>
        /// Pull the next complete message, or null when more bytes are needed.
        /// Call repeatedly after each Feed until it returns null.
        /// </summary>
        public WireMessage TryRead()
        {
            var available = (int)buffer.Length;
            if (available < WireProtocol.HeaderSize)
            {
                return null;
            }

            var raw = buffer.GetBuffer();

            var magic = WireProtocol.ReadUInt32(raw, 0);
            if (magic != WireProtocol.Magic)
            {
                throw new WireProtocolException(
                    "bad magic 0x" + magic.ToString("X8") + "; the stream is desynced or the " +
                    "peer is not a Teams bridge");
            }

            var version = raw[4];
            if (version != WireProtocol.Version)
            {
                throw new WireProtocolException("unsupported wire version " + version);
            }

            var payloadLength = (int)WireProtocol.ReadUInt32(raw, 20);
            if (payloadLength < 0 || payloadLength > WireProtocol.MaxPayloadBytes)
            {
                throw new WireProtocolException(
                    "declared payload of " + payloadLength + " bytes exceeds the ceiling");
            }

            var total = WireProtocol.HeaderSize + payloadLength;
            if (available < total)
            {
                return null;
            }

            var message = new WireMessage
            {
                Type = (WireMessageType)raw[5],
                Flags = (WireFlags)raw[6],
                Seq = WireProtocol.ReadUInt32(raw, 8),
                PtsUs = WireProtocol.ReadInt64(raw, 12),
                Payload = new byte[payloadLength],
            };
            Buffer.BlockCopy(raw, WireProtocol.HeaderSize, message.Payload, 0, payloadLength);

            // Compact: shift the remainder down rather than growing without bound.
            var remaining = available - total;
            if (remaining > 0)
            {
                Buffer.BlockCopy(raw, total, raw, 0, remaining);
            }
            buffer.SetLength(remaining);
            buffer.Position = remaining;

            if (!Enum.IsDefined(typeof(WireMessageType), message.Type))
            {
                throw new WireProtocolException(
                    "unknown message type 0x" + ((byte)message.Type).ToString("X2"));
            }

            return message;
        }
    }
}
