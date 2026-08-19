import unittest
import warnings
from types import SimpleNamespace
from unittest.mock import patch


from fastapi import HTTPException
from pydantic import ValidationError

from webui.app import CheckGCashReq, CheckPlusReq, api_check_gcash, api_check_plus, app


def _gcash_request(*, host="127.0.0.1", acknowledged=True, admin_token=""):
    headers = {}
    if acknowledged:
        headers["x-gcash-probe-confirmation"] = "checkout-side-effects-acknowledged"
    if admin_token:
        headers["x-gpt-admin-token"] = admin_token
    return SimpleNamespace(client=SimpleNamespace(host=host), headers=headers)


class _PlusResponse:
    status_code = 200
    text = ""

    @staticmethod
    def json():
        return {
            "accounts": {
                "default": {
                    "account": {"plan_type": "free", "is_deactivated": False},
                    "entitlement": {
                        "subscription_plan": "chatgptfreeplan",
                        "has_active_subscription": False,
                    },
                    "eligible_promo_campaigns": {},
                }
            }
        }


class _PlusSession:
    def __init__(self):
        self.get_kwargs = None
        self.get_count = 0

    def get(self, _url, **kwargs):
        self.get_count += 1
        self.get_kwargs = kwargs
        return _PlusResponse()

    def close(self):
        pass


class PlusEligibilityApiTests(unittest.TestCase):
    def test_accounts_check_never_follows_redirects_with_authorization_header(self):
        session = _PlusSession()
        credential = {"access_token": "access-token", "device_id": "device-id"}

        with patch("http_client.create_http_session", return_value=session), \
             patch("webui.app.db.get_registered", return_value=credential), \
             patch("webui.app.db.update_plus_check"):
            response = api_check_plus(CheckPlusReq(emails=["person@example.com"]))

        self.assertTrue(response["ok"])
        self.assertIs(session.get_kwargs["allow_redirects"], False)

    def test_normalizes_deduplicates_and_bounds_accounts(self):
        session = _PlusSession()
        credential = {"access_token": "access-token", "device_id": "device-id"}

        with patch("http_client.create_http_session", return_value=session), \
             patch("webui.app.db.get_registered", return_value=credential) as get_registered, \
             patch("webui.app.db.update_plus_check"):
            response = api_check_plus(CheckPlusReq(
                emails=[" Person@Example.com ", "person@example.com"]
            ))

        self.assertEqual(list(response["results"]), ["person@example.com"])
        get_registered.assert_called_once_with("person@example.com")
        self.assertEqual(session.get_count, 1)

        with self.assertRaises(ValidationError):
            CheckPlusReq(
                emails=[f"person-{index}@example.com" for index in range(51)]
            )


class GCashEligibilityApiTests(unittest.TestCase):
    def test_fastapi_route_accepts_confirmed_loopback_request(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from fastapi.testclient import TestClient

        client = TestClient(app, client=("127.0.0.1", 50000))

        with patch("webui.app.db.get_registered", return_value=None):
            response = client.post(
                "/api/registered/check_gcash",
                headers={
                    "X-GCash-Probe-Confirmation": "checkout-side-effects-acknowledged"
                },
                json={"emails": ["person@example.com"]},
            )

        self.assertEqual(response.status_code, 200)
        result = response.json()["results"]["person@example.com"]
        self.assertEqual(result["decision"], "account_not_found")

    def test_deduplicates_emails_and_returns_structured_results(self):
        credential = {
            "email": "person@example.com",
            "access_token": "access-token",
            "device_id": "device-id",
            "cookie_header": "session=cookie",
        }
        probe_result = {
            "classification": "eligible",
            "eligible": True,
            "conclusive": True,
            "decision": "gcash_zero_due_available",
            "status": "eligible",
            "label": "GCash eligible",
        }

        with patch("webui.app.db.get_registered", return_value=credential) as get_registered, \
             patch("webui.app.probe_gcash", return_value=probe_result) as probe, \
             patch("webui.app.db.update_eligibility_check") as persist:
            response = api_check_gcash(
                CheckGCashReq(
                    emails=[" Person@Example.com ", "person@example.com"],
                    proxy="http://proxy.example:8080",
                ),
                _gcash_request(),
            )

        self.assertTrue(response["ok"])
        self.assertEqual(response["summary"]["eligible"], 1)
        self.assertEqual(list(response["results"]), ["person@example.com"])
        get_registered.assert_called_once_with("person@example.com")
        self.assertEqual(probe.call_args.kwargs["proxy"], "http://proxy.example:8080")
        self.assertEqual(probe.call_args.kwargs["access_token"], "access-token")
        persist.assert_called_once_with("person@example.com", "gcash_check", probe_result)

    def test_missing_access_token_does_not_call_or_persist_probe(self):
        with patch("webui.app.db.get_registered", return_value={"email": "person@example.com"}), \
             patch("webui.app.probe_gcash") as probe, \
             patch("webui.app.db.update_eligibility_check") as persist:
            response = api_check_gcash(
                CheckGCashReq(emails=["person@example.com"]), _gcash_request()
            )

        result = response["results"]["person@example.com"]
        self.assertEqual(result["status"], "no_at")
        self.assertEqual(result["classification"], "unknown")
        probe.assert_not_called()
        persist.assert_not_called()

    def test_batch_size_is_bounded(self):
        with self.assertRaises(ValidationError):
            CheckGCashReq(emails=["person@example.com"] * 51)

        with self.assertRaises(ValidationError):
            CheckGCashReq(emails=["x" * 321])

    def test_one_probe_failure_does_not_abort_remaining_accounts(self):
        credentials = {
            "one@example.com": {"access_token": "one"},
            "two@example.com": {"access_token": "two"},
        }
        successful = {
            "classification": "ineligible",
            "eligible": False,
            "conclusive": True,
            "decision": "gcash_unavailable",
            "status": "ineligible",
            "label": "GCash unavailable",
        }

        def probe_side_effect(*, access_token, **_):
            if access_token == "one":
                raise RuntimeError("sensitive upstream detail")
            return successful

        with patch("webui.app.db.get_registered", side_effect=lambda email: credentials[email]), \
             patch("webui.app.probe_gcash", side_effect=probe_side_effect), \
             patch("webui.app.db.update_eligibility_check"):
            response = api_check_gcash(
                CheckGCashReq(emails=["one@example.com", "two@example.com"]),
                _gcash_request(),
            )

        first = response["results"]["one@example.com"]
        self.assertEqual(first["classification"], "unknown")
        self.assertEqual(first["decision"], "probe_unexpected_error")
        self.assertNotIn("sensitive upstream detail", repr(first))
        self.assertEqual(response["results"]["two@example.com"]["classification"], "ineligible")

    def test_requires_explicit_checkout_side_effect_acknowledgement(self):
        with patch("webui.app.db.get_registered") as get_registered, \
             self.assertRaises(HTTPException) as caught:
            api_check_gcash(
                CheckGCashReq(emails=["person@example.com"]),
                _gcash_request(acknowledged=False),
            )

        self.assertEqual(caught.exception.status_code, 403)
        get_registered.assert_not_called()

    def test_rejects_direct_non_loopback_clients(self):
        with patch("webui.app.db.get_registered") as get_registered, \
             self.assertRaises(HTTPException) as caught:
            api_check_gcash(
                CheckGCashReq(emails=["person@example.com"]),
                _gcash_request(host="192.0.2.10"),
            )

        self.assertEqual(caught.exception.status_code, 403)
        get_registered.assert_not_called()

    def test_non_loopback_reverse_proxy_requires_matching_admin_token(self):
        request = _gcash_request(host="192.0.2.10", admin_token="test-admin-token")

        with patch.dict(
            "os.environ", {"GPT_AUTO_REGISTER_ADMIN_TOKEN": "test-admin-token"}
        ), patch("webui.app.db.get_registered", return_value=None):
            response = api_check_gcash(
                CheckGCashReq(emails=["person@example.com"]), request
            )

        self.assertTrue(response["ok"])
        self.assertEqual(
            response["results"]["person@example.com"]["decision"],
            "account_not_found",
        )

    def test_non_loopback_reverse_proxy_rejects_wrong_admin_token(self):
        request = _gcash_request(host="192.0.2.10", admin_token="wrong-token")

        with patch.dict(
            "os.environ", {"GPT_AUTO_REGISTER_ADMIN_TOKEN": "test-admin-token"}
        ), patch("webui.app.db.get_registered") as get_registered, \
             self.assertRaises(HTTPException) as caught:
            api_check_gcash(
                CheckGCashReq(emails=["person@example.com"]), request
            )

        self.assertEqual(caught.exception.status_code, 403)
        get_registered.assert_not_called()


if __name__ == "__main__":
    unittest.main()
