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
            store.record_decision(account["id"], 123, "name", "Name", decision)
            store.record_decision(account["id"], 123, "name", "Name", decision)
            task_id, selected = store.create_task("Review first", 10)
            task = store.tasks()[0]
            self.assertEqual(task["id"], task_id)
            self.assertEqual(selected, 1)
            self.assertEqual(task["enabled"], 0)
            self.assertEqual(task["queued"], 1)
            self.assertEqual(store.setting("global_paused"), "1")
            self.assertEqual(store.setting("delivery_enabled"), "0")


if __name__ == "__main__":
    unittest.main()
