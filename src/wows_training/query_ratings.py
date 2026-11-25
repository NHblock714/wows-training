"""Public career projections using the F1 WOWS calculations and dated account snapshots."""

from .f1_ratings import analysis, battle_style, challenges, clan, personal_rating, ships
from .f1_ratings.models import PlayerStats


def _inputs(career, profile):
    if not profile or profile.get('mode') != 'pvp' or not profile.get('snapshot_id'):
        return {}, {}, {}, {}
    record = career.store.get_run(profile['snapshot_id'])
    snapshot = record['payload']
    if (record['kind'] != 'career_snapshot' or snapshot.get('account_status') != 'ok'
            or snapshot.get('region') != profile.get('region')
            or str(snapshot.get('account_id')) != str(profile.get('account_id'))):
        return {}, {}, {}, {}
    entry = snapshot.get('mode_results', {}).get('pvp') or {}
    refs = snapshot.get('rating_references') or career.legacy_references
    return (entry.get('account') or {}).get('statistics') or {}, entry.get('ships') or {}, refs, snapshot


def _frame(fleet, refs):
    encyclopedia = dict(refs.get('encyclopedia') or {})
    if not refs.get('legacy_only'):
        for sid, meta in (refs.get('local_catalog') or {}).items():
            if sid not in encyclopedia and meta.get('tier') and meta.get('species'):
                encyclopedia[sid] = {'tier': meta['tier'], 'type': meta['species'],
                    'name': meta.get('display_name') or meta.get('internal_name'), 'nation': meta.get('nation')}
    raw = [{'ship_id': row['ship_id'], 'pvp': row.get('statistics')} for row in fleet.get('ships') or []]
    return ships.build_dataframe(raw, encyclopedia, refs.get('expected'), mechanical_means=refs.get('mechanical_means'))


def player_frame(career, profile):
    _, fleet, refs, _ = _inputs(career, profile)
    return _frame(fleet, refs)


def _subset(frame, species, tier):
    if not frame.empty and species is not None:
        frame = frame[frame['type'] == species]
    if not frame.empty and tier is not None:
        frame = frame[frame['tier'] == tier]
    return frame


_AXIS_FIELDS = {'胜率': ('wins',), '场均伤害': ('damage_dealt',), '场均击杀': ('frags',),
                '场均经验': ('xp',), '击杀效率': ('frags', 'survived_battles'),
                '主炮命中': ('main_battery.hits', 'main_battery.shots'),
                '场均抗伤': ('art_agro', 'torpedo_agro'), '场均侦查': ('damage_scouting',)}


def _has(row, field):
    value = row
    for key in field.split('.'):
        if not isinstance(value, dict):
            return False
        value = value.get(key)
    return type(value) in (int, float)


def _known_scores(scores, rows):
    return {key: value for key, value in (scores or {}).items()
            if all(_has(row, field) for row in rows for field in _AXIS_FIELDS.get(key, ()))}


def player_ratings(career, profile, *, species=None, tier=None, achievements=None):
    raw, fleet, refs, snapshot = _inputs(career, profile)
    result = dict(scores={}, strengths=[], weaknesses=[], styles=[], challenges=[], overall=None, power=None,
                  modifiers={}, all_challenges_count=len(challenges.CHALLENGES))
    if achievements is None:
        achievements = (profile or {}).get('achievements')
    solo_entry = (snapshot.get('mode_results') or {}).get('pvp_solo') or {}
    rankings = snapshot.get('numbers_rankings')
    if isinstance(rankings, dict) and 'account_id' in rankings and rankings['account_id'] != snapshot.get('account_id'):
        rankings = None
    gallery, collections = challenges.evaluate_challenges(raw, fleet, achievements,
        encyclopedia=refs.get('encyclopedia'), local_catalog=refs.get('local_catalog'),
        solo=(solo_entry.get('account') or {}).get('statistics'),
        created_at=snapshot.get('account_created_at'), region=snapshot.get('region'),
        as_of=snapshot.get('timestamp') if snapshot.get('time_kind') == 'fetched_at_utc' else None,
        numbers_rankings=rankings)
    completed = challenges.get_completed_challenges(gallery)
    result.update(achievement_gallery=gallery, ship_collections=collections, challenges=completed,
                  challenge_complete_data=all(row['complete'] for row in gallery))
    stats = PlayerStats.from_mode_dict(raw)
    if stats is None:
        return result
    frame = _frame(fleet, refs)
    benchmarks = refs.get('benchmarks') or {}
    full_scores = _known_scores(analysis.compute_scores(stats, benchmarks, frame), [raw])
    if full_scores and all(k in full_scores for k in _AXIS_FIELDS):
        result['styles'] = battle_style.infer_battle_style(full_scores, stats, frame)
    pr = profile.get('rating') or {}
    if pr.get('battle_power') is not None and not frame.empty:
        earned_ids = {row['id'] for row in gallery if row['status'] == 'earned'}
        earned_rarities = {rule.id: rule.rarity for rule in challenges.CHALLENGES if rule.id in earned_ids}
        ship_battle_records = ((row.get('ship_id'), (row.get('statistics') or {}).get('battles'))
                               for row in fleet.get('ships') or ())
        mods = personal_rating.compute_battle_power_modifiers(stats, frame,
            achievement_rarities=earned_rarities.values(), ship_battle_records=ship_battle_records)
        result.update(power=personal_rating.apply_battle_power_modifiers(pr['battle_power'], mods), modifiers=mods)
    filtered = _subset(frame, species, tier)
    if species is not None or tier is not None:
        if filtered.empty:
            return result
        ids = set(filtered['ship_id'].astype(str))
        source_rows = [r['statistics'] for r in fleet['ships'] if str(r['ship_id']) in ids]
        scores = _known_scores(analysis.compute_scores(ships.aggregate_to_stats(filtered), benchmarks, filtered), source_rows)
    else:
        scores = full_scores
    # The original total radar replaces average frags with the fleet-composition axis.
    dynamic = next((key for key in scores if key == '🐴含量' or key.endswith('场次')), None)
    order = ('胜率', '场均伤害', dynamic, '场均经验', '击杀效率', '主炮命中', '场均抗伤', '场均侦查')
    shown = {key: scores[key] for key in order if key in scores}
    if shown:
        strengths, weaknesses = analysis.analyze_strengths(shown)
        result.update(scores=shown, strengths=strengths, weaknesses=weaknesses,
            overall=sum(value[1] for value in shown.values()) / len(shown) * analysis.compute_tier_difficulty(filtered, benchmarks))
    return result


def ship_ratings(career, profile, ship_id):
    _, fleet, refs, _ = _inputs(career, profile)
    frame = _frame(fleet, refs)
    if not frame.empty:
        frame = frame[frame['ship_id'].astype(str) == str(ship_id)]
    if frame.empty:
        return dict(scores={}, battles=None, confidence=None)
    scores, confidence = analysis.compute_single_ship_scores(frame, refs.get('benchmarks') or {})
    rows = [r['statistics'] for r in fleet['ships'] if str(r['ship_id']) == str(ship_id)]
    return dict(scores=_known_scores(scores, rows), battles=int(frame['battles'].sum()), confidence=confidence)


def clan_ratings(clan_info, batch_players, benchmarks, *, member_frames=None, species=None, tier=None):
    roster = clan.build_clan_data(clan_info, batch_players)
    result = dict(scores={}, members=[], overall=None, power=None, fleet_pr=None)
    if roster is None:
        return result
    filtered = species is not None or tier is not None
    frames = {int(key): val for key, val in (member_frames or {}).items()}
    for member in roster.members:
        aid = str(member.account_id)
        account = batch_players.get(aid) or {}
        raw = (account.get('statistics') or {}).get('pvp')
        row = dict(account_id=aid, nickname=account.get('nickname') or
                   (clan_info['members'].get(aid) or {}).get('account_name') or aid,
                   role=member.role, battles=None, win_rate=None, avg_damage=None, score=None)
        if account.get('hidden_profile') is True:
            row['status'] = 'private'
        elif not raw or not _has(raw, 'battles'):
            row['status'] = 'unknown'
        elif filtered:
            frame = frames.get(member.account_id)
            data = clan.compute_member_filtered_score(frame, benchmarks, species, tier)
            row['status'] = 'not_fetched' if frame is None else 'ok' if data else 'no_matches'
            if data:
                row.update(data)
                selected = _subset(frame, species, tier)
                row['avg_damage'] = selected['damage_dealt'].sum() / data['battles']
        else:
            row.update(battles=member.battles, status='ok' if member.battles else 'no_battles',
                       win_rate=member.win_rate if member.battles and _has(raw,'wins') else None,
                       avg_damage=member.avg_damage if member.battles and _has(raw,'damage_dealt') else None)
        result['members'].append(row)
    scores = (clan.compute_clan_radar_scores_filtered(roster, frames, benchmarks, species, tier)
              if filtered else clan.compute_clan_radar_scores(roster, benchmarks))
    # Keep undisclosed members distinct from an incomplete public batch.
    if not filtered and any(row['status'] == 'unknown' for row in result['members']):
        scores.pop('隐藏🐴', None)
    if not filtered:
        scores = _known_scores(scores, [(batch_players[r['account_id']]['statistics']['pvp'])
                               for r in result['members'] if r['status'] == 'ok'])
    if scores:
        fleet_pr = clan.compute_clan_fleet_power_from_scores(scores)
        result.update(scores=scores, overall=sum(v[1] for v in scores.values()) / len(scores),
                      power=personal_rating.compute_battle_power(fleet_pr), fleet_pr=fleet_pr)
    return result
