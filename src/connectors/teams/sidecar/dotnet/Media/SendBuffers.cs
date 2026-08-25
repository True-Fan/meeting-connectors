// Send buffers and the one pixel-format conversion Teams requires.
//
// The media platform consumes unmanaged memory: both AudioMediaBuffer and
// VideoMediaBuffer expose Data as an IntPtr that the platform reads from on its own
// thread. So every frame the bridge sends has to be copied out of the managed byte[]
// and into unmanaged memory, and freed once the platform signals it is done.
//
// That mandatory copy is exactly why I420 -> NV12 conversion belongs here rather than
// in Python: the interleave happens *during* a copy we were already making, so it costs
// a single pass over memory instead of an extra 1.4 MB allocation and shuffle inside the
// bridge's event loop (doc 005 §4.1).

using System;
using System.Runtime.InteropServices;
using Microsoft.Skype.Bots.Media;

namespace MeetingConnectors.Teams.Sidecar.Media
{
    /// <summary>One PCM buffer handed to the audio socket.</summary>
    public sealed class PcmSendBuffer : AudioMediaBuffer
    {
        public PcmSendBuffer(byte[] pcm, int offset, int count, AudioFormat format, long timestamp)
        {
            var native = Marshal.AllocHGlobal(count);
            Marshal.Copy(pcm, offset, native, count);

            Data = native;
            Length = count;
            AudioFormat = format;
            Timestamp = timestamp;
        }

        protected override void Dispose(bool disposing)
        {
            // The platform calls this once it has consumed the buffer. Freeing earlier
            // would hand it memory we had already released.
            if (Data != IntPtr.Zero)
            {
                Marshal.FreeHGlobal(Data);
                Data = IntPtr.Zero;
            }
        }
    }

    /// <summary>One NV12 frame handed to the video socket.</summary>
    public sealed class Nv12SendBuffer : VideoMediaBuffer
    {
        public Nv12SendBuffer(IntPtr nv12, int length, VideoFormat format, long timestamp)
        {
            Data = nv12;
            Length = length;
            VideoFormat = format;
            Timestamp = timestamp;
        }

        protected override void Dispose(bool disposing)
        {
            if (Data != IntPtr.Zero)
            {
                Marshal.FreeHGlobal(Data);
                Data = IntPtr.Zero;
            }
        }
    }

    public static class PixelFormats
    {
        /// <summary>
        /// Convert packed I420 to NV12 directly into freshly-allocated unmanaged memory.
        ///
        /// Both formats are 4:2:0 8-bit and both start with an identical full-resolution
        /// Y plane, so the Y plane is a straight block copy. They differ only in chroma
        /// layout: I420 stores U and V as two separate quarter-size planes, NV12 stores
        /// them interleaved as one half-height plane of UV pairs. So the conversion is a
        /// copy plus one interleave pass — no resampling, no colour-space maths, and no
        /// quality loss.
        ///
        /// Returns unmanaged memory that <see cref="Nv12SendBuffer"/> takes ownership of.
        /// </summary>
        public static IntPtr I420ToNv12(byte[] i420, int width, int height, out int length)
        {
            var lumaSize = width * height;
            var chromaWidth = width / 2;
            var chromaHeight = height / 2;
            var chromaSize = chromaWidth * chromaHeight;
            var expected = lumaSize + (2 * chromaSize);

            if (i420.Length < expected)
            {
                throw new ArgumentException(
                    "I420 frame is " + i420.Length + " bytes, expected " + expected +
                    " for " + width + "x" + height);
            }

            length = expected; // NV12 is the same total size as I420
            var native = Marshal.AllocHGlobal(length);

            try
            {
                // Y plane: identical in both formats.
                Marshal.Copy(i420, 0, native, lumaSize);

                // Chroma: interleave U and V into UVUVUV...
                //
                // Staged through a managed array because Marshal has no byte-at-a-time
                // write that is not a P/Invoke per call — one 8 KB..500 KB buffer plus
                // one bulk copy beats chromaSize individual transitions by a wide margin.
                var interleaved = new byte[2 * chromaSize];
                var uStart = lumaSize;
                var vStart = lumaSize + chromaSize;

                for (var i = 0; i < chromaSize; i++)
                {
                    interleaved[2 * i] = i420[uStart + i];
                    interleaved[(2 * i) + 1] = i420[vStart + i];
                }

                Marshal.Copy(interleaved, 0, native + lumaSize, interleaved.Length);
                return native;
            }
            catch
            {
                Marshal.FreeHGlobal(native);
                throw;
            }
        }
    }
}
