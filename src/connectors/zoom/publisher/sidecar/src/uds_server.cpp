// Unix domain socket server and frame reader.
//
// Accepts one connection at a time: one sidecar process serves one meeting
// (doc 001 §12.3), so concurrency here would be complexity with no purpose.

#include "uds_server.h"

#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#include <cerrno>
#include <cstdio>
#include <cstring>
#include <filesystem>

namespace mc {

UdsServer::~UdsServer() { Close(); }

bool UdsServer::Listen(const std::string& path) {
  path_ = path;

  // A stale socket file from a crashed predecessor would make bind() fail with
  // EADDRINUSE forever, so remove it first.
  std::error_code ignored;
  std::filesystem::remove(path_, ignored);
  std::filesystem::create_directories(std::filesystem::path(path_).parent_path(), ignored);

  listen_fd_ = ::socket(AF_UNIX, SOCK_STREAM, 0);
  if (listen_fd_ < 0) {
    std::fprintf(stderr, "socket() failed: %s\n", std::strerror(errno));
    return false;
  }

  sockaddr_un address{};
  address.sun_family = AF_UNIX;
  if (path_.size() >= sizeof(address.sun_path)) {
    std::fprintf(stderr, "socket path too long: %s\n", path_.c_str());
    Close();
    return false;
  }
  std::strncpy(address.sun_path, path_.c_str(), sizeof(address.sun_path) - 1);

  if (::bind(listen_fd_, reinterpret_cast<sockaddr*>(&address), sizeof(address)) < 0) {
    std::fprintf(stderr, "bind(%s) failed: %s\n", path_.c_str(), std::strerror(errno));
    Close();
    return false;
  }
  if (::listen(listen_fd_, 1) < 0) {
    std::fprintf(stderr, "listen() failed: %s\n", std::strerror(errno));
    Close();
    return false;
  }

  // The bridge runs as a different user in its own container; both need access to
  // the shared volume.
  ::chmod(path_.c_str(), 0660);
  return true;
}

bool UdsServer::Accept() {
  if (listen_fd_ < 0) return false;
  client_fd_ = ::accept(listen_fd_, nullptr, nullptr);
  if (client_fd_ < 0) {
    std::fprintf(stderr, "accept() failed: %s\n", std::strerror(errno));
    return false;
  }
  buffer_.clear();
  return true;
}

bool UdsServer::SendAll(const std::vector<std::uint8_t>& payload) {
  if (client_fd_ < 0) return false;
  std::size_t written = 0;
  while (written < payload.size()) {
    const ssize_t n = ::write(client_fd_, payload.data() + written, payload.size() - written);
    if (n <= 0) {
      if (errno == EINTR) continue;
      return false;
    }
    written += static_cast<std::size_t>(n);
  }
  return true;
}

ReadResult UdsServer::ReadMessage(Message* out) {
  // Serve anything already buffered before touching the socket: one read() can
  // deliver several messages, and dropping the remainder would desync the stream.
  if (TryParse(out)) return ReadResult::kMessage;

  std::uint8_t chunk[65536];
  while (true) {
    const ssize_t n = ::read(client_fd_, chunk, sizeof(chunk));
    if (n == 0) return ReadResult::kClosed;
    if (n < 0) {
      if (errno == EINTR) continue;
      return ReadResult::kError;
    }
    buffer_.insert(buffer_.end(), chunk, chunk + n);
    if (TryParse(out)) return ReadResult::kMessage;
    if (desynced_) return ReadResult::kError;
  }
}

bool UdsServer::TryParse(Message* out) {
  if (buffer_.size() < kHeaderSize) return false;

  Header header;
  if (!ParseHeader(buffer_.data(), &header)) {
    // Fatal by design (spec §6): a desynced binary stream cannot be realigned with
    // confidence, and guessing would publish garbage video while reporting success.
    std::fprintf(stderr, "framing desync: bad magic/version/length; closing\n");
    desynced_ = true;
    return false;
  }

  const std::size_t total = kHeaderSize + header.length;
  if (buffer_.size() < total) return false;

  out->header = header;
  out->payload.assign(buffer_.begin() + kHeaderSize, buffer_.begin() + total);
  buffer_.erase(buffer_.begin(), buffer_.begin() + total);
  return true;
}

void UdsServer::CloseClient() {
  if (client_fd_ >= 0) {
    ::close(client_fd_);
    client_fd_ = -1;
  }
  buffer_.clear();
  desynced_ = false;
}

void UdsServer::Close() {
  CloseClient();
  if (listen_fd_ >= 0) {
    ::close(listen_fd_);
    listen_fd_ = -1;
  }
  if (!path_.empty()) {
    std::error_code ignored;
    std::filesystem::remove(path_, ignored);
  }
}

}  // namespace mc
