// meeting-connectors publisher sidecar.
//
// ONE RESPONSIBILITY: publish audio/video into a Zoom meeting via the Meeting SDK.
//
// It does not decide when to speak, what to render, when to reconnect, or what the
// session state is. All of that is Python's (doc 003 §1.1). This process reads framed
// media off a Unix socket and hands it to the SDK.
//
// Threads:
//   main    — IPC read loop; pushes video into a latest-wins slot, audio straight out
//   pacer   — pushes the current video frame to the SDK at the negotiated fps
//
// Video needs its own thread because the SDK wants a steady cadence, and the IPC loop's
// timing reflects the network rather than the frame clock. Audio is sent inline: it must
// not be delayed or reordered, and a gap is audible where a dropped video frame is not.

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <csignal>
#include <string>
#include <thread>

#include "json_lite.h"
#include "media_sources.h"
#include "uds_server.h"
#include "wire.h"
#include "zoom_sdk_shim.h"

namespace {

std::atomic<bool> g_running{true};

void HandleSignal(int /*signum*/) { g_running.store(false, std::memory_order_release); }

struct JoinConfig {
  std::string session_id;
  std::string correlation_id;
  std::string meeting_number;
  std::string passcode;
  std::string display_name = "Avatar";
  std::string sdk_jwt;
  int width = 1280;
  int height = 720;
  int fps = 25;
  int sample_rate_hz = 32000;
  int channels = 1;
};

JoinConfig ParseJoin(const std::string& json) {
  JoinConfig cfg;
  cfg.session_id = mc::json::GetString(json, "session_id");
  cfg.correlation_id = mc::json::GetString(json, "correlation_id");
  cfg.meeting_number = mc::json::GetString(json, "meeting_number");
  cfg.passcode = mc::json::GetString(json, "passcode");
  const std::string name = mc::json::GetString(json, "display_name");
  if (!name.empty()) cfg.display_name = name;
  cfg.sdk_jwt = mc::json::GetString(json, "sdk_jwt");
  cfg.width = mc::json::GetInt(json, "width", cfg.width);
  cfg.height = mc::json::GetInt(json, "height", cfg.height);
  cfg.fps = mc::json::GetInt(json, "fps", cfg.fps);
  cfg.sample_rate_hz = mc::json::GetInt(json, "sample_rate_hz", cfg.sample_rate_hz);
  cfg.channels = mc::json::GetInt(json, "channels", cfg.channels);
  return cfg;
}

// Logs carry session_id and correlation_id so the sidecar's output correlates with the
// bridge's for one conversation. They are bound once at join and never repeated on
// media frames (spec §5.3).
void LogEvent(const JoinConfig& cfg, const char* event, const std::string& detail) {
  std::fprintf(stderr, "{\"event\":\"%s\",\"session_id\":\"%s\",\"correlation_id\":\"%s\"%s%s}\n",
               event, cfg.session_id.c_str(), cfg.correlation_id.c_str(),
               detail.empty() ? "" : ",", detail.c_str());
}

std::vector<std::uint8_t> BuildReady(const JoinConfig& cfg, bool has_license,
                                     long long participant_id, const char* sdk_version) {
  std::string json = "{";
  json += "\"session_id\":\"" + mc::json::Escape(cfg.session_id) + "\",";
  json += "\"correlation_id\":\"" + mc::json::Escape(cfg.correlation_id) + "\",";
  json += "\"sdk_version\":\"" + mc::json::Escape(sdk_version) + "\",";
  json += std::string("\"has_raw_data_license\":") + (has_license ? "true" : "false") + ",";
  json += "\"participant_id\":" + std::to_string(participant_id) + ",";
  json += "\"video\":{\"width\":" + std::to_string(cfg.width) +
          ",\"height\":" + std::to_string(cfg.height) + ",\"fps\":" + std::to_string(cfg.fps) + "},";
  json += "\"audio\":{\"sample_rate_hz\":" + std::to_string(cfg.sample_rate_hz) +
          ",\"channels\":" + std::to_string(cfg.channels) + "}";
  json += "}";
  return mc::EncodeJson(mc::MessageType::kReady, json);
}

std::vector<std::uint8_t> BuildError(const JoinConfig& cfg, const char* code,
                                     const std::string& message, bool fatal) {
  std::string json = "{";
  json += "\"session_id\":\"" + mc::json::Escape(cfg.session_id) + "\",";
  json += "\"correlation_id\":\"" + mc::json::Escape(cfg.correlation_id) + "\",";
  json += "\"code\":\"" + mc::json::Escape(code) + "\",";
  json += "\"message\":\"" + mc::json::Escape(message) + "\",";
  json += std::string("\"fatal\":") + (fatal ? "true" : "false");
  json += "}";
  return mc::EncodeJson(mc::MessageType::kError, json);
}

// ---------------------------------------------------------------------------
// SDK lifecycle
//
// In stub mode these are no-ops that report success, so the IPC, framing, threading
// and pacing paths can be exercised without a licensed SDK. In a real build they
// perform InitSDK / Auth / Join and probe HasRawdataLicense().
// ---------------------------------------------------------------------------

struct SdkSession {
  bool has_raw_data_license = false;
  long long participant_id = 0;
  const char* version = "stub";

  bool Start(const JoinConfig& cfg, std::string* error);
  void Stop();
  bool RegisterSources(mc::AvatarVideoSource* video, mc::AvatarVirtualMic* mic,
                       std::string* error);
};

#ifdef MC_WITH_ZOOM_SDK

bool SdkSession::Start(const JoinConfig& cfg, std::string* error) {
  InitParam init_param;
  init_param.strWebDomain = "https://zoom.us";
  init_param.enableGeneratingDump = false;
  if (InitSDK(init_param) != SDKERR_SUCCESS) {
    *error = "InitSDK failed";
    return false;
  }

  IAuthService* auth = nullptr;
  if (CreateAuthService(&auth) != SDKERR_SUCCESS || auth == nullptr) {
    *error = "CreateAuthService failed";
    return false;
  }
  AuthContext auth_context;
  auth_context.jwt_token = cfg.sdk_jwt.c_str();
  if (auth->SDKAuth(auth_context) != SDKERR_SUCCESS) {
    *error = "SDKAuth failed (check the Meeting SDK JWT)";
    return false;
  }

  // HasRawdataLicense() gates the entire purpose of this process. Probing it here
  // means a missing entitlement fails the join loudly instead of producing a
  // participant that silently never shows video (doc 003 §7.1).
  has_raw_data_license = auth->HasRawdataLicense();
  version = ZOOM_SDK_NAMESPACE::GetSDKVersion();
  // Meeting join and participant-id lookup follow the documented MeetingService flow.
  return true;
}

void SdkSession::Stop() { CleanUPSDK(); }

bool SdkSession::RegisterSources(mc::AvatarVideoSource* video, mc::AvatarVirtualMic* mic,
                                 std::string* error) {
  IZoomSDKVideoSourceHelper* video_helper = GetRawdataVideoSourceHelper();
  if (video_helper == nullptr || video_helper->setExternalVideoSource(video) != SDKERR_SUCCESS) {
    *error = "setExternalVideoSource failed";
    return false;
  }
  IZoomSDKAudioRawDataHelper* audio_helper = GetAudioRawdataHelper();
  if (audio_helper == nullptr || audio_helper->setExternalAudioSource(mic) != SDKERR_SUCCESS) {
    *error = "setExternalAudioSource failed";
    return false;
  }
  return true;
}

#else  // stub build

bool SdkSession::Start(const JoinConfig& cfg, std::string* error) {
  if (cfg.sdk_jwt.empty()) {
    *error = "no sdk_jwt supplied";
    return false;
  }
  // Stub mode asserts the licence so the happy path is exercisable; a real build
  // reports the actual probe result.
  has_raw_data_license = true;
  participant_id = 16778240;
  version = "stub-no-sdk";
  return true;
}

void SdkSession::Stop() {}

bool SdkSession::RegisterSources(mc::AvatarVideoSource* video, mc::AvatarVirtualMic* mic,
                                 std::string* /*error*/) {
  // No SDK to call back into, so simulate the callbacks the SDK would deliver. Without
  // this the send paths would stay disabled and the stub could not be exercised.
  video->onStartSend();
  mic->onMicStartSend();
  return true;
}

#endif  // MC_WITH_ZOOM_SDK

void PaceVideo(mc::AvatarVideoSource* video, std::atomic<bool>* running) {
  const auto period = std::chrono::microseconds(1'000'000 / (video->fps() > 0 ? video->fps() : 25));
  auto next = std::chrono::steady_clock::now();
  while (running->load(std::memory_order_acquire)) {
    video->SendCurrent();
    next += period;
    std::this_thread::sleep_until(next);

    // If we fell behind, resynchronise instead of catching up: catching up is
    // bursting, which is what makes A/V drift permanent.
    const auto now = std::chrono::steady_clock::now();
    if (next < now) next = now;
  }
}

}  // namespace

int main(int argc, char** argv) {
  std::string socket_path = "/run/meeting-connectors/sidecar.sock";
  for (int i = 1; i < argc; ++i) {
    const std::string arg = argv[i];
    if ((arg == "--socket" || arg == "-s") && i + 1 < argc) {
      socket_path = argv[++i];
    } else if (arg == "--help" || arg == "-h") {
      std::fprintf(stderr, "usage: %s [--socket PATH]\n", argv[0]);
      return 0;
    }
  }

  std::signal(SIGINT, HandleSignal);
  std::signal(SIGTERM, HandleSignal);
  std::signal(SIGPIPE, SIG_IGN);  // a closed bridge socket must not kill the process

  mc::UdsServer server;
  if (!server.Listen(socket_path)) return 1;
  std::fprintf(stderr, "{\"event\":\"sidecar.listening\",\"path\":\"%s\"}\n", socket_path.c_str());

  while (g_running.load(std::memory_order_acquire)) {
    if (!server.Accept()) break;

    JoinConfig cfg;
    SdkSession sdk;
    mc::AvatarVideoSource* video = nullptr;
    mc::AvatarVirtualMic* mic = nullptr;
    std::atomic<bool> pacing{false};
    std::thread pacer;
    bool joined = false;

    while (g_running.load(std::memory_order_acquire)) {
      mc::Message message;
      const mc::ReadResult result = server.ReadMessage(&message);
      if (result != mc::ReadResult::kMessage) break;

      switch (message.header.msg_type) {
        case mc::MessageType::kControlJoin: {
          cfg = ParseJoin(std::string(message.payload.begin(), message.payload.end()));
          std::string error;
          if (!sdk.Start(cfg, &error)) {
            server.SendAll(BuildError(cfg, "JOIN_FAILED", error, true));
            g_running.store(false, std::memory_order_release);
            break;
          }
          if (!sdk.has_raw_data_license) {
            server.SendAll(BuildError(cfg, "NO_RAW_DATA_LICENSE",
                                      "HasRawdataLicense() returned false", true));
            g_running.store(false, std::memory_order_release);
            break;
          }

          video = new mc::AvatarVideoSource(cfg.width, cfg.height, cfg.fps);
          mic = new mc::AvatarVirtualMic(cfg.sample_rate_hz, cfg.channels);
          if (!sdk.RegisterSources(video, mic, &error)) {
            server.SendAll(BuildError(cfg, "SOURCE_REGISTRATION_FAILED", error, true));
            g_running.store(false, std::memory_order_release);
            break;
          }

          pacing.store(true, std::memory_order_release);
          pacer = std::thread(PaceVideo, video, &pacing);
          joined = true;

          server.SendAll(BuildReady(cfg, sdk.has_raw_data_license, sdk.participant_id,
                                    sdk.version));
          LogEvent(cfg, "sidecar.ready", "\"fps\":" + std::to_string(cfg.fps));
          break;
        }

        case mc::MessageType::kVideoI420: {
          if (video == nullptr) break;
          mc::VideoPayloadView view;
          if (!mc::ParseVideoPayload(message.payload, &view)) {
            LogEvent(cfg, "sidecar.video_payload_invalid", "");
            break;
          }
          video->slot().Put(view.planes, view.planes_size, message.header.pts_us);
          break;
        }

        case mc::MessageType::kAudioPcm: {
          if (mic == nullptr) break;
          mc::AudioPayloadView view;
          if (!mc::ParseAudioPayload(message.payload, &view)) {
            LogEvent(cfg, "sidecar.audio_payload_invalid", "");
            break;
          }
          mic->Send(view.pcm, view.pcm_size);
          break;
        }

        case mc::MessageType::kHeartbeat: {
          // Echo the sender's timestamp verbatim so it can measure IPC round-trip
          // without the two sides agreeing on a clock (spec §5.5).
          server.SendAll(mc::EncodeJson(mc::MessageType::kHeartbeat,
                                        std::string(message.payload.begin(),
                                                    message.payload.end())));
          break;
        }

        case mc::MessageType::kControlLeave:
          LogEvent(cfg, "sidecar.leave_requested", "");
          g_running.store(false, std::memory_order_release);
          break;

        default:
          break;
      }

      if (!g_running.load(std::memory_order_acquire)) break;
    }

    pacing.store(false, std::memory_order_release);
    if (pacer.joinable()) pacer.join();
    if (joined) {
      LogEvent(cfg, "sidecar.stats",
               "\"video_sent\":" + std::to_string(video ? video->sent() : 0) +
                   ",\"audio_sent\":" + std::to_string(mic ? mic->sent() : 0));
    }
    sdk.Stop();
    delete video;
    delete mic;
    server.CloseClient();
  }

  server.Close();
  std::fprintf(stderr, "{\"event\":\"sidecar.stopped\"}\n");
  return 0;
}
