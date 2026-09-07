import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "full_path_speedtest.py"
SPEC = importlib.util.spec_from_file_location("full_path_speedtest", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class FullPathSpeedtestTests(unittest.TestCase):
    def test_existing_subscription_link_becomes_temporary_xhttp_client(self):
        link = (
            "vless://11111111-1111-1111-1111-111111111111@203.0.113.10:443"
            "?type=xhttp&security=tls&host=cdn.example.com&sni=cdn.example.com"
            "&path=%2Fexisting-path&mode=packet-up#node"
        )
        config = MODULE.xray_config(link, 19090)
        outbound = config["outbounds"][0]
        self.assertEqual("203.0.113.10", outbound["settings"]["vnext"][0]["address"])
        self.assertEqual("xhttp", outbound["streamSettings"]["network"])
        self.assertEqual("/existing-path", outbound["streamSettings"]["xhttpSettings"]["path"])
        self.assertEqual("127.0.0.1", config["inbounds"][0]["listen"])


if __name__ == "__main__":
    unittest.main()
