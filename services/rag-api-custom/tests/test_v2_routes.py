import unittest

from bauer_rag_v2.routes import _display_label


class V2RouteTests(unittest.TestCase):
    def test_display_label_normalizes_machine_separators(self):
        self.assertEqual(
            _display_label("## Page 4 | Use | Order number"),
            "Page 4 / Use / Order number",
        )
        self.assertEqual(
            _display_label("2024-01_B-DETECTION_Sensor_calibration_EN"),
            "2024-01 B-DETECTION Sensor calibration EN",
        )


if __name__ == "__main__":
    unittest.main()
