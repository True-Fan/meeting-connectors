// Sidecar configuration.
//
// Everything here is *infrastructure* — where to listen, which certificate to present,
// what public FQDN the media platform advertises. The Azure AD credentials and the
// meeting to join are deliberately NOT here: they arrive per-session in CONTROL_JOIN,
// so rotating a client secret is a bridge-side config change and this Windows host holds
// nothing durable worth stealing (doc 005 §5.2).

using System;
using System.Collections.Generic;
using System.Net;

namespace MeetingConnectors.Teams.Sidecar
{
    public sealed class SidecarOptions
    {
        /// <summary>Address the bridge connects to. Loopback by default: exposing a media
        /// bridge on 0.0.0.0 should be a deliberate act, not the default.</summary>
        public IPAddress IpcListenAddress { get; set; } = IPAddress.Loopback;

        public int IpcPort { get; set; } = 8445;

        /// <summary>Thumbprint of the certificate presented to the bridge on the IPC link.
        /// Null disables TLS, which is a local-development-only configuration.</summary>
        public string IpcCertificateThumbprint { get; set; }

        /// <summary>Require and validate a client certificate from the bridge (mutual TLS).</summary>
        public bool IpcRequireClientCertificate { get; set; }

        /// <summary>Accepted bridge client-certificate thumbprints. Empty means "any
        /// certificate that chains to a trusted root".</summary>
        public HashSet<string> IpcAllowedClientThumbprints { get; } =
            new HashSet<string>(StringComparer.OrdinalIgnoreCase);

        // -- media platform ------------------------------------------------

        /// <summary>Public DNS name Teams reaches this host on. Must match the media
        /// certificate's subject; the media platform refuses to initialise otherwise.</summary>
        public string ServiceFqdn { get; set; }

        /// <summary>Thumbprint of the certificate for the media platform and the Graph
        /// notification endpoint. Distinct from the IPC certificate: this one must be
        /// publicly trusted because Microsoft's service validates it.</summary>
        public string MediaCertificateThumbprint { get; set; }

        /// <summary>Port Teams sends media to, as reachable from the internet.</summary>
        public int MediaPublicPort { get; set; } = 8445;

        /// <summary>Port the media platform binds locally. Differs from the public port
        /// behind a load balancer or NAT, which is the normal Azure deployment.</summary>
        public int MediaInstanceInternalPort { get; set; } = 8446;

        /// <summary>HTTPS port Graph posts call notifications to.</summary>
        public int NotificationPort { get; set; } = 9441;

        public string NotificationPath { get; set; } = "/api/calls";

        public Uri NotificationUri
        {
            get
            {
                return new Uri("https://" + ServiceFqdn + ":" + MediaPublicPort + NotificationPath);
            }
        }

        public Uri NotificationListenUri
        {
            get { return new Uri("https://+:" + NotificationPort + "/"); }
        }

        /// <summary>Parse <c>--key value</c> arguments, falling back to MC_TEAMS_SIDECAR_*
        /// environment variables so a Windows service definition can use either.</summary>
        public static SidecarOptions Parse(string[] args)
        {
            var options = new SidecarOptions();
            var parsed = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);

            for (var i = 0; i < args.Length - 1; i++)
            {
                if (args[i].StartsWith("--", StringComparison.Ordinal))
                {
                    parsed[args[i].Substring(2)] = args[i + 1];
                    i++;
                }
            }

            options.ServiceFqdn = Pick(parsed, "service-fqdn", "MC_TEAMS_SIDECAR_SERVICE_FQDN");
            options.MediaCertificateThumbprint =
                Pick(parsed, "media-cert-thumbprint", "MC_TEAMS_SIDECAR_MEDIA_CERT_THUMBPRINT");
            options.IpcCertificateThumbprint =
                Pick(parsed, "ipc-cert-thumbprint", "MC_TEAMS_SIDECAR_IPC_CERT_THUMBPRINT");

            var listen = Pick(parsed, "ipc-listen", "MC_TEAMS_SIDECAR_IPC_LISTEN");
            if (!string.IsNullOrEmpty(listen))
            {
                options.IpcListenAddress = IPAddress.Parse(listen);
            }

            options.IpcPort = PickInt(parsed, "ipc-port", "MC_TEAMS_SIDECAR_IPC_PORT", options.IpcPort);
            options.MediaPublicPort = PickInt(
                parsed, "media-public-port", "MC_TEAMS_SIDECAR_MEDIA_PUBLIC_PORT",
                options.MediaPublicPort);
            options.MediaInstanceInternalPort = PickInt(
                parsed, "media-internal-port", "MC_TEAMS_SIDECAR_MEDIA_INTERNAL_PORT",
                options.MediaInstanceInternalPort);
            options.NotificationPort = PickInt(
                parsed, "notification-port", "MC_TEAMS_SIDECAR_NOTIFICATION_PORT",
                options.NotificationPort);

            options.IpcRequireClientCertificate = PickBool(
                parsed, "ipc-require-client-cert", "MC_TEAMS_SIDECAR_IPC_REQUIRE_CLIENT_CERT");

            var allowed = Pick(parsed, "ipc-client-thumbprints", "MC_TEAMS_SIDECAR_IPC_CLIENT_THUMBPRINTS");
            if (!string.IsNullOrEmpty(allowed))
            {
                foreach (var thumbprint in allowed.Split(','))
                {
                    var trimmed = thumbprint.Trim();
                    if (trimmed.Length > 0)
                    {
                        options.IpcAllowedClientThumbprints.Add(trimmed);
                    }
                }
            }

            return options;
        }

        /// <summary>Fail fast on a configuration that cannot possibly work.</summary>
        public void Validate()
        {
            if (string.IsNullOrEmpty(ServiceFqdn))
            {
                throw new ArgumentException(
                    "--service-fqdn is required: the media platform advertises it to Teams and " +
                    "refuses to initialise without one");
            }
            if (string.IsNullOrEmpty(MediaCertificateThumbprint))
            {
                throw new ArgumentException(
                    "--media-cert-thumbprint is required: Teams validates the certificate on " +
                    "both the media and notification endpoints");
            }
            if (MediaPublicPort == MediaInstanceInternalPort)
            {
                throw new ArgumentException(
                    "media public and internal ports must differ; the media platform binds the " +
                    "internal port and advertises the public one");
            }
        }

        private static string Pick(IDictionary<string, string> args, string key, string envVar)
        {
            string value;
            if (args.TryGetValue(key, out value) && !string.IsNullOrEmpty(value))
            {
                return value;
            }
            return Environment.GetEnvironmentVariable(envVar);
        }

        private static int PickInt(
            IDictionary<string, string> args, string key, string envVar, int fallback)
        {
            var raw = Pick(args, key, envVar);
            int parsed;
            return !string.IsNullOrEmpty(raw) && int.TryParse(raw, out parsed) ? parsed : fallback;
        }

        private static bool PickBool(IDictionary<string, string> args, string key, string envVar)
        {
            var raw = Pick(args, key, envVar);
            if (string.IsNullOrEmpty(raw))
            {
                return false;
            }
            return raw.Equals("1", StringComparison.Ordinal)
                || raw.Equals("true", StringComparison.OrdinalIgnoreCase)
                || raw.Equals("yes", StringComparison.OrdinalIgnoreCase);
        }
    }
}
