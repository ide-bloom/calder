cmake_minimum_required(VERSION 3.25)
project(calder_native LANGUAGES CXX)

# Build once per architecture: -A x64 produces the "64" binaries, -A Win32 the "32" ones.
if(CMAKE_SIZEOF_VOID_P EQUAL 8)
    set(CALDER_BITS 64)
else()
    set(CALDER_BITS 32)
endif()

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
# Static CRT: the capture DLL must not add runtime dependencies to the vendor process.
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
if(NOT CALDER_OUTPUT_DIR)
    set(CALDER_OUTPUT_DIR "${CMAKE_BINARY_DIR}/bin")
endif()
foreach(cfg IN ITEMS "" _DEBUG _RELEASE _RELWITHDEBINFO _MINSIZEREL)
    set(CMAKE_RUNTIME_OUTPUT_DIRECTORY${cfg} "${CALDER_OUTPUT_DIR}")
    set(CMAKE_LIBRARY_OUTPUT_DIRECTORY${cfg} "${CALDER_OUTPUT_DIR}")
endforeach()

find_path(DETOURS_INCLUDE_DIRS "detours/detours.h" REQUIRED)
# vcpkg installs release and debug builds side by side; pick per configuration.
get_filename_component(DETOURS_ROOT "${DETOURS_INCLUDE_DIRS}" DIRECTORY)
set(DETOURS_LIBRARY
    "$<$<CONFIG:Debug>:${DETOURS_ROOT}/debug/lib/detours.lib>$<$<NOT:$<CONFIG:Debug>>:${DETOURS_ROOT}/lib/detours.lib>")

add_compile_definitions(UNICODE _UNICODE _CRT_SECURE_NO_WARNINGS)
add_compile_options(/W4 /permissive- /utf-8 /EHsc)

add_library(calder_capture SHARED
    capture/src/common.cpp
    capture/src/dllmain.cpp
    capture/src/hooks_file.cpp
    capture/src/hooks_winsock.cpp
    capture/src/hooks_devices.cpp
    capture/src/capture.def)
set_target_properties(calder_capture PROPERTIES OUTPUT_NAME "calder_capture${CALDER_BITS}")
target_include_directories(calder_capture PRIVATE capture/include ${DETOURS_INCLUDE_DIRS})
target_link_libraries(calder_capture PRIVATE ${DETOURS_LIBRARY} ws2_32)

add_executable(calder_launch capture/launcher/launcher.cpp)
set_target_properties(calder_launch PROPERTIES OUTPUT_NAME "calder_launch${CALDER_BITS}")
target_include_directories(calder_launch PRIVATE ${DETOURS_INCLUDE_DIRS})
target_link_libraries(calder_launch PRIVATE ${DETOURS_LIBRARY})

option(CALDER_BUILD_TEST_TOOLS "Build the fake VISA/FTDI libraries and the hook test program" ON)
if(CALDER_BUILD_TEST_TOOLS)
    add_library(fake_visa SHARED capture/testlibs/fake_visa.cpp capture/testlibs/fake_visa.def)
    set_target_properties(fake_visa PROPERTIES OUTPUT_NAME "visa${CALDER_BITS}")
    add_library(fake_ftd2xx SHARED capture/testlibs/fake_ftd2xx.cpp capture/testlibs/fake_ftd2xx.def)
    set_target_properties(fake_ftd2xx PROPERTIES OUTPUT_NAME "ftd2xx")
    # Real drivers export distinct functions; keep the fakes that way too.
    target_link_options(fake_visa PRIVATE /OPT:NOICF)
    target_link_options(fake_ftd2xx PRIVATE /OPT:NOICF)
    add_executable(calder_hooktest capture/hooktest/hooktest.cpp)
    set_target_properties(calder_hooktest PROPERTIES OUTPUT_NAME "calder_hooktest${CALDER_BITS}")
    target_link_libraries(calder_hooktest PRIVATE ws2_32)
endif()
