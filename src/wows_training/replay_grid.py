"""Locate observed ships on the same north-up grid as the replay minimap."""
import math
import re

from .replay_intel import number


GRID_ROWS = "ABCDEFGHIJ"
_DIRECTIONS = ("北", "东北", "东", "东南", "南", "西南", "西", "西北")


def map_cell(bounds, x, z):
    if not isinstance(bounds, dict) or not number(x) or not number(z):
        return None
    limits = [bounds.get(key) for key in ("min_x", "max_x", "min_z", "max_z")]
    if not all(number(value) for value in limits):
        return None
    left, right, bottom, top = limits
    if not left < right or not bottom < top or not (left <= x <= right and bottom <= z <= top):
        return None
    columns = len(GRID_ROWS)
    col = min(columns - 1, int(columns * (x - left) / (right - left)))
    row = min(columns - 1, int(columns * (top - z) / (top - bottom)))
    return GRID_ROWS[row] + str(col + 1)


def map_location(bounds, x, z, capture_points=()):
    cell = map_cell(bounds, x, z)
    if cell is None:
        return {}
    result = {"map_cell": cell}
    points = [point for point in capture_points
              if isinstance(point, dict)
              and re.fullmatch(r"[A-J]", str(point.get("name", "")))
              and all(number(point.get(key)) for key in ("x", "z", "radius"))
              and point["radius"] > 0 and map_cell(bounds, point["x"], point["z"])]
    if not points:
        return result
    point = min(points, key=lambda item: math.hypot(x - item["x"], z - item["z"]))
    dx, dz = x - point["x"], z - point["z"]
    distance = math.hypot(dx, dz)
    position = "圈内"
    if distance > point["radius"]:
        sector = math.floor((math.atan2(dx, dz) + math.pi / 8) / (math.pi / 4)) % 8
        position = _DIRECTIONS[sector] + "侧"
    result.update(cap_name=point["name"], cap_cell=map_cell(bounds, point["x"], point["z"]),
                  cap_relative_position=position)
    return result


def location_label(location):
    cell = location.get("map_cell", "")
    if location.get("cap_name") and location.get("cap_relative_position"):
        return f"{cell} · {location['cap_name']} 点{location['cap_relative_position']}"
    return cell
