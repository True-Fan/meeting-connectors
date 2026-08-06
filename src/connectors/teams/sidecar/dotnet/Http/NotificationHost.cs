// Graph notification endpoint.
//
// **Why this lives here and not in the Python bridge's FastAPI app.** Graph drives a
// call's lifecycle by POSTing notifications, and the Graph Communications *SDK* consumes
// them: ICommunicationsClient.ProcessNotificationAsync is what advances the call state
// machine, raises OnUpdated, and delivers roster changes. Those notifications have to
// reach the object that owns the call — which is this process, on Windows.
//
// That is the one structural difference from Zoom worth noticing. Zoom's webhook is
// verified and handled in Python (connectors/zoom/webhook/router.py) because it only
// carries routing data the bridge acts on. Teams' notifications are SDK input, so
// forwarding them from Python would mean relaying them straight back out again.
// Consequence: connectors/teams adds no FastAPI router at all, and src/api is untouched
// by this connector.
//
// Authentication is the SDK's own: it validates the inbound Microsoft-issued token
// against the tenant and app id it was built with, which is why no signature check is
// hand-written here.

using System;
using System.Net;
using System.Net.Http;
using System.Threading.Tasks;
using System.Web.Http;
using Microsoft.Graph.Communications.Client;
using Microsoft.Graph.Communications.Common.Telemetry;
using Microsoft.Owin.Hosting;
using Owin;

namespace MeetingConnectors.Teams.Sidecar.Http
{
    /// <summary>Self-hosted HTTPS listener for Graph call notifications.</summary>
    public sealed class NotificationHost : IDisposable
    {
        private IDisposable server;

        public void Start(SidecarOptions options, ICommunicationsClient client, IGraphLogger logger)
        {
            NotificationController.Configure(client, logger);

            var listenUri = options.NotificationListenUri.ToString();
            server = WebApp.Start(listenUri, app =>
            {
                var config = new HttpConfiguration();
                config.MapHttpAttributeRoutes();
                config.Formatters.JsonFormatter.SerializerSettings.NullValueHandling =
                    Newtonsoft.Json.NullValueHandling.Ignore;
                app.UseWebApi(config);
            });

            logger.Info(
                "Graph notifications listening on " + listenUri +
                ", advertised to Teams as " + options.NotificationUri);
            logger.Info(
                "Bind the media certificate to this port first: " +
                "netsh http add sslcert ipport=0.0.0.0:" + options.NotificationPort +
                " certhash=<thumbprint> appid={00000000-0000-0000-0000-000000000000}");
        }

        public void Dispose()
        {
            server?.Dispose();
        }
    }

    [RoutePrefix("api")]
    public sealed class NotificationController : ApiController
    {
        private static ICommunicationsClient client;
        private static IGraphLogger logger;

        internal static void Configure(ICommunicationsClient communicationsClient, IGraphLogger log)
        {
            client = communicationsClient;
            logger = log;
        }

        [HttpPost]
        [Route("calls")]
        public async Task<HttpResponseMessage> OnNotification()
        {
            if (client == null)
            {
                return Request.CreateResponse(HttpStatusCode.ServiceUnavailable);
            }

            try
            {
                // The SDK owns validation, deserialisation, and dispatch. Handing it the
                // raw request is the whole job.
                await client.ProcessNotificationAsync(Request).ConfigureAwait(false);
                return Request.CreateResponse(HttpStatusCode.Accepted);
            }
            catch (Exception exc)
            {
                logger?.Error(exc, "processing a Graph notification failed");
                // 202 regardless: Graph retries on a non-2xx, and replaying a
                // notification we already failed to process rarely succeeds while the
                // retries reliably make the log harder to read.
                return Request.CreateResponse(HttpStatusCode.Accepted);
            }
        }

        [HttpGet]
        [Route("health")]
        public HttpResponseMessage Health()
        {
            return Request.CreateResponse(HttpStatusCode.OK, new { status = "ok" });
        }
    }
}
