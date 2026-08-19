import gc
import threading
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch


from webui import db


class EligibilityPersistenceTests(unittest.TestCase):
    def setUp(self):
        self._original_db_path = db.DB_PATH
        self._temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        db.DB_PATH = Path(self._temp_dir.name) / "test.db"
        db.init_db()
        db.save_registered({
            "email": "person@example.com",
            "access_token": "sensitive-access-token",
        })

    def tearDown(self):
        db.DB_PATH = self._original_db_path
        gc.collect()
        self._temp_dir.cleanup()

    def test_list_registered_exposes_plus_and_gcash_checks(self):
        db.update_eligibility_check(
            "person@example.com",
            "plus_check",
            {"classification": "eligible", "conclusive": True, "status": "plus_eligible"},
        )
        db.update_eligibility_check(
            "person@example.com",
            "gcash_check",
            {"classification": "eligible", "conclusive": True, "status": "eligible"},
        )

        row = db.list_registered()[0]

        self.assertEqual(row["plus_check"]["classification"], "eligible")
        self.assertEqual(row["gcash_check"]["classification"], "eligible")

    def test_unknown_attempt_preserves_last_conclusive_result(self):
        eligible = {
            "classification": "eligible",
            "conclusive": True,
            "eligible": True,
            "decision": "gcash_zero_due_available",
        }
        unknown = {
            "classification": "unknown",
            "conclusive": False,
            "eligible": None,
            "decision": "checkout_timeout",
        }
        db.update_eligibility_check("person@example.com", "gcash_check", eligible)
        db.update_eligibility_check("person@example.com", "gcash_check", unknown)

        check = db.list_registered()[0]["gcash_check"]

        self.assertEqual(check["classification"], "unknown")
        self.assertEqual(check["last_conclusive"]["classification"], "eligible")
        self.assertNotIn("sensitive-access-token", repr(check))

    def test_rejects_unknown_extra_json_key(self):
        with self.assertRaises(ValueError):
            db.update_eligibility_check(
                "person@example.com",
                "attacker_controlled_key",
                {"classification": "eligible", "conclusive": True},
            )

    def test_concurrent_plus_and_gcash_updates_do_not_overwrite_each_other(self):
        barrier = threading.Barrier(2)
        original_sanitizer = db._safe_eligibility_result

        def synchronized_sanitizer(value):
            safe = original_sanitizer(value)
            try:
                barrier.wait(timeout=0.25)
            except threading.BrokenBarrierError:
                # Once the database lock covers the whole read/merge/write cycle,
                # the second worker cannot reach this barrier concurrently.
                pass
            return safe

        with patch("webui.db._safe_eligibility_result", side_effect=synchronized_sanitizer):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(
                        db.update_eligibility_check,
                        "person@example.com",
                        "plus_check",
                        {"classification": "eligible", "conclusive": True},
                    ),
                    pool.submit(
                        db.update_eligibility_check,
                        "person@example.com",
                        "gcash_check",
                        {"classification": "ineligible", "conclusive": True},
                    ),
                ]
                for future in futures:
                    future.result(timeout=10)

        row = db.list_registered()[0]
        self.assertEqual(row["plus_check"]["classification"], "eligible")
        self.assertEqual(row["gcash_check"]["classification"], "ineligible")


if __name__ == "__main__":
    unittest.main()
