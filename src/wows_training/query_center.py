"""Public search, bookmarks and versioned clan results used by the desktop lookup."""
from copy import deepcopy
import json
from pathlib import Path
import re

from .career_service import _cancel, _identity


REGION_NAMES = {'亚服': 'asia', '欧服': 'eu', '美服': 'na', '北美': 'na', 'com': 'na'}


def player_target(region, account_id, nickname):
    region, account_id = _identity(REGION_NAMES.get(region, region), account_id)
    if not isinstance(nickname, str) or not nickname.strip() or len(nickname) > 128:
        raise ValueError('账号昵称无效')
    return {'kind': 'player', 'region': region, 'id': account_id, 'name': nickname.strip()}


class QueryCenter:
    def __init__(self, career):
        self.career = career
        self.store = career.store

    def directory(self):
        """Enumerate identities without deserializing and scoring every stored fleet."""
        rows = {}
        with self.store._connection() as connection:
            query = """SELECT kind,json_extract(payload_json,'$.region') AS region,
                       json_extract(payload_json,'$.account_id') AS aid,
                       json_extract(payload_json,'$.nickname') AS nickname,
                       json_extract(payload_json,'$.clan_info.clan_id') AS clan_id,
                       json_extract(payload_json,'$.clan_info.tag') AS tag,
                       json_extract(payload_json,'$.clan_info.name') AS clan_name
                       FROM runs WHERE kind IN ('career_snapshot','clan_lookup')
                       ORDER BY created_at,id"""
            for entry in connection.execute(query):
                region = entry['region']
                if entry['kind'] == 'career_snapshot':
                    target = player_target(region, entry['aid'], entry['nickname'] or str(entry['aid']))
                else:
                    target = {'kind': 'clan', 'region': region, 'id': str(entry['clan_id']),
                              'name': self.clan_name(entry['tag'], entry['clan_name'])}
                rows[self.key(target)] = target
        return sorted(rows.values(), key=lambda item: (item['name'].casefold(), item['region'], item['kind']))

    @staticmethod
    def key(target):
        return target['kind'], target['region'], str(target['id'])

    @staticmethod
    def clan_name(tag, name):
        return ' '.join(part for part in (f'[{tag}]' if tag else '', str(name or '')) if part) or '未知公会'

    def bookmarks(self):
        docs = self.store.list_documents('query_bookmarks')
        return docs[-1]['payload'].get('items', []) if docs else []

    def _publish_bookmarks(self, entries):
        docs = self.store.list_documents('query_bookmarks')
        self.store.save_document('query_bookmarks', '常用查询', {'items': entries}, docs[-1]['id'] if docs else None)

    def toggle_bookmark(self, target):
        items = self.bookmarks()
        remaining = [item for item in items if self.key(item) != self.key(target)]
        added = len(remaining) == len(items)
        if added:
            remaining.append(deepcopy(target))
        self._publish_bookmarks(remaining)
        return added

    def import_bookmarks(self, filename):
        path = Path(filename)
        if path.stat().st_size > 1024 * 1024:
            raise ValueError('收藏文件过大')
        entries = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(entries, list) or len(entries) > 2000:
            raise ValueError('请选择原项目的 quick_queries.json')
        prepared = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError('收藏条目格式无效')
            prepared.append(player_target(entry.get('region'), entry.get('account_id'), entry.get('nickname')))
        old = self.bookmarks()
        keys = {self.key(item) for item in old}
        added = 0
        for item in prepared:
            if self.key(item) not in keys:
                old.append(item)
                keys.add(self.key(item))
                added += 1
        if added:
            self._publish_bookmarks(old)
        return added

    def search(self, region, text, kind='all', cancel=None):
        text = text.strip()
        if text.isascii() and text.isdecimal():
            region, identifier = _identity(region, text)
            kinds = ('player', 'clan') if kind == 'all' else (kind,)
            return {'candidates': [{'kind': k, 'region': region, 'id': identifier, 'name': identifier} for k in kinds], 'messages': []}
        candidates, messages = [], []
        for group, lookup in (('player', self.career.search), ('clan', self.career.search_clans)):
            if kind not in ('all', group):
                continue
            if group == 'player' and kind == 'all' and not re.fullmatch('[A-Za-z0-9_]{3,25}',text):
                continue
            _cancel(cancel)
            response = lookup(region, text, cancel)
            if response['status'] != 'ok':
                messages.append((group, response['status'], response.get('reason')))
            for entry in (response.get('data') or {}).get('candidates', []):
                if group == 'player':
                    candidates.append(player_target(region, entry['account_id'], entry['nickname']))
                else:
                    candidates.append({'kind': 'clan', 'region': region, 'id': str(entry['clan_id']),
                                       'name': self.clan_name(entry.get('tag'), entry.get('name'))})
        return {'candidates': candidates, 'messages': messages}

    def cached_clan(self, region, clan_id):
        region, clan_id = _identity(region, clan_id)
        with self.store._connection() as db:
            match = db.execute("""SELECT id FROM runs WHERE kind='clan_lookup'
                AND json_extract(payload_json,'$.region')=?
                AND json_extract(payload_json,'$.clan_info.clan_id')=?
                ORDER BY created_at DESC,id DESC LIMIT 1""", (region, clan_id)).fetchone()
        if not match:
            return None
        record = self.store.get_run(match['id'])
        return dict(record['payload'], snapshot_id=record['id'])

    def refresh_clan(self, region, clan_id, cancel=None, progress=None):
        _cancel(cancel)
        client = self.career._client(cancel)
        roster = client.clan(region, clan_id)
        _cancel(cancel)
        if roster['status'] != 'ok':
            return roster
        clan = dict(roster['data'])
        members = {str(row['account_id']): dict(row,account_name=row.get('nickname')) for row in clan['members']}
        if len(members) != len(clan['members']):
            raise ValueError('公会成员名单含重复账号')
        clan['members'] = members
        if progress:
            progress(f'读取 {len(members)} 名成员的公开战绩…')
        batch = client.accounts_batch(region, list(members))
        _cancel(cancel)
        if batch['status'] != 'ok':
            return batch
        payload = {'status': 'ok', 'region': roster['region'], 'clan_info': clan,
                   'batch_players': batch['data']['players'], 'timestamp': roster['fetched_at_utc']}
        record = self.store.save_run('clan_lookup', self.clan_name(clan.get('tag'), clan.get('name')), payload)
        return dict(payload, snapshot_id=record['id'])

    def load_member_ships(self, clan, cancel, progress):
        settings = self.career.settings()
        if not settings['enabled'] or not settings['credential_present']:
            raise ValueError('请在查询设置中启用联网并保存查询凭据')
        previous = self.cached_member_ships(clan) or {}
        results = {}
        members = {aid: row for aid, row in clan['batch_players'].items()
                   if row.get('hidden_profile') is False and (row.get('statistics') or {}).get('pvp', {}).get('battles', 0) > 0}
        for index, (aid, row) in enumerate(members.items(), 1):
            _cancel(cancel)
            progress(f"读取成员船仓 {index}/{len(members)} · {row.get('nickname') or aid}")
            profile = self.career.refresh(clan['region'], aid, mode='pvp', cancel=cancel, record_failures=False)
            _cancel(cancel)
            ships_status = profile.get('ships_status')
            if profile.get('status') == 'private':
                results[aid] = {'status':'private', 'snapshot_id':None}
            elif ships_status == 'error' or profile.get('status') in ('error','not_fetched'):
                results[aid] = dict(previous.get(aid,{'snapshot_id':None,'status':'error'}), refresh_failed=True)
            else:
                results[aid] = {'snapshot_id': profile.get('snapshot_id'), 'status': ships_status}
        _cancel(cancel)
        self.store.save_run('clan_lookup_fleets', '公会成员船仓', {'clan_snapshot_id': clan['snapshot_id'], 'members': results})
        return results

    def cached_member_ships(self, clan):
        with self.store._connection() as db:
            match = db.execute("""SELECT payload_json FROM runs WHERE kind='clan_lookup_fleets'
                AND json_extract(payload_json,'$.clan_snapshot_id')=? ORDER BY created_at DESC,id DESC LIMIT 1""",
                (clan['snapshot_id'],)).fetchone()
        return json.loads(match['payload_json'])['members'] if match else None
