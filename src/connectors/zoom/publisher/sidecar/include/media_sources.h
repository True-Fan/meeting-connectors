// Zoom Meeting SDK external media sources.
//
// Implements the two officially documented raw-data *send* interfaces:
//
//   video — IZoomSDKVideoSource / IZoomSDKVideoSender, registered via
//           GetRawdataVideoSourceHelper()->setExternalVideoSource(). Frames are
//           YUV420 (I420).
//   audio — IZoomSDKAudioRawDataHelper::setExternalAudioSource() with a virtual
//           microphone event handler, fed PCM.
//
// Nothing else. This process has exactly one responsibility: hand frames to the
// Zoom SDK. All decisions live in Python (doc 003 §1.1).

#pragma once

#include <atomic>
#include <cstdint>
#include <mutex>
#include <vector>

#include "zoom_sdk_shim.h"

namespace mc {

// Latest-frame-wins buffer shared between the IPC reader thread and the SDK's
// sender thread.
//
// Latest-wins rather than a queue on purpose: the Python pacer has already placed
// every frame on the shared media clock, so if the SDK asks later than expected the
// correct frame to send is the newest one. Queueing here would re-introduce the
// buffering the pacer exists to prevent, and a backlog would show as A/V drift.
class FrameSlot {
 public:
  void Put(const std::uint8_t* data, std::size_t size, std::int64_t pts_us) {
    std::lock_guard<std::mutex> lock(mutex_);
    buffer_.assign(data, data + size);
    pts_us_ = pts_us;
    has_frame_ = true;
    ++writes_;
  }

  // Copies the current frame out. Returns false when nothing has arrived yet.
  bool Get(std::vector<std::uint8_t>* out, std::int64_t* pts_us) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (!has_frame_) return false;
    *out = buffer_;
    *pts_us = pts_us_;
    return true;
  }

  std::uint64_t writes() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return writes_;
  }

 private:
  mutable std::mutex mutex_;
  std::vector<std::uint8_t> buffer_;
  std::int64_t pts_us_ = 0;
  bool has_frame_ = false;
  std::uint64_t writes_ = 0;
};

// External video source. The SDK calls onInitialize once, then onStartSend when the
// bot's camera goes live; sendVideoFrame may only be called between onStartSend and
// onStopSend.
class AvatarVideoSource : public IZoomSDKVideoSource {
 public:
  AvatarVideoSource(int width, int height, int fps)
      : width_(width), height_(height), fps_(fps) {}

  void onInitialize(IZoomSDKVideoSender* sender,
                    IList<VideoSourceCapability>* /*support_cap_list*/,
                    VideoSourceCapability& /*suggest_cap*/) override {
    sender_ = sender;
  }

  void onPropertyChange(IList<VideoSourceCapability>* /*support_cap_list*/,
                        VideoSourceCapability /*suggest_cap*/) override {}

  void onStartSend() override { sending_.store(true, std::memory_order_release); }
  void onStopSend() override { sending_.store(false, std::memory_order_release); }
  void onUninitialized() override {
    sending_.store(false, std::memory_order_release);
    sender_ = nullptr;
  }

  // Called from the pacing thread. Pushes the current slot contents into the SDK.
  bool SendCurrent() {
    if (!sending_.load(std::memory_order_acquire) || sender_ == nullptr) return false;

    std::vector<std::uint8_t> frame;
    std::int64_t pts_us = 0;
    if (!slot_.Get(&frame, &pts_us)) return false;

    const std::size_t expected =
        static_cast<std::size_t>(width_) * static_cast<std::size_t>(height_) * 3 / 2;
    if (frame.size() != expected) return false;

    sender_->sendVideoFrame(reinterpret_cast<char*>(frame.data()),
                            static_cast<int>(frame.size()), width_, height_,
                            static_cast<unsigned int>(pts_us / 1000), FrameDataFormat_I420_FULL);
    ++sent_;
    return true;
  }

  FrameSlot& slot() { return slot_; }
  bool is_sending() const { return sending_.load(std::memory_order_acquire); }
  std::uint64_t sent() const { return sent_; }
  int fps() const { return fps_; }

 private:
  int width_;
  int height_;
  int fps_;
  IZoomSDKVideoSender* sender_ = nullptr;
  std::atomic<bool> sending_{false};
  FrameSlot slot_;
  std::uint64_t sent_ = 0;
};

// Virtual microphone. The SDK pulls PCM through the sender handed to us in
// onMicInitialize; onMicStartSend marks the point after which sending is legal.
class AvatarVirtualMic : public IZoomSDKVirtualAudioMicEvent {
 public:
  AvatarVirtualMic(int sample_rate_hz, int channels)
      : sample_rate_hz_(sample_rate_hz), channels_(channels) {}

  void onMicInitialize(IZoomSDKAudioRawDataSender* sender) override { sender_ = sender; }
  void onMicStartSend() override { sending_.store(true, std::memory_order_release); }
  void onMicStopSend() override { sending_.store(false, std::memory_order_release); }
  void onMicUninitialized() override {
    sending_.store(false, std::memory_order_release);
    sender_ = nullptr;
  }

  // Audio is pushed as it arrives rather than latest-wins: a dropped audio frame is
  // an audible gap, whereas a dropped video frame costs only smoothness (spec §6).
  bool Send(const std::uint8_t* pcm, std::size_t size) {
    if (!sending_.load(std::memory_order_acquire) || sender_ == nullptr) return false;
    sender_->send(reinterpret_cast<char*>(const_cast<std::uint8_t*>(pcm)),
                  static_cast<unsigned int>(size), sample_rate_hz_, channels_);
    ++sent_;
    return true;
  }

  bool is_sending() const { return sending_.load(std::memory_order_acquire); }
  std::uint64_t sent() const { return sent_; }

 private:
  int sample_rate_hz_;
  int channels_;
  IZoomSDKAudioRawDataSender* sender_ = nullptr;
  std::atomic<bool> sending_{false};
  std::uint64_t sent_ = 0;
};

}  // namespace mc
