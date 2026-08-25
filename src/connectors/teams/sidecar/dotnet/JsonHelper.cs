// One JSON configuration, used by every control message.
//
// Settings are pinned rather than left to defaults so the bytes are deterministic:
// nulls are omitted (the bridge switches on key presence for the optional passcode) and
// nothing is indented (whitespace is pure overhead on a per-frame link).

using Newtonsoft.Json;

namespace MeetingConnectors.Teams.Sidecar
{
    public static class JsonHelper
    {
        private static readonly JsonSerializerSettings Settings = new JsonSerializerSettings
        {
            NullValueHandling = NullValueHandling.Ignore,
            Formatting = Formatting.None,
        };

        public static string Serialize(object value)
        {
            return JsonConvert.SerializeObject(value, Settings);
        }

        public static T Deserialize<T>(string json)
        {
            return JsonConvert.DeserializeObject<T>(json, Settings);
        }
    }
}
