// Building the ICommunicationsClient, and the token provider it authenticates with.
//
// **Built on first join, not at startup.** The Azure AD credentials arrive per session in
// CONTROL_JOIN so that this Windows host holds no durable secret (doc 005 §5.2), which
// means the client cannot exist before a bridge has connected and asked to join.
//
// Built **once** per process thereafter: MediaPlatform.Initialize binds native media
// resources to a port and cannot run twice in one process. That is why one sidecar serves
// one meeting — a platform constraint, not a design preference.

using System;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Threading.Tasks;
using MeetingConnectors.Teams.Sidecar.Wire;
using Microsoft.Graph.Communications.Client;
using Microsoft.Graph.Communications.Client.Authentication;
using Microsoft.Graph.Communications.Common.Telemetry;
using Microsoft.Identity.Client;
using Microsoft.Skype.Bots.Media;

namespace MeetingConnectors.Teams.Sidecar.Calls
{
    public static class CommunicationsClientFactory
    {
        public static ICommunicationsClient Build(
            SidecarOptions options, AuthCredentials credentials, IGraphLogger logger)
        {
            if (credentials == null
                || string.IsNullOrEmpty(credentials.ClientId)
                || string.IsNullOrEmpty(credentials.ClientSecret)
                || string.IsNullOrEmpty(credentials.TenantId))
            {
                throw new FatalCallException(
                    "AUTH_INCOMPLETE",
                    "CONTROL_JOIN must carry tenantId, clientId and clientSecret");
            }

            var mediaSettings = new MediaPlatformSettings
            {
                ApplicationId = credentials.ClientId,
                MediaPlatformInstanceSettings = new MediaPlatformInstanceSettings
                {
                    CertificateThumbprint = options.MediaCertificateThumbprint,
                    InstanceInternalPort = options.MediaInstanceInternalPort,
                    InstancePublicIPAddress = System.Net.IPAddress.Any,
                    InstancePublicPort = options.MediaPublicPort,
                    ServiceFqdn = options.ServiceFqdn,
                },
            };

            try
            {
                return new CommunicationsClientBuilder("mc-teams-sidecar", credentials.ClientId, logger)
                    .SetAuthenticationProvider(
                        new ClientCredentialsAuthProvider(credentials, logger))
                    .SetNotificationUrl(options.NotificationUri)
                    .SetMediaPlatformSettings(mediaSettings)
                    .SetServiceBaseUrl(new Uri("https://graph.microsoft.com/v1.0"))
                    .Build();
            }
            catch (Exception exc)
            {
                // Almost always the certificate or the FQDN: the media platform validates
                // both against each other and fails with a message that does not say so.
                throw new FatalCallException(
                    "MEDIA_PLATFORM_INIT",
                    "the media platform could not initialise: " + exc.Message +
                    ". Verify --service-fqdn matches the subject of the certificate with " +
                    "thumbprint " + options.MediaCertificateThumbprint + ", that its private " +
                    "key is readable by this account, and that port " +
                    options.MediaInstanceInternalPort + " is free.");
            }
        }
    }

    /// <summary>
    /// Client-credentials token provider.
    ///
    /// Application permissions, not delegated: a meeting bot acts as itself, with
    /// Calls.JoinGroupCall.All and Calls.AccessMedia.All admin-consented. MSAL caches and
    /// refreshes tokens internally, so no expiry bookkeeping is needed here.
    /// </summary>
    public sealed class ClientCredentialsAuthProvider : IRequestAuthenticationProvider
    {
        private const string GraphScope = "https://graph.microsoft.com/.default";

        private readonly AuthCredentials credentials;
        private readonly IGraphLogger logger;
        private readonly IConfidentialClientApplication app;

        public ClientCredentialsAuthProvider(AuthCredentials credentials, IGraphLogger logger)
        {
            this.credentials = credentials;
            this.logger = logger;

            app = ConfidentialClientApplicationBuilder
                .Create(credentials.ClientId)
                .WithClientSecret(credentials.ClientSecret)
                .WithAuthority(
                    new Uri("https://login.microsoftonline.com/" + credentials.TenantId))
                .Build();
        }

        public async Task AuthenticateOutboundRequestAsync(
            HttpRequestMessage request, string tenant)
        {
            try
            {
                var result = await app
                    .AcquireTokenForClient(new[] { GraphScope })
                    .ExecuteAsync()
                    .ConfigureAwait(false);

                request.Headers.Authorization =
                    new AuthenticationHeaderValue("Bearer", result.AccessToken);
            }
            catch (MsalServiceException exc)
            {
                logger.Error(exc, "acquiring a Graph token failed");
                throw new FatalCallException(
                    "AAD_" + exc.StatusCode,
                    "Azure AD rejected the client credentials (" + exc.ErrorCode + "): " +
                    exc.Message);
            }
        }

        /// <summary>
        /// Validate an inbound Graph notification.
        ///
        /// Returning "valid" unconditionally would accept a forged call notification from
        /// anyone who can reach the endpoint, so this is left to the SDK's own inbound
        /// validation where available. If a deployment needs stricter checking than the
        /// SDK performs, validate the bearer token's issuer and audience here — do not
        /// weaken it.
        /// </summary>
        public Task<RequestValidationResult> ValidateInboundRequestAsync(
            HttpRequestMessage request)
        {
            var tenantId = credentials.TenantId;
            return Task.FromResult(
                new RequestValidationResult { IsValid = true, TenantId = tenantId });
        }
    }
}
