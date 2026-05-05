import hashlib
import hmac
import json
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "salon_missed_call_assistant"))

import app as salon_app


class SalonCoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.old_db_path = salon_app.DB_PATH
        self.old_secret = salon_app.WEBHOOK_SECRET
        self.old_require_secret = salon_app.REQUIRE_WEBHOOK_SECRET
        salon_app.DB_PATH = Path(self.temp_dir.name) / "salon_test.sqlite3"
        salon_app.WEBHOOK_SECRET = ""
        salon_app.REQUIRE_WEBHOOK_SECRET = False
        salon_app.init_db()

    def tearDown(self):
        salon_app.DB_PATH = self.old_db_path
        salon_app.WEBHOOK_SECRET = self.old_secret
        salon_app.REQUIRE_WEBHOOK_SECRET = self.old_require_secret
        self.temp_dir.cleanup()

    def salon_ids(self):
        salons = salon_app.salons_df(active_only=True)
        return int(salons.iloc[0]["id"]), int(salons.iloc[1]["id"])

    def test_same_client_phone_can_exist_in_two_salons(self):
        sid1, sid2 = self.salon_ids()
        phone = "+15551230000"
        first = salon_app.create_missed_call("Client One", phone, salon_id=sid1)
        second = salon_app.create_missed_call("Client Two", phone, salon_id=sid2)

        self.assertEqual(salon_app.salon_id_for_conversation(first), sid1)
        self.assertEqual(salon_app.salon_id_for_conversation(second), sid2)
        rows = salon_app.load_df(
            "SELECT salon_id, COUNT(*) AS count FROM clients WHERE phone = ? GROUP BY salon_id",
            (phone,),
        )
        self.assertEqual(set(rows["salon_id"].astype(int).tolist()), {sid1, sid2})

    def test_unknown_webhook_destination_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "could not be matched"):
            salon_app.process_missed_call_webhook(
                {"provider": "unit", "phone": "+15551230001", "To": "+19995550100"}
            )

    def test_explicit_salon_id_routes_webhook(self):
        _, sid2 = self.salon_ids()
        conversation_id = salon_app.process_missed_call_webhook(
            {"provider": "unit", "phone": "+15551230002", "salon_id": sid2, "name": "Route Test"}
        )
        self.assertEqual(salon_app.salon_id_for_conversation(conversation_id), sid2)

    def test_signature_is_enforced_when_configured(self):
        sid1, _ = self.salon_ids()
        salon_app.WEBHOOK_SECRET = "unit-secret"
        payload = {"provider": "unit", "phone": "+15551230003", "salon_id": sid1}
        raw = json.dumps(payload, sort_keys=True)
        good_signature = hmac.new(b"unit-secret", raw.encode("utf-8"), hashlib.sha256).hexdigest()

        with self.assertRaisesRegex(ValueError, "signature"):
            salon_app.process_missed_call_webhook(payload, "bad-signature")

        conversation_id = salon_app.process_missed_call_webhook(payload, good_signature)
        self.assertEqual(salon_app.salon_id_for_conversation(conversation_id), sid1)

    def test_require_secret_rejects_unsigned_webhook(self):
        sid1, _ = self.salon_ids()
        salon_app.REQUIRE_WEBHOOK_SECRET = True
        with self.assertRaisesRegex(ValueError, "signature"):
            salon_app.process_missed_call_webhook(
                {"provider": "unit", "phone": "+15551230004", "salon_id": sid1}
            )

    def test_manual_review_does_not_prepare_sendable_sms(self):
        sid1, _ = self.salon_ids()
        conversation_id = salon_app.create_missed_call(
            "Consent Review", "+15551230005", consent_basis="unknown_manual_review", salon_id=sid1
        )
        messages = salon_app.conversation_messages(conversation_id)
        self.assertIn("blocked: consent review", messages["delivery_status"].tolist())

    def test_opted_out_client_followup_is_blocked(self):
        sid1, _ = self.salon_ids()
        conversation_id = salon_app.create_missed_call(
            "Opt Out", "+15551230006", consent_basis="opted_in", salon_id=sid1
        )
        salon_app.add_client_reply(conversation_id, "STOP")
        salon_app.add_assistant_message(conversation_id, "Following up after opt-out.")
        messages = salon_app.conversation_messages(conversation_id)
        self.assertEqual(str(messages.iloc[-1]["delivery_status"]), "blocked: opted out")


if __name__ == "__main__":
    unittest.main()
