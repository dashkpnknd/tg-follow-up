from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from analyzer import Decision, DialogStatus
from store import Store


class StoreTests(unittest.TestCase):
    def test_candidate_is_idempotent_and_task_is_disabled(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "followup.sqlite3")
            store.import_account("session", "Account", "/tmp/session.session")
            account = store.accounts()[0]
            decision = Decision(DialogStatus.CANDIDATE, "safe", datetime.now(timezone.utc) - timedelta(days=3))
            store.record_decision(account["id"], 123, "name", "Name", decision, 99)
            store.record_decision(account["id"], 123, "name", "Name", decision, 99)
            self.assertEqual(store.dialog_source_message_id(account["id"], 123), 99)
            task_id, selected = store.create_task("Review first", 10)
            task = store.tasks()[0]
            self.assertEqual(task["id"], task_id)
            self.assertEqual(selected, 1)
            self.assertEqual(task["enabled"], 0)
            self.assertEqual(task["queued"], 1)
            self.assertEqual(store.setting("global_paused"), "1")
            self.assertEqual(store.setting("delivery_enabled"), "0")

    def test_enabled_task_receives_new_candidate_after_48_hour_recheck(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "followup.sqlite3")
            store.import_account("session", "Account", "/tmp/session.session")
            account = store.accounts()[0]
            task_id, _ = store.create_task("Continuous", 0)
            store.set_task_enabled(task_id, True)
            decision = Decision(DialogStatus.CANDIDATE, "safe", datetime.now(timezone.utc) - timedelta(days=3))
            store.record_decision(account["id"], 456, "name", "Name", decision, 100)
            task = store.tasks()[0]
            self.assertEqual(task["queued"], 1)
            self.assertEqual(task["auto_include_new"], 1)

    def test_too_fresh_dialog_requires_recheck_at_48_hours(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "followup.sqlite3")
            store.import_account("session", "Account", "/tmp/session.session")
            account = store.accounts()[0]
            fresh = Decision(DialogStatus.TOO_FRESH, "wait", datetime.now(timezone.utc) - timedelta(hours=47))
            store.record_decision(account["id"], 789, "name", "Name", fresh, 101)
            self.assertFalse(store.dialog_requires_recheck(account["id"], 789))
            due = Decision(DialogStatus.TOO_FRESH, "wait", datetime.now(timezone.utc) - timedelta(hours=49))
            store.record_decision(account["id"], 789, "name", "Name", due, 101)
            self.assertTrue(store.dialog_requires_recheck(account["id"], 789))

    def test_pending_selects_one_head_per_account(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "followup.sqlite3")
            store.import_account("one", "One", "/tmp/one.session")
            store.import_account("two", "Two", "/tmp/two.session")
            decision = Decision(DialogStatus.CANDIDATE, "safe", datetime.now(timezone.utc) - timedelta(days=3))
            first, second = store.accounts()
            store.set_account_auth_status(first["id"], "authorized")
            store.set_account_auth_status(second["id"], "authorized")
            for peer_id in (1, 2, 3):
                store.record_decision(first["id"], peer_id, None, None, decision, peer_id)
            store.record_decision(second["id"], 4, None, None, decision, 4)
            task_id, _ = store.create_task("Continuous", 0)
            store.set_task_enabled(task_id, True)
            for row in store.pending_revalidation(limit=10):
                store.mark_validated(row["id"])
            self.assertEqual({row["account_id"] for row in store.pending()}, {first["id"], second["id"]})

    def test_unavailable_account_is_removed_from_delivery_queue(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "followup.sqlite3")
            store.import_account("session", "Account", "/tmp/session.session")
            account = store.accounts()[0]
            store.set_account_auth_status(account["id"], "authorized")
            decision = Decision(DialogStatus.CANDIDATE, "safe", datetime.now(timezone.utc) - timedelta(days=3))
            store.record_decision(account["id"], 42, None, None, decision, 42)
            task_id, _ = store.create_task("Continuous", 0)
            store.set_task_enabled(task_id, True)
            store.mark_validated(store.pending_revalidation()[0]["id"])
            self.assertEqual(len(store.pending()), 1)
            self.assertEqual(store.disable_sending_for_account(account["id"], "auth_error"), 1)
            self.assertEqual(store.pending(), [])
            self.assertEqual(store.account(account["id"])["send_status"], "unavailable")

    def test_rate_limited_account_rejoins_after_cooldown_without_losing_queue(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "followup.sqlite3")
            store.import_account("session", "Account", "/tmp/session.session")
            account = store.accounts()[0]
            store.set_account_auth_status(account["id"], "authorized")
            decision = Decision(DialogStatus.CANDIDATE, "safe", datetime.now(timezone.utc) - timedelta(days=3))
            store.record_decision(account["id"], 42, None, None, decision, 42)
            task_id, _ = store.create_task("Continuous", 0)
            store.set_task_enabled(task_id, True)
            store.mark_validated(store.pending_revalidation()[0]["id"])
            row = store.pending()[0]
            store.mark_queue(row["id"], "sending")
            store.cooldown_sending_for_account(account["id"], "error:PeerFloodError")
            self.assertEqual(store.pending(), [])
            self.assertEqual(store.account(account["id"])["send_status"], "available")
            with store.connect() as db:
                db.execute("UPDATE accounts SET cooldown_until=? WHERE id=?", ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), account["id"]))
            self.assertEqual(len(store.pending()), 1)

    def test_delivery_requires_full_history_validation(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "followup.sqlite3")
            store.import_account("session", "Account", "/tmp/session.session")
            account = store.accounts()[0]
            store.set_account_auth_status(account["id"], "authorized")
            decision = Decision(DialogStatus.CANDIDATE, "safe", datetime.now(timezone.utc) - timedelta(days=3))
            store.record_decision(account["id"], 42, None, None, decision, 42)
            task_id, _ = store.create_task("Continuous", 0)
            store.set_task_enabled(task_id, True)
            self.assertEqual(store.pending(), [])
            row = store.pending_revalidation()[0]
            store.mark_validated(row["id"])
            self.assertEqual(len(store.pending()), 1)

    def test_restore_authorized_sender_requeues_only_candidates(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "followup.sqlite3")
            store.import_account("session", "Account", "/tmp/session.session")
            account = store.accounts()[0]
            store.set_account_auth_status(account["id"], "authorized")
            decision = Decision(DialogStatus.CANDIDATE, "safe", datetime.now(timezone.utc) - timedelta(days=3))
            store.record_decision(account["id"], 42, None, None, decision, 42)
            task_id, _ = store.create_task("Continuous", 0)
            store.set_task_enabled(task_id, True)
            store.disable_sending_for_account(account["id"], "error:PeerFloodError")
            restored = store.restore_authorized_cold_accounts(["session"])
            self.assertEqual(restored, [account["id"]])
            self.assertEqual(store.requeue_candidate_dialogs(restored), 1)
            row = store.pending_revalidation()[0]
            self.assertEqual(row["peer_id"], 42)

    def test_cold_scan_progress_finishes_only_after_every_account(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "followup.sqlite3")
            store.import_account("one", "One", "/tmp/one.session")
            store.import_account("two", "Two", "/tmp/two.session")
            account_ids = [row["id"] for row in store.accounts()]
            store.prepare_cold_scan(account_ids)
            self.assertFalse(store.cold_scan_complete(account_ids))
            store.complete_cold_scan(account_ids[0])
            self.assertFalse(store.cold_scan_complete(account_ids))
            store.complete_cold_scan(account_ids[1])
            self.assertTrue(store.cold_scan_complete(account_ids))

    def test_sent_today_total_counts_all_accounts(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "followup.sqlite3")
            store.import_account("one", "One", "/tmp/one.session")
            store.import_account("two", "Two", "/tmp/two.session")
            first, second = store.accounts()
            store.set_account_auth_status(first["id"], "authorized")
            store.set_account_auth_status(second["id"], "authorized")
            decision = Decision(DialogStatus.CANDIDATE, "safe", datetime.now(timezone.utc) - timedelta(days=3))
            store.record_decision(first["id"], 1, None, None, decision, 1)
            store.record_decision(second["id"], 2, None, None, decision, 2)
            task_id, _ = store.create_task("Continuous", 0)
            store.set_task_enabled(task_id, True)
            for row in store.pending_revalidation(limit=10):
                store.mark_validated(row["id"])
            rows = store.pending()
            for row in rows:
                store.mark_sent(row["id"], row["account_id"], row["peer_id"], "Тест")
            self.assertEqual(store.sent_today_total(), 2)

    def test_access_hash_is_saved_and_preserved(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "followup.sqlite3")
            store.import_account("session", "Account", "/tmp/session.session")
            account = store.accounts()[0]
            decision = Decision(DialogStatus.CANDIDATE, "safe", datetime.now(timezone.utc) - timedelta(days=3))
            store.record_decision(account["id"], 99, None, None, decision, 1, 12345)
            store.record_decision(account["id"], 99, None, None, decision, 1)
            with store.connect() as db:
                value = db.execute("SELECT peer_access_hash FROM dialog_state WHERE account_id=? AND peer_id=?", (account["id"], 99)).fetchone()["peer_access_hash"]
            self.assertEqual(value, 12345)

    def test_auth_status_is_recorded(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "followup.sqlite3")
            store.import_account("session", "Account", "/tmp/session.session")
            account = store.accounts()[0]
            store.set_account_auth_status(account["id"], "authorized")
            self.assertEqual(store.account(account["id"])["auth_status"], "authorized")

    def test_task_snapshots_all_followup_variants(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "followup.sqlite3")
            store.set_followup_templates(["Первый", "Второй", "Первый"])
            task_id, _ = store.create_task("Variants", 0)
            self.assertEqual(store.task_templates(task_id), ["Первый", "Второй"])
            store.set_followup_templates(["Третий"])
            self.assertEqual(store.task_templates(task_id), ["Первый", "Второй"])

    def test_revalidation_does_not_repeat_for_an_already_audited_candidate(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "followup.sqlite3")
            store.import_account("session", "Account", "/tmp/session.session")
            account = store.accounts()[0]
            store.set_account_auth_status(account["id"], "authorized")
            decision = Decision(DialogStatus.CANDIDATE, "safe", datetime.now(timezone.utc) - timedelta(days=3))
            store.record_decision(account["id"], 888, None, None, decision, 1)
            task_id, _ = store.create_task("Audit", 0)
            store.set_task_enabled(task_id, True)
            row = store.pending_revalidation()[0]
            store.mark_validated(row["id"])
            self.assertEqual(store.pending_revalidation(), [])
            store.record_decision(account["id"], 888, None, None, decision, 2)
            self.assertEqual(store.pending_revalidation(), [])

    def test_cold_followup_sent_to_peer_is_global_across_accounts(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "followup.sqlite3")
            store.import_account("one", "One", "/tmp/one.session")
            store.import_account("two", "Two", "/tmp/two.session")
            first, second = store.accounts()
            for account in (first, second):
                store.set_account_auth_status(account["id"], "authorized")
            decision = Decision(DialogStatus.CANDIDATE, "safe", datetime.now(timezone.utc) - timedelta(days=3))
            store.record_decision(first["id"], 42, None, None, decision, 1)
            task_id, _ = store.create_task("Continuous", 0)
            store.set_task_enabled(task_id, True)
            store.mark_validated(store.pending_revalidation()[0]["id"])
            row = store.pending()[0]
            store.mark_sent(row["id"], row["account_id"], row["peer_id"], "Тест")
            self.assertTrue(store.cold_followup_sent_to_peer(42))
            self.assertFalse(store.cold_followup_sent_to_peer(43))


if __name__ == "__main__":
    unittest.main()
