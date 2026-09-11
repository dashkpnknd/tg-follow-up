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
            for peer_id in (1, 2, 3):
                store.record_decision(first["id"], peer_id, None, None, decision, peer_id)
            store.record_decision(second["id"], 4, None, None, decision, 4)
            task_id, _ = store.create_task("Continuous", 0)
            store.set_task_enabled(task_id, True)
            self.assertEqual({row["account_id"] for row in store.pending()}, {first["id"], second["id"]})

    def test_unavailable_account_is_removed_from_delivery_queue(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "followup.sqlite3")
            store.import_account("session", "Account", "/tmp/session.session")
            account = store.accounts()[0]
            decision = Decision(DialogStatus.CANDIDATE, "safe", datetime.now(timezone.utc) - timedelta(days=3))
            store.record_decision(account["id"], 42, None, None, decision, 42)
            task_id, _ = store.create_task("Continuous", 0)
            store.set_task_enabled(task_id, True)
            self.assertEqual(len(store.pending()), 1)
            self.assertEqual(store.disable_sending_for_account(account["id"], "auth_error"), 1)
            self.assertEqual(store.pending(), [])
            self.assertEqual(store.account(account["id"])["send_status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
