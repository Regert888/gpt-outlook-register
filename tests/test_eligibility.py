import unittest


from eligibility import (
    PLUS_TRIAL_CAMPAIGN_ID,
    parse_plus_eligibility,
    plus_probe_error,
    redact_sensitive_text,
)


def _accounts_payload(*, campaign_id=PLUS_TRIAL_CAMPAIGN_ID, plan="free", active=False):
    campaigns = {}
    if campaign_id:
        campaigns = {
            "plus": {
                "id": campaign_id,
                "metadata": {
                    "title": "One month of Plus",
                    "discount": {"percentage": 100},
                    "duration": {"num_periods": 1, "period": "month"},
                },
            }
        }
    return {
        "accounts": {
            "account-a": {
                "account": {"plan_type": plan, "is_deactivated": False},
                "entitlement": {
                    "subscription_plan": "chatgptplusplan" if active else "chatgptfreeplan",
                    "has_active_subscription": active,
                },
                "eligible_promo_campaigns": campaigns,
            }
        }
    }


class PlusEligibilityTests(unittest.TestCase):
    def test_exact_campaign_is_eligible_and_exposes_safe_metadata(self):
        result = parse_plus_eligibility(_accounts_payload(), account_id="account-a", checked_at=123.0)

        self.assertEqual(result["classification"], "eligible")
        self.assertTrue(result["eligible"])
        self.assertTrue(result["conclusive"])
        self.assertEqual(result["decision"], "plus_1_month_free_available")
        self.assertEqual(result["campaign_id"], PLUS_TRIAL_CAMPAIGN_ID)
        self.assertEqual(result["discount_percentage"], 100)
        self.assertEqual(result["duration_periods"], 1)
        self.assertEqual(result["duration_unit"], "month")
        self.assertEqual(result["checked_at"], 123.0)
        self.assertEqual(result["status"], "plus_eligible")
        self.assertEqual(result["label"], "Plus trial eligible")

    def test_different_plus_campaign_is_not_eligible(self):
        result = parse_plus_eligibility(_accounts_payload(campaign_id="different-offer"))

        self.assertEqual(result["classification"], "ineligible")
        self.assertFalse(result["eligible"])
        self.assertEqual(result["decision"], "campaign_not_available")
        self.assertEqual(result["campaign_id"], "different-offer")

    def test_active_subscription_is_reported_separately(self):
        result = parse_plus_eligibility(_accounts_payload(plan="plus", active=True))

        self.assertEqual(result["classification"], "ineligible")
        self.assertEqual(result["decision"], "active_subscription")
        self.assertEqual(result["status"], "plus_active")
        self.assertEqual(result["label"], "Plus active")

    def test_free_account_without_campaign_is_conclusively_ineligible(self):
        result = parse_plus_eligibility(_accounts_payload(campaign_id=""))

        self.assertEqual(result["classification"], "ineligible")
        self.assertEqual(result["status"], "free")
        self.assertEqual(result["decision"], "campaign_not_available")

    def test_deactivated_account_is_conclusively_ineligible(self):
        payload = _accounts_payload()
        payload["accounts"]["account-a"]["account"]["is_deactivated"] = True

        result = parse_plus_eligibility(payload, account_id="account-a")

        self.assertEqual(result["classification"], "ineligible")
        self.assertEqual(result["decision"], "account_deactivated")
        self.assertEqual(result["status"], "banned")

    def test_missing_accounts_is_unknown_instead_of_ineligible(self):
        result = parse_plus_eligibility({"unexpected": True})

        self.assertEqual(result["classification"], "unknown")
        self.assertIsNone(result["eligible"])
        self.assertFalse(result["conclusive"])
        self.assertTrue(result["retryable"])
        self.assertEqual(result["decision"], "accounts_missing")

    def test_account_entry_without_plan_evidence_is_unknown(self):
        result = parse_plus_eligibility({
            "accounts": {
                "default": {
                    "account": {},
                    "entitlement": {},
                    "eligible_promo_campaigns": {},
                }
            }
        })

        self.assertEqual(result["classification"], "unknown")
        self.assertEqual(result["decision"], "plan_evidence_missing")
        self.assertTrue(result["retryable"])

    def test_requested_account_id_never_falls_back_to_another_account(self):
        payload = _accounts_payload()

        result = parse_plus_eligibility(payload, account_id="account-missing")

        self.assertEqual(result["classification"], "unknown")
        self.assertEqual(result["decision"], "account_entry_missing")

    def test_probe_error_is_structured_and_does_not_echo_secret_text(self):
        result = plus_probe_error(
            "proxy_transport_error",
            retryable=True,
            status="error",
            label="Check failed",
            checked_at=456.0,
        )

        self.assertEqual(result["classification"], "unknown")
        self.assertEqual(result["decision"], "proxy_transport_error")
        self.assertTrue(result["retryable"])
        self.assertEqual(result["checked_at"], 456.0)
        self.assertNotIn("error", result)

    def test_log_sanitizer_removes_proxy_and_bearer_credentials(self):
        sanitized = redact_sensitive_text(
            "failed via http://proxy-user:proxy-pass@example.test:8080 "
            "Authorization: Bearer access-token-secret Cookie: session=secret-cookie"
        )

        self.assertNotIn("proxy-pass", sanitized)
        self.assertNotIn("access-token-secret", sanitized)
        self.assertNotIn("secret-cookie", sanitized)
        self.assertIn("[REDACTED]", sanitized)


if __name__ == "__main__":
    unittest.main()
