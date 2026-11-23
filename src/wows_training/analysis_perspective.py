"""Analysis lenses and the order in which each samples verified replay facts."""
from .performance_evidence import PERFORMANCE_KINDS
from .ship_traits import SHIP_TRAIT_KIND
from .single_battle_metrics import SETTLEMENT_KIND
from .replay_battle_evidence import KINDS as BATTLE_STATE_KINDS

RANDOM_BATTLE = "random_battle"
COMPETITION = "competition"
PERSPECTIVE_LABELS = {RANDOM_BATTLE: "随机战复盘", COMPETITION: "比赛复盘"}

REPLAY_CONTEXT_KINDS = frozenset({
    "replay_relative_position", "replay_map_control", "replay_hull_angle",
    "replay_action_frequency", "replay_context_coverage", "replay_minimap_sequence", "replay_fleet_key", "replay_spotting_changes",
    "replay_damage_window", "replay_team_identity", "replay_engagement_loadouts", SHIP_TRAIT_KIND,
}) | PERFORMANCE_KINDS | BATTLE_STATE_KINDS | {"replay_terrain_profile"}

_PRIORITY = {
    RANDOM_BATTLE: (
        "replay_team_identity", "replay_minimap_sequence", SHIP_TRAIT_KIND, SETTLEMENT_KIND,
        "replay_damage_over_time", "replay_incoming_damage", "replay_engagement_loadouts",
        "replay_fleet_key", "replay_spotting_changes", "replay_ship_benchmark", "replay_gunnery",
        "replay_target_impact", "replay_damage_window", "replay_battle_contribution",
        "replay_team_score_flow", "replay_capture_pressure", "replay_terrain_profile",
        "replay_context_coverage", "replay_ship_phase", "replay_damage_composition", "replay_damage_efficiency",
        "replay_consumable_timing", "replay_consumable_overlap", "replay_performance_coverage",
        "replay_relative_position", "replay_hull_angle",
        "replay_action_frequency", "replay_map_control", "ordinary_event", "ordinary_result",
    ),
    COMPETITION: (
        "replay_team_identity", "replay_minimap_sequence", SHIP_TRAIT_KIND, SETTLEMENT_KIND,
        "replay_damage_over_time", "replay_incoming_damage", "replay_engagement_loadouts",
        "replay_fleet_key", "replay_spotting_changes", "replay_battle_contribution", "replay_gunnery",
        "replay_target_impact", "replay_damage_window", "replay_ship_benchmark",
        "replay_team_score_flow", "replay_capture_pressure", "replay_terrain_profile",
        "replay_context_coverage", "replay_map_control", "replay_relative_position",
        "zone_commitment", "support_arrival", "fleet_geometry", "region_transfer",
        "replay_consumable_overlap", "replay_ship_phase", "replay_consumable_timing", "replay_damage_composition",
        "replay_damage_efficiency", "replay_performance_coverage", "replay_hull_angle", "replay_action_frequency", "ordinary_event",
    ),
}


def evidence_priority(kind, perspective):
    order = _PRIORITY[perspective]
    return order.index(kind) if kind in order else len(order)
