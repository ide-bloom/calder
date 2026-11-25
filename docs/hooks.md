# Capture hooks

`calder_capture32.dll` and `calder_capture64.dll` (C++20, Microsoft Detours, static CRT) are
injected only by `calder_launch32.exe` / `calder_launch64.exe`. The launcher reads the vendor
executable's bitness from its PE header (in Python), starts it suspended through
`DetourCreateProcessWithDllExW` with the DLL of the same bitness, prints `PID <n>` and resumes it.
The DLL exports ordinal 1 (`DetourFinishHelperProcess`) as Detours requires.

## Configuration

Through the environment the recorder sets for the child:

| Variable | Meaning |
|---|---|
| `CALDER_RING` | Name of the shared-memory ring. Without it the DLL does nothing. |
| `CALDER_FILTER` | `;`-separated wildcard patterns on resource identities (empty = all candidates) |
| `CALDER_TRANSPORTS` | `,`-separated transports to record |

## Event delivery

`native/capture/include/calder_ring.h` (mirrored by `src/calder/capture/ring.py`): a 64-byte header
(magic, version, capacity, monotonic write and read positions, dropped count, QPC frequency)
followed by a data region. Records carry a QPC timestamp, process and thread ids, transport,
handle, direction, status, JSON metadata and payload, padded to 8 bytes. Writers in the vendor
process are serialized by an SRW lock; when the ring is full a record is dropped and counted
rather than blocking the application. The recorder polls every 10 ms.

## Hooks

Every hook calls the real function first, logs only for tracked handles, and preserves
`GetLastError`/`WSAGetLastError`.

### Files, serial ports and pipes (kernel32/kernelbase)

| Hook | Behavior |
|---|---|
| `CreateFileW`, `CreateFileA` | Track `COMn` (`\\.\COMn`), `\\.\pipe\...` (serial transport; the test vendor app's stand-in for a COM port), HID paths (`hid#`) and WinUSB device paths (`usb#vid_`, remembered until `WinUsb_Initialize`), if the identity passes the filter; emit `open` |
| `CloseHandle` | `close` for tracked handles |
| `ReadFile`, `WriteFile` | `data`; overlapped reads are remembered by their `OVERLAPPED` |
| `ReadFileEx`, `WriteFileEx` | The same; the read's completion routine is wrapped to log the data |
| `GetOverlappedResult(Ex)`, `GetQueuedCompletionStatus(Ex)` | Complete remembered reads (including Winsock receives), so completion ports (.NET async I/O) are covered |
| `WaitCommEvent` | `signal` with the event mask |
| `DeviceIoControl` | Serial IOCTLs decoded: baud rate, line control, handflow, timeouts (`config`); DTR/RTS/break/purge (`signal`) |
| `SetCommState`, `SetCommTimeouts`, `EscapeCommFunction` | Decoded `config`/`signal`; the `DeviceIoControl` calls they make internally are suppressed |
| `LoadLibraryW/A`, `LoadLibraryExW/A` | Attach the hooks below when their DLL is loaded; only when the outermost load returns, because images still being processed by the loader must not be patched |
| `GetProcAddress` | The same, as a safety net before the application resolves a function |

### Winsock (ws2_32)

`connect`, `WSAConnect` (track sockets whose `tcp:<address>:<port>` identity passes the filter;
IPv4-mapped IPv6 addresses are reported as IPv4), `send`, `recv` (except `MSG_PEEK`), `WSASend`,
`WSARecv` (overlapped receives remembered), `WSAGetOverlappedResult`, `closesocket`.

### VISA (visa32.dll / visa64.dll)

`viOpen`/`viClose` (identity `visa:<resource>`), `viWrite`, `viRead`, `viBufWrite`, `viBufRead`,
`viSetAttribute` (serial attributes, timeout and termination character decoded). `viPrintf` and
`viVPrintf` render the formatted text with the library's own `viVSPrintf` on a copy of the
arguments and log it before the real call. `viQueryf`/`viVQueryf` log their write half the same
way. A thread-local depth counter keeps nested calls inside formatted I/O from being logged twice.

### FTDI D2XX (ftd2xx.dll)

`FT_Open`, `FT_OpenEx` (identity by serial number, description or location), `FT_Close`, `FT_Read`,
`FT_Write`, `FT_SetBaudRate`, `FT_SetDataCharacteristics`, `FT_SetFlowControl`, `FT_SetTimeouts`.

### WinUSB (winusb.dll)

`WinUsb_Initialize` (opens an interface for a tracked device path), `WinUsb_Free`,
`WinUsb_WritePipe`, `WinUsb_ReadPipe`, `WinUsb_ControlTransfer` (setup packet in metadata;
direction from `bmRequestType`).

### HID (hid.dll)

`HidD_SetFeature`, `HidD_GetFeature`, `HidD_SetOutputReport`, `HidD_GetInputReport`; interrupt
reports travel through `ReadFile`/`WriteFile` on the HID device handle.

Failed device calls (FTDI, WinUSB, HID) are reported as `error` events with the status.

## Known limits

- The read halves of `viScanf`, `viVScanf`, `viQueryf` and `viVQueryf` stay inside VISA's
  formatted-I/O buffer; they are reported as `signal` events with the format string and
  `bytes_unavailable: true`. Raw `viRead`/`viBufRead` traffic is captured completely.
- `ConnectEx` and `AcceptEx` (obtained through `WSAIoctl`) are not hooked.
- Child processes of the vendor application are not injected.
- Vendor software with its own kernel driver bypasses user-mode hooks; use USBPcap and
  `calder import`, or a passive serial tap.

## Testing

`native/capture/hooktest/hooktest.cpp` exercises every family against named pipes, a TCP server,
fake VISA and FTDI libraries (`native/capture/testlibs/`, built as `visa32/64.dll` and
`ftd2xx.dll`) and failing WinUSB/HID calls. `tests/capture/test_capture.py` runs it in both
bitnesses and records the WPF test vendor application.
