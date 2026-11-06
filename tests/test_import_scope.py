import asyncio
import json
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

    def test_cold_scan_excludes_local_traffic(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "followup.sqlite3")
            store.import_account("mailing", "Даниил 📞", "/tmp/mailing.session")
            store.import_account("local-traffic", "Local Traffic", "/tmp/local-traffic.session")
            service = FollowupService(store, TelegramSettings(1, "hash", 10))
            checked: list[int] = []
            scanned: list[int] = []

            async def check(account_id: int) -> str:
                checked.append(account_id)
                return "authorized"

            async def scan(account_id: int, *_args, **_kwargs) -> dict[str, int]:
                scanned.append(account_id)
                return {"candidate": 1}

            service.check_account = check
            service.scan_account = scan
            self.assertEqual(asyncio.run(service.scan_all()), {"candidate": 1})
            self.assertEqual(checked, scanned)
            self.assertEqual(len(checked), 1)
            self.assertEqual(store.account(checked[0])["title"], "Даниил 📞")

    def test_cold_scan_uses_explicit_sender_pool(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "followup.sqlite3")
            store.import_account("allowed", "Даниил 📞", "/tmp/allowed.session")
            store.import_account("unrelated", "Другой аккаунт", "/tmp/unrelated.session")
            store.set_setting("cold_sender_session_names", json.dumps(["allowed"]))
            service = FollowupService(store, TelegramSettings(1, "hash", 10))
            checked: list[int] = []

            async def check(account_id: int) -> str:
                checked.append(account_id)
                return "authorized"

            async def scan(_account_id: int, *_args, **_kwargs) -> dict[str, int]:
                return {"candidate": 1}

            service.check_account = check
            service.scan_account = scan
            self.assertEqual(asyncio.run(service.scan_all()), {"candidate": 1})
            self.assertEqual(store.account(checked[0])["session_name"], "allowed")


if __name__ == "__main__":
    unittest.main()
