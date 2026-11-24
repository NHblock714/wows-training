"""Version-verified ship identity metadata for public career statistics."""
from functools import lru_cache

from .ordinary_replay import CURRENT
from .ship_traits import _build_profiles, _profiles


# GameParams 15.9/15.10 Black Friday variants have distinct IDs. In particular,
# the Closed Beta Arkansas ("阿肯色B") and regular Black are separate editions.
# 2026 release roster: https://worldofwarships.com/en/news/sales-and-events/black-friday-2026/
BLACK_FRIDAY_IDS = frozenset("""
3319740400 3331274160 3332323152 3332356080 3340645584 3340678352 3340679152
3341694416 3341759792 3341760464 3342776112 3425613520 3437180912 3438196528
3445536112 3666786288 3666818864 3666818896 3666819056 3667801296 3667801808
3667802064 3667834576 3667834672 3667867440 3667867632 3667899856 3667900112
3667900208 3667900400 3668850672 3668916016 3669898960 3669964624 3676223184
3676223216 3679369200 3699291600 3708761808 3730782000 3732879312 3739137488
3740153840
""".split())
# The collab uses both AZUR internal markers and localized AL labels with or
# without whitespace. These IDs were checked against the paired client tables.
AZUR_LANE_IDS = frozenset("""
3330258640 3331208656 3332289744 3332323312 3341760240 3342808912 3352213296
3353294640 3353327600 3354310384 3361617616 3361650480 3362732016 3541972688
3542005744 3551475152 3552524016 3709777872 3746477520 3750672080 3752802096
""".split())


def ship_edition(ship_id):
    ship_id = str(ship_id)
    if ship_id in BLACK_FRIDAY_IDS:
        return "black_friday"
    if ship_id in AZUR_LANE_IDS:
        return "azur_lane"
    return None


@lru_cache(maxsize=1)
def current_ship_catalog():
    """Identify localized ships without altering combat or rating reference data."""
    builds = _build_profiles(CURRENT).get("ships", {})
    catalog = {}
    for ship_id, traits in _profiles(CURRENT).items():
        kind = traits.get("species")
        tier = traits.get("tier")
        if not kind or type(tier) is not int or not 1 <= tier <= 11:
            continue
        catalog[ship_id] = {
            "display_name": builds.get(ship_id, {}).get("display_name"),
            "tier": tier, "species": kind, "nation": traits.get("nation"),
            "client_build": CURRENT,
        }
    return catalog


def enrich_ship_catalog(catalog):
    """Fill missing metadata without replacing existing snapshot names or classes."""
    result = dict(catalog)
    for ship_id, current in current_ship_catalog().items():
        existing = result.get(ship_id) or {}
        result[ship_id] = {**existing, **{key: value for key, value in current.items()
                          if not existing.get(key) and (key != "client_build" or not existing)}}
    return result
