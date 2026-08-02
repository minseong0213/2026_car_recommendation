import unittest

from warning_diagnostics import diagnose_warning, normalize_dtc_code


class WarningDiagnosticsTests(unittest.TestCase):
    def test_known_code_returns_five_ranked_causes(self):
        result = diagnose_warning("p0300", {"rpm": 812, "coolantC": 88})

        self.assertEqual(result["dtc_code"], "P0300")
        self.assertEqual(result["warning_light"], "엔진 경고등")
        self.assertEqual(len(result["causes"]), 5)
        self.assertEqual(result["causes"][0]["title"], "점화 코일 성능 저하")
        self.assertEqual(result["pid_values"]["rpm"], 812)

    def test_unknown_code_uses_generic_diagnosis(self):
        result = diagnose_warning(" u9999 ")

        self.assertEqual(normalize_dtc_code(" u9999 "), "U9999")
        self.assertEqual(result["dtc_code"], "U9999")
        self.assertEqual(len(result["causes"]), 5)

    def test_voltage_code_uses_battery_warning(self):
        result = diagnose_warning("P0562")

        self.assertEqual(result["warning_light"], "배터리 경고등")
        self.assertEqual(result["causes"][0]["title"], "발전기 충전 성능 저하")
        self.assertEqual(len(result["causes"]), 5)


if __name__ == "__main__":
    unittest.main()
