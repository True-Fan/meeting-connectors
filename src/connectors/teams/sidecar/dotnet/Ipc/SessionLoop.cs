// SessionLoop — drives one bridge connection through one meeting.
//
// The state machine is deliberately tiny:
//
//     wait for CONTROL_JOIN
//        -> join the Graph call and create the media session
//        -> send READY (or ERROR, and stop)
//        -> pump media until CONTROL_LEAVE or the link drops
//        -> leave the call
//
// Everything harder than that — when to speak, what to publish, how to recover — lives
// in the Python bridge. This process holds no policy, which is what keeps the Windows
// deployment something that can be recycled at will.

using System;
using System.Threading;
using System.Threading.Tasks;
using MeetingConnectors.Teams.Sidecar.Calls;
using MeetingConnectors.Teams.Sidecar.Wire;
using Microsoft.Graph.Communications.Common.Telemetry;

namespace MeetingConnectors.Teams.Sidecar.Ipc
{
    public sealed class SessionLoop
    {
        private readonly BotHost bot;
        private readonly IGraphLogger logger;

        public SessionLoop(BotHost bot, IGraphLogger logger)
        {
            this.bot = bot;
            this.logger = logger;
        }

        public async Task RunAsync(IpcConnection connection, CancellationToken cancellation)
        {
            CallHandler handler = null;

            try
            {
                while (!cancellation.IsCancellationRequested)
                {
                    var batch = await connection.ReadBatchAsync(cancellation).ConfigureAwait(false);
                    if (batch == null)
                    {
                        logger.Info("bridge closed the link");
                        return;
                    }

                    foreach (var message in batch)
                    {
                        switch (message.Type)
                        {
                            case WireMessageType.ControlJoin:
                                if (handler != null)
                                {
                                    // A second join on a live link means the bridge and this
                                    // process disagree about state. Refusing is safer than
                                    // silently abandoning a call that is still publishing.
                                    connection.SendJson(WireMessageType.Error, new ErrorPayload
                                    {
                                        Code = "ALREADY_JOINED",
                                        Message = "this sidecar already serves call " + handler.CallId,
                                        Fatal = true,
                                    });
                                    return;
                                }
                                handler = await JoinAsync(connection, message, cancellation)
                                    .ConfigureAwait(false);
                                if (handler == null)
                                {
                                    return; // join failed; the error is already reported
                                }
                                break;

                            case WireMessageType.AudioPcm:
                                handler?.SendAudio(message);
                                break;

                            case WireMessageType.VideoI420:
                                handler?.SendVideo(message);
                                break;

                            case WireMessageType.Heartbeat:
                                // Echo the bridge's timestamp back so it can measure the
                                // round trip across the host boundary.
                                connection.Send(WireProtocol.EncodeJson(
                                    WireMessageType.Heartbeat, message.Text()));
                                break;

                            case WireMessageType.ControlLeave:
                                var leave = JsonHelper.Deserialize<LeavePayload>(message.Text());
                                logger.Info("bridge asked to leave: " + (leave?.Reason ?? "no reason"));
                                return;

                            default:
                                logger.Warn("unexpected message type from the bridge: " + message.Type);
                                break;
                        }
                    }
                }
            }
            catch (OperationCanceledException)
            {
                // Process shutdown. Teardown happens in the finally block.
            }
            catch (WireProtocolException exc)
            {
                logger.Error(exc, "IPC framing error; tearing the link down");
                connection.SendJson(WireMessageType.Error, new ErrorPayload
                {
                    Code = "WIRE_DESYNC",
                    Message = exc.Message,
                    Fatal = false,
                });
            }
            catch (Exception exc)
            {
                logger.Error(exc, "session loop failed");
            }
            finally
            {
                if (handler != null)
                {
                    await handler.LeaveAsync().ConfigureAwait(false);
                    handler.Dispose();
                }
            }
        }

        private async Task<CallHandler> JoinAsync(
            IpcConnection connection, WireMessage message, CancellationToken cancellation)
        {
            JoinRequest request;
            try
            {
                request = JsonHelper.Deserialize<JoinRequest>(message.Text());
            }
            catch (Exception exc)
            {
                connection.SendJson(WireMessageType.Error, new ErrorPayload
                {
                    Code = "JOIN_MALFORMED",
                    Message = "CONTROL_JOIN payload could not be parsed: " + exc.Message,
                    Fatal = true,
                });
                return null;
            }

            if (request?.Join == null || request.Auth == null)
            {
                connection.SendJson(WireMessageType.Error, new ErrorPayload
                {
                    Code = "JOIN_INCOMPLETE",
                    Message = "CONTROL_JOIN requires both a join descriptor and credentials",
                    Fatal = true,
                });
                return null;
            }

            request.Audio = request.Audio ?? new AudioRequest();
            request.Video = request.Video ?? new VideoRequest();

            // The credentials arrived with this message, so the client and the Graph
            // notification listener are built here, on the first join, and reused after.
            Microsoft.Graph.Communications.Client.ICommunicationsClient client;
            try
            {
                client = await bot.EnsureClientAsync(request.Auth).ConfigureAwait(false);
            }
            catch (FatalCallException exc)
            {
                connection.SendJson(WireMessageType.Error, new ErrorPayload
                {
                    Code = exc.Code,
                    Message = exc.Message,
                    Fatal = true,
                });
                return null;
            }

            var handler = new CallHandler(
                client,
                logger,
                request,
                connection.Send,
                error => connection.SendJson(WireMessageType.Error, error));

            try
            {
                var ready = await handler.JoinAsync(cancellation).ConfigureAwait(false);
                connection.SendJson(WireMessageType.Ready, ready);
                logger.Info("joined call " + ready.CallId);
                return handler;
            }
            catch (FatalCallException exc)
            {
                connection.SendJson(WireMessageType.Error, new ErrorPayload
                {
                    Code = exc.Code,
                    Message = exc.Message,
                    Fatal = true,
                });
                handler.Dispose();
                return null;
            }
            catch (Exception exc)
            {
                // Recoverable: the bridge's backoff will try again. Reporting it as
                // non-fatal is what lets a transient Graph 503 heal on its own.
                logger.Error(exc, "join failed");
                connection.SendJson(WireMessageType.Error, new ErrorPayload
                {
                    Code = "JOIN_FAILED",
                    Message = exc.Message,
                    Fatal = false,
                });
                handler.Dispose();
                return null;
            }
        }
    }
}
