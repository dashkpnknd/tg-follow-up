from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from openpyxl import Workbook
from openpyxl.styles import PatternFill

from renewal_followups import RenewalFollowupService
from store import Store, project_chat_match_score


RED = PatternFill(fill_type="solid", fgColor="FFFF0000")
PALE_RED = PatternFill(fill_type="solid", fgColor="FFF4CCCC")
WHITE = PatternFill(fill_type="solid", fgColor="FFFFFFFF")
GREEN = PatternFill(fill_type="solid", fgColor="FFD9EAD3")


class RenewalSheetTests(unittest.TestCase):
    def test_red_is_immediate_white_waits_until_next_month_and_green_is_ignored(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "followup.sqlite3")
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = "Октябрь"
            for row, channel, project, amount, fill in (
                (4, "ВК", "Красный магазин", 25000, RED),
                (5, "Авито", "Белый магазин", 30000, WHITE),
                (6, "ВК", "Зелёный магазин", 30000, GREEN),
            ):
                sheet.cell(row, 2, channel)
                sheet.cell(row, 3, project).fill = fill
                sheet.cell(row, 4, amount)
            service = RenewalFollowupService(store, None, sheet_xlsx_url="https://example.test/export.xlsx", sender_account_title="Local Traffic")
            service._workbook = lambda: workbook
            totals = service.scan_sheet(date(2026, 10, 19))
            self.assertEqual(totals["red"], 1)
            self.assertEqual(totals["white"], 1)
            self.assertEqual(totals["ignored"], 1)
            statuses = store.renewal_summary()
            self.assertEqual(statuses, {"new": 1, "waiting": 1})
            white = next(item for item in store.renewal_followups_to_resolve() if item["project_name"] == "Красный магазин")
            self.assertEqual(white["trigger_kind"], "red")

    def test_old_white_rows_expire_instead_of_sending_late(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "followup.sqlite3")
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = "Сентябрь"
            sheet.cell(4, 2, "ВК")
            sheet.cell(4, 3, "Старый белый").fill = WHITE
            sheet.cell(4, 4, 25000)
            service = RenewalFollowupService(store, None, sheet_xlsx_url="https://example.test/export.xlsx", sender_account_title="Local Traffic")
            service._workbook = lambda: workbook
            service.scan_sheet(date(2026, 10, 19))
            self.assertEqual(store.renewal_summary(), {"expired": 1})

    def test_waiting_white_row_expires_after_an_outage_longer_than_grace(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "followup.sqlite3")
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = "Октябрь"
            sheet.cell(4, 2, "ВК")
            sheet.cell(4, 3, "Белый магазин").fill = WHITE
            sheet.cell(4, 4, 25000)
            service = RenewalFollowupService(store, None, sheet_xlsx_url="https://example.test/export.xlsx", sender_account_title="Local Traffic")
            service._workbook = lambda: workbook

            service.scan_sheet(date(2026, 10, 19))
            self.assertEqual(store.renewal_summary(), {"waiting": 1})

            # The November 5 due date is now more than seven days old.
            service.scan_sheet(date(2026, 11, 20))
            self.assertEqual(store.renewal_summary(), {"expired": 1})

    def test_red_templates_rotate(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "followup.sqlite3")
            templates = store.renewal_templates("red")
            self.assertEqual(len(templates), 5)
            self.assertEqual(store.next_renewal_template("red"), templates[0])

    def test_pale_red_is_the_same_immediate_followup_status(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "followup.sqlite3")
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = "Октябрь"
            sheet.cell(4, 2, "ВК")
            sheet.cell(4, 3, "Светло-красный магазин").fill = PALE_RED
            sheet.cell(4, 4, 25000)
            service = RenewalFollowupService(store, None, sheet_xlsx_url="https://example.test/export.xlsx", sender_account_title="Local Traffic")
            service._workbook = lambda: workbook
            service.scan_sheet(date(2026, 10, 21))
            self.assertEqual(store.renewal_summary(), {"new": 1})

    def test_month_sheet_title_can_include_a_year(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "followup.sqlite3")
            service = RenewalFollowupService(store, None, sheet_xlsx_url="https://example.test/export.xlsx", sender_account_title="Local Traffic")
            self.assertEqual(service._renewal_due_date("Ноябрь 2026", date(2026, 11, 12)), date(2026, 12, 5))
            self.assertEqual(service._renewal_due_date("Октябрь 2026", date(2026, 11, 12)), date(2026, 11, 5))


class RenewalMatchingTests(unittest.TestCase):
    def service(self):
        return RenewalFollowupService(None, None, sheet_xlsx_url="https://example.test/export.xlsx", sender_account_title="Local Traffic")

    def test_duplicate_group_is_matched_by_channel_marker(self):
        service = self.service()
        self.assertTrue(service._matches_channel(SimpleNamespace(title="Магазин — AVITO", username=None), "Авито"))
        self.assertTrue(service._matches_channel(SimpleNamespace(title="Магазин", username="shop_vk"), "ВК"))
        self.assertFalse(service._matches_channel(SimpleNamespace(title="Магазин VK", username=None), "Авито"))
        self.assertTrue(service._has_known_channel_marker(SimpleNamespace(title="Магазин VK", username=None)))
        self.assertFalse(service._has_known_channel_marker(SimpleNamespace(title="Магазин", username=None)))

    def test_project_title_match_removes_service_suffixes_and_accepts_specific_extensions(self):
        self.assertEqual(project_chat_match_score("Update-Tula", "Update Tula VK Target"), 100)
        self.assertEqual(project_chat_match_score("King Store Ufa", "King Store, Avito, Ufa, Казань"), 90)
        self.assertEqual(project_chat_match_score("King Store Ufa", "Другой Store Ufa"), 0)

    def test_unique_largest_private_history_is_selected(self):
        contacts = [(11, None, None, "One"), (22, None, None, "Two"), (33, None, None, "Three")]
        selected = self.service()._unique_most_active_contact(contacts, {11: 4, 22: 9, 33: 5})
        self.assertEqual(selected, (contacts[1], 9))

    def test_tied_largest_private_histories_stay_for_review(self):
        contacts = [(11, None, None, "One"), (22, None, None, "Two")]
        self.assertIsNone(self.service()._unique_most_active_contact(contacts, {11: 7, 22: 7}))


if __name__ == "__main__":
    unittest.main()
