import json
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.domain import verify_chain
from src.medical_mission_allocation import validate_event
from src.service import AllocationError, AllocationService, BACKFILL_PARTNER

UTC = timezone.utc
T0 = datetime(2026, 9, 24, 9, 0, tzinfo=UTC)
SLOT_WINDOW = ("2026-10-01T00:00:00+00:00", "2026-10-31T00:00:00+00:00")
MATERIALS_VALID_UNTIL = "2026-12-31T00:00:00+00:00"


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)


def register_candidate(svc, subject, *, region, urgency, languages=("fr",),
                       window=SLOT_WINDOW, consent=True, followup=True,
                       materials_until=MATERIALS_VALID_UNTIL, skills=("cardiology",),
                       identity_ref=None):
    svc.append("SCREENING_REGISTERED", subject, {
        "region": region, "urgency": urgency, "languages": list(languages),
        "window": list(window) if window else None,
        "identity_ref": identity_ref or f"id-doc-{subject}",
    })
    svc.append("INDICATION_RECORDED", subject, {"skills_needed": list(skills)})
    if consent:
        svc.append("ELIGIBILITY_SIGNED", subject, {})
    if followup:
        svc.append("FOLLOWUP_COMMITTED", subject, {"partner": "clinic-local"})
    if materials_until:
        svc.append("MATERIAL_SUBMITTED", subject, {"valid_until": materials_until})


def publish_slot(svc, slot_id="slot-cardio", *, languages=("zh", "fr"),
                 care_chain=("hospital-a", "coordinator-x"), capacity=1,
                 window=SLOT_WINDOW):
    svc.append("SLOT_PUBLISHED", "program", {
        "slot_id": slot_id, "skill": "cardiology", "window": list(window),
        "languages": list(languages), "care_chain": list(care_chain),
        "capacity": capacity,
    })


def build_service(clock=None, targets=("highlands",)):
    clock = clock or Clock(T0)
    svc = AllocationService(now=clock)
    publish_slot(svc)
    for region in targets:
        svc.append("REGION_TARGET_SET", "program", {"region": region, "target": 1})
    return svc, clock


class ContractTest(unittest.TestCase):
    def test_sample_matches_domain_contract(self):
        record = json.loads((Path(__file__).parents[1] / "data" / "sample.json").read_text(encoding="utf-8"))
        self.assertEqual(validate_event(record), [])

    def test_legacy_event_kinds_still_valid(self):
        for kind in ("SCREENING_REGISTERED", "ELIGIBILITY_SIGNED", "SLOT_ALLOCATED",
                     "EXCEPTION_APPROVED", "OUTCOME_HANDED_OFF"):
            self.assertEqual(validate_event({
                "event_id": f"x-{kind}", "kind": kind,
                "occurred_at": "2026-09-24T09:00:00+00:00",
                "subject_id": "s", "payload": {},
            }), [])

    def test_naive_timestamp_rejected(self):
        problems = validate_event({
            "event_id": "x", "kind": "SLOT_PUBLISHED",
            "occurred_at": "2026-09-24T09:00:00",
            "subject_id": "s", "payload": {},
        })
        self.assertIn("occurred_at", problems)


class RankingExplainabilityTest(unittest.TestCase):
    def test_remote_high_risk_outranks_first_come(self):
        svc, _ = build_service()
        # 先登记本地低风险患者（先到），再登记偏远高风险患者。
        register_candidate(svc, "p-local", region="capital", urgency=0.4)
        register_candidate(svc, "p-remote", region="highlands", urgency=0.95)
        advice = svc.advise()
        top = advice["suggestions"][0]
        self.assertEqual((top["subject_id"], top["slot_id"]), ("p-remote", "slot-cardio"))
        self.assertTrue(top["eligible"])
        # 解释中可看到每个门限与因子贡献，并标出公平目标缺口。
        self.assertTrue(any("highlands" in f for f in top["fairness_flags"]))
        self.assertIn("urgency", top["contributions"])
        self.assertIn("筛查有效", top["explanation"])

    def test_gate_failures_are_explained_not_hidden(self):
        svc, _ = build_service()
        register_candidate(svc, "p-noconsent", region="highlands", urgency=0.99, consent=False)
        advice = svc.advise(persist=False)
        sug = next(s for s in advice["suggestions"] if s["subject_id"] == "p-noconsent")
        self.assertFalse(sug["eligible"])
        self.assertFalse(sug["gates"]["consent"])
        self.assertIn("授权", sug["gate_notes"]["consent"])

    def test_advice_is_only_a_suggestion(self):
        svc, _ = build_service()
        register_candidate(svc, "p-remote", region="highlands", urgency=0.9)
        before = len(svc.log)
        svc.advise()
        self.assertTrue(any(e["kind"] == "CANDIDATE_RANKED" for e in svc.log[before:]))
        # 建议事件本身不产生任何占有或归属。
        self.assertEqual(svc._allocations, {})
        self.assertEqual([h for h in svc._holds.values() if h["status"] == "active"], [])


class HoldAndBackfillTest(unittest.TestCase):
    def test_hold_ttl_expiry_then_backfill_by_original_rule(self):
        clock = Clock(T0)
        svc, _ = build_service(clock)
        register_candidate(svc, "p-a", region="highlands", urgency=0.9)
        register_candidate(svc, "p-b", region="capital", urgency=0.8)
        svc.place_hold("p-a", "slot-cardio", "partner-A", ttl_seconds=100)
        clock.advance(101)
        released = svc.sweep_expired()
        self.assertEqual(len(released), 1)
        self.assertEqual(released[0]["kind"], "HOLD_EXPIRED")
        promoted = [e for e in svc.log if e["kind"] == "WAITLIST_PROMOTED"]
        self.assertEqual(len(promoted), 1)
        self.assertEqual(promoted[0]["subject_id"], "p-b")
        hold = svc._active_hold_for_subject("p-b")
        self.assertIsNotNone(hold)
        self.assertEqual(hold["partner"], BACKFILL_PARTNER)

    def test_withdraw_releases_and_backfills(self):
        svc, _ = build_service()
        register_candidate(svc, "p-a", region="highlands", urgency=0.9)
        register_candidate(svc, "p-b", region="capital", urgency=0.8)
        svc.place_hold("p-a", "slot-cardio", "partner-A")
        svc.withdraw("p-a", "签证拒签")
        self.assertIsNone(svc._active_hold_for_subject("p-a"))
        self.assertIsNotNone(svc._active_hold_for_subject("p-b"))

    def test_material_expiry_releases_and_backfills(self):
        svc, clock = build_service()
        register_candidate(svc, "p-a", region="highlands", urgency=0.9)
        register_candidate(svc, "p-b", region="capital", urgency=0.8)
        svc.place_hold("p-a", "slot-cardio", "partner-A")
        svc.expire_materials("p-a")
        self.assertIsNotNone(svc._active_hold_for_subject("p-b"))
        # 材料已失效的患者不会再次被递补。
        svc.expire_materials("p-b")
        self.assertIsNone(svc._active_hold_for_subject("p-b"))

    def test_withdraw_after_confirmation_revokes_allocation_and_backfills(self):
        svc, _ = build_service()
        register_candidate(svc, "p-a", region="highlands", urgency=0.9)
        register_candidate(svc, "p-b", region="capital", urgency=0.8)
        hold = svc.place_hold("p-a", "slot-cardio", "partner-A")
        svc.confirm(hold["hold_id"], "partner-A", request_key="k1")
        # 未出发即退出：归属被撤销（留下事件），名额按原规则递补给 p-b。
        svc.withdraw("p-a", "突发原因退出")
        self.assertNotIn("slot-cardio", svc._allocations)
        self.assertTrue(any(e["kind"] == "ALLOCATION_REVOKED" for e in svc.log))
        self.assertIsNotNone(svc._active_hold_for_subject("p-b"))

    def test_locked_arrangement_not_revoked_after_departure(self):
        svc, _ = build_service()
        register_candidate(svc, "p-a", region="highlands", urgency=0.9)
        hold = svc.place_hold("p-a", "slot-cardio", "partner-A")
        svc.confirm(hold["hold_id"], "partner-A", request_key="k1")
        svc.depart("p-a")
        svc.expire_materials("p-a")  # 出发后：归属保持
        self.assertEqual(svc._allocations["slot-cardio"]["subject"], "p-a")

    def test_sweep_is_idempotent_across_calls(self):
        clock = Clock(T0)
        svc, _ = build_service(clock)
        register_candidate(svc, "p-a", region="highlands", urgency=0.9)
        svc.place_hold("p-a", "slot-cardio", "partner-A", ttl_seconds=10)
        clock.advance(11)
        first = svc.sweep_expired()
        second = svc.sweep_expired()
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(len([e for e in svc.log if e["kind"] == "HOLD_EXPIRED"]), 1)


class CrossPartnerDedupTest(unittest.TestCase):
    def test_same_patient_cannot_hold_across_partners(self):
        svc, _ = build_service(targets=("highlands", "north"))
        publish_slot(svc, "slot-cardio-2", care_chain=("hospital-b",), languages=("zh", "fr"))
        register_candidate(svc, "p-a", region="highlands", urgency=0.9)
        svc.place_hold("p-a", "slot-cardio", "partner-A")
        with self.assertRaises(AllocationError):
            svc.place_hold("p-a", "slot-cardio-2", "partner-B")
        # 拒绝事实被记录，但没有产生第二个占有。
        self.assertTrue(any(e["kind"] == "HELD_BY_OTHER_PARTNER" for e in svc.log))
        self.assertEqual(len(svc._active_holds_for_slot("slot-cardio-2")), 0)

    def test_cannot_hold_after_allocation(self):
        svc, _ = build_service()
        publish_slot(svc, "slot-cardio-2")
        register_candidate(svc, "p-a", region="highlands", urgency=0.9)
        hold = svc.place_hold("p-a", "slot-cardio", "partner-A")
        svc.confirm(hold["hold_id"], "partner-A", request_key="confirm-1")
        with self.assertRaises(AllocationError):
            svc.place_hold("p-a", "slot-cardio-2", "partner-B")

    def test_alias_registration_detects_collision(self):
        svc, _ = build_service()
        svc.register_alias("p-a", "partner-A", "local-77")
        svc.register_alias("p-b", "partner-B", "local-77")  # 不同合作方的同名本地号允许
        with self.assertRaises(AllocationError):
            svc.register_alias("p-different", "partner-A", "local-77")


class ConcurrentConfirmTest(unittest.TestCase):
    def test_only_one_confirmation_wins(self):
        svc, _ = build_service()
        register_candidate(svc, "p-a", region="highlands", urgency=0.9)
        hold = svc.place_hold("p-a", "slot-cardio", "partner-A")
        errors = []

        def confirm(i):
            try:
                svc.confirm(hold["hold_id"], "partner-A", request_key=f"key-{i}")
            except AllocationError as exc:
                errors.append(exc)

        threads = [threading.Thread(target=confirm, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        allocations = [e for e in svc.log if e["kind"] == "SLOT_ALLOCATED"]
        self.assertEqual(len(allocations), 1)
        self.assertEqual(len(errors), 7)

    def test_same_request_key_is_idempotent(self):
        svc, _ = build_service()
        register_candidate(svc, "p-a", region="highlands", urgency=0.9)
        hold = svc.place_hold("p-a", "slot-cardio", "partner-A")
        first = svc.confirm(hold["hold_id"], "partner-A", request_key="stable-key")
        second = svc.confirm("anything-else", "partner-A", request_key="stable-key")
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(len([e for e in svc.log if e["kind"] == "SLOT_ALLOCATED"]), 1)


class ExceptionReviewTest(unittest.TestCase):
    def _pending_exception(self, svc):
        # 无授权的候选无法通过自动门限，需走例外。
        register_candidate(svc, "p-x", region="highlands", urgency=0.99, consent=False)
        svc.request_exception("p-x", "slot-cardio", "授权表格延迟但患者已口头同意",
                              requested_by="coordinator-x", request_id="exc-1")

    def test_reviewer_must_be_independent(self):
        svc, _ = build_service()
        self._pending_exception(svc)
        with self.assertRaises(AllocationError):
            svc.approve_exception("exc-1", reviewer="coordinator-x", impact="无")
        with self.assertRaises(AllocationError):
            svc.approve_exception("exc-1", reviewer="hospital-a", impact="无")  # 接诊链内

    def test_approved_exception_allocates_and_records_impact(self):
        svc, _ = build_service()
        self._pending_exception(svc)
        svc.approve_exception("exc-1", reviewer="ethics-board",
                              impact="跳过授权门限；要求出发前补签，否则取消归属")
        event = svc.exception_allocation("exc-1", "partner-A", request_key="exc-confirm-1")
        self.assertEqual(event["kind"], "SLOT_ALLOCATED")
        self.assertEqual(svc._allocations["slot-cardio"]["subject"], "p-x")
        approval = next(e for e in svc.log if e["kind"] == "EXCEPTION_APPROVED")
        self.assertEqual(approval["payload"]["reviewer"], "ethics-board")
        self.assertIn("补签", approval["payload"]["impact"])

    def test_unapproved_exception_cannot_allocate(self):
        svc, _ = build_service()
        self._pending_exception(svc)
        with self.assertRaises(AllocationError):
            svc.exception_allocation("exc-1", "partner-A", request_key="x")


class PrivacyTest(unittest.TestCase):
    def test_identity_only_disclosed_to_care_chain(self):
        svc, _ = build_service()
        register_candidate(svc, "p-a", region="highlands", urgency=0.9,
                           identity_ref="sealed-envelope-42")
        hold = svc.place_hold("p-a", "slot-cardio", "partner-A")
        with self.assertRaises(AllocationError):
            svc.disclose_identity("p-a", "hospital-b")  # 未归属且不在接诊链
        svc.confirm(hold["hold_id"], "partner-A", request_key="k1")
        self.assertEqual(svc.disclose_identity("p-a", "hospital-a"), "sealed-envelope-42")
        with self.assertRaises(AllocationError):
            svc.disclose_identity("p-a", "hospital-b")
        self.assertTrue(any(e["kind"] == "DISCLOSURE_MADE" and
                            e["payload"]["requester"] == "hospital-a" for e in svc.log))


class RecomputeBoundaryTest(unittest.TestCase):
    def test_screening_correction_only_affects_pending(self):
        svc, _ = build_service()
        register_candidate(svc, "p-a", region="highlands", urgency=0.9)
        register_candidate(svc, "p-b", region="capital", urgency=0.5)
        hold = svc.place_hold("p-a", "slot-cardio", "partner-A")
        svc.confirm(hold["hold_id"], "partner-A", request_key="k1")
        svc.depart("p-a")
        # 出发后筛查被更正为低紧急度：归属与安排保持不变。
        svc.append("SCREENING_CORRECTED", "p-a", {"changes": {"urgency": 0.05}})
        svc.recompute_pending()
        self.assertEqual(svc._allocations["slot-cardio"]["subject"], "p-a")
        self.assertNotIn("p-a", {h["subject"] for h in svc._holds.values() if h["status"] == "closed"})

    def test_flight_change_recomputes_pending_hold_and_backfills(self):
        svc, _ = build_service()
        register_candidate(svc, "p-a", region="highlands", urgency=0.9)
        register_candidate(svc, "p-b", region="capital", urgency=0.5)
        svc.place_hold("p-a", "slot-cardio", "partner-A")
        # 航班改到名额窗口之外：未出发的占有被释放并递补。
        svc.append("FLIGHT_CHANGED", "p-a", {"window": [
            "2027-01-01T00:00:00+00:00", "2027-01-10T00:00:00+00:00"]})
        svc.recompute_pending()
        self.assertIsNone(svc._active_hold_for_subject("p-a"))
        self.assertIsNotNone(svc._active_hold_for_subject("p-b"))

    def test_completed_care_fact_is_immutable(self):
        svc, _ = build_service()
        register_candidate(svc, "p-a", region="highlands", urgency=0.9)
        hold = svc.place_hold("p-a", "slot-cardio", "partner-A")
        svc.confirm(hold["hold_id"], "partner-A", request_key="k1")
        svc.depart("p-a")
        svc.complete_care("p-a", "手术成功，随访中")
        svc.append("SCREENING_CORRECTED", "p-a", {"changes": {"urgency": 0.0}})
        svc.append("FLIGHT_CHANGED", "p-a", {"window": [
            "2027-01-01T00:00:00+00:00", "2027-01-10T00:00:00+00:00"]})
        svc.recompute_pending()
        completed = [e for e in svc.log if e["kind"] == "CARE_COMPLETED"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["payload"]["outcome"], "手术成功，随访中")
        self.assertEqual(svc._allocations["slot-cardio"]["subject"], "p-a")


class RestartRecoveryTest(unittest.TestCase):
    def test_expiry_promotion_and_notification_do_not_duplicate_after_restart(self):
        clock = Clock(T0)
        svc, _ = build_service(clock)
        register_candidate(svc, "p-a", region="highlands", urgency=0.9)
        register_candidate(svc, "p-b", region="capital", urgency=0.8)
        hold = svc.place_hold("p-a", "slot-cardio", "partner-A", ttl_seconds=100)
        svc.confirm(hold["hold_id"], "partner-A", request_key="k1")
        # 归属通知恰好一次。
        self.assertEqual(len([e for e in svc.log if e["kind"] == "NOTIFICATION_SENT"]), 1)

        svc.withdraw("p-a", "退出")
        promoted_hold = svc._active_hold_for_subject("p-b")
        self.assertIsNotNone(promoted_hold)
        data = svc.dump()

        # 重启：让递补占有也到期，验证恢复后只产生一轮到期/再递补事件。
        clock.advance(100000)
        restored = AllocationService.load(data, now=clock)
        restored.recover()
        restored.recover()  # 重复恢复不得重复派生事件
        for kind in ("HOLD_EXPIRED", "WAITLIST_PROMOTED", "NOTIFICATION_SENT", "SLOT_ALLOCATED"):
            counts_before = len([e for e in svc.log if e["kind"] == kind])
            counts_after = len([e for e in restored.log if e["kind"] == kind])
            # 恢复只允许新增到期链路上的事件，且不超过一次新增。
            self.assertLessEqual(counts_after - counts_before, 1, kind)
        # 原有归属与通知不重复。
        self.assertEqual(len([e for e in restored.log if e["kind"] == "SLOT_ALLOCATED"]), 1)
        self.assertEqual(len([e for e in restored.log if e["kind"] == "NOTIFICATION_SENT"]), 1)

    def test_tampered_log_detected_on_recovery(self):
        svc, _ = build_service()
        register_candidate(svc, "p-a", region="highlands", urgency=0.9)
        svc.place_hold("p-a", "slot-cardio", "partner-A")
        data = json.loads(svc.dump())
        data[0]["payload"]["v"] = "tampered"
        with self.assertRaises(Exception):
            AllocationService.load(json.dumps(data))


class AuditReplayTest(unittest.TestCase):
    def test_audit_reproduces_allocation_data_version_and_score(self):
        svc, _ = build_service()
        register_candidate(svc, "p-a", region="highlands", urgency=0.9)
        register_candidate(svc, "p-b", region="capital", urgency=0.95)
        # p-a 因地区公平缺口应排在 p-b 之前。
        hold_a = svc.place_hold("p-a", "slot-cardio", "partner-A")
        svc.confirm(hold_a["hold_id"], "partner-A", request_key="k1")
        allocation_id = svc._allocations["slot-cardio"]["event_id"]

        report = svc.audit_allocation(allocation_id)
        self.assertTrue(report["chain_verified"])
        self.assertTrue(report["eligible_at_allocation"])
        self.assertEqual(report["subject"], "p-a")
        # 归属前一刻的确定分数：urgency .9×.5 + 公平缺口 1×.35 + 窗口余量 1×.15 = .95。
        # 同等病情的 p-b 因地区无缺口只得 .625，公平目标把偏远候选提到首位。
        self.assertAlmostEqual(report["score_at_allocation"], 0.95, places=6)
        self.assertEqual(set(report["weights"]), {"urgency", "regional_equity", "window_margin"})
        self.assertEqual(report["region_targets"], {"highlands": 1})

    def test_audit_detects_tampering(self):
        svc, _ = build_service()
        register_candidate(svc, "p-a", region="highlands", urgency=0.9)
        hold = svc.place_hold("p-a", "slot-cardio", "partner-A")
        svc.confirm(hold["hold_id"], "partner-A", request_key="k1")
        allocation_id = svc._allocations["slot-cardio"]["event_id"]
        # 篡改归属之前的一条事实。
        for event in svc.log:
            if event["kind"] == "REGION_TARGET_SET":
                event["payload"]["target"] = 99
                break
        with self.assertRaises(Exception):
            svc.audit_allocation(allocation_id)
        with self.assertRaises(Exception):
            verify_chain(svc.log)


if __name__ == "__main__":
    unittest.main()
