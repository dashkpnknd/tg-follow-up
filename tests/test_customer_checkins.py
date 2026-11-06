from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from customer_checkins import CheckinSettings, CustomerCheckinService, column_index, parse_amount, parse_sheet_date
from store import Store, project_chat_key


class CustomerCheckinParsingTests(unittest.TestCase):
    def test_chat_name_matching_ignores_traffic_channel_spelling(self):
        self.assertEqual(
            project_chat_key("Sotiks Иваново Авито"),
            project_chat_key("SOTIKS | Иваново | ВК / Avito"),
        )
        self.assertEqual(
            project_chat_key("Apple Rostov - Ростов-на-Дону ВК"),
            project_chat_key("Apple-Rostov | Ростов-на-Дону | VK"),
        )
        self.assertNotEqual(
            project_chat_key("Sotiks Иваново Авито"),
            project_chat_key("Sotiks Балаково Авито"),
        )

    def test_sheet_values_use_existing_b_d_e_columns(self):
        self.assertEqual(column_index("B"), 1)
        self.assertEqual(column_index("D"), 3)
        self.assertEqual(parse_amount("р.17 500,00"), 17500)
        self.assertEqual(parse_sheet_date("30 августа", 2026), date(2026, 8, 30))
        self.assertEqual(parse_sheet_date("1 декабря", 2026, date(2026, 10, 19)), date(2025, 12, 1))

    def test_all_sales_except_tests_and_topups_are_imported_once(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sales.csv"
            source.write_text(
                "flag,date,earned,project,deal\n"
                "0,1 октября 2026,\"р.6 250,00\",Нормальный магазин ВК,\"р.12 500,00\"\n"
                "0,1 октября 2026,\"р.6 250,00\",Магазин тест,\"р.12 500,00\"\n"
                "0,1 октября 2026,\"р.2 500,00\",Магазин Нейро-продавец,\"р.5 000,00\"\n"
                "0,1 октября 2026,\"р.2 500,00\",Магазин доплата,\"р.5 000,00\"\n"
                "0,30 сентября 2026,\"р.6 250,00\",Старая продажа ВК,\"р.12 500,00\"\n",
                encoding="utf-8",
            )
            store = Store(root / "followup.sqlite3")
            service = CustomerCheckinService(
                store, None,
                CheckinSettings(sales_csv_path=str(source), sender_account_title="Local Traffic", sheet_year=2026, start_date="2026-10-01"),
            )
            first = service.import_due_sales(date(2026, 10, 15))
            second = service.import_due_sales(date(2026, 10, 15))
            self.assertEqual(first["imported"], 2)
            self.assertEqual(first["skipped"], 3)
            self.assertEqual(second["existing"], 2)
            self.assertEqual(store.checkin_summary(), {"new": 2})

    def test_only_agreed_exceptions_stay_out_of_automatic_campaign(self):
        allowed = CustomerCheckinService._is_approved_traffic_start
        self.assertTrue(allowed("Новый магазин Авито"))
        self.assertTrue(allowed("Магазин Нейро-продавец"))
        self.assertTrue(allowed("Магазин BTF ВК"))
        self.assertTrue(allowed("Текущий клиент: новый запуск"))
        self.assertFalse(allowed("Магазин тестовый"))
        self.assertFalse(allowed("Магазин доплаты"))
        self.assertFalse(allowed("Магазин ВК (оплатил вторую часть)"))
        self.assertFalse(allowed("Магазин ВК (2 часть)"))
        self.assertFalse(allowed("Магазин ВК (повторная оплата)"))

    def test_due_window_can_catch_up_after_a_short_outage(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sales.csv"
            source.write_text(
                "flag,date,earned,project,deal\n"
                "0,1 октября 2026,0,Магазин один,10000\n"
                "0,2 октября 2026,0,Магазин два,10000\n",
                encoding="utf-8",
            )
            service = CustomerCheckinService(
                Store(root / "followup.sqlite3"), None,
                CheckinSettings(sales_csv_path=str(source), sender_account_title="Local Traffic", sheet_year=2026, start_date="2026-10-01"),
            )
            result = service.import_due_sales(date(2026, 10, 17), date(2026, 10, 15))
            self.assertEqual(result["imported"], 2)

    def test_team_usernames_are_excluded_from_customer_choice(self):
        with TemporaryDirectory() as directory:
            service = CustomerCheckinService(
                Store(Path(directory) / "followup.sqlite3"), None,
                CheckinSettings(
                    excluded_usernames=("localtraffic", "gelikky", "ksenia_locallead"),
                    excluded_names=("даниил гилемзянов",),
                ),
            )
            self.assertTrue(service.is_excluded_username("@LocalTraffic"))
            self.assertTrue(service.is_excluded_username("GELIKKY"))
            self.assertTrue(service.is_excluded_contact(None, "Даниил Гилемзянов"))
            self.assertFalse(service.is_excluded_username("customer_name"))


if __name__ == "__main__":
    unittest.main()
