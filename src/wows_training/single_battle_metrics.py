"""Single-replay gunnery, target damage, team contribution and regional comparisons."""
from collections import defaultdict
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import sys

from .contracts import outcome
from .ordinary_replay import CURRENT, OLD, number
from .replay_context_evidence import _fact


SETTLEMENT_KIND = 'replay_battle_settlement'
KINDS = frozenset({'replay_gunnery', 'replay_target_impact', 'replay_battle_contribution', 'replay_ship_benchmark',
                   SETTLEMENT_KIND})
_REGIONS = {'ASIA': 'asia', 'NA': 'na', 'US': 'na', 'EU': 'eu'}
_REGION_NAMES = {'asia': '亚服', 'na': '美服', 'eu': '欧服'}
_RIBBON_LABELS = {
    'MAIN_CALIBER': '主炮命中', 'TORPEDO': '鱼雷命中', 'BOMB': '航空炸弹命中', 'PLANE': '击落飞机',
    'CRIT': '瘫痪', 'FRAG': '击沉', 'BURN': '使敌舰起火', 'FLOOD': '造成进水', 'CITADEL': '命中装甲区',
    'BASE_DEFENSE': '防御', 'BASE_CAPTURE': '占领', 'BASE_CAPTURE_ASSIST': '协助占领', 'SUPPRESSED': '压制',
    'SECONDARY_CALIBER': '副炮命中', 'MAIN_CALIBER_OVER_PENETRATION': '主炮过穿',
    'MAIN_CALIBER_PENETRATION': '主炮击穿', 'MAIN_CALIBER_NO_PENETRATION': '主炮未击穿',
    'MAIN_CALIBER_RICOCHET': '主炮跳弹', 'BUILDING_KILL': '摧毁建筑', 'DETECTED': '发现敌舰',
    'BOMB_OVER_PENETRATION': '炸弹过穿', 'BOMB_PENETRATION': '炸弹击穿', 'BOMB_NO_PENETRATION': '炸弹未击穿',
    'BOMB_RICOCHET': '炸弹跳弹', 'ROCKET': '火箭弹命中', 'ROCKET_PENETRATION': '火箭弹击穿',
    'ROCKET_NO_PENETRATION': '火箭弹未击穿', 'SPLANE': '战斗机击落', 'BULGE': '命中鱼雷防护',
    'BOMB_BULGE': '炸弹命中鱼雷防护', 'ROCKET_BULGE': '火箭弹命中鱼雷防护', 'DBOMB': '深水炸弹命中',
    'ACOUSTIC_HIT': '声呐脉冲命中', 'DROP': '拾得增益', 'ROCKET_RICOCHET': '火箭弹跳弹',
    'ROCKET_OVER_PENETRATION': '火箭弹过穿', 'WAVE_KILL_TORPEDO': '震波摧毁鱼雷', 'WAVE_CUT_WAVE': '震波打断震波',
    'WAVE_HIT_VEHICLE': '震波命中战舰', 'ACOUSTIC_HIT_VEHICLE_NEW': '声呐命中一次',
    'ACOUSTIC_HIT_VEHICLE_CURR': '声呐命中两次', 'ACOUSTIC_HIT_VEHICLE_BLOCK': '声呐脉冲被中和',
    'ACID': '强酸命中', 'DBOMB_FULL_DAMAGE': '深水炸弹直接命中', 'DBOMB_PARTIAL_DAMAGE': '深水炸弹弹片伤害',
    'MINE': '水雷伤害', 'DEMINING_MINE': '摧毁水雷', 'DEMINING_MINEFIELD': '清除雷区',
    'TORPEDO_PHOTON_HIT': '光子鱼雷直接命中', 'TORPEDO_PHOTON_SPLASH': '光子鱼雷溅射命中',
    'AIM_PULSE_TORPEDO_PHOTON': '瞄准脉冲光子鱼雷', 'PHASER_LASER': '相位激光命中',
    'SHIELD_HIT': '命中护盾', 'SHIELD_REMOVED': '击毁护盾', 'ASSIST': '协助摧毁',
    'MISSILE': '导弹命中', 'SHOT_DOWN_MISSILE': '击落导弹',
}


def _gunnery(rows, entity):
    samples = [row['gunnery'] for row in rows if isinstance(row.get('gunnery'), dict)]
    support = []
    for shell, label in (('ap', 'AP'), ('cs', 'SAP'), ('he', 'HE')):
        pairs = {(s.get('hits_main_'+shell), s.get('shots_main_'+shell)) for s in samples}
        if len(pairs) != 1:
            continue
        hits, shots = pairs.pop()
        if not all(type(v) is int and v >= 0 for v in (hits, shots)) or hits > shots:
            continue
        item = {'label': label, 'numerator': hits, 'denominator': shots, 'value': hits/shots if shots else None}
        damage = _unique_result([r['dealt_damage_by_field'] for r in rows if isinstance(r.get('dealt_damage_by_field'), dict)],
                                'damage_main_'+shell)
        if number(damage) and hits:
            item.update(dealt_amount=damage, mean=damage/hits)
        support.append(item)
    count = sum(r['denominator'] for r in support)
    if not count:
        return None
    hits = sum(r['numerator'] for r in support)
    coverage = '主炮' if len(support) == 3 else '有结算记录的主炮弹种'
    parts = [f"{r['label']} {r['numerator']}/{r['denominator']}（{r['value']:.1%}）"
             + (f"，每发命中平均直伤 {r['mean']:,.0f}" if 'mean' in r else '') for r in support if r['denominator']]
    text = f'{coverage}命中 {hits}/{count} 发（{hits/count:.1%}）。分弹种为'+'、'.join(parts)+'。'
    return _fact('replay_gunnery', text, None, {'entity_id': entity, 'support': support, 'numerator': hits,
                 'denominator': count, 'value': hits/count, 'known_fields': len(support), 'expected_fields': 3,
                 'unit': 'shell_hits_per_shell_fired', 'source_fields': ['shots_main_{ap,cs,he}', 'hits_main_{ap,cs,he}', 'damage_main_{ap,cs,he}']}, [entity])


def _unique_result(rows, key):
    values = [r[key] for r in rows if key in r and r[key] is not None]
    return values[0] if values and all(v == values[0] for v in values[1:]) else None


def settlement_fact(report, review, participant, player, rows, entity):
    version = review.get('version')
    metrics = rows if version == CURRENT else [r.get('battle_metrics') or {} for r in rows]
    # Public exp is the scoreboard XP after the battle-result multiplier. Raw
    # exp is the score before that multiplier; keep it separate from base XP.
    summary = {key: _unique_result(metrics, field) for key, field in (
        ('damage', 'damage'), ('base_xp', 'exp'), ('scouting_damage', 'scouting_damage'),
        ('ships_killed', 'ships_killed'), ('life_time_sec', 'life_time_sec'))}
    for key, value in summary.items():
        if not number(value) or key in ('base_xp', 'ships_killed') and type(value) is not int:
            summary[key] = None
    native = _unique_result(rows, 'ribbons')
    ribbons = None
    if isinstance(native, dict) and all(type(k) is str and type(v) is int and number(v) and v > 0 for k, v in native.items()):
        ribbons = []
        for key, count in native.items():
            label = _RIBBON_LABELS.get(key.removeprefix('RIBBON_'))
            if label:
                ribbons.append({'key': key, 'label': label, 'count': count})
        if len(ribbons) != len(native):
            ribbons = None
    contribution = _unique_result(rows, 'contribution')
    contribution = contribution if isinstance(contribution, dict) else {}
    raw_xp = contribution.get('raw_exp')
    raw_xp = raw_xp if type(raw_xp) is int and number(raw_xp) else None
    ribbon_metrics = []
    for key, label, value in (('ships_killed', '击沉', summary['ships_killed']),
                              ('first_ships_spotted_by_ship', '首先发现敌舰', contribution.get('first_ships_spotted_by_ship'))):
        if type(value) is int and number(value) and value > 0:
            ribbon_metrics.append({'key': key, 'label': label, 'count': value})
    common = report.get('post_battle_common') or {}
    winner, team = common.get('winner_team_id'), player.get('teamId')
    known_source = {CURRENT: 'verified_15.10_common_results', OLD: 'verified_15.9_common_results'}.get(version)
    verified = (known_source is not None and report.get('winner_source') == known_source
                and not report.get('post_battle_result_errors') and bool(report.get('arena_id'))
                and str(common.get('arena_id')) == str(report['arena_id'])
                and type(winner) is int and winner in (-1, 0, 1) and report.get('winner_absolute_team') == winner)
    result = outcome(winner, team) if verified and type(team) is int and team in (0, 1) else 'unknown'
    duration = common.get('duration_sec') if verified and number(common.get('duration_sec')) else None
    if result == 'unknown' and ribbons is None and all(value is None for value in summary.values()):
        return None
    title = '战斗结算'
    ship = participant.get('ship')
    map_name, mode = review.get('map'), review.get('mode')
    subtitle = ' · '.join(v for v in (ship, map_name, mode) if v and v != '未知')
    fields = ['playersPublicInfo.damage', 'playersPublicInfo.exp', 'playersPublicInfo.ships_killed',
              'playersPublicInfo.scouting_damage', 'playersPublicInfo.life_time_sec']
    if ribbons is not None:
        fields.append('playersPublicInfo SubRibbon suffix')
    if raw_xp is not None:
        fields.append('playersPublicInfo.raw_exp')
    if verified:
        fields.extend(('commonList.winner_team_id', 'commonList.duration_sec'))
    parts = [f'{label} {summary[key]:,.0f}' for key, label in (
        ('damage', '伤害'), ('base_xp', '基础经验'), ('scouting_damage', '侦察伤害'), ('ships_killed', '击沉'))
             if summary[key] is not None]
    if ribbons:
        parts.extend(f"{item['label']} ×{item['count']}" for item in ribbons[:8])
    return _fact(SETTLEMENT_KIND, '，'.join(parts) + '。' if parts else '录像包含已核对的赛后结算。', None,
                 {**summary, 'entity_id': entity, 'name': participant['name'], 'ship_name': ship, 'map_name': map_name,
                  'mode': mode, 'version': version, 'outcome': result if result != 'unknown' else None, 'duration': duration,
                  'title': title, 'subtitle': subtitle, 'raw_xp': raw_xp, 'settled_exp': None,
                  'ribbons': ribbons, 'ribbon_metrics': ribbon_metrics, 'source_fields': fields}, [entity])


def _verified_targets(rows, settled_damage, enemies):
    targets = _unique_result(rows, 'target_results')
    if not isinstance(targets, list) or not number(settled_damage):
        return None
    seen, output = set(), {}
    for target in targets:
        key, amount = target.get('entity_id'), target.get('damage')
        if type(key) is not int or key in seen or not number(amount):
            return None
        seen.add(key)
        if amount:
            if key not in enemies:
                return None
            output[key] = amount
    return output if sum(output.values()) == settled_damage else None


def _impact(people, players, rows, focus, review):
    team = players[focus].get('teamId')
    if type(team) is not int or team not in (0, 1):
        return []
    enemies = {e for e,p in players.items() if p.get('teamId') == 1-team}
    allies = {e for e,p in players.items() if p.get('teamId') == team}
    observed = {'dealt': defaultdict(list), 'received': defaultdict(list)}
    for hit in review.get('damage_events') or []:
        if hit['actor'] == focus and hit['target'] in enemies:
            observed['dealt'][hit['target']].append(hit)
        if hit['target'] == focus and hit['actor'] in enemies:
            observed['received'][hit['actor']].append(hit)
    outgoing = _verified_targets(rows[focus], people[focus]['result'].get('damage'), enemies)
    incoming = {}
    for other in enemies:
        attacks = _verified_targets(rows[other], people[other]['result'].get('damage'), allies)
        if attacks is None:
            incoming = None
            break
        if focus in attacks:
            incoming[other] = attacks[focus]
    facts = []
    for direction, amounts, title in (('dealt', outgoing, '输出'), ('received', incoming, '承伤')):
        settled = amounts is not None
        stream = observed[direction]
        if not settled:
            amounts = {other: sum(h['amount'] for h in hits) for other,hits in stream.items()}
        if not amounts:
            continue
        support = []
        for other, damage in amounts.items():
            hp = players[other if direction == 'dealt' else focus].get('maxHealth')
            hp = hp if number(hp) and hp > 0 else None
            item = {'target_id': other, 'ship_name': people[other]['ship'], 'label': people[other]['ship'],
                    'numerator': damage, 'denominator': hp, 'share': damage/hp if hp else None}
            clocks = [h['clock'] for h in stream.get(other, ())]
            if clocks:
                item.update(clock_start=min(clocks), clock_end=max(clocks), count=len(clocks), time_basis='visible_damage_callbacks')
            support.append(item)
        support.sort(key=lambda r: (-r['numerator'], r['target_id']))
        basis = '逐目标结算' if settled else '该视角的伤害记录'
        lead = '对' if direction == 'dealt' else '来自'
        subject = '目标' if direction == 'dealt' else '本舰'
        parts = [f"{lead}{r['label']} {r['numerator']:,.0f}" + (f"（相当于{subject}最大血量的 {r['share']:.1%}）" if r['share'] is not None else '')
                 for r in support[:2]]
        text = f"{basis}可核对 {len(support)} 艘敌舰的{title}共 {sum(amounts.values()):,.0f}。"+'、'.join(parts)+'。'
        facts.append(_fact('replay_target_impact', text, None, {'entity_id': focus, 'direction': direction,
                     'basis': 'reconciled_target_settlement' if settled else 'visible_damage_callbacks',
                     'count': len(support), 'value': sum(amounts.values()), 'support': support[:8],
                     'source_fields': ['interactions', 'damage', 'roster.maxHealth', 'observed_damage'] if settled else ['observed_damage', 'roster.maxHealth']},
                     [focus, *(r['target_id'] for r in support[:8])]))
    return facts


def _contribution(people, rows, players, entity):
    value = _unique_result(rows[entity], 'contribution')
    if not isinstance(value, dict):
        return None
    result = people[entity]['result']
    support, parts = [], []
    for key, label in (('raw_exp', '原始经验'), ('first_ships_spotted_by_ship', '本舰首先发现敌舰'),
                       ('capture_points', '占领分'), ('dropped_capture_points', '防守分')):
        if number(value.get(key)):
            support.append({'field':key, 'label':label, 'value':value[key]})
            text = f'{label} {value[key]:,.0f}'+(' 艘' if key == 'first_ships_spotted_by_ship' else '')
            if key == 'raw_exp' and number(result.get('exp')):
                text = f"结算经验 {result['exp']:,.0f}（{text}）"
            parts.append(text)
    for key, label in (('agro_art', '炮弹潜在伤害'), ('agro_tpd', '鱼雷潜在伤害')):
        if number(value.get(key)) and value[key] > 0:
            support.append({'field':key, 'label':label, 'value':value[key]})
            parts.append(f'{label} {value[key]:,.0f}')
    team = players[entity].get('teamId')
    allies = [p for key,p in people.items() if key in players and players[key].get('teamId') == team]
    for key, label in (('damage', '输出'), ('scouting_damage', '侦察伤害')):
        numbers = [p['result'].get(key) for p in allies]
        if type(team) is int and team in (0,1) and all(number(n) for n in numbers) and sum(numbers) > 0 and number(result.get(key)):
            total = sum(numbers)
            support.append({'field':key, 'label':label, 'numerator':result[key], 'denominator':total, 'share':result[key]/total})
            parts.append(f"{label} {result[key]:,.0f}，占本方已核对 {len(allies)} 艘舰合计的 {result[key]/total:.1%}")
    if not parts:
        return None
    return _fact('replay_battle_contribution', '；'.join(parts)+'。', None,
                 {'entity_id':entity, 'support':support, 'source_fields':['playersPublicInfo', 'team_id']}, [entity])


@lru_cache(maxsize=1)
def _sample():
    base = Path(sys._MEIPASS) if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[2]
    try:
        content = (base/'career_data'/'regional_random_15_9.json').read_bytes()
    except OSError:
        return {}
    if hashlib.sha256(content).hexdigest() != 'c7c65dbd766fc9e0d57c71d0a9e0bb3f0d9875fc1b260ea3ea6fab89f4d7d0ba':
        return {}
    return json.loads(content)


def _benchmark(people, players, rows, entity, review):
    result = people[entity]['result']
    ship = players[entity].get('shipParamsId')
    realms = {_REGIONS.get(_unique_result(rows[k], 'home_realm')) for k in players}
    region = next(iter(realms)) if len(realms) == 1 else None
    data = _sample()
    server = data.get('regions', {}).get(region, {})
    record = server.get('ships', {}).get(str(ship)) if review['mode'] == '随机战' else None
    details = {'entity_id':entity, 'ship_id':ship, 'mode':'pvp' if review['mode']=='随机战' else review['mode'],
               'region':region, 'scope':'regional_random_battle_sample', 'status':'unknown',
               'basis':'unanimous_settled_home_realm' if region else 'unconfirmed_region',
               'source_fields':['playersPublicInfo.home_realm', 'shipParamsId', 'gameType']}
    if review['mode'] != '随机战':
        reason = '公开参照覆盖随机战，本场模式缺少对应资料。'
    elif not region:
        reason = '赛后阵容没有可一致核对的区服，本场与区服同舰均值的差距未知。'
    elif not record or not record[0]:
        reason = '该区服缺少能核对舰船 ID 与出场次数的同舰样本。'
    else:
        totals = dict(zip(data['fields'], record))
        support, parts = [], []
        for key, field, divisor, label, digits in (('damage','damage','games','输出',0), ('scouting_damage','spot','games_510','侦察伤害',0),
                                                  ('ships_killed','frags','games','击沉',2)):
            own = result.get(key)
            count = totals[divisor]
            if not number(own) or count <= 0:
                continue
            mean = totals[field]/count
            support.append({'field':key, 'label':label, 'value':own, 'reference_value':mean, 'difference':own-mean,
                            'numerator':totals[field], 'denominator':count})
            parts.append(f'{label} {own:,.0f} 对 {mean:,.{digits}f}（{own-mean:+,.{digits}f}）')
        if support:
            period = data['version'].removeprefix('0.')
            details.update(status='available', provider=data['provider'], period=period, population=data['population'],
                           source_url=server['source_url'], snapshot_date=data['snapshot_date'], battle_count=totals['games'], support=support)
            text = (f"按阵容结算区服，{_REGION_NAMES[region]}同舰随机战 {period} 版公开样本共 {totals['games']:,} 次出场。"
                    +'本局对该样本场均：'+'、'.join(parts)+f"。Ship Tool 快照日期 {data['snapshot_date']}，"
                    +'按出场加权，仅含累计至少 200 场随机战的公开账号。')
            if '.'.join(review.get('version','').split('.')[:2]) != period:
                text += '本局与样本跨版本。'
            return _fact('replay_ship_benchmark', text, None, details, [entity])
        reason = '本场缺少可与区服同舰样本对照的个人结算。'
    return _fact('replay_ship_benchmark', reason, None, details, [entity])


def single_battle_facts(people, players, rows, focus, review):
    result = [row for row in (_gunnery(rows[focus],focus), _contribution(people,rows,players,focus),
                               _benchmark(people,players,rows,focus,review)) if row]
    return result + _impact(people, players, rows, focus, review)
