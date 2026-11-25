// Shared-memory event ring between the capture DLL and the Python recorder.
// The layout is mirrored in src/calder/capture/ring.py; change both together.
#pragma once

#include <cstdint>

namespace calder {

constexpr std::uint32_t kRingMagic = 0x474E5243;  // "CRNG"
constexpr std::uint32_t kRingVersion = 1;
constexpr std::uint32_t kRingHeaderSize = 64;

#pragma pack(push, 1)
struct RingHeader {
    std::uint32_t magic;
    std::uint32_t version;
    std::uint64_t capacity;          // bytes in the data region
    volatile std::int64_t write_pos; // monotonic byte counter, advanced by the writer
    volatile std::int64_t read_pos;  // monotonic byte counter, advanced by the recorder
    volatile std::int64_t dropped;   // records dropped because the ring was full
    std::uint64_t qpc_frequency;
    std::uint32_t writer_pid;
    std::uint32_t flags;
    std::uint8_t reserved[8];
};

struct RecordHeader {
    std::uint32_t size;  // header + meta + data, padded to a multiple of 8
    std::uint16_t kind;
    std::uint16_t transport;
    std::uint64_t qpc;
    std::uint32_t pid;
    std::uint32_t tid;
    std::uint64_t handle;
    std::uint32_t direction;
    std::int32_t status;
    std::uint32_t meta_len;
    std::uint32_t data_len;
};
#pragma pack(pop)

static_assert(sizeof(RingHeader) == kRingHeaderSize, "ring header layout");
static_assert(sizeof(RecordHeader) == 48, "record header layout");

enum class Kind : std::uint16_t { Open = 1, Close = 2, Data = 3, Config = 4, Signal = 5, Error = 6, Info = 7 };
enum class Transport : std::uint16_t { None = 0, Serial = 1, Visa = 2, Ftdi = 3, WinUsb = 4, Hid = 5, Tcp = 6 };
enum class Direction : std::uint32_t { None = 0, Tx = 1, Rx = 2 };

}  // namespace calder
