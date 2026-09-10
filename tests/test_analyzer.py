from datetime import datetime, timedelta, timezone
import unittest

from analyzer import DialogStatus, Message, classify


NOW = datetime(2026, 9, 10, tzinfo=timezone.utc)


class AnalyzerTests(unittest.TestCase):
    def message(self, outgoing, text, age_hours=72):
        return Message(1, NOW - timedelta(hours=age_hours), outgoing, text)

    def test_silent_old_dialog_is_candidate(self):
        self.assertEqual(classify([self.message(True, "Здравствуйте")], now=NOW).status, DialogStatus.CANDIDATE)

    def test_any_answer_is_excluded(self):
        history = [self.message(True, "Здравствуйте"), Message(2, NOW - timedelta(hours=71), False, "Сколько стоит?")]
        self.assertEqual(classify(history, now=NOW).status, DialogStatus.REPLIED)

    def test_application_marker_wins(self):
        self.assertEqual(classify([self.message(True, "@LocalTraffic")], now=NOW).status, DialogStatus.APPLICATION)

    def test_refusal_is_detected(self):
        history = [self.message(True, "Здравствуйте"), Message(2, NOW - timedelta(hours=70), False, "Спасибо, не нужно")]
        self.assertEqual(classify(history, now=NOW).status, DialogStatus.REFUSAL)

    def test_existing_followup_is_never_repeated(self):
        self.assertEqual(classify([self.message(True, "Фиксирую отказ?")], now=NOW).status, DialogStatus.FOLLOWUP_SENT)

    def test_fresh_dialog_is_excluded(self):
        self.assertEqual(classify([self.message(True, "Здравствуйте", 47)], now=NOW).status, DialogStatus.TOO_FRESH)


if __name__ == "__main__":
    unittest.main()
