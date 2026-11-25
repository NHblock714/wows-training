from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from wows_training.query_center import QueryCenter, player_target
from wows_training.workbench_store import WorkbenchStore


class QueryHistoryTests(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.store = WorkbenchStore(Path(scratch.name))
        self.query = QueryCenter(SimpleNamespace(store=self.store))

    def snapshot(self, region, identifier, name='Player'):
        return self.store.save_run('career_snapshot', name, {'region': region, 'account_id': identifier, 'nickname': name})['id']

    def clan(self, region, identifier, members):
        roster = {account: {'account_id': account} for account in members}
        public_stats = {account: {'statistics': {'pvp': {'battles': 8}}} for account in members}
        result = self.store.save_run('clan_lookup', 'Test clan', {
            'region': region, 'clan_info': {'clan_id': identifier, 'members': roster}, 'batch_players': public_stats,
        })['id']
        fleets = self.store.save_run('clan_lookup_fleets', 'Test fleets', {
            'clan_snapshot_id': result, 'members': {
                account: {'status': 'ok', 'snapshot_id': snapshot} for account, snapshot in members.items()
            },
        })['id']
        return result, fleets

    def test_player_clear_removes_same_region_stats_and_retains_other_data(self):
        player = player_target('asia', '123', 'Player')
        self.query.toggle_bookmark(player)
        own = self.snapshot('asia', '123')
        old = self.snapshot('asia', 123)
        other_player = self.snapshot('asia', '124')
        other_region = self.snapshot('eu', '123')
        clan, fleets = self.clan('asia', '123', {'123': own, '124': other_player})
        outside_clan, outside_fleets = self.clan('eu', '123', {'123': other_region})
        dossier = self.store.save_document('player', 'Keep dossier', {'account_id': '123'})
        report = self.store.save_run('career_ai', 'Keep report', {'snapshot_id': own})

        self.assertEqual(self.query.clear_history(player), 4)
        for identifier in (own, old):
            with self.assertRaises(KeyError):
                self.store.get_run(identifier)
        for identifier in (other_player, other_region, report['id']):
            self.store.get_run(identifier)
        self.assertEqual(self.query.bookmarks(), [player])
        self.assertEqual(self.store.get_document(dossier['id']), dossier)
        self.assertNotIn(self.query.key(player), {self.query.key(target) for target in self.query.directory()})
        saved_clan = self.store.get_run(clan)['payload']
        self.assertEqual(set(saved_clan['clan_info']['members']), {'123', '124'})
        self.assertEqual(set(saved_clan['batch_players']), {'124'})
        self.assertEqual(set(self.store.get_run(fleets)['payload']['members']), {'124'})
        self.assertIn('123', self.store.get_run(outside_clan)['payload']['batch_players'])
        self.assertIn('123', self.store.get_run(outside_fleets)['payload']['members'])
        self.assertEqual(self.query.clear_history(player), 0)

    def test_clan_clear_removes_its_versions_and_fleets_without_touching_players(self):
        member = self.snapshot('asia', '123')
        matches = [self.clan('asia', clan_id, {'123': member}) for clan_id in ('123', 123)]
        peers = [self.clan('asia', '124', {}), self.clan('na', '123', {})]
        target = {'kind': 'clan', 'region': 'asia', 'id': '123', 'name': 'Test clan'}
        self.query.toggle_bookmark(target)

        self.assertEqual(self.query.clear_history(target), 4)
        for pair in matches:
            for identifier in pair:
                with self.assertRaises(KeyError):
                    self.store.get_run(identifier)
        for pair in peers:
            for identifier in pair:
                self.store.get_run(identifier)
        self.store.get_run(member)
        self.assertIsNone(self.query.cached_clan('asia', '123'))
        self.assertEqual(self.query.bookmarks(), [target])

    def test_clear_rejects_invalid_region_kind_or_identity(self):
        for target in (None, {'kind': 'all', 'region': 'asia', 'id': '123'},
                       {'kind': 'clan', 'region': 'bad', 'id': '123'},
                       {'kind': 'player', 'region': 'asia', 'id': 'invalid'}):
            with self.subTest(target=target), self.assertRaises(ValueError):
                self.query.clear_history(target)


if __name__ == '__main__':
    unittest.main()
