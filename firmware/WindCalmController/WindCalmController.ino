// SPDX-License-Identifier: MIT
#include "Application.h"
void setup() { windcalm::application::begin({"controller-dev", nullptr}); }
void loop() { windcalm::application::tick(); }
