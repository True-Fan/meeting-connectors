// Unix domain socket server for the sidecar.

#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include "wire.h"

namespace mc {

enum class ReadResult {
  kMessage,
  kClosed,
  kError,
};

class UdsServer {
 public:
  UdsServer() = default;
  ~UdsServer();

  UdsServer(const UdsServer&) = delete;
  UdsServer& operator=(const UdsServer&) = delete;

  bool Listen(const std::string& path);
  bool Accept();

  // Reads one complete framed message, buffering partial reads across calls.
  ReadResult ReadMessage(Message* out);

  bool SendAll(const std::vector<std::uint8_t>& payload);

  void CloseClient();
  void Close();

  bool has_client() const { return client_fd_ >= 0; }

 private:
  bool TryParse(Message* out);

  int listen_fd_ = -1;
  int client_fd_ = -1;
  std::string path_;
  std::vector<std::uint8_t> buffer_;
  bool desynced_ = false;
};

}  // namespace mc
