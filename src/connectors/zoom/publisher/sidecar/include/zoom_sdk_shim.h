// Zoom Meeting SDK header shim.
//
// Two build modes:
//
//   MC_WITH_ZOOM_SDK defined   -> include the real Meeting SDK for Linux headers.
//   otherwise                  -> compile against minimal local declarations.
//
// The stub mode is not a mock of Zoom's behaviour and does not join meetings. Its
// purpose is narrower and genuinely useful: it lets the sidecar's *own* logic — wire
// framing, IPC, threading, pacing, JSON — be compiled and exercised on any machine,
// including CI, without a licensed SDK download. That means the frozen wire protocol
// is verified from both sides (Python and C++) before the SDK is ever installed.
//
// The declarations below mirror the documented public signatures of the interfaces we
// implement. If the real SDK differs on a detail, the real build fails loudly at
// compile time — which is the correct place to find out.

#pragma once

#ifdef MC_WITH_ZOOM_SDK

#include "zoom_sdk.h"
#include "meeting_service_interface.h"
#include "auth_service_interface.h"
#include "setting_service_interface.h"
#include "rawdata/zoom_rawdata_api.h"
#include "rawdata/rawdata_audio_helper_interface.h"
#include "rawdata/rawdata_video_source_helper_interface.h"

using namespace ZOOMSDK;

#else  // ------------------------------- stub declarations -------------------------

#include <cstddef>
#include <cstdint>

// Frame formats the video sender accepts.
enum FrameDataFormat {
  FrameDataFormat_I420_FULL = 0,
  FrameDataFormat_I420_LIMITED = 1,
};

struct VideoSourceCapability {
  int width = 0;
  int height = 0;
  int frame = 0;  // frame rate; the real SDK's field naming may differ
};

template <typename T>
class IList {
 public:
  virtual ~IList() = default;
  virtual int GetCount() = 0;
  virtual T GetItem(int index) = 0;
};

class IZoomSDKVideoSender {
 public:
  virtual ~IZoomSDKVideoSender() = default;
  virtual void sendVideoFrame(char* frame_buffer, int frame_length, int width, int height,
                              unsigned int timestamp, FrameDataFormat format) = 0;
};

class IZoomSDKVideoSource {
 public:
  virtual ~IZoomSDKVideoSource() = default;
  virtual void onInitialize(IZoomSDKVideoSender* sender,
                            IList<VideoSourceCapability>* support_cap_list,
                            VideoSourceCapability& suggest_cap) = 0;
  virtual void onPropertyChange(IList<VideoSourceCapability>* support_cap_list,
                                VideoSourceCapability suggest_cap) = 0;
  virtual void onStartSend() = 0;
  virtual void onStopSend() = 0;
  virtual void onUninitialized() = 0;
};

class IZoomSDKAudioRawDataSender {
 public:
  virtual ~IZoomSDKAudioRawDataSender() = default;
  virtual void send(char* data, unsigned int data_length, int sample_rate, int channels) = 0;
};

class IZoomSDKVirtualAudioMicEvent {
 public:
  virtual ~IZoomSDKVirtualAudioMicEvent() = default;
  virtual void onMicInitialize(IZoomSDKAudioRawDataSender* sender) = 0;
  virtual void onMicStartSend() = 0;
  virtual void onMicStopSend() = 0;
  virtual void onMicUninitialized() = 0;
};

#endif  // MC_WITH_ZOOM_SDK
