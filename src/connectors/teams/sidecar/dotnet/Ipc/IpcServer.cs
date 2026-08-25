// IPC server — the Windows end of the bridge link.
//
// One bridge connection at a time, on purpose: one sidecar process serves one meeting.
// That keeps a media-platform failure blast-radius equal to one meeting, mirrors how the
// Zoom sidecar is deployed, and means the process can be recycled between calls without
// coordinating with anyone.
//
// Writes are serialised through a lock rather than a queue. Media frames arrive on the
// media platform's callback threads and control messages on the accept loop's thread, so
// interleaved writes would corrupt framing; a lock is the smallest correct answer, and it
// is never held across an await.

using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Security;
using System.Net.Sockets;
using System.Security.Authentication;
using System.Security.Cryptography.X509Certificates;
using System.Threading;
using System.Threading.Tasks;
using MeetingConnectors.Teams.Sidecar.Wire;
using Microsoft.Graph.Communications.Common.Telemetry;

namespace MeetingConnectors.Teams.Sidecar.Ipc
{
    public sealed class IpcServer : IDisposable
    {
        private readonly SidecarOptions options;
        private readonly IGraphLogger logger;
        private readonly Func<IpcConnection, Task> onConnection;
        private readonly X509Certificate2 serverCertificate;

        private TcpListener listener;

        public IpcServer(
            SidecarOptions options,
            IGraphLogger logger,
            Func<IpcConnection, Task> onConnection)
        {
            this.options = options;
            this.logger = logger;
            this.onConnection = onConnection;

            if (!string.IsNullOrEmpty(options.IpcCertificateThumbprint))
            {
                serverCertificate = CertificateStore.Find(options.IpcCertificateThumbprint);
            }
            else
            {
                logger.Warn(
                    "IPC TLS is disabled: no --ipc-cert-thumbprint supplied. Acceptable for " +
                    "local development only — this link carries meeting audio and an Azure AD " +
                    "client secret.");
            }
        }

        public async Task RunAsync(CancellationToken cancellation)
        {
            listener = new TcpListener(options.IpcListenAddress, options.IpcPort);
            listener.Start();
            logger.Info("IPC listening on " + options.IpcListenAddress + ":" + options.IpcPort);

            using (cancellation.Register(() => listener.Stop()))
            {
                while (!cancellation.IsCancellationRequested)
                {
                    TcpClient client;
                    try
                    {
                        client = await listener.AcceptTcpClientAsync().ConfigureAwait(false);
                    }
                    catch (ObjectDisposedException)
                    {
                        return; // listener.Stop() during shutdown
                    }
                    catch (SocketException exc)
                    {
                        logger.Warn("accept failed: " + exc.Message);
                        continue;
                    }

                    // Handled inline, not fanned out: one meeting per process, so a second
                    // concurrent bridge is a misconfiguration rather than load to absorb.
                    await HandleClientAsync(client, cancellation).ConfigureAwait(false);
                }
            }
        }

        private async Task HandleClientAsync(TcpClient client, CancellationToken cancellation)
        {
            var remote = client.Client.RemoteEndPoint?.ToString() ?? "unknown";
            logger.Info("bridge connected from " + remote);

            try
            {
                client.NoDelay = true; // 20 ms audio frames must not be Nagle-batched
                Stream stream = client.GetStream();

                if (serverCertificate != null)
                {
                    var tls = new SslStream(stream, false, ValidateBridgeCertificate);
                    await tls.AuthenticateAsServerAsync(
                        serverCertificate,
                        clientCertificateRequired: options.IpcRequireClientCertificate,
                        enabledSslProtocols: SslProtocols.Tls12 | SslProtocols.Tls13,
                        checkCertificateRevocation: false).ConfigureAwait(false);
                    stream = tls;
                }

                using (var connection = new IpcConnection(stream, logger))
                {
                    await onConnection(connection).ConfigureAwait(false);
                }
            }
            catch (AuthenticationException exc)
            {
                logger.Error(exc, "TLS handshake with the bridge failed");
            }
            catch (Exception exc)
            {
                logger.Error(exc, "bridge connection failed");
            }
            finally
            {
                logger.Info("bridge disconnected: " + remote);
                client.Close();
            }
        }

        private bool ValidateBridgeCertificate(
            object sender,
            X509Certificate certificate,
            X509Chain chain,
            SslPolicyErrors errors)
        {
            if (!options.IpcRequireClientCertificate)
            {
                return true;
            }
            if (certificate == null)
            {
                logger.Warn("bridge presented no client certificate but one is required");
                return false;
            }

            // A thumbprint allow-list is the check that actually matters here: chain
            // validity alone would accept any certificate from the same internal CA,
            // including one issued to a different service.
            if (options.IpcAllowedClientThumbprints.Count > 0)
            {
                var thumbprint = new X509Certificate2(certificate).Thumbprint;
                if (!options.IpcAllowedClientThumbprints.Contains(thumbprint))
                {
                    logger.Warn("bridge client certificate " + thumbprint + " is not allow-listed");
                    return false;
                }
                return true;
            }

            if (errors != SslPolicyErrors.None)
            {
                logger.Warn("bridge client certificate rejected: " + errors);
                return false;
            }
            return true;
        }

        public void Dispose()
        {
            listener?.Stop();
            serverCertificate?.Dispose();
        }
    }

    /// <summary>One framed, write-serialised connection to the bridge.</summary>
    public sealed class IpcConnection : IDisposable
    {
        private readonly Stream stream;
        private readonly IGraphLogger logger;
        private readonly WireFrameDecoder decoder = new WireFrameDecoder();
        private readonly object writeLock = new object();
        private readonly byte[] readBuffer = new byte[64 * 1024];

        public IpcConnection(Stream stream, IGraphLogger logger)
        {
            this.stream = stream;
            this.logger = logger;
        }

        /// <summary>
        /// Send a pre-encoded frame. Synchronous and lock-guarded so media callback
        /// threads cannot interleave partial frames.
        /// </summary>
        public void Send(byte[] frame)
        {
            try
            {
                lock (writeLock)
                {
                    stream.Write(frame, 0, frame.Length);
                    stream.Flush();
                }
            }
            catch (Exception exc)
            {
                // A dead link is normal at teardown and is detected by the read loop;
                // throwing from a media callback thread would take the process down.
                logger.Warn("IPC write failed: " + exc.Message);
            }
        }

        public void SendJson(WireMessageType type, object payload)
        {
            Send(WireProtocol.EncodeJson(type, JsonHelper.Serialize(payload)));
        }

        /// <summary>
        /// Read messages until the bridge disconnects.
        ///
        /// A framing error is fatal for the connection and is not recovered from: a
        /// desynced binary stream cannot be realigned with confidence, and continuing
        /// would publish corrupt audio into a live meeting.
        /// </summary>
        public async Task<IEnumerable<WireMessage>> ReadBatchAsync(CancellationToken cancellation)
        {
            var read = await stream.ReadAsync(readBuffer, 0, readBuffer.Length, cancellation)
                .ConfigureAwait(false);
            if (read <= 0)
            {
                return null; // EOF
            }

            decoder.Feed(readBuffer, read);

            var messages = new List<WireMessage>();
            WireMessage message;
            while ((message = decoder.TryRead()) != null)
            {
                messages.Add(message);
            }
            return messages;
        }

        public void Dispose()
        {
            try
            {
                stream.Dispose();
            }
            catch (Exception)
            {
                // Closing an already-broken stream is the normal path here.
            }
        }
    }

    public static class CertificateStore
    {
        /// <summary>
        /// Look a certificate up by thumbprint in LocalMachine\My.
        ///
        /// <c>validOnly: false</c> is deliberate: an expired certificate should fail with
        /// "certificate has expired" at handshake time, not with "certificate not found",
        /// which sends whoever is on call looking for a deployment problem instead.
        /// </summary>
        public static X509Certificate2 Find(string thumbprint)
        {
            var normalised = thumbprint.Replace(" ", string.Empty).Replace("‎", string.Empty);

            using (var store = new X509Store(StoreName.My, StoreLocation.LocalMachine))
            {
                store.Open(OpenFlags.ReadOnly);
                var found = store.Certificates.Find(
                    X509FindType.FindByThumbprint, normalised, validOnly: false);

                if (found.Count == 0)
                {
                    throw new ArgumentException(
                        "no certificate with thumbprint " + normalised +
                        " in LocalMachine\\My. Install it and grant the service account read " +
                        "access to its private key.");
                }
                return found[0];
            }
        }
    }
}
