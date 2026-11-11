idf_component_register(SRCS "main.cpp" "../../WindCalmController/Application.cpp" INCLUDE_DIRS "." "../../WindCalmController" REQUIRES arduino espressif__esp_matter)
# Arduino's component emits ARDUINO_XIAO_ESP32C6_DEV; match the sketch's
# Arduino-CLI board macro as well. The actual variant is separately validated.
target_compile_definitions(${COMPONENT_LIB} PRIVATE ARDUINO_XIAO_ESP32C6=1)
