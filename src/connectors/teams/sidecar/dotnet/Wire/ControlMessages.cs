// Control-message payloads.
//
// These are the C# mirror of src/connectors/teams/graph/models.py. Property names are
// camelCase on the wire because that is what Graph itself uses, so the join descriptor
// passes through with no renaming step.
//
// Newtonsoft rather than System.Text.Json: this targets .NET Framework 4.7.2, where
// System.Text.Json is an add-on package and the Graph Communications SDK already brings
// Newtonsoft along. One JSON stack, not two.

using System.Collections.Generic;
using Newtonsoft.Json;

namespace MeetingConnectors.Teams.Sidecar.Wire
{
    public sealed class JoinRequest
    {
        [JsonProperty("sessionId")]
        public string SessionId { get; set; }

        [JsonProperty("correlationId")]
        public string CorrelationId { get; set; }

        [JsonProperty("join")]
        public JoinDescriptor Join { get; set; }

        [JsonProperty("auth")]
        public AuthCredentials Auth { get; set; }

        [JsonProperty("audio")]
        public AudioRequest Audio { get; set; }

        [JsonProperty("video")]
        public VideoRequest Video { get; set; }
    }

    public sealed class JoinDescriptor
    {
        /// <summary>"meeting_id" or "chat_info" — which Graph join route to take.</summary>
        [JsonProperty("mode")]
        public string Mode { get; set; }

        [JsonProperty("tenantId")]
        public string TenantId { get; set; }

        [JsonProperty("displayName")]
        public string DisplayName { get; set; }

        [JsonProperty("joinMeetingId")]
        public string JoinMeetingId { get; set; }

        [JsonProperty("passcode")]
        public string Passcode { get; set; }

        [JsonProperty("chatInfo")]
        public ChatInfoPayload ChatInfo { get; set; }

        [JsonProperty("organizer")]
        public OrganizerPayload Organizer { get; set; }
    }

    public sealed class ChatInfoPayload
    {
        [JsonProperty("threadId")]
        public string ThreadId { get; set; }

        [JsonProperty("messageId")]
        public string MessageId { get; set; }

        [JsonProperty("replyChainMessageId")]
        public string ReplyChainMessageId { get; set; }
    }

    public sealed class OrganizerPayload
    {
        [JsonProperty("id")]
        public string Id { get; set; }

        [JsonProperty("tenantId")]
        public string TenantId { get; set; }
    }

    public sealed class AuthCredentials
    {
        [JsonProperty("tenantId")]
        public string TenantId { get; set; }

        [JsonProperty("clientId")]
        public string ClientId { get; set; }

        [JsonProperty("clientSecret")]
        public string ClientSecret { get; set; }
    }

    public sealed class AudioRequest
    {
        [JsonProperty("sampleRateHz")]
        public int SampleRateHz { get; set; } = 16000;

        [JsonProperty("channels")]
        public int Channels { get; set; } = 1;

        [JsonProperty("unmixed")]
        public bool Unmixed { get; set; } = true;
    }

    public sealed class VideoRequest
    {
        [JsonProperty("width")]
        public int Width { get; set; } = 1280;

        [JsonProperty("height")]
        public int Height { get; set; } = 720;

        [JsonProperty("fps")]
        public int Fps { get; set; } = 30;
    }

    public sealed class ReadyPayload
    {
        [JsonProperty("callId")]
        public string CallId { get; set; }

        [JsonProperty("wireVersion")]
        public int WireVersion { get; set; } = WireProtocol.Version;

        [JsonProperty("audioSampleRateHz")]
        public int AudioSampleRateHz { get; set; }

        [JsonProperty("audioChannels")]
        public int AudioChannels { get; set; } = 1;

        [JsonProperty("unmixedAudio")]
        public bool UnmixedAudio { get; set; }

        [JsonProperty("videoWidth")]
        public int VideoWidth { get; set; }

        [JsonProperty("videoHeight")]
        public int VideoHeight { get; set; }

        [JsonProperty("videoFps")]
        public int VideoFps { get; set; }

        [JsonProperty("selfMsi")]
        public uint? SelfMsi { get; set; }

        [JsonProperty("sdkVersion")]
        public string SdkVersion { get; set; }
    }

    public sealed class ErrorPayload
    {
        [JsonProperty("code")]
        public string Code { get; set; }

        [JsonProperty("message")]
        public string Message { get; set; }

        /// <summary>
        /// True when retrying cannot help — a rejected credential, a missing
        /// Calls.AccessMedia.All consent, an uninitialisable media platform. The bridge
        /// fails the session immediately on these instead of spending its reconnect
        /// budget on an error that will recur identically.
        /// </summary>
        [JsonProperty("fatal")]
        public bool Fatal { get; set; }
    }

    public sealed class RosterPayload
    {
        [JsonProperty("participants")]
        public List<RosterEntry> Participants { get; set; } = new List<RosterEntry>();
    }

    public sealed class RosterEntry
    {
        /// <summary>Media Source Id — what unmixed audio buffers are tagged with, and
        /// therefore the only participant identifier the bridge can match a frame to.</summary>
        [JsonProperty("msi")]
        public uint Msi { get; set; }

        [JsonProperty("displayName")]
        public string DisplayName { get; set; }

        [JsonProperty("aadObjectId")]
        public string AadObjectId { get; set; }

        [JsonProperty("isSelf")]
        public bool IsSelf { get; set; }
    }

    public sealed class CallStatePayload
    {
        [JsonProperty("state")]
        public int State { get; set; }

        [JsonProperty("reason")]
        public string Reason { get; set; }
    }

    public sealed class HeartbeatPayload
    {
        [JsonProperty("sent_at_us")]
        public long SentAtUs { get; set; }
    }

    public sealed class LeavePayload
    {
        [JsonProperty("reason")]
        public string Reason { get; set; }
    }
}
