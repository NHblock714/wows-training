"""Tie recorded changes in enemy spotting of the recorder to the damage timeline."""
from .ordinary_replay import current_combat_verified, OLD
from .replay_intel import number


def spotting_facts(report, review, focus):
    combat = report.get('combat') or {}
    version = str(report.get('replay_version', '')).replace(',', '.')
    if (report.get('multiview') or report.get('framing_error') or
            not (current_combat_verified(report) or version == OLD and combat.get('schema_version') == 1)):
        return []
    recorders = [p['entity_id'] for p in review['participants'] if p['name'] == review['recorder']]
    if len(recorders) != 1 or type(recorders[0]) is not int or focus not in (None, recorders[0]):
        return []
    entity = recorders[0]
    end = combat.get('observation_end_clock')
    if not number(end):
        return []
    end = min([end] + [e['clock'] for e in report.get('death_events') or []
                       if e.get('victim_entity_id') == entity and number(e.get('clock'))])
    samples = (combat.get('ships', {}).get(str(entity)) or {}).get('samples') or []
    # Full-state samples repeat the last flag. Only timestamped changes count.
    changes = {}
    for sample in samples:
        clock, flag = sample.get('spotted_clock'), sample.get('spotted_by_enemy')
        if number(clock) and 0 <= clock <= end and sample.get('clock') == clock:
            if clock in changes and changes[clock] != flag:
                changes[clock] = None
            else:
                changes[clock] = flag if type(flag) is bool else None
    spans, start = [], None
    for clock, state in sorted(changes.items()):
        if start is None and state is True:
            start = clock
        elif start is not None and state is not True:
            spans.append((start, clock, state is False))
            start = None
    if start is not None:
        spans.append((start, end, False))
    if not spans:
        return []
    hits = [h for h in review['damage_events'] if h['target'] == entity]
    rows = []
    for start, stop, lost in spans:
        amount = sum(h['amount'] for h in hits if start <= h['clock'] < stop)
        rows.append({'clock_start':start, 'clock_end':stop, 'received_amount':round(amount, 1),
                     'end_source':'recorded_unspotted' if lost else 'observation_limit'})
    # Preserve order and coverage of the whole recorded fight, not just the largest hit.
    chunks = [rows[i:i+8] for i in range(0, len(rows), 8)]
    facts = []
    for block in chunks[:12]:
        peak = max(block, key=lambda x:x['received_amount'])
        text = (f"录制舰的 {len(block)} 段被点亮记录中，窗口内观测承伤最高的一段为"
                f"录像 {int(peak['clock_start']//60):02d}:{int(peak['clock_start']%60):02d} 起，"
                f"期间收到 {peak['received_amount']:,.0f} 伤害回调。窗口内的伤害仅说明时间重合。")
        facts.append({'kind':'replay_spotting_changes', 'entity_ids':[entity], 'clock_start':block[0]['clock_start'],
                      'clock_end':block[-1]['clock_end'], 'text':text, 'details':{'entity_id':entity,
                          'support':block, 'count':len(block), 'source_fields':['combat.ships.spotted_by_enemy',
                                                                               'damage_events']}})
    return facts
