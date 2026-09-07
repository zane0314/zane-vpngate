import unittest
from unittest import mock

import vpn_utils


class IpReputationTests(unittest.TestCase):
    def test_weighted_score_uses_netcoffee_and_cleanip_roles(self):
        def response(url, timeout=8):
            if "cleanip.io" in url:
                return {
                    "purity": {
                        "score": 97, "grade": "A+", "confidence": 0.91,
                        "residential_probability": 0.88,
                        "dimensions": {"abuse": 90, "blacklist": 80, "attack": 100, "anonymity": 90},
                    },
                    "risk": {"is_vpn": True, "is_proxy": False, "is_datacenter": False, "risk_score": 4},
                    "geo": {"country_code": "JP"},
                    "network": {"asn_type": "isp"},
                }
            if "ip.net.coffee" in url:
                return {"countryCode": "jp", "isResidential": True, "is_datacenter": False, "asn_kind": "isp", "asn": 64500}
            return None

        with mock.patch.object(vpn_utils, "_read_json_url", side_effect=response):
            result = vpn_utils.score_ip_reputation("203.0.113.8")
        self.assertEqual(86, result["ip_quality_score"])
        self.assertEqual("weighted_netcoffee_cleanip", result["score_mode"])
        self.assertEqual(
            {"asn_isp": 0.40, "history_reputation": 0.30, "proxy_detection": 0.20, "single_site_score": 0.10},
            result["score_weights"],
        )
        self.assertEqual(100, result["score_components"]["asn_isp"])
        self.assertEqual(50, result["score_components"]["proxy_detection"])
        self.assertEqual("residential", result["ip_type"])
        self.assertTrue(result["risk_free"])
        self.assertIn("vpn_history", result["risk_flags"])

    def test_proxy_history_is_scored_instead_of_structural_rejection(self):
        cleanip = {
            "purity": {"score": 92, "residential_probability": 95},
            "risk": {"is_proxy": True},
            "geo": {"country_code": "JP"},
            "network": {"asn_type": "isp"},
        }
        netcoffee = {"countryCode": "jp", "isResidential": True, "asn_kind": "isp"}
        with mock.patch.object(
            vpn_utils,
            "_read_json_url",
            side_effect=lambda url, timeout=8: cleanip if "cleanip.io" in url else netcoffee,
        ):
            result = vpn_utils.score_ip_reputation("203.0.113.9")
        self.assertTrue(result["risk_free"])
        self.assertEqual("residential", result["ip_type"])
        self.assertEqual(0, result["score_components"]["proxy_detection"])

    def test_hosting_asn_is_a_hard_rejection(self):
        netcoffee = {"countryCode": "jp", "isResidential": False, "is_datacenter": True, "asn_kind": "hosting"}
        with mock.patch.object(
            vpn_utils,
            "_read_json_url",
            side_effect=lambda url, timeout=8: netcoffee if "ip.net.coffee" in url else None,
        ):
            result = vpn_utils.score_ip_reputation("203.0.113.10")
        self.assertFalse(result["risk_free"])
        self.assertEqual(0, result["score_components"]["asn_isp"])

    def test_ipv6_is_rejected_without_external_calls(self):
        with mock.patch.object(vpn_utils, "_read_json_url") as request:
            result = vpn_utils.score_ip_reputation("2001:db8::1")
        self.assertEqual(["ipv6_not_allowed"], result["risk_flags"])
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
