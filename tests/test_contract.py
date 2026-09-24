import json
import unittest
from pathlib import Path

from src.medical_mission_allocation import EVENT_KINDS, validate_event

DATA_DIR = Path(__file__).parents[1] / "data"


class ContractTest(unittest.TestCase):
    def test_sample_matches_domain_contract(self):
        record = json.loads((DATA_DIR / "sample.json").read_text(encoding="utf-8"))
        self.assertEqual(validate_event(record), [])

    def test_timeline_events_match_domain_contract(self):
        events = json.loads((DATA_DIR / "sample_timeline.json").read_text(encoding="utf-8"))
        self.assertTrue(events)
        for event in events:
            self.assertEqual(validate_event(event), [], event["event_id"])

    def test_unknown_kind_rejected(self):
        record = {"event_id": "x", "kind": "NOPE", "occurred_at": "2026-09-01T00:00:00+08:00",
                  "subject_id": "s", "payload": {}}
        self.assertIn("kind", validate_event(record))

    def test_missing_payload_field_reported(self):
        record = {"event_id": "x", "kind": "SLOT_HOLD_PLACED",
                  "occurred_at": "2026-09-01T00:00:00+08:00", "subject_id": "s",
                  "payload": {"hold_id": "h", "slot_id": "s1", "partner_id": "p",
                              "idempotency_key": "k"}}
        self.assertIn("payload.expires_at", validate_event(record))

    def test_every_kind_has_payload_contract(self):
        from src.medical_mission_allocation import PAYLOAD_CONTRACTS
        for kind in EVENT_KINDS:
            self.assertIn(kind, PAYLOAD_CONTRACTS)


if __name__ == "__main__":
    unittest.main()
