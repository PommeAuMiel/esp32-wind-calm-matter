"""Offline regressions for the dedicated SDK configuration safety gate.

The fixture comes from the project's saved defaults. No SDK installation,
compiler, serial port or hardware is used by these tests.
"""
import importlib.util
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("thread_sdk", ROOT / "tools/thread_sdk.py")
thread_sdk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(thread_sdk)


class ThreadSdkConfiguration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.defaults = (ROOT / "firmware/WindCalmThreadSDK/sdkconfig.defaults").read_text()

    def with_setting(self, name, value):
        """Change one real setting, or add an otherwise implicit IDF option."""
        key = "CONFIG_" + name
        pattern = re.compile(r"^" + re.escape(key) + r"=.*$", re.MULTILINE)
        replacement = key + "=" + value
        if pattern.search(self.defaults):
            changed, count = pattern.subn(lambda match: replacement, self.defaults)
            self.assertEqual(count, 1, "The saved defaults contain duplicate settings: " + key)
            return changed
        return self.defaults + "\n" + replacement + "\n"

    def without_setting(self, name):
        key = "CONFIG_" + name
        pattern = re.compile(r"^" + re.escape(key) + r"=.*\n?", re.MULTILINE)
        changed, count = pattern.subn("", self.defaults)
        self.assertEqual(count, 1, "The regression fixture must contain " + key)
        return changed

    def assert_rejected(self, source, setting):
        with self.assertRaisesRegex(ValueError, re.escape("CONFIG_" + setting)):
            thread_sdk.validate_config(source)

    def check_required_settings(self, settings):
        for setting in settings:
            with self.subTest(setting=setting, regression="deleted"):
                self.assert_rejected(self.without_setting(setting), setting)
            with self.subTest(setting=setting, regression="disabled"):
                self.assert_rejected(self.with_setting(setting, "n"), setting)

    def test_current_saved_defaults_are_accepted(self):
        thread_sdk.validate_config(self.defaults)

    def test_generated_not_set_comments_are_accepted(self):
        # ESP-IDF represents disabled bools this way in its generated sdkconfig.
        generated = re.sub(r"^(CONFIG_[A-Za-z0-9_]+)=n$", r"# \1 is not set",
                           self.defaults, flags=re.MULTILINE)
        thread_sdk.validate_config(generated)

    def test_required_thread_and_commissioning_features_cannot_disappear(self):
        self.check_required_settings((
            "ENABLE_MATTER_OVER_THREAD", "THREAD_NETWORK_COMMISSIONING_DRIVER",
            "OPENTHREAD_ENABLED", "OPENTHREAD_FTD", "ENABLE_CHIPOBLE", "BT_NIMBLE_ENABLED",
            "BT_NIMBLE_ROLE_PERIPHERAL", "BT_NIMBLE_ROLE_BROADCASTER", "BT_NIMBLE_GATT_SERVER",
            "ESP_COEX_SW_COEXIST_ENABLE", "LWIP_IPV6", "LWIP_HOOK_IP6_SELECT_SRC_ADDR_DEFAULT",
        ))

    def test_required_cryptography_cannot_disappear(self):
        self.check_required_settings((
            "MBEDTLS_AES_C", "MBEDTLS_CCM_C", "MBEDTLS_CMAC_C", "MBEDTLS_ECP_C",
            "MBEDTLS_ECDH_C", "MBEDTLS_ECDSA_C", "MBEDTLS_ECP_DP_SECP256R1_ENABLED",
            "MBEDTLS_HKDF_C", "MBEDTLS_SSL_PROTO_DTLS", "MBEDTLS_KEY_EXCHANGE_ECJPAKE",
            "BT_NIMBLE_SECURITY_ENABLE", "BT_NIMBLE_SM_SC",
            "BT_NIMBLE_LL_CFG_FEAT_LE_ENCRYPTION",
        ))

    def test_stack_safety_checks_cannot_disappear(self):
        self.check_required_settings((
            "FREERTOS_CHECK_STACKOVERFLOW_CANARY", "COMPILER_STACK_CHECK_MODE_NORM",
        ))

    def test_required_matter_clusters_cannot_disappear(self):
        # Root commissioning/security and the actual fan, light and mode model.
        self.check_required_settings(tuple("SUPPORT_" + cluster + "_CLUSTER" for cluster in (
            "ACCESS_CONTROL", "OPERATIONAL_CREDENTIALS", "ADMINISTRATOR_COMMISSIONING",
            "GENERAL_COMMISSIONING", "NETWORK_COMMISSIONING", "BASIC_INFORMATION",
            "GENERAL_DIAGNOSTICS", "GROUP_KEY_MANAGEMENT", "DESCRIPTOR",
            "THREAD_NETWORK_DIAGNOSTICS", "ON_OFF", "FAN_CONTROL", "MODE_SELECT",
            "GROUPS", "IDENTIFY", "SCENES", "BINDING", "LEVEL_CONTROL", "COLOR_CONTROL",
        )))

    def test_unused_or_unsafe_features_cannot_be_reenabled(self):
        for setting in (
            "ESP_WIFI_ENABLED", "ENABLE_WIFI_STATION", "WIFI_NETWORK_COMMISSIONING_DRIVER",
            "COMPILER_CXX_EXCEPTIONS", "COMPILER_CXX_RTTI", "BT_NIMBLE_ROLE_CENTRAL",
            "BT_NIMBLE_ROLE_OBSERVER", "BT_NIMBLE_GATT_CLIENT", "SECURITY_TEST_MODE",
            "TEST_EVENT_TRIGGER_ENABLED", "CHIP_ENABLE_PAIRING_AUTOSTART", "ENABLE_OTA_REQUESTOR",
            "USE_BLE_ONLY_FOR_COMMISSIONING", "USE_MINIMAL_MDNS",
        ):
            with self.subTest(setting=setting):
                self.assert_rejected(self.with_setting(setting, "y"), setting)

    def test_board_network_endpoint_clock_and_partitions_must_match(self):
        for setting, wrong in (
            ("THREAD_NETWORK_ENDPOINT_ID", "2"),
            ("ARDUINO_VARIANT", '"esp32c6"'),
            ("FREERTOS_HZ", "100"),
            ("PARTITION_TABLE_CUSTOM_FILENAME", '"partitions_singleapp.csv"'),
            ("ESP_MAIN_TASK_STACK_SIZE", "3584"),
            ("ESP_TIMER_TASK_STACK_SIZE", "3584"),
            ("ESP_IPC_TASK_STACK_SIZE", "1024"),
            ("FREERTOS_IDLE_TASK_STACKSIZE", "1536"),
            ("FREERTOS_ISR_STACKSIZE", "1536"),
            ("LWIP_TCPIP_TASK_STACK_SIZE", "3072"),
            ("BT_LE_CONTROLLER_TASK_STACK_SIZE", "4096"),
        ):
            with self.subTest(setting=setting, regression="changed"):
                self.assert_rejected(self.with_setting(setting, wrong), setting)
            with self.subTest(setting=setting, regression="deleted"):
                self.assert_rejected(self.without_setting(setting), setting)


if __name__ == "__main__":
    unittest.main()
