import unittest


from gcash_probe import classify_gcash_evidence, probe_gcash


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []
        self.closed = False

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.responses.pop(0)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.responses.pop(0)

    def close(self):
        self.closed = True


def _gcash_payload(amount=0, currency="php"):
    return {
        "total_summary": {"due": amount},
        "currency": currency,
        "custom_payment_method_data": [
            {"id": "cpmt_live_discovered", "display_name": "GCash"}
        ],
    }


class GCashClassificationTests(unittest.TestCase):
    def test_explicit_gcash_zero_due_php_is_eligible(self):
        result = classify_gcash_evidence([_gcash_payload()], require_zero=True, checked_at=10.0)

        self.assertEqual(result["classification"], "eligible")
        self.assertTrue(result["eligible"])
        self.assertEqual(result["decision"], "gcash_zero_due_available")
        self.assertEqual(result["amount_minor"], 0)
        self.assertEqual(result["currency"], "PHP")
        self.assertTrue(result["method_available"])
        self.assertEqual(result["status"], "eligible")
        self.assertEqual(result["label"], "GCash eligible")

    def test_nonzero_offer_is_conclusively_ineligible(self):
        result = classify_gcash_evidence([_gcash_payload(amount=115000)])

        self.assertEqual(result["classification"], "ineligible")
        self.assertFalse(result["eligible"])
        self.assertEqual(result["decision"], "nonzero_offer")

    def test_wrong_currency_is_conclusively_ineligible(self):
        result = classify_gcash_evidence([_gcash_payload(currency="usd")])

        self.assertEqual(result["classification"], "ineligible")
        self.assertEqual(result["decision"], "currency_mismatch")

    def test_explicit_custom_method_list_without_gcash_is_ineligible(self):
        payload = {
            "total_summary": {"due": 0},
            "currency": "php",
            "custom_payment_method_data": [{"id": "cpmt_other", "display_name": "Other"}],
        }

        result = classify_gcash_evidence([payload])

        self.assertEqual(result["classification"], "ineligible")
        self.assertEqual(result["decision"], "gcash_unavailable")

    def test_missing_amount_is_unknown_when_zero_is_required(self):
        payload = {
            "currency": "php",
            "custom_payment_method_data": [
                {"id": "cpmt_discovered", "display_name": "GCash"}
            ],
        }

        result = classify_gcash_evidence([payload])

        self.assertEqual(result["classification"], "unknown")
        self.assertIsNone(result["eligible"])
        self.assertEqual(result["decision"], "amount_unknown")
        self.assertTrue(result["retryable"])

    def test_conflicting_amounts_are_unknown(self):
        result = classify_gcash_evidence([_gcash_payload(0), _gcash_payload(100)])

        self.assertEqual(result["classification"], "unknown")
        self.assertEqual(result["decision"], "conflicting_amount_evidence")


class GCashNetworkProbeTests(unittest.TestCase):
    def test_rejects_checkout_session_path_injection(self):
        session = FakeSession([
            FakeResponse({
                "checkout_session_id": "cs_valid/../../confirm",
                "processor_entity": "openai_ie",
            })
        ])

        result = probe_gcash(
            "access-token",
            session_factory=lambda **_: session,
        )

        self.assertEqual(result["classification"], "unknown")
        self.assertEqual(result["decision"], "checkout_session_invalid")
        self.assertEqual(len(session.calls), 1)

    def test_opaque_custom_method_id_is_discovered_then_verified_by_stripe(self):
        checkout = {
            "checkout_session_id": "cs_test_dynamic_method",
            "processor_entity": "openai_ie",
            "publishable_key": "pk_test_dynamic",
            "customer_session_client_secret": "cuss_dynamic",
            "custom_payment_methods": ["cpmt_dynamic_from_checkout"],
            "total_summary": {"due": 0},
            "currency": "php",
        }
        session = FakeSession([
            FakeResponse(checkout),
            FakeResponse({"total_summary": {"due": 0}, "currency": "php"}),
            FakeResponse({"total_summary": {"due": 0}, "currency": "php"}),
            FakeResponse({
                "custom_payment_methods": ["cpmt_dynamic_from_checkout"],
                "total_summary": {"due": 0},
                "currency": "php",
            }),
            FakeResponse({
                "custom_payment_method_data": [
                    {"id": "cpmt_dynamic_from_checkout", "display_name": "GCash"}
                ],
                "total_summary": {"due": 0},
                "currency": "php",
            }),
        ])

        result = probe_gcash(
            "access-token",
            session_factory=lambda **_: session,
        )

        self.assertEqual(result["classification"], "eligible")
        self.assertEqual(len(session.calls), 5)
        stripe_call = session.calls[-1]
        self.assertEqual(stripe_call[1], "https://api.stripe.com/v1/elements/sessions")
        self.assertEqual(
            stripe_call[2]["params"]["custom_payment_methods[0]"],
            "cpmt_dynamic_from_checkout",
        )

    def test_failed_promotion_update_is_inconclusive_and_stops_the_probe(self):
        checkout = {
            "checkout_session_id": "cs_test_promotion_failure",
            "processor_entity": "openai_ie",
            **_gcash_payload(),
        }
        session = FakeSession([
            FakeResponse(checkout),
            FakeResponse({"message": "temporary"}, status_code=503),
            FakeResponse(_gcash_payload()),
            FakeResponse(_gcash_payload()),
        ])

        result = probe_gcash(
            "access-token",
            session_factory=lambda **_: session,
        )

        self.assertEqual(result["classification"], "unknown")
        self.assertEqual(result["decision"], "promotion_update_http_503")
        self.assertTrue(result["retryable"])
        self.assertEqual(len(session.calls), 2)

    def test_tax_failure_is_best_effort_when_resolve_has_conclusive_evidence(self):
        checkout = {
            "checkout_session_id": "cs_test_optional_tax",
            "processor_entity": "openai_ie",
            **_gcash_payload(),
        }
        session = FakeSession([
            FakeResponse(checkout),
            FakeResponse({"total_summary": {"due": 0}, "currency": "php"}),
            FakeResponse({"message": "temporary"}, status_code=503),
            FakeResponse(_gcash_payload()),
        ])

        result = probe_gcash(
            "access-token",
            session_factory=lambda **_: session,
        )

        self.assertEqual(result["classification"], "eligible")
        self.assertEqual(len(session.calls), 4)

    def test_probe_stops_before_any_payment_execution_endpoint(self):
        checkout = {
            "checkout_session_id": "cs_test_safe_probe",
            "processor_entity": "openai_ie",
            "publishable_key": "pk_test_safe",
            "customer_session_client_secret": "cuss_secret_not_returned",
            **_gcash_payload(),
        }
        update = {"total_summary": {"due": 0}, "currency": "php"}
        taxes = {"total_summary": {"due": 0}, "currency": "php"}
        resolved = _gcash_payload()
        stripe = _gcash_payload()
        session = FakeSession([
            FakeResponse(checkout),
            FakeResponse(update),
            FakeResponse(taxes),
            FakeResponse(resolved),
            FakeResponse(stripe),
        ])
        factory_calls = []

        def factory(*, proxy=None, impersonate=""):
            factory_calls.append({"proxy": proxy, "impersonate": impersonate})
            return session

        result = probe_gcash(
            "access-token-secret",
            account_id="account-a",
            device_id="device-a",
            cookie_header="session=secret-cookie",
            proxy="socks5://proxy-user:proxy-pass@example.test:1080",
            session_factory=factory,
            checked_at=99.0,
        )

        self.assertEqual(result["classification"], "eligible")
        self.assertEqual(factory_calls[0]["proxy"], "socks5://proxy-user:proxy-pass@example.test:1080")
        self.assertTrue(session.closed)
        urls = [url for _, url, _ in session.calls]
        self.assertEqual(len(urls), 5)
        self.assertTrue(any(url.endswith("/payments/checkout") for url in urls))
        self.assertTrue(any(url.endswith("/payments/checkout/update") for url in urls))
        self.assertTrue(any(url.endswith("/payments/checkout/taxes") for url in urls))
        self.assertTrue(any("/payments/checkout/openai_ie/cs_test_safe_probe" in url for url in urls))
        self.assertTrue(any(url == "https://api.stripe.com/v1/elements/sessions" for url in urls))
        self.assertFalse(any("confirm" in url or "custom_payment_method/start" in url for url in urls))
        self.assertTrue(all(call[2]["allow_redirects"] is False for call in session.calls))
        serialized = repr(result)
        self.assertNotIn("access-token-secret", serialized)
        self.assertNotIn("secret-cookie", serialized)
        self.assertNotIn("cuss_secret_not_returned", serialized)
        self.assertNotIn("cs_test_safe_probe", serialized)
        self.assertNotIn("proxy-pass", serialized)

    def test_transport_error_is_unknown_and_redacted(self):
        class BrokenSession(FakeSession):
            def post(self, url, **kwargs):
                raise RuntimeError(
                    "connect failed via http://user:password@proxy.example and Bearer token-secret"
                )

        session = BrokenSession([])
        result = probe_gcash(
            "token-secret",
            proxy="http://user:password@proxy.example",
            session_factory=lambda **_: session,
        )

        self.assertEqual(result["classification"], "unknown")
        self.assertEqual(result["decision"], "checkout_transport_error")
        self.assertTrue(result["retryable"])
        self.assertNotIn("password", repr(result))
        self.assertNotIn("token-secret", repr(result))


if __name__ == "__main__":
    unittest.main()
