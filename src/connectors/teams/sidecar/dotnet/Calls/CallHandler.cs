// CallHandler — one Graph call and its media session.
//
// This is the only place in the repository where Teams' media SDK appears, and it does
// exactly one thing: bind a Graph call's LocalMediaSession to the IPC link. Every
// decision about *what* to publish and *when* lives in the Python bridge; this side
// moves bytes between the wire and the media sockets.
//
// The three facts that shape the code:
//
//   1. One media session covers both directions. AudioSocket is Sendrecv and the
//      VideoSocket is Sendonly, but both hang off a single LocalMediaSession bound to a
//      single call. There is no way to lose one and keep the other, which is why the
//      bridge models Teams as a single link (doc 005 §2).
//
//   2. The app-hosted media blob can only be produced here. MediaSession.GetMediaConfiguration()
//      is what Graph needs in the AddAsync call, so the *sidecar* has to create the
//      call — the bridge cannot make the Graph request itself (doc 005 §3.1).
//
//   3. The media platform calls us on its own threads. Every handler below runs off the
//      event loop the bridge knows nothing about, so each one disposes its buffer and
//      swallows its own exceptions: an unhandled exception on a media callback thread
//      takes the process down and ejects the bot from the meeting.

using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using MeetingConnectors.Teams.Sidecar.Media;
using MeetingConnectors.Teams.Sidecar.Wire;
using Microsoft.Graph;
using Microsoft.Graph.Communications.Calls;
using Microsoft.Graph.Communications.Calls.Media;
using Microsoft.Graph.Communications.Client;
using Microsoft.Graph.Communications.Common.Telemetry;
using Microsoft.Graph.Communications.Resources;
using Microsoft.Skype.Bots.Media;

namespace MeetingConnectors.Teams.Sidecar.Calls
{
    /// <summary>Raised for conditions the bridge must not retry.</summary>
    public sealed class FatalCallException : Exception
    {
        public FatalCallException(string code, string message) : base(message)
        {
            Code = code;
        }

        public string Code { get; }
    }

    public sealed class CallHandler : IDisposable
    {
        private readonly ICommunicationsClient client;
        private readonly IGraphLogger logger;
        private readonly JoinRequest request;
        private readonly Action<byte[]> send;
        private readonly Action<ErrorPayload> reportError;

        private ICall call;
        private ILocalMediaSession mediaSession;
        private AudioSocket audioSocket;
        private VideoSocket videoSocket;

        private AudioFormat sendAudioFormat;
        private VideoFormat sendVideoFormat;

        private uint audioSeq;
        private uint videoSeq;
        private int disposed;

        /// <summary>Bot's own participant id, so roster entries can be marked isSelf.</summary>
        private string myParticipantId;

        public CallHandler(
            ICommunicationsClient client,
            IGraphLogger logger,
            JoinRequest request,
            Action<byte[]> send,
            Action<ErrorPayload> reportError)
        {
            this.client = client;
            this.logger = logger;
            this.request = request;
            this.send = send;
            this.reportError = reportError;
        }

        public string CallId
        {
            get { return call?.Id; }
        }

        public bool UnmixedAudioGranted { get; private set; }

        // -- join ----------------------------------------------------------

        public async Task<ReadyPayload> JoinAsync(CancellationToken cancellation)
        {
            sendAudioFormat = ResolveAudioFormat(request.Audio.SampleRateHz);
            sendVideoFormat = ResolveVideoFormat(
                request.Video.Width, request.Video.Height, request.Video.Fps);

            var wantUnmixed = request.Audio.Unmixed;

            var audioSettings = new AudioSocketSettings
            {
                StreamDirections = StreamDirection.Sendrecv,
                // Pcm16K is not an arbitrary choice: it is exactly the avatar agent's
                // fixed input format, so nothing in the pipeline resamples (doc 005 §4.2).
                SupportedAudioFormat = AudioFormat.Pcm16K,
                ReceiveUnmixedMeetingAudio = wantUnmixed,
            };

            var videoSettings = new VideoSocketSettings
            {
                StreamDirections = StreamDirection.Sendonly,
                ReceiveColorFormat = VideoColorFormat.NV12,
                SupportedSendVideoFormats = new List<VideoFormat> { sendVideoFormat },
                MaxConcurrentSendStreams = 1,
            };

            mediaSession = client.CreateMediaSession(
                audioSettings, videoSettings, mediaSessionId: request.CorrelationId);

            audioSocket = (AudioSocket)mediaSession.AudioSocket;
            videoSocket = (VideoSocket)mediaSession.VideoSockets.FirstOrDefault();

            audioSocket.AudioMediaReceived += OnAudioMediaReceived;
            audioSocket.AudioSendStatusChanged += OnAudioSendStatusChanged;
            if (videoSocket != null)
            {
                videoSocket.VideoSendStatusChanged += OnVideoSendStatusChanged;
            }

            var joinParameters = BuildJoinParameters();

            try
            {
                call = await client.Calls().AddAsync(joinParameters, cancellation).ConfigureAwait(false);
            }
            catch (ServiceException exc)
            {
                throw ClassifyGraphFailure(exc);
            }

            call.OnUpdated += OnCallUpdated;
            call.Participants.OnUpdated += OnParticipantsUpdated;

            myParticipantId = call.Resource?.MyParticipantId;
            UnmixedAudioGranted = wantUnmixed;

            logger.Info("call created: " + call.Id);

            return new ReadyPayload
            {
                CallId = call.Id,
                AudioSampleRateHz = request.Audio.SampleRateHz,
                AudioChannels = 1,
                UnmixedAudio = UnmixedAudioGranted,
                VideoWidth = request.Video.Width,
                VideoHeight = request.Video.Height,
                VideoFps = request.Video.Fps,
                SdkVersion = typeof(ICommunicationsClient).Assembly.GetName().Version?.ToString(),
            };
        }

        private JoinCallParameters BuildJoinParameters()
        {
            var join = request.Join;

            if (string.Equals(join.Mode, "meeting_id", StringComparison.OrdinalIgnoreCase))
            {
                // Join by the numeric "Meeting ID" printed in the invite. chatInfo is
                // null on this route — Graph resolves the conversation from the id.
                var meetingInfo = new JoinMeetingIdMeetingInfo
                {
                    JoinMeetingId = join.JoinMeetingId,
                    Passcode = string.IsNullOrEmpty(join.Passcode) ? null : join.Passcode,
                };

                return new JoinCallParameters(null, meetingInfo, mediaSession)
                {
                    TenantId = join.TenantId,
                };
            }

            if (join.ChatInfo == null || join.Organizer == null)
            {
                throw new FatalCallException(
                    "JOIN_DESCRIPTOR_INVALID",
                    "chat_info mode requires both chatInfo and organizer");
            }

            var chatInfo = new ChatInfo
            {
                ThreadId = join.ChatInfo.ThreadId,
                MessageId = join.ChatInfo.MessageId ?? "0",
                ReplyChainMessageId = join.ChatInfo.ReplyChainMessageId,
            };

            var organizerInfo = new OrganizerMeetingInfo
            {
                Organizer = new IdentitySet
                {
                    User = new Identity
                    {
                        Id = join.Organizer.Id,
                        AdditionalData = new Dictionary<string, object>
                        {
                            { "tenantId", join.Organizer.TenantId },
                        },
                    },
                },
            };

            return new JoinCallParameters(chatInfo, organizerInfo, mediaSession)
            {
                TenantId = join.TenantId,
            };
        }

        /// <summary>
        /// Translate a Graph failure into "retry" or "give up".
        ///
        /// The distinction is worth the code: a missing admin consent or a bad client
        /// secret will fail identically on all ten reconnect attempts, so reporting it as
        /// recoverable turns a five-second diagnosis into a two-minute one that ends in
        /// the same place.
        /// </summary>
        private static Exception ClassifyGraphFailure(ServiceException exc)
        {
            var code = exc.Error?.Code ?? "GRAPH_ERROR";
            var status = (int)exc.StatusCode;

            var fatal = status == 401 || status == 403
                || code.IndexOf("Authorization", StringComparison.OrdinalIgnoreCase) >= 0
                || code.IndexOf("Forbidden", StringComparison.OrdinalIgnoreCase) >= 0;

            if (fatal)
            {
                return new FatalCallException(
                    "GRAPH_" + status,
                    "Graph rejected the join (" + code + "): " + exc.Message +
                    ". Check the app registration has Calls.JoinGroupCall.All and " +
                    "Calls.AccessMedia.All with admin consent.");
            }

            return new InvalidOperationException(
                "Graph call creation failed (" + status + " " + code + "): " + exc.Message, exc);
        }

        // -- outbound media (bridge -> Teams) -------------------------------

        public void SendAudio(WireMessage message)
        {
            if (audioSocket == null)
            {
                return;
            }

            var header = WireProtocol.DecodeAudioHeader(message.Payload);
            var pcmOffset = WireProtocol.AudioHeaderSize;
            var pcmLength = message.Payload.Length - pcmOffset;
            if (pcmLength <= 0)
            {
                return;
            }

            if (header.SampleRateHz != request.Audio.SampleRateHz)
            {
                // Sending at the wrong rate produces audible pitch-shifted speech, which
                // reads as an avatar bug rather than a configuration one. Refuse instead.
                reportError(new ErrorPayload
                {
                    Code = "AUDIO_RATE_MISMATCH",
                    Message = "bridge sent " + header.SampleRateHz + " Hz, socket negotiated " +
                              request.Audio.SampleRateHz + " Hz",
                    Fatal = false,
                });
                return;
            }

            using (var buffer = new PcmSendBuffer(
                message.Payload, pcmOffset, pcmLength, sendAudioFormat, CurrentMediaTimestamp()))
            {
                try
                {
                    audioSocket.Send(buffer);
                }
                catch (Exception exc)
                {
                    logger.Error(exc, "audio send failed");
                }
            }
        }

        public void SendVideo(WireMessage message)
        {
            if (videoSocket == null)
            {
                return;
            }

            var header = WireProtocol.DecodeVideoHeader(message.Payload);
            var planeOffset = WireProtocol.VideoHeaderSize;
            var planeLength = message.Payload.Length - planeOffset;
            if (planeLength <= 0)
            {
                return;
            }

            if (header.Width != request.Video.Width || header.Height != request.Video.Height)
            {
                reportError(new ErrorPayload
                {
                    Code = "VIDEO_GEOMETRY_MISMATCH",
                    Message = "bridge sent " + header.Width + "x" + header.Height +
                              ", socket negotiated " + request.Video.Width + "x" +
                              request.Video.Height,
                    Fatal = false,
                });
                return;
            }

            var i420 = new byte[planeLength];
            Buffer.BlockCopy(message.Payload, planeOffset, i420, 0, planeLength);

            try
            {
                int nv12Length;
                var nv12 = PixelFormats.I420ToNv12(i420, header.Width, header.Height, out nv12Length);

                // Ownership of nv12 passes to the buffer, which frees it on Dispose.
                using (var buffer = new Nv12SendBuffer(
                    nv12, nv12Length, sendVideoFormat, CurrentMediaTimestamp()))
                {
                    videoSocket.Send(buffer);
                }
            }
            catch (Exception exc)
            {
                logger.Error(exc, "video send failed");
            }
        }

        // -- inbound media (Teams -> bridge) --------------------------------

        private void OnAudioMediaReceived(object sender, AudioMediaReceivedEventArgs args)
        {
            // The platform owns args.Buffer and reuses its memory: it MUST be disposed
            // here, and its data must be copied before this method returns.
            try
            {
                var buffer = args.Buffer;
                var unmixed = buffer.UnmixedAudioBuffers;

                if (unmixed != null && unmixed.Count > 0)
                {
                    foreach (var stream in unmixed)
                    {
                        ForwardPcm(
                            stream.Data, stream.Length, buffer.Timestamp,
                            (uint)stream.ActiveSpeakerId, WireFlags.Unmixed);
                    }
                }
                else if (buffer.Data != IntPtr.Zero && buffer.Length > 0)
                {
                    // Mixed stream. Legitimate, not a failure: the bridge's EchoGuard
                    // falls back to its speaking gate when attribution is unavailable.
                    ForwardPcm(
                        buffer.Data, buffer.Length, buffer.Timestamp,
                        WireProtocol.MixedSource, WireFlags.None);
                }
            }
            catch (Exception exc)
            {
                logger.Error(exc, "audio receive failed");
            }
            finally
            {
                args.Buffer?.Dispose();
            }
        }

        private void ForwardPcm(
            IntPtr data, long length, long timestamp, uint sourceMsi, WireFlags flags)
        {
            if (data == IntPtr.Zero || length <= 0)
            {
                return;
            }

            var pcm = new byte[length];
            System.Runtime.InteropServices.Marshal.Copy(data, pcm, 0, (int)length);

            var frameMs = (int)(length * 1000
                / (request.Audio.SampleRateHz * 2L)); // 2 bytes per mono S16 sample

            var frame = WireProtocol.EncodeAudio(
                pcm, 0, pcm.Length,
                request.Audio.SampleRateHz,
                1,
                frameMs,
                sourceMsi,
                TicksToMicroseconds(timestamp),
                unchecked(audioSeq++),
                flags);

            send(frame);
        }

        // -- call and roster events -----------------------------------------

        private void OnCallUpdated(ICall source, ResourceEventArgs<Call> args)
        {
            var state = args.NewResource?.State;
            logger.Info("call state: " + state);

            var mapped = MapCallState(state);
            if (mapped == null)
            {
                return;
            }

            var payload = new CallStatePayload
            {
                State = (int)mapped.Value,
                Reason = args.NewResource?.ResultInfo?.Message,
            };
            send(WireProtocol.EncodeJson(
                WireMessageType.CallState, JsonHelper.Serialize(payload)));

            if (mapped == WireCallState.Established)
            {
                myParticipantId = source.Resource?.MyParticipantId ?? myParticipantId;
            }
        }

        private static WireCallState? MapCallState(CallState? state)
        {
            switch (state)
            {
                case CallState.Establishing:
                    return WireCallState.Establishing;
                case CallState.Established:
                    return WireCallState.Established;
                case CallState.Terminating:
                    return WireCallState.Terminating;
                case CallState.Terminated:
                    return WireCallState.Terminated;
                default:
                    return null;
            }
        }

        private void OnParticipantsUpdated(
            IParticipantCollection sender, CollectionEventArgs<IParticipant> args)
        {
            try
            {
                var payload = new RosterPayload();

                foreach (var participant in sender)
                {
                    var resource = participant.Resource;
                    if (resource == null)
                    {
                        continue;
                    }

                    // A participant's audio MSI comes from its media stream entry. Only
                    // that id can be matched against an unmixed audio buffer, so a
                    // participant without one is not useful to the bridge.
                    var audioStream = resource.MediaStreams?.FirstOrDefault(
                        s => s.MediaType == Modality.Audio && !string.IsNullOrEmpty(s.SourceId));
                    if (audioStream == null)
                    {
                        continue;
                    }

                    uint msi;
                    if (!uint.TryParse(audioStream.SourceId, out msi))
                    {
                        continue;
                    }

                    var identity = resource.Info?.Identity?.User;
                    payload.Participants.Add(new RosterEntry
                    {
                        Msi = msi,
                        DisplayName = identity?.DisplayName,
                        AadObjectId = identity?.Id,
                        IsSelf = !string.IsNullOrEmpty(myParticipantId)
                                 && string.Equals(resource.Id, myParticipantId, StringComparison.Ordinal),
                    });
                }

                send(WireProtocol.EncodeJson(
                    WireMessageType.Roster, JsonHelper.Serialize(payload)));
            }
            catch (Exception exc)
            {
                logger.Error(exc, "roster update failed");
            }
        }

        private void OnAudioSendStatusChanged(object sender, AudioSendStatusChangedEventArgs args)
        {
            logger.Info("audio send status: " + args.MediaSendStatus);
        }

        private void OnVideoSendStatusChanged(object sender, VideoSendStatusChangedEventArgs args)
        {
            logger.Info("video send status: " + args.MediaSendStatus);
        }

        // -- teardown -------------------------------------------------------

        public async Task LeaveAsync()
        {
            var current = call;
            if (current == null)
            {
                return;
            }

            try
            {
                await current.DeleteAsync().ConfigureAwait(false);
            }
            catch (Exception exc)
            {
                // Best-effort: if the service already tore the call down there is nothing
                // to delete, and failing here would block the bridge's teardown.
                logger.Warn("leaving the call failed: " + exc.Message);
            }
        }

        public void Dispose()
        {
            if (Interlocked.Exchange(ref disposed, 1) != 0)
            {
                return;
            }

            if (audioSocket != null)
            {
                audioSocket.AudioMediaReceived -= OnAudioMediaReceived;
                audioSocket.AudioSendStatusChanged -= OnAudioSendStatusChanged;
            }
            if (videoSocket != null)
            {
                videoSocket.VideoSendStatusChanged -= OnVideoSendStatusChanged;
            }
            if (call != null)
            {
                call.OnUpdated -= OnCallUpdated;
                call.Participants.OnUpdated -= OnParticipantsUpdated;
            }

            mediaSession?.Dispose();
        }

        // -- helpers --------------------------------------------------------

        /// <summary>
        /// The media platform expects timestamps in DateTime ticks, which is what the
        /// official samples use. Note this is a *different* timebase from the bridge's
        /// media clock, and deliberately never mixed with it: the bridge stamps its own
        /// PTS on receipt (see ingest/mapping.py) precisely so two machines' clock offset
        /// cannot leak into A/V sync.
        /// </summary>
        private static long CurrentMediaTimestamp()
        {
            return DateTime.UtcNow.Ticks;
        }

        private static long TicksToMicroseconds(long ticks)
        {
            return ticks / 10; // 1 tick = 100 ns
        }

        private static AudioFormat ResolveAudioFormat(int sampleRateHz)
        {
            switch (sampleRateHz)
            {
                case 8000:
                    return AudioFormat.Pcm8K;
                case 16000:
                    return AudioFormat.Pcm16K;
                case 32000:
                    return AudioFormat.Pcm32K;
                case 44100:
                    return AudioFormat.Pcm44KStereo;
                case 48000:
                    return AudioFormat.Pcm48KStereo;
                default:
                    throw new FatalCallException(
                        "AUDIO_FORMAT_UNSUPPORTED",
                        sampleRateHz + " Hz is not an AudioFormat the media platform offers");
            }
        }

        private static VideoFormat ResolveVideoFormat(int width, int height, int fps)
        {
            var candidate = VideoFormat.NV12_1280x720_30Fps;

            if (width == 1920 && height == 1080 && fps == 30) candidate = VideoFormat.NV12_1920x1080_30Fps;
            else if (width == 1280 && height == 720 && fps == 30) candidate = VideoFormat.NV12_1280x720_30Fps;
            else if (width == 1280 && height == 720 && fps == 15) candidate = VideoFormat.NV12_1280x720_15Fps;
            else if (width == 640 && height == 360 && fps == 30) candidate = VideoFormat.NV12_640x360_30Fps;
            else if (width == 640 && height == 360 && fps == 15) candidate = VideoFormat.NV12_640x360_15Fps;
            else if (width == 320 && height == 180 && fps == 30) candidate = VideoFormat.NV12_320x180_30Fps;
            else if (width == 320 && height == 180 && fps == 15) candidate = VideoFormat.NV12_320x180_15Fps;
            else
            {
                throw new FatalCallException(
                    "VIDEO_FORMAT_UNSUPPORTED",
                    width + "x" + height + "@" + fps + " is not a send format the media " +
                    "platform offers. The bridge validates this in " +
                    "connectors/teams/config.py, so reaching here means the two lists drifted.");
            }

            return candidate;
        }
    }
}
