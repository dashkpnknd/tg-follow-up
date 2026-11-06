from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from store import Store


class CustomerCheckinStoreTests(unittest.TestCase):
    def test_customer_texts_rotate_in_order_and_are_saved_with_send(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "followup.sqlite3")
            store.set_checkin_templates(["Первый", "Второй", "Третий"])
            self.assertEqual(store.next_checkin_template(), "Первый")
            for index, expected in enumerate(("Первый", "Второй", "Третий"), start=1):
                checkin_id, _ = store.upsert_customer_checkin(
                    source_key=f"sale-{index}", source_row=index, sale_date="2026-10-01",
                    due_date="2026-10-15", amount=12000, project_name=f"Project {index}",
                )
                store.mark_checkin_sent(checkin_id, index, expected)
            self.assertEqual(store.next_checkin_template(), "Первый")
            self.assertEqual(store.checkin(2)["sent_template"], "Второй")

    def test_same_sales_row_is_recorded_once_without_sheet_columns(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "followup.sqlite3")
            first, created_first = store.upsert_customer_checkin(
                source_key="stable-fingerprint", source_row=150, sale_date="2026-10-01",
                due_date="2026-10-15", amount=12000, project_name="Shop Pro ВК",
            )
            second, created_second = store.upsert_customer_checkin(
                source_key="stable-fingerprint", source_row=151, sale_date="2026-10-01",
                due_date="2026-10-15", amount=12000, project_name="Shop Pro ВК",
            )
            self.assertEqual(first, second)
            self.assertTrue(created_first)
            self.assertFalse(created_second)
            self.assertEqual(store.checkin_summary(), {"new": 1})

    def test_same_store_two_channels_on_one_day_are_merged(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "followup.sqlite3")
            first, first_created = store.upsert_customer_checkin(
                source_key="shop-vk", source_row=1, sale_date="2026-10-08",
                due_date="2026-10-22", amount=25000, project_name="Sotiks Иваново ВК",
            )
            second, second_created = store.upsert_customer_checkin(
                source_key="shop-avito", source_row=2, sale_date="2026-10-08",
                due_date="2026-10-22", amount=35000, project_name="Sotiks Иваново Авито",
            )
            self.assertTrue(first_created)
            self.assertFalse(second_created)
            self.assertEqual(store.checkin(first)["status"], "new")
            self.assertEqual(store.checkin(second)["status"], "merged")

    def test_ready_checkin_is_not_available_before_its_due_date(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "followup.sqlite3")
            store.import_account("local", "Local Traffic", "/tmp/local.session")
            account = store.account_by_title("Local Traffic")
            with store.connect() as db:
                db.execute("UPDATE accounts SET auth_status='authorized' WHERE id=?", (account["id"],))
            checkin_id, _ = store.upsert_customer_checkin(
                source_key="future-sale", source_row=1, sale_date="2026-10-08",
                due_date="2026-10-22", amount=25000, project_name="Sotiks Иваново ВК",
            )
            store.mark_checkin_ready(
                checkin_id, account_id=account["id"], group_peer_id=1, group_title="Sotiks",
                group_username=None, peer_id=2, access_hash=3, username="valeria", name="Валерия",
            )
            self.assertEqual(store.pending_checkin_delivery("2026-10-21"), [])
            self.assertEqual(len(store.pending_checkin_delivery("2026-10-22")), 1)

    def test_operator_can_choose_or_block_without_writing_to_sales_sheet(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "followup.sqlite3")
            store.import_account("local", "Local Traffic", "/tmp/local.session")
            account = store.account_by_title("Local Traffic")
            checkin_id, _ = store.upsert_customer_checkin(
                source_key="project-one", source_row=160, sale_date="2026-10-01",
                due_date="2026-10-15", amount=15000, project_name="Project One",
            )
            store.mark_checkin_review(
                checkin_id, "Два человека", [(1, 100, "one", "One"), (2, 200, "two", "Two")],
                account_id=account["id"], group_peer_id=10, group_title="Project One", group_username="project_one",
            )
            self.assertEqual(len(store.checkin_candidates(checkin_id)), 2)
            self.assertEqual(store.checkin(checkin_id)["group_username"], "project_one")
            store.block_checkin_project(checkin_id, 99)
            self.assertEqual(store.checkin(checkin_id)["status"], "blocked")
            later_id, _ = store.upsert_customer_checkin(
                source_key="project-one-again", source_row=190, sale_date="2026-11-01",
                due_date="2026-11-15", amount=15000, project_name="project-one",
            )
            self.assertEqual(store.checkin(later_id)["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
