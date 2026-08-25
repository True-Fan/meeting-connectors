// Minimal JSON field extraction for control messages.
//
// Deliberately not a JSON library. Control messages are produced by exactly one
// writer (src/connectors/zoom/publisher/publisher.py) with a known, flat shape, and
// vendoring a parser into a process whose entire job is to forward frames would be
// dependency weight for no benefit. Media payloads are binary and never come near this.
//
// Scope: flat string and integer fields, and integers nested one level inside the
// documented "video"/"audio" objects. Anything else returns the default.

#pragma once

#include <cstddef>
#include <cstdlib>
#include <string>

namespace mc::json {

inline std::string Escape(const std::string& value) {
  std::string out;
  out.reserve(value.size());
  for (const char c : value) {
    switch (c) {
      case '"': out += "\\\""; break;
      case '\\': out += "\\\\"; break;
      case '\n': out += "\\n"; break;
      case '\r': out += "\\r"; break;
      case '\t': out += "\\t"; break;
      default:
        if (static_cast<unsigned char>(c) < 0x20) {
          continue;  // drop control characters rather than emit invalid JSON
        }
        out += c;
    }
  }
  return out;
}

// Finds the value position for "key": within json, or npos.
inline std::size_t FindValue(const std::string& json, const std::string& key) {
  const std::string needle = "\"" + key + "\"";
  std::size_t at = json.find(needle);
  if (at == std::string::npos) return std::string::npos;
  at = json.find(':', at + needle.size());
  if (at == std::string::npos) return std::string::npos;
  ++at;
  while (at < json.size() && (json[at] == ' ' || json[at] == '\t')) ++at;
  return at < json.size() ? at : std::string::npos;
}

inline std::string GetString(const std::string& json, const std::string& key) {
  const std::size_t at = FindValue(json, key);
  if (at == std::string::npos || json[at] != '"') return {};

  std::string out;
  for (std::size_t i = at + 1; i < json.size(); ++i) {
    const char c = json[i];
    if (c == '\\' && i + 1 < json.size()) {
      const char next = json[++i];
      switch (next) {
        case 'n': out += '\n'; break;
        case 'r': out += '\r'; break;
        case 't': out += '\t'; break;
        default: out += next;
      }
      continue;
    }
    if (c == '"') break;
    out += c;
  }
  return out;
}

inline int GetInt(const std::string& json, const std::string& key, int fallback) {
  const std::size_t at = FindValue(json, key);
  if (at == std::string::npos) return fallback;
  const char* start = json.c_str() + at;
  char* end = nullptr;
  const long value = std::strtol(start, &end, 10);
  if (end == start) return fallback;
  return static_cast<int>(value);
}

}  // namespace mc::json
