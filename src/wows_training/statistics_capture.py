"""Version-bound damage result fields and untyped observed damage callbacks."""
from copy import deepcopy
import hashlib
import json
import math

VERSION='15.9.0.13357625'
SCHEMA_VERSION=1


def number(value):return type(value) in (int,float) and math.isfinite(value) and 0<=value<=1e12


def weapon_damage_fields(public_prefix):
    """Two contiguous source-defined weapon blocks, distinct from AA, shields and spotting."""
    start = public_prefix.index('damage_main_ap')
    incoming = public_prefix.index('received_damage_main_ap')
    end = public_prefix.index('received_damage_from_buildings_skip_airsupport') + 1
    return public_prefix[start:incoming], public_prefix[incoming:end]


def field_values(public, names):
    return {key: public[key] if number(public.get(key)) else None for key in names}


GUN_FIELDS = tuple(f'{key}_main_{shell}' for shell in ('ap', 'cs', 'he') for key in ('shots', 'hits'))
CONTRIBUTION_FIELDS = ('raw_exp', 'capture_points', 'dropped_capture_points', 'first_ships_spotted_by_ship',
                       'agro_art', 'agro_tpd')


AIRCRAFT_TOTALS = {'damage_adbomb': ('damage_dbomb_airsupport',),
                   **{weapon: tuple(weapon+s for s in ('_avia', '_alt', '_airsupport'))
                      for weapon in ('damage_bomb', 'damage_tbomb', 'damage_skip', 'damage_rocket')}}


def performance_result(public):
    realm = public.get('home_realm')
    return {'gunnery': field_values(public, GUN_FIELDS), 'contribution': field_values(public, CONTRIBUTION_FIELDS),
            'home_realm': realm if realm in ('ASIA', 'EU', 'NA', 'US') else None}


def load_profile(bundle):
    profile=bundle.json('statistics_schema.json')
    manifest=bundle.json('resource_manifest.json')
    fields=profile.get('public_prefix',[])
    damage=profile.get('received_damage_fields',[])
    if (profile.get('schema_version')!=1 or profile.get('client_version')!=VERSION
            or profile.get('status')!='source_verified_prefix'
            or profile.get('source_archive_sha256')!=manifest['scripts_zip_audit']['sha256']
            or len(fields)!=len(set(fields)) or len(fields)!=profile.get('minimum_public_length')
            or not damage or any(not k.startswith('received_damage_') or k not in fields for k in damage)):
        raise ValueError('Statistics result schema is not verified for this exact client')
    result=deepcopy(profile)
    result['profile_sha256']=bundle.manifest['files']['statistics_schema.json']
    return result


class StatisticsCapture:
    def __init__(self,profile,max_events=250000):
        self.profile=profile;self.max_events=max_events
        self.source={'clock':0.0,'packet_clock':0.0,'packet_offset':None,'packet_type':None}
        self.events=[];self.results=[];self.errors=[];self.truncated=False;self.hooks=[]
        self.results_truncated=False

    def begin_packet(self,frame):
        self.source={'clock':frame.clock,'packet_clock':frame.clock,'packet_offset':frame.offset,'packet_type':frame.type}

    def attach(self,entity_class):
        key='Vehicle_receiveDamagesOnShip'
        def callback(entity,damages):
            try:self.receive_damage(entity.id,damages)
            except Exception as exc:self.errors.append({'kind':'damage_callback','message':str(exc),'source':dict(self.source)})
        table=entity_class._methods_subscriptions;table.setdefault(key,[]).append(callback)
        self.hooks.append((table,key,callback));return self

    def detach(self):
        for table,key,callback in self.hooks:
            if callback in table.get(key,[]):table[key].remove(callback)
        self.hooks=[]

    def receive_damage(self,victim,damages):
        if type(victim) is not int or not isinstance(damages,(list,tuple)):raise ValueError('Invalid damage callback')
        for index,row in enumerate(damages):
            if len(self.events)>=self.max_events:self.truncated=True;return
            if not isinstance(row,dict) or type(row.get('vehicleID')) is not int or not number(row.get('damage')):
                self.errors.append({'kind':'invalid_damage_amount','source':dict(self.source)});continue
            self.events.append({'id':f'damage:{self.source["packet_offset"]}:{index}','victim_entity_id':victim,
                'attacker_entity_id':row['vehicleID'],'amount':row['damage'],'damage_type':None,
                'type_status':'not_present_in_callback','source':dict(self.source)})

    def observe_result(self,data,common):
        if len(self.results)>=32:
            self.results_truncated=True;return
        fields=self.profile['public_prefix'];players=data.get('playersPublicInfo')
        if not isinstance(players,dict) or len(players)>256:
            self.errors.append({'kind':'missing_public_results','source':dict(self.source)});return
        dealt_fields, _ = weapon_damage_fields(fields)
        result={'arena_id':str(common['arena_id']),'result_duration_sec':common.get('duration_sec'),
                'recorder_account_id':data.get('accountDBID'),'source':dict(self.source),'players':[]}
        for account,row in players.items():
            if not isinstance(row,list) or not len(fields)<=len(row)<=self.profile['maximum_public_length']:
                self.errors.append({'kind':'public_prefix_length','account_id':str(account),'source':dict(self.source)});continue
            public=dict(zip(fields,row))
            if type(public.get('account_db_id')) is not int or str(public['account_db_id'])!=str(account):
                self.errors.append({'kind':'public_account_mismatch','account_id':str(account),'source':dict(self.source)});continue
            values=field_values(public,self.profile['received_damage_fields'])
            from .ordinary_replay import metrics, result_ribbons
            result['players'].append({'account_id':public['account_db_id'],'reported_name':public.get('name'),
                'ship_params_id':public.get('vehicle_type_id'),'team_id':public.get('team_id'),
                **performance_result(public),
                'battle_metrics':metrics(public),
                'ribbons':result_ribbons(row, VERSION, len(fields)),
                'dealt_damage_by_field':field_values(public,dealt_fields),
                'damage_by_field':values,'unknown_fields':[name for name,value in values.items() if value is None],
                'source_public_key':str(account),'source_public_length':len(row)})
        self.results.append(result)

    def finish(self,roster,header,arena_id):
        from .ordinary_replay import PERFORMANCE_SCHEMA_VERSION
        participants={str(p['accountDBID']):p for p in roster if type(p.get('accountDBID')) is int}
        host=next((p for p in roster if p.get('name')==header.get('playerName')),None)
        results=[]
        for snapshot in self.results:
            if snapshot['arena_id']!=str(arena_id):
                self.errors.append({'kind':'result_arena_mismatch','source':snapshot['source']});continue
            if host and snapshot['recorder_account_id']!=host.get('accountDBID'):
                self.errors.append({'kind':'result_recorder_mismatch','source':snapshot['source']});continue
            selected=[]
            for row in snapshot['players']:
                player=participants.get(str(row['account_id']))
                if player is None:continue
                if (row['reported_name']!=player.get('name') or row['ship_params_id']!=player.get('shipParamsId')
                        or row['team_id']!=player.get('teamId')):
                    self.errors.append({'kind':'result_roster_mismatch','account_id':row['account_id'],'source':snapshot['source']});continue
                selected.append(dict(row,entity_id=player['shipId']))
            results.append(dict(snapshot,players=selected))
        return {'schema_version':SCHEMA_VERSION,'client_version':VERSION,'profile_sha256':self.profile['profile_sha256'],
            'performance_schema_version':PERFORMANCE_SCHEMA_VERSION,
            'status':'result_fields_available' if results and not self.results_truncated else 'unknown','result_snapshots':results if not self.results_truncated else [],'damage_events':self.events,
            'result_stream_truncated':self.results_truncated,
            'observed_damage_events_complete':False,'event_stream_truncated':self.truncated,'diagnostics':self.errors[:100],
            'received_damage_fields':list(self.profile['received_damage_fields']),
            'public_field_indices':{name:self.profile['public_prefix'].index(name) for name in self.profile['received_damage_fields']},
            'limitations':['Result aggregates have no per-hit timestamps. Missing fields remain unknown.',
                            'Damage callbacks contain amounts, not types, and are observation-limited; do not add them to result totals.']}
