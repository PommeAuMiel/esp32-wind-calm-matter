cmake_minimum_required(VERSION 3.16)
include("${CMAKE_CURRENT_LIST_DIR}/version.cmake")
set(EXTRA_COMPONENT_DIRS "${CMAKE_CURRENT_LIST_DIR}/../../.local/dedicated/arduino")
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(windcalm-thread-ota)

# Match Arduino platform.txt's controller-memory bookkeeping and panic hook.
# The component's CMake omits these wrappers; never free BLE memory twice.
target_link_options(${PROJECT_NAME}.elf PRIVATE
    "-Wl,--wrap=esp_bt_mem_release" "-Wl,--wrap=esp_bt_controller_mem_release"
    "-Wl,--wrap=esp_panic_handler")

# Match the Arduino 3.3.12 builder's GCC14 / Matter 1.5.1 compatibility patch.
idf_component_get_property(matter_library espressif__esp_matter COMPONENT_LIB)
target_compile_options(${matter_library} PRIVATE
    "SHELL:-include \"${CMAKE_CURRENT_LIST_DIR}/patches/matter_closure_patch.h\"")
idf_build_set_property(CXX_COMPILE_OPTIONS "-std=gnu++2a;-DCHIP_HAVE_CONFIG_H" APPEND)
idf_build_set_property(COMPILE_DEFINITIONS
    "ARDUINO_USB_MODE=1;ARDUINO_USB_CDC_ON_BOOT=1;CORE_DEBUG_LEVEL=0" APPEND)
