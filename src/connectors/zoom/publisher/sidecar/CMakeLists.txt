cmake_minimum_required(VERSION 3.22)
project(mc_zoom_sidecar CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

# Path to an unpacked Zoom Meeting SDK for Linux. When absent the sidecar builds
# against the local declarations in zoom_sdk_shim.h — no meeting joining, but the IPC,
# framing, threading and pacing paths are all real and testable. That is what lets the
# frozen wire protocol be verified from both sides before the SDK is installed.
set(ZOOM_SDK_ROOT "" CACHE PATH "Zoom Meeting SDK for Linux root directory")

add_executable(mc_zoom_sidecar
  src/main.cpp
  src/uds_server.cpp
)

target_include_directories(mc_zoom_sidecar PRIVATE include)

target_compile_options(mc_zoom_sidecar PRIVATE
  -Wall -Wextra -Wpedantic -Wshadow -Wnon-virtual-dtor
)

find_package(Threads REQUIRED)
target_link_libraries(mc_zoom_sidecar PRIVATE Threads::Threads)

if(ZOOM_SDK_ROOT)
  if(NOT EXISTS "${ZOOM_SDK_ROOT}/h/zoom_sdk.h")
    message(FATAL_ERROR
      "ZOOM_SDK_ROOT=${ZOOM_SDK_ROOT} does not contain h/zoom_sdk.h. "
      "Point it at an unpacked Meeting SDK for Linux.")
  endif()
  message(STATUS "Building WITH the Zoom Meeting SDK at ${ZOOM_SDK_ROOT}")
  target_compile_definitions(mc_zoom_sidecar PRIVATE MC_WITH_ZOOM_SDK)
  target_include_directories(mc_zoom_sidecar PRIVATE
    "${ZOOM_SDK_ROOT}/h"
    "${ZOOM_SDK_ROOT}/h/rawdata"
  )
  target_link_directories(mc_zoom_sidecar PRIVATE "${ZOOM_SDK_ROOT}")
  target_link_libraries(mc_zoom_sidecar PRIVATE meetingsdk)
  set_target_properties(mc_zoom_sidecar PROPERTIES
    BUILD_RPATH "${ZOOM_SDK_ROOT}"
    INSTALL_RPATH "${ZOOM_SDK_ROOT}"
  )
else()
  message(WARNING
    "ZOOM_SDK_ROOT not set — building in STUB mode. "
    "The sidecar will speak the IPC protocol correctly but will NOT join meetings. "
    "Set -DZOOM_SDK_ROOT=/path/to/zoom-meeting-sdk-linux for a real build.")
endif()

install(TARGETS mc_zoom_sidecar RUNTIME DESTINATION bin)
