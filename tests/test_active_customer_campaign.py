import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from active_customer_campaign import ActiveCustomerCampaign, SourceOverview, map_sales_rows, recent_client_message
from store import Store


class FakeTelegram:
    pass


class FakeClient:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.requests = []

    async def get_input_entity(self, destination):
        return destination

    async def __call__(self, request):
        self.requests.append(request)
        if self.fail:
            raise TimeoutError('network timeout after request')
        return SimpleNamespace(id=len(self.requests), updates=[])


class ActiveCustomerCampaignTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / 'bot.sqlite3')
        self.campaign = ActiveCustomerCampaign(
            self.store, FakeTelegram(), source_url='', sender_title='Local Traffic')

    def tearDown(self):
        self.temporary.cleanup()

    def test_two_sales_channels_map_to_one_project_and_tests_are_skipped(self):
        project = {'key': 'aquaapple::вологда', 'brand': 'Aqua Apple',
                   'city': 'Вологда', 'cities': ['Вологда']}
        rows = [[], ['', '', '', 'Aqua Apple Вологда ВК'],
                ['', '', '', 'Aqua Apple Вологда Авито'],
                ['', '', '', 'Aqua Apple Вологда тест Авито'],
                ['', '', '', 'другой магазин Вологда']]
        overview = map_sales_rows(rows, [project])
        self.assertEqual(list(overview.projects.values()), [[2, 3]])
        self.assertEqual(overview.skipped, 1)
        self.assertEqual(len(overview.unmatched), 1)

    def test_operator_mapping_requires_unchanged_row_name(self):
        project = {'key': 'aquaapple::вологда', 'brand': 'Aqua Apple',
                   'city': 'Вологда', 'cities': ['Вологда']}
        rows = [[], ['', '', '', 'Aqua Apple продажа']]
        good = map_sales_rows(rows, [project], {2: ('Aqua Apple продажа', project['key'])})
        stale = map_sales_rows(rows, [project], {2: ('Другое название', project['key'])})
        self.assertEqual(good.projects, {project['key']: [2]})
        self.assertEqual(len(stale.unmatched), 1)

    def test_only_incoming_non_team_message_proves_activity(self):
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=10)
        for sender, outgoing, age, expected in [
            (12, False, 1, True), (99, False, 1, False),
            (12, True, 1, False), (12, False, 11, False),
        ]:
            message = SimpleNamespace(sender_id=sender, out=outgoing,
                                      date=now - timedelta(days=age), action=None)
            self.assertEqual(recent_client_message(message, {99}, cutoff), expected)

    def test_reservation_dedupes_same_contact_across_projects(self):
        self.store.set_setting('active_customer_chat_text', 'Как у вас дела с трафиком?')
        self.store.set_setting('active_customer_private_text', 'Как дела с трафиком?')
        now = datetime.now(timezone.utc)
        self.assertTrue(self.campaign._reserve('project-a', 100, 200, [7], now))
        self.assertFalse(self.campaign._reserve('project-b', 101, 200, [8], now))
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM active_customer_deliveries').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM active_customer_messages').fetchone()[0], 2)

    def test_chat_then_private_completes_one_bundle(self):
        self.store.set_setting('active_customer_chat_text', 'Чат')
        self.store.set_setting('active_customer_private_text', 'ЛС')
        self.campaign._reserve('project-a', 100, 200, [7], datetime.now(timezone.utc))
        client = FakeClient()
        async def run():
            first = await self.campaign._send_message(client, 'project-a', 'chat', 100, 111, 'Чат')
            second = await self.campaign._send_message(client, 'project-a', 'private', 200, 222, 'ЛС')
            return first, second
        self.assertEqual(asyncio.run(run()), ('partial', 'sent'))
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM active_customer_deliveries').fetchone()[0], 'sent')
            self.assertEqual([row[0] for row in db.execute('SELECT status FROM active_customer_messages')], ['sent', 'sent'])

    def test_uncertain_send_is_never_left_reserved(self):
        self.store.set_setting('active_customer_chat_text', 'Чат')
        self.store.set_setting('active_customer_private_text', 'ЛС')
        self.campaign._reserve('project-a', 100, 200, [7], datetime.now(timezone.utc))
        with self.assertRaises(TimeoutError):
            asyncio.run(self.campaign._send_message(FakeClient(fail=True), 'project-a', 'chat', 100, 111, 'Чат'))
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM active_customer_deliveries').fetchone()[0], 'uncertain')
            self.assertEqual(db.execute("SELECT status FROM active_customer_messages WHERE kind='chat'").fetchone()[0], 'attempted')

    def test_one_time_snapshot_finishes_without_contacting_future_rows(self):
        self.store.set_setting('active_customer_chat_text', 'Чат')
        self.store.set_setting('active_customer_private_text', 'ЛС')
        self.store.set_setting('active_customer_enabled', '1')
        self.store.set_setting('active_customer_source_keys', '["old-project"]')
        self.store.set_setting('active_customer_work_start_hour', '0')
        self.store.set_setting('active_customer_work_end_hour', '24')
        self.store.set_setting('active_customer_max_per_day', '0')
        self.store.set_setting('global_paused', '0')
        self.campaign._manifest = lambda: {'projects': []}
        self.campaign.source_overview = lambda manifest: SourceOverview(
            {'old-project': [4], 'new-future-project': [5]}, (), 0)
        self.campaign._skip('old-project', 'no_recent_client_message')
        self.assertEqual(asyncio.run(self.campaign.run_once()), 'complete')


if __name__ == '__main__':
    unittest.main()
