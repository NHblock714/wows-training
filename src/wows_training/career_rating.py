"""Pure career projections using the original F1 PR arithmetic and baselines."""
from collections import defaultdict
import math

ALGORITHM='f1-career-pr-1'


def number(value):
    try:return type(value) in (int,float) and math.isfinite(value) and value>=0
    except OverflowError:return False


def ratio(numerator,denominator,*,bounded=False):
    if not number(numerator) or not number(denominator) or denominator<=0:return None
    if bounded and numerator>denominator:return None
    value=numerator/denominator
    return value if math.isfinite(value) else None


def metrics(stats):
    stats=stats if isinstance(stats,dict) else {}
    def count(key):return stats.get(key) if number(stats.get(key)) else None
    def nested(group,key):
        values=stats.get(group);value=values.get(key) if isinstance(values,dict) else None
        return value if number(value) else None
    battles=count('battles');wins=count('wins');draws=count('draws');losses=count('losses')
    if losses is None and all(number(v) for v in (battles,wins,draws)) and wins+draws<=battles:losses=battles-wins-draws
    damage=count('damage_dealt');frags=count('frags');survived=count('survived_battles')
    art,torp=count('art_agro'),count('torpedo_agro')
    deaths=battles-survived if number(battles) and number(survived) and survived<=battles else None
    return {'battles':battles,'wins':wins,'draws':draws,'losses':losses,'damage_dealt':damage,'frags':frags,
        'win_rate':ratio(wins,battles,bounded=True),'avg_damage':ratio(damage,battles),'avg_frags':ratio(frags,battles),
        'survival_rate':ratio(survived,battles,bounded=True),'avg_xp':ratio(count('xp'),battles),'kd_ratio':ratio(frags,deaths),
        'main_battery_hit_rate':ratio(nested('main_battery','hits'),nested('main_battery','shots'),bounded=True),
        'torpedo_hit_rate':ratio(nested('torpedoes','hits'),nested('torpedoes','shots'),bounded=True),
        'avg_potential_damage':ratio(art+torp,battles) if number(art) and number(torp) else None,
        'avg_scouting_damage':ratio(count('damage_scouting'),battles),'avg_ships_spotted':ratio(count('ships_spotted'),battles),
        'avg_planes_killed':ratio(count('planes_killed'),battles),
        'avg_capture_points':ratio(count('capture_points'),battles),
        'avg_dropped_capture_points':ratio(count('dropped_capture_points'),battles)}


def normalize(actual,expected,lower):
    if not number(actual) or not number(expected) or expected<=0:return None
    value=(actual/expected-lower)/(1-lower)
    return max(0.,value) if math.isfinite(value) else None


def personal_rating(values):
    if any(v is None for v in values):return None
    value=700*values[0]+300*values[1]+150*values[2]
    return value if math.isfinite(value) else None


def battle_power(pr):
    if not number(pr) or pr<=0:return None if pr is None else 0
    return int(70000/(1+math.exp(-(pr-1700)/250)))


def ship_power(pr,battles):
    if not number(pr) or not number(battles) or battles<=0:return None
    if pr<=0:return 0
    return int(30000/(1+math.exp(-(pr-1700)/300))*min(1.,math.sqrt(battles/100)))


def mechanical_accuracy(artillery):
    if not isinstance(artillery,dict):return None
    distance,dispersion=artillery.get('distance'),artillery.get('max_dispersion')
    if not number(distance) or distance<=0 or not number(dispersion) or dispersion<=0:return None
    shells=artillery.get('shells') or {}
    for kind in ('AP','HE','SAP'):
        velocity=(shells.get(kind) or {}).get('bullet_speed')
        if number(velocity) and velocity>0:return distance*1000/dispersion*(velocity/800)
    return None


def mechanical_means(encyclopedia):
    accuracy=defaultdict(list)
    for meta in encyclopedia.values():
        value=mechanical_accuracy((meta.get('default_profile') or {}).get('artillery'))
        if value is not None and meta.get('type'):accuracy[meta['type']].append(value)
    return {key:sum(values)/len(values) for key,values in accuracy.items()}


def _unique_ship_rows(records):
    seen=set()
    for row in records or []:
        value=row.get('ship_id') if isinstance(row,dict) else None
        raw=str(value)
        if type(value) not in (int,str) or not raw.isascii() or not raw.isdigit() or len(raw)>20 or int(raw)<=0:
            raise ValueError('Invalid ship identity')
        sid=str(int(raw))
        if sid in seen:raise ValueError('Duplicate ship identity')
        seen.add(sid)
        yield sid,row


def project_ships(records,mode,references,catalog=None):
    encyclopedia=references.get('encyclopedia') or {};expected=references.get('expected') or {};catalog=catalog or {}
    means=references.get('mechanical_means') or mechanical_means(encyclopedia)
    result=[]
    for sid,row in _unique_ship_rows(records):
        legacy=encyclopedia.get(sid) or {};native=catalog.get(sid) or {};value=metrics(row.get('statistics'))
        values=[None,None,None]
        if mode=='pvp':
            base=expected.get(sid) or {}
            values=[normalize(value['avg_damage'],base.get('expected_damage'),.4),normalize(value['avg_frags'],base.get('expected_frags'),.1),normalize(value['win_rate'],base.get('expected_winrate'),.7)]
        pr=personal_rating(values);kind=legacy.get('type') or native.get('species');tier=legacy.get('tier',native.get('tier'))
        art=mechanical_accuracy((legacy.get('default_profile') or {}).get('artillery'))
        result.append({'ship_id':sid,'name':native.get('display_name') or legacy.get('name') or native.get('internal_name') or sid,
            'species':kind,'tier':tier,'nation':legacy.get('nation') or native.get('nation'),**value,
            'pr':pr,'normalized_pr':values,'ship_battle_power':ship_power(pr,value['battles']),
            'mechanical_hit_factor':art/means[kind] if art and means.get(kind) else None,
            'legacy_metadata_known':bool(legacy),
            'rating_metadata_known':bool(legacy) if references.get('legacy_only') else bool(legacy or native),
            'metadata_source':'official_encyclopedia' if legacy else 'local_client_15_9' if native else 'unknown',
            'pr_status':'known' if pr is not None else 'mode_not_supported' if mode!='pvp' else 'expected_values_missing',
            'last_battle_time':row.get('last_battle_time')})
    return sorted(result,key=lambda row:(-(row['battles'] or 0),row['name'],row['ship_id']))


def summarize_ships(ships,mode,references):
    ships=[row for _,row in _unique_ship_rows(ships)]
    total=sum(row['battles'] for row in ships if number(row['battles']))
    active=[r for r in ships if number(r['battles']) and r['battles']>0]
    classes=defaultdict(float);tiers=defaultdict(float)
    for row in active:classes[row['species']]+=row['battles'];tiers[row['tier']]+=row['battles']
    legacy=[r for r in active if r.get('rating_metadata_known',r.get('legacy_metadata_known'))]
    covered=[r for r in legacy if r['pr'] is not None]
    covered_battles=sum(r['battles'] for r in covered);legacy_total=sum(r['battles'] for r in legacy)
    rating=None
    if mode=='pvp' and covered_battles>0:
        normalized=[sum(r['normalized_pr'][i]*r['battles'] for r in covered)/covered_battles for i in range(3)]
        raw=personal_rating(normalized);table=(references.get('benchmarks') or {}).get('tier_difficulty') or {}
        tier_rows=[r for r in legacy if type(r['tier']) in (int,float) and math.isfinite(r['tier'])]
        denominator=sum(r['battles'] for r in tier_rows)
        difficulty=sum(r['battles']*table.get(str(int(r['tier'])),1.) for r in tier_rows)/denominator if denominator else 1.
        rating={'pr':raw,'pr_adjusted':raw*difficulty,'battle_power':battle_power(raw*difficulty),'tier_difficulty':difficulty,
            'covered_battles':covered_battles,'total_battles':total,'legacy_included_battles':legacy_total,
            'coverage_ratio':covered_battles/total if total else None,'algorithm':ALGORITHM,'baseline':references.get('metadata') or {},
            'interpretation':'公开战绩指标。基础战力由随机战 PR 换算，综合战力额外包含挑战与舰种等修正。'}
    ace=None
    for threshold in (80,30,10,1):
        candidates=[r for r in legacy if r['battles']>=threshold]
        if candidates:
            known=[r for r in candidates if r['pr'] is not None]
            if known:ace=dict(max(known,key=lambda r:r['pr']*math.sqrt(r['battles'])),selection='legacy_pr_times_sqrt_battles',minimum_battles=threshold)
            else:
                complete=[r for r in candidates if all(number(r[k]) for k in ('win_rate','avg_damage','avg_xp'))]
                if complete:
                    damage=max(r['avg_damage'] for r in complete) or 1;xp=max(r['avg_xp'] for r in complete) or 1
                    ace=dict(max(complete,key=lambda r:(r['win_rate']*.5+r['avg_damage']/damage*.3+r['avg_xp']/xp*.2)*math.sqrt(r['battles'])),
                             selection='legacy_fallback_without_pr',minimum_battles=threshold)
            break
    return {'rating':rating,'ace_ship':ace,
        'class_distribution':[{'species':k,'battles':int(v),'share':v/total if total else None} for k,v in sorted(classes.items(),key=lambda i:-i[1])],
        'tier_distribution':[{'tier':k,'battles':int(v),'share':v/total if total else None} for k,v in sorted(tiers.items(),key=lambda i:(i[0] is None,i[0] or 0))]}
