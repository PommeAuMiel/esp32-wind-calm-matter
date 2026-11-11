// SPDX-License-Identifier: MIT
#include "Application.h"
#include <esp_app_desc.h>
void setup() { windcalm::application::begin({esp_app_get_description()->version, nullptr}); }
void loop() { windcalm::application::tick(); }
