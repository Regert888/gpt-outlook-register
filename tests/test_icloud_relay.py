import unittest
from unittest.mock import patch

from mail_providers.icloud_relay import (
    ICloudRelayProvider,
    _discover_endpoints,
    _extract_credentials,
    parse_relay_html,
)


class ICloudRelayDiscoveryTests(unittest.TestCase):
    def test_discovers_inline_api_path_with_query_string(self):
        html = """
        <script>
          fetch('/api/pickup/messages?limit=20', {
            headers: {Authorization: 'Bearer ' + key}
          });
        </script>
        """

        endpoints = _discover_endpoints(
            html, "https://relay.example/pickup#email=user%40icloud.com&key=secret"
        )

        self.assertEqual(endpoints, ["https://relay.example/api/pickup/messages"])

    def test_extracts_credentials_from_fragment_and_parses_relay_json(self):
        url = "https://relay.example/pickup#email=user%40icloud.com&key=secret"
        self.assertEqual(
            _extract_credentials(url),
            {"email": "user@icloud.com", "key": "secret"},
        )

        payload = '{"messages":[{"id":"m1","from":"no-reply@openai.com",' \
                  '"subject":"Your code is 123456","date":"2026-08-20T00:00:00Z",' \
                  '"preview":"Your verification code is 123456"}]}'
        messages = parse_relay_html(payload)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["otp"], "123456")

    def test_wait_accepts_recent_otp_already_present_in_initial_snapshot(self):
        provider = ICloudRelayProvider(
            "user@icloud.com",
            "https://relay.example/pickup#email=user%40icloud.com&key=secret",
        )
        message = {
            "uid": "new-message",
            "sender": "no-reply@openai.com",
            "subject": "Verification code",
            "body": "",
            "date_str": "2026-08-20 00:00:00",
            "ts": 1_000.0,
            "otp": "123456",
            "layout": "scan-json",
        }
        provider._messages = lambda: [message]

        with patch(
            "mail_providers.icloud_relay.time.time",
            side_effect=[1_000.0, 1_000.0, 1_061.0],
        ), patch("mail_providers.icloud_relay.time.sleep"):
            otp = provider.wait_for_otp(
                "user@icloud.com", timeout=60, issued_after=1_000.0
            )

        self.assertEqual(otp, "123456")

    def test_provider_loads_query_endpoint_without_revealing_credentials(self):
        provider = ICloudRelayProvider(
            "user@icloud.com",
            "https://relay.example/pickup#email=user%40icloud.com&key=secret",
        )
        provider._fetch = lambda: "<script>fetch('/api/pickup/messages?limit=20')</script>"
        provider._fetch_text = lambda _url: ""
        provider._try_api = lambda url, limit=50: (
            '{"messages":[{"id":"m1","from":"no-reply@openai.com",'
            '"subject":"Verification code","date":"2026-08-20T00:00:00Z",'
            '"preview":"Code: 123456"}]}'
            if url.endswith("/api/pickup/messages")
            else ""
        )

        messages = provider._load()

        self.assertEqual(provider._source, "https://relay.example/api/pickup/messages")
        self.assertEqual(messages[0]["otp"], "123456")


if __name__ == "__main__":
    unittest.main()
