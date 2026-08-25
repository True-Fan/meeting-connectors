// Sidecar IPC wire protocol — version 1. FROZEN.
//
// C++ counterpart of src/connectors/zoom/publisher/protocol.py.
// Specification: docs/design/004-sidecar-ipc-protocol.md
//
// The Python side is the reference implementation and holds the conformance vector
// (tests/unit/test_sidecar_protocol.py). This file must match it byte for byte.
// Do not change the layout; additive changes only, per spec §1.

#pragma once

#include <cstdint>
#include <cstring>
#include <string>
#include <vector>

namespace mc {

constexpr std::uint32_t kMagic = 0x5A4D4331u;  // "ZMC1"
constexpr std::uint8_t kWireVersion = 1;
constexpr std::size_t kHeaderSize = 24;
constexpr std::size_t kVideoHeaderSize = 12;
constexpr std::size_t kAudioHeaderSize = 8;
constexpr std::uint32_t kMaxPayloadBytes = 8u * 1024u * 1024u;

enum class MessageType : std::uint8_t {
  kVideoI420 = 0x01,
  kAudioPcm = 0x02,
  kControlJoin = 0x03,
  kControlLeave = 0x04,
  kHeartbeat = 0x05,
  kReady = 0x06,
  kError = 0x07,
};

enum Flags : std::uint8_t {
  kFlagNone = 0x00,
  kFlagKeyframe = 0x01,
  kFlagIdle = 0x02,
  kFlagEndOfStream = 0x04,
};

enum class SampleFormat : std::uint8_t {
  kS16Le = 1,
};

struct Header {
  std::uint32_t magic = kMagic;
  std::uint8_t version = kWireVersion;
  MessageType msg_type = MessageType::kHeartbeat;
  std::uint8_t flags = kFlagNone;
  std::uint8_t reserved = 0;
  std::uint32_t seq = 0;
  std::int64_t pts_us = 0;
  std::uint32_t length = 0;
};

struct Message {
  Header header;
  std::vector<std::uint8_t> payload;
};

struct VideoPayloadView {
  std::uint16_t width = 0;
  std::uint16_t height = 0;
  std::uint16_t stride_y = 0;
  std::uint16_t stride_u = 0;
  std::uint16_t stride_v = 0;
  const std::uint8_t* planes = nullptr;
  std::size_t planes_size = 0;
};

struct AudioPayloadView {
  std::uint32_t sample_rate_hz = 0;
  std::uint8_t channels = 0;
  SampleFormat sample_format = SampleFormat::kS16Le;
  const std::uint8_t* pcm = nullptr;
  std::size_t pcm_size = 0;
};

// ---------------------------------------------------------------------------
// Big-endian helpers.
//
// The wire is network order (spec §2) so Python and C++ cannot disagree on host
// endianness. Reading byte by byte rather than with ntohl keeps this header
// dependency-free and correct regardless of alignment.
// ---------------------------------------------------------------------------

inline std::uint16_t ReadU16(const std::uint8_t* p) {
  return static_cast<std::uint16_t>((static_cast<std::uint16_t>(p[0]) << 8) | p[1]);
}

inline std::uint32_t ReadU32(const std::uint8_t* p) {
  return (static_cast<std::uint32_t>(p[0]) << 24) | (static_cast<std::uint32_t>(p[1]) << 16) |
         (static_cast<std::uint32_t>(p[2]) << 8) | static_cast<std::uint32_t>(p[3]);
}

inline std::int64_t ReadI64(const std::uint8_t* p) {
  std::uint64_t value = 0;
  for (int i = 0; i < 8; ++i) {
    value = (value << 8) | p[i];
  }
  return static_cast<std::int64_t>(value);
}

inline void WriteU32(std::uint8_t* p, std::uint32_t value) {
  p[0] = static_cast<std::uint8_t>(value >> 24);
  p[1] = static_cast<std::uint8_t>(value >> 16);
  p[2] = static_cast<std::uint8_t>(value >> 8);
  p[3] = static_cast<std::uint8_t>(value);
}

inline void WriteI64(std::uint8_t* p, std::int64_t signed_value) {
  const auto value = static_cast<std::uint64_t>(signed_value);
  for (int i = 0; i < 8; ++i) {
    p[i] = static_cast<std::uint8_t>(value >> (56 - 8 * i));
  }
}

// Parses a 24-byte header. Returns false when the magic or version is wrong —
// framing desync is fatal by design (spec §6): a heuristic resync would publish
// garbage while reporting success.
inline bool ParseHeader(const std::uint8_t* p, Header* out) {
  const std::uint32_t magic = ReadU32(p);
  if (magic != kMagic) return false;
  if (p[4] != kWireVersion) return false;

  out->magic = magic;
  out->version = p[4];
  out->msg_type = static_cast<MessageType>(p[5]);
  out->flags = p[6];
  out->reserved = p[7];
  out->seq = ReadU32(p + 8);
  out->pts_us = ReadI64(p + 12);
  out->length = ReadU32(p + 20);
  return out->length <= kMaxPayloadBytes;
}

inline bool ParseVideoPayload(const std::vector<std::uint8_t>& payload, VideoPayloadView* out) {
  if (payload.size() < kVideoHeaderSize) return false;
  const std::uint8_t* p = payload.data();
  out->width = ReadU16(p);
  out->height = ReadU16(p + 2);
  out->stride_y = ReadU16(p + 4);
  out->stride_u = ReadU16(p + 6);
  out->stride_v = ReadU16(p + 8);
  out->planes = p + kVideoHeaderSize;
  out->planes_size = payload.size() - kVideoHeaderSize;

  // Geometry travels per frame (spec §5.1), so validate against it rather than
  // against a value negotiated at join that may now be stale.
  const std::size_t expected =
      static_cast<std::size_t>(out->width) * out->height * 3 / 2;
  return out->planes_size == expected;
}

inline bool ParseAudioPayload(const std::vector<std::uint8_t>& payload, AudioPayloadView* out) {
  if (payload.size() < kAudioHeaderSize) return false;
  const std::uint8_t* p = payload.data();
  out->sample_rate_hz = ReadU32(p);
  out->channels = p[4];
  if (p[5] != static_cast<std::uint8_t>(SampleFormat::kS16Le)) return false;
  out->sample_format = SampleFormat::kS16Le;
  out->pcm = p + kAudioHeaderSize;
  out->pcm_size = payload.size() - kAudioHeaderSize;
  return out->channels != 0;
}

// Serialises a control message with a JSON body.
inline std::vector<std::uint8_t> EncodeJson(MessageType type, const std::string& json) {
  std::vector<std::uint8_t> out(kHeaderSize + json.size());
  std::uint8_t* p = out.data();
  WriteU32(p, kMagic);
  p[4] = kWireVersion;
  p[5] = static_cast<std::uint8_t>(type);
  p[6] = kFlagNone;
  p[7] = 0;
  WriteU32(p + 8, 0);
  WriteI64(p + 12, 0);
  WriteU32(p + 20, static_cast<std::uint32_t>(json.size()));
  if (!json.empty()) {
    std::memcpy(p + kHeaderSize, json.data(), json.size());
  }
  return out;
}

}  // namespace mc
