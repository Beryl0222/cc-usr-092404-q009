import json
import unittest
from pathlib import Path

from src.allocation import (
    DEFAULT_POLICY,
    data_version,
    evaluate_candidate,
    evaluation_event,
    explain,
    hold_expires_at,
    policy_by_version,
    rank_candidates,
    recompute_scope,
    replay,
)
from src.medical_mission_allocation import validate_event

POLICY = DEFAULT_POLICY
T0 = "2026-09-01T09:00:00+08:00"


def slot(**overrides):
    base = {
        "slot_id": "slot-t", "partner_id": "partner-a",
        "indications": ["cataract"], "skills": ["ophthalmology"],
        "mission_window": ["2026-10-05", "2026-10-12"],
        "languages": ["sw"], "interpreter_available": True,
        "region_shares": {"remote-west": 0.10, "central": 0.60},
    }
    base.update(overrides)
    return base


def candidate(**overrides):
    base = {
        "subject_id": "sub-1", "region": "remote-west", "urgency": 4, "risk_score": 0.6,
        "screening_status": "FIT", "indication": "cataract", "required_skill": "ophthalmology",
        "consent_signed": True, "follow_up_committed": True, "language": "sw",
        "travel_windows": [["2026-10-04", "2026-10-13"]],
    }
    base.update(overrides)
    return base


def event(event_id, kind, at, subject, payload):
    return {"event_id": event_id, "kind": kind, "occurred_at": at,
            "subject_id": subject, "payload": payload}


def eval_event(subject, slot_id, rank, at=T0, policy_version="policy-2026.09"):
    return event(f"ev-{subject}-{slot_id}", "CANDIDATE_EVALUATED", at, subject,
                 {"slot_id": slot_id, "policy_version": policy_version,
                  "data_version": "sha256:x", "eligible": True, "factors": {},
                  "suggested_rank": rank, "inputs": {}})


def hold_event(hold_id, subject, slot_id, partner, at, key=None):
    return event(f"hold-{hold_id}", "SLOT_HOLD_PLACED", at, subject,
                 {"hold_id": hold_id, "slot_id": slot_id, "partner_id": partner,
                  "expires_at": hold_expires_at(at), "idempotency_key": key or hold_id})


def confirm_event(hold_id, by, fence, at, key=None):
    return event(f"confirm-{hold_id}-{fence}", "ALLOCATION_CONFIRMED", at, "sub-x",
                 {"hold_id": hold_id, "confirmed_by": by, "fence_token": fence,
                  "policy_version": "policy-2026.09", "data_version": "sha256:x",
                  "idempotency_key": key or f"confirm-{hold_id}-{fence}"})


def codes(state):
    return [p["code"] for p in state["problems"]]


class ScoringTest(unittest.TestCase):
    def test_remote_high_risk_outranks_central(self):
        remote = candidate(subject_id="remote-1", region="remote-west", urgency=5, risk_score=0.8)
        central = candidate(subject_id="central-1", region="central", urgency=2,
                            risk_score=0.3, language="en")
        result = rank_candidates([central, remote], slot(), POLICY)
        self.assertEqual(result["ranking"][0]["subject_id"], "remote-1")
        self.assertEqual(result["ranking"][0]["suggested_rank"], 1)
        self.assertEqual(result["blocked"], [])

    def test_gate_blocks_missing_consent(self):
        evaluation = evaluate_candidate(candidate(consent_signed=False), slot(), POLICY)
        self.assertFalse(evaluation["eligible"])
        self.assertIn("CONSENT", [g["gate"] for g in evaluation["gate_failures"]])

    def test_gate_blocks_missing_follow_up(self):
        evaluation = evaluate_candidate(candidate(follow_up_committed=False), slot(), POLICY)
        self.assertIn("FOLLOW_UP", [g["gate"] for g in evaluation["gate_failures"]])

    def test_gate_blocks_when_travel_window_missed(self):
        evaluation = evaluate_candidate(
            candidate(travel_windows=[["2026-11-01", "2026-11-10"]]), slot(), POLICY)
        self.assertIn("TRAVEL_WINDOW", [g["gate"] for g in evaluation["gate_failures"]])

    def test_gate_blocks_on_skill_mismatch(self):
        evaluation = evaluate_candidate(candidate(required_skill="neurosurgery"), slot(), POLICY)
        self.assertIn("SKILL", [g["gate"] for g in evaluation["gate_failures"]])

    def test_gate_blocks_unfit_screening(self):
        evaluation = evaluate_candidate(candidate(screening_status="UNFIT"), slot(), POLICY)
        self.assertIn("SCREENING_FIT", [g["gate"] for g in evaluation["gate_failures"]])

    def test_equity_factor_rewards_underserved_region(self):
        under = evaluate_candidate(candidate(region="remote-west"), slot(), POLICY)
        over = evaluate_candidate(candidate(region="central"), slot(), POLICY)
        self.assertGreater(under["factors"]["equity"]["score"], 0)
        self.assertEqual(over["factors"]["equity"]["score"], 0)

    def test_evaluation_is_deterministic_and_auditable(self):
        c, s = candidate(), slot()
        first = evaluate_candidate(c, s, POLICY)
        second = evaluate_candidate(c, s, POLICY)
        self.assertEqual(first, second)
        self.assertEqual(first["data_version"], data_version(c, s, POLICY))
        record = evaluation_event(c, s, POLICY, event_id="e1", occurred_at=T0, suggested_rank=1)
        self.assertEqual(validate_event(record), [])

    def test_explain_lists_factors_and_gates(self):
        eligible = evaluate_candidate(candidate(), slot(), POLICY)
        self.assertIn("urgency", explain(eligible))
        blocked = evaluate_candidate(candidate(consent_signed=False), slot(), POLICY)
        self.assertIn("CONSENT", explain(blocked))


class LifecycleTest(unittest.TestCase):
    def test_concurrent_confirm_keeps_single_owner(self):
        log = [
            hold_event("hold-1", "sub-1", "slot-1", "partner-a", T0),
            hold_event("hold-2", "sub-2", "slot-1", "partner-a", "2026-09-01T09:01:00+08:00"),
            confirm_event("hold-1", "coord-a", "fence-1", "2026-09-02T09:00:00+08:00"),
            confirm_event("hold-2", "coord-b", "fence-2", "2026-09-02T09:01:00+08:00"),
            confirm_event("hold-1", "coord-c", "fence-3", "2026-09-02T09:02:00+08:00"),
        ]
        state = replay(log)
        self.assertEqual(len(state["confirmations"]), 1)
        self.assertEqual(state["confirmations"]["hold-1"]["fence_token"], "fence-1")
        self.assertIn("SLOT_ALREADY_ALLOCATED", codes(state))
        self.assertIn("CONFIRM_WITHOUT_ACTIVE_HOLD", codes(state))

    def test_cross_partner_duplicate_hold_rejected(self):
        log = [
            hold_event("hold-1", "sub-1", "slot-1", "partner-a", T0),
            hold_event("hold-2", "sub-1", "slot-2", "partner-b", "2026-09-01T10:00:00+08:00"),
        ]
        state = replay(log)
        self.assertIn("DUPLICATE_HOLD", codes(state))
        self.assertNotIn("hold-2", state["holds"])

    def test_expired_hold_released_once_across_restart(self):
        log = [
            hold_event("hold-1", "sub-1", "slot-1", "partner-a", T0),
            event("tick", "SCREENING_REGISTERED", "2026-09-05T09:00:00+08:00", "sub-9",
                  {"region": "central", "urgency": 1, "screening_status": "FIT",
                   "risk_score": 0.1}),
        ]
        state = replay(log)
        expired = [n for n in state["notifications"] if n["type"] == "HOLD_EXPIRED"]
        self.assertEqual(len(expired), 1)
        self.assertEqual(state["holds"]["hold-1"]["release_reason"], "EXPIRED")
        # 服务重启后在同一状态上重放同一日志，不重复产生通知。
        replay(log, state)
        expired = [n for n in state["notifications"] if n["type"] == "HOLD_EXPIRED"]
        self.assertEqual(len(expired), 1)
        # 同一事件因重发在日志中出现两次，也只生效一次。
        fresh = replay(log + log)
        self.assertEqual(len([n for n in fresh["notifications"]
                              if n["type"] == "HOLD_EXPIRED"]), 1)

    def test_withdrawn_backfill_follows_original_rule(self):
        log = [
            eval_event("sub-1", "slot-1", 1),
            eval_event("sub-2", "slot-1", 2),
            eval_event("sub-3", "slot-1", 3),
            hold_event("hold-1", "sub-1", "slot-1", "partner-a", "2026-09-02T09:00:00+08:00"),
            event("rel-1", "SLOT_HOLD_RELEASED", "2026-09-03T09:00:00+08:00", "sub-1",
                  {"hold_id": "hold-1", "reason": "WITHDRAWN", "idempotency_key": "rel-1"}),
            event("promo-1", "WAITLIST_PROMOTED", "2026-09-03T10:00:00+08:00", "sub-2",
                  {"released_hold_id": "hold-1", "hold_id": "hold-2",
                   "rule_version": "policy-2026.09",
                   "expires_at": hold_expires_at("2026-09-03T10:00:00+08:00"),
                   "idempotency_key": "promo-1"}),
        ]
        state = replay(log)
        self.assertEqual(state["problems"], [])
        self.assertEqual(state["holds"]["hold-2"]["subject_id"], "sub-2")
        self.assertEqual(state["holds"]["hold-2"]["status"], "ACTIVE")

    def test_backfill_rejects_wrong_order_or_rule(self):
        base = [
            eval_event("sub-1", "slot-1", 1),
            eval_event("sub-2", "slot-1", 2),
            eval_event("sub-3", "slot-1", 3),
            hold_event("hold-1", "sub-1", "slot-1", "partner-a", "2026-09-02T09:00:00+08:00"),
            event("rel-1", "SLOT_HOLD_RELEASED", "2026-09-03T09:00:00+08:00", "sub-1",
                  {"hold_id": "hold-1", "reason": "MATERIALS_STALE", "idempotency_key": "rel-1"}),
        ]
        wrong_order = base + [event("promo-x", "WAITLIST_PROMOTED", "2026-09-03T10:00:00+08:00",
                                    "sub-3",
                                    {"released_hold_id": "hold-1", "hold_id": "hold-9",
                                     "rule_version": "policy-2026.09",
                                     "expires_at": hold_expires_at("2026-09-03T10:00:00+08:00"),
                                     "idempotency_key": "promo-x"})]
        self.assertIn("PROMOTION_ORDER", codes(replay(wrong_order)))
        wrong_rule = base + [event("promo-y", "WAITLIST_PROMOTED", "2026-09-03T10:00:00+08:00",
                                   "sub-2",
                                   {"released_hold_id": "hold-1", "hold_id": "hold-9",
                                    "rule_version": "policy-1999",
                                    "expires_at": hold_expires_at("2026-09-03T10:00:00+08:00"),
                                    "idempotency_key": "promo-y"})]
        self.assertIn("PROMOTION_RULE", codes(replay(wrong_rule)))

    def test_recompute_scope_freezes_departed_and_completed(self):
        log = [
            hold_event("hold-1", "sub-1", "slot-1", "partner-a", "2026-09-02T09:00:00+08:00"),
            hold_event("hold-2", "sub-2", "slot-2", "partner-a", "2026-09-02T09:00:00+08:00"),
            hold_event("hold-3", "sub-3", "slot-3", "partner-a", "2026-09-02T09:00:00+08:00"),
            confirm_event("hold-1", "coord-a", "fence-1", "2026-09-03T09:00:00+08:00"),
            event("dep-1", "DEPARTURE_RECORDED", "2026-09-03T10:00:00+08:00", "sub-1",
                  {"hold_id": "hold-1"}),
            event("done-3", "TREATMENT_COMPLETED", "2026-09-04T09:00:00+08:00", "sub-3",
                  {"hold_id": "hold-3", "summary_ref": "note-1"}),
        ]
        scope = recompute_scope(replay(log))
        self.assertEqual(scope["recompute"], ["hold-2"])
        self.assertEqual(scope["frozen"], ["hold-1", "hold-3"])

    def test_disclosure_limited_to_care_chain(self):
        log = [
            hold_event("hold-1", "sub-1", "slot-1", "partner-a", T0),
            event("disc-1", "IDENTITY_DISCLOSED", "2026-09-02T09:00:00+08:00", "sub-1",
                  {"disclosed_to": ["partner-a"], "scope": "care-chain"}),
            event("disc-2", "IDENTITY_DISCLOSED", "2026-09-02T10:00:00+08:00", "sub-1",
                  {"disclosed_to": ["partner-b"], "scope": "care-chain"}),
        ]
        state = replay(log)
        self.assertIn("DISCLOSURE_OUTSIDE_CARE_CHAIN", codes(state))
        self.assertEqual(state["disclosures"]["sub-1"], {"partner-a"})

    def test_exception_requires_independent_review_and_impact(self):
        log = [
            event("ex-1", "EXCEPTION_APPROVED", T0, "sub-1",
                  {"requested_by": "coord-a", "approved_by": "coord-a", "impact": "已记录"}),
            event("ex-2", "EXCEPTION_APPROVED", T0, "sub-2",
                  {"requested_by": "coord-a", "approved_by": "reviewer-b", "impact": ""}),
            event("ex-3", "EXCEPTION_APPROVED", T0, "sub-3",
                  {"requested_by": "coord-a", "approved_by": "reviewer-b",
                   "impact": "挤占建议名次第 2 位，公平缺口变化已记录"}),
        ]
        state = replay(log)
        self.assertIn("EXCEPTION_NEEDS_INDEPENDENT_REVIEW", codes(state))
        self.assertIn("EXCEPTION_IMPACT_MISSING", codes(state))
        self.assertEqual(len(state["problems"]), 2)

    def test_notifications_not_duplicated_after_restart(self):
        log = [
            eval_event("sub-1", "slot-1", 1),
            eval_event("sub-2", "slot-1", 2),
            hold_event("hold-1", "sub-1", "slot-1", "partner-a", "2026-09-02T09:00:00+08:00"),
            event("rel-1", "SLOT_HOLD_RELEASED", "2026-09-03T09:00:00+08:00", "sub-1",
                  {"hold_id": "hold-1", "reason": "WITHDRAWN", "idempotency_key": "rel-1"}),
            event("promo-1", "WAITLIST_PROMOTED", "2026-09-03T10:00:00+08:00", "sub-2",
                  {"released_hold_id": "hold-1", "hold_id": "hold-2",
                   "rule_version": "policy-2026.09",
                   "expires_at": hold_expires_at("2026-09-03T10:00:00+08:00"),
                   "idempotency_key": "promo-1"}),
            event("hand-1", "OUTCOME_HANDED_OFF", "2026-09-04T09:00:00+08:00", "sub-2",
                  {"hold_id": "hold-2", "handed_to": "partner-a",
                   "idempotency_key": "hand-1"}),
        ]
        state = replay(log)
        count = len(state["notifications"])
        self.assertEqual(count, 3)
        replay(log, state)  # 重启后重放同一日志
        self.assertEqual(len(state["notifications"]), count)
        keys = [n["idempotency_key"] for n in state["notifications"]]
        self.assertEqual(len(keys), len(set(keys)))


class TimelineTest(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).parents[1] / "data" / "sample_timeline.json"
        self.events = json.loads(path.read_text(encoding="utf-8"))

    def test_replay_has_no_invariant_violations(self):
        state = replay(self.events)
        self.assertEqual(state["problems"], [])
        keys = [n["idempotency_key"] for n in state["notifications"]]
        self.assertEqual(len(keys), len(set(keys)))

    def test_timeline_recompute_scope(self):
        scope = recompute_scope(replay(self.events))
        self.assertEqual(scope["frozen"], ["hold-1"])
        self.assertEqual(scope["recompute"], ["hold-3", "hold-4"])

    def test_auditor_can_recompute_evaluations(self):
        for record in self.events:
            if record["kind"] != "CANDIDATE_EVALUATED":
                continue
            payload = record["payload"]
            policy = policy_by_version(payload["policy_version"])
            self.assertIsNotNone(policy, record["event_id"])
            again = evaluate_candidate(payload["inputs"]["candidate"],
                                       payload["inputs"]["slot"], policy)
            self.assertEqual(again["data_version"], payload["data_version"], record["event_id"])
            self.assertEqual(again["total"], payload["total"], record["event_id"])
            self.assertEqual(again["factors"], payload["factors"], record["event_id"])


if __name__ == "__main__":
    unittest.main()
