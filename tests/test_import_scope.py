import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from store import Store
from telegram_service import FollowupService, TelegramSettings


class ImportScopeTests(unittest.TestCase):
    def test_title_prefix_limits_dialoghub_import(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            sessions = root / "source_sessions"
            sessions.mkdir()
            hub = root / "dialoghub.sqlite3"
            db = sqlite3.connect(hub)
            db.execute("CREATE TABLE accounts (session_name TEXT, title TEXT, enabled INTEGER)")
            db.executemany("INSERT INTO accounts VALUES(?,?,1)", [("daniil", "Даниил ⌚",), ("other", "Лолита",)])
            db.commit(); db.close()
            for name in ("daniil", "other"):
                session = sqlite3.connect(sessions / f"{name}.session")
                session.execute("CREATE TABLE marker (value TEXT)"); session.commit(); session.close()
            store = Store(root / "data" / "followup.sqlite3")
            service = FollowupService(store, TelegramSettings(1, "hash", 10))
            self.assertEqual(service.import_dialoghub_accounts(str(hub), str(sessions), "Даниил"), 1)
            self.assertEqual(len(store.accounts()), 1)
            self.assertEqual(store.accounts()[0]["title"], "Даниил ⌚")


if __name__ == "__main__":
    unittest.main()
