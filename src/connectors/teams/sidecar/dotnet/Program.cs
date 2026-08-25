// Entry point.
//
// Startup order matters and is not arbitrary:
//
//   1. Validate options. A missing FQDN or certificate must fail here, in seconds, not
//      inside MediaPlatform.Initialize with a message about a native handle.
//   2. Accept the bridge and wait for CONTROL_JOIN, which is what carries the Azure AD
//      credentials — so the ICommunicationsClient and the Graph notification listener are
//      built at that point, not at startup (see BotHost).
//
// The credentials arriving per session rather than at startup is deliberate: this Windows
// host then holds no durable secret worth stealing, and rotating a client secret is a
// bridge-side configuration change (doc 005 §5.2).

using System;
using System.Threading;
using System.Threading.Tasks;
using MeetingConnectors.Teams.Sidecar.Calls;
using MeetingConnectors.Teams.Sidecar.Http;
using MeetingConnectors.Teams.Sidecar.Ipc;
using MeetingConnectors.Teams.Sidecar.Wire;
using Microsoft.Graph.Communications.Client;
using Microsoft.Graph.Communications.Common.Telemetry;

namespace MeetingConnectors.Teams.Sidecar
{
    public static class Program
    {
        public static int Main(string[] args)
        {
            var logger = new GraphLogger("mc-teams-sidecar", redirectToTrace: true);

            SidecarOptions options;
            try
            {
                options = SidecarOptions.Parse(args);
                options.Validate();
            }
            catch (Exception exc)
            {
                Console.Error.WriteLine("configuration error: " + exc.Message);
                Console.Error.WriteLine();
                Console.Error.WriteLine(Usage);
                return 2;
            }

            using (var shutdown = new CancellationTokenSource())
            {
                Console.CancelKeyPress += (sender, e) =>
                {
                    e.Cancel = true; // leave the call cleanly rather than vanishing from it
                    logger.Info("shutdown requested");
                    shutdown.Cancel();
                };

                try
                {
                    return RunAsync(options, logger, shutdown.Token).GetAwaiter().GetResult();
                }
                catch (Exception exc)
                {
                    logger.Error(exc, "sidecar terminated");
                    return 1;
                }
            }
        }

        private static async Task<int> RunAsync(
            SidecarOptions options, IGraphLogger logger, CancellationToken cancellation)
        {
            using (var bot = new BotHost(options, logger))
            using (var ipc = new IpcServer(
                options,
                logger,
                connection => new SessionLoop(bot, logger).RunAsync(connection, cancellation)))
            {
                logger.Info("sidecar ready; waiting for the bridge");
                await ipc.RunAsync(cancellation).ConfigureAwait(false);
                return 0;
            }
        }

        private const string Usage = @"
mc-teams-sidecar — Microsoft Teams app-hosted media bot for meeting-connectors.

Required:
  --service-fqdn <fqdn>              Public DNS name Teams reaches this host on.
                                     Must match the media certificate subject.
  --media-cert-thumbprint <hex>      Publicly-trusted certificate for the media and
                                     notification endpoints.

Optional:
  --ipc-listen <ip>                  Bridge-facing bind address (default 127.0.0.1).
  --ipc-port <port>                  Bridge-facing port (default 8445).
  --ipc-cert-thumbprint <hex>        TLS certificate for the bridge link. Omitting it
                                     disables TLS — local development only.
  --ipc-require-client-cert          Require mutual TLS from the bridge.
  --ipc-client-thumbprints <a,b>     Allow-listed bridge client certificates.
  --media-public-port <port>         Port Teams sends media to (default 8445).
  --media-internal-port <port>       Port the media platform binds (default 8446).
  --notification-port <port>         HTTPS port for Graph notifications (default 9441).

Every flag also reads from MC_TEAMS_SIDECAR_<UPPER_SNAKE_CASE>.

Azure AD credentials and the meeting to join are NOT configured here: the bridge sends
them per session in CONTROL_JOIN, so this host holds no durable secret.
";
    }

    /// <summary>
    /// Owns the ICommunicationsClient and the Graph notification listener, both built on
    /// the first join because their configuration arrives with the session.
    /// </summary>
    public sealed class BotHost : IDisposable
    {
        private readonly SidecarOptions options;
        private readonly IGraphLogger logger;
        private readonly SemaphoreSlim gate = new SemaphoreSlim(1, 1);
        private readonly NotificationHost notifications = new NotificationHost();

        private ICommunicationsClient client;

        public BotHost(SidecarOptions options, IGraphLogger logger)
        {
            this.options = options;
            this.logger = logger;
        }

        /// <summary>
        /// Return the client, building it from <paramref name="credentials"/> on first call.
        ///
        /// The notification listener starts in the same step and before any call is created:
        /// Graph POSTs to it as soon as a call exists, and a notification arriving before
        /// the listener is up loses the call.
        /// </summary>
        public async Task<ICommunicationsClient> EnsureClientAsync(AuthCredentials credentials)
        {
            await gate.WaitAsync().ConfigureAwait(false);
            try
            {
                if (client != null)
                {
                    return client;
                }

                client = CommunicationsClientFactory.Build(options, credentials, logger);
                notifications.Start(options, client, logger);
                return client;
            }
            finally
            {
                gate.Release();
            }
        }

        public void Dispose()
        {
            notifications.Dispose();
            gate.Dispose();
            client?.Dispose();
        }
    }
}
