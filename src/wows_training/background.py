"""Source-proven projection to unrotated minimap PNGs for one identity mapping."""
from __future__ import annotations

import json
import math
from pathlib import Path


CLIENT_VERSION = "15.9.0.13357625"
EXE_SHA256 = "48dd69c535cd733909d78f8c4d202c86d562845f6431607de3a08945c5226d6f"
ARCHIVE_SHA256 = "260ed43948ae8dbe30ebbf22932bac1c5237e50c9a6cc39a020ec328c78698db"


class BackgroundUnavailable(ValueError):
    pass


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


def _rect(bounds):
    values = tuple(_number(bounds[key], key) for key in ("min_x", "max_x", "min_z", "max_z"))
    if values[1] <= values[0] or values[3] <= values[2]:
        raise ValueError("Rectangle must have positive dimensions")
    return values


def load_background_schema(path: Path | str, expected_version=CLIENT_VERSION) -> dict:
    schema = json.loads(Path(path).read_text(encoding="utf-8"))
    if expected_version != CLIENT_VERSION or schema.get("client_version") != expected_version or schema.get("schema_version") != 1:
        raise BackgroundUnavailable("Background profile version mismatch")
    if schema.get("source_executable_sha256") != EXE_SHA256 or schema.get("source_archive_sha256") != ARCHIVE_SHA256:
        raise BackgroundUnavailable("Background profile does not match audited client source hashes")
    if schema.get("native_tuple_order") != "max_min" or schema.get("game_execution") is not False:
        raise BackgroundUnavailable("Native tuple order or static evidence is invalid")
    if not all(schema.get("verified", {}).get(key) is True for key in ("native_original_bounds", "native_chunk_default", "png_consumer_transform", "native_tuple_order")):
        raise BackgroundUnavailable("Background proof is incomplete")
    if schema.get("mapping_scope") != "single_identity_mapping":
        raise BackgroundUnavailable("Only a single identity geometry mapping is supported")
    return schema


def _map(schema, map_id):
    if schema.get("native_tuple_order") != "max_min" or schema.get("mapping_scope") != "single_identity_mapping":
        raise BackgroundUnavailable("Unsupported mapping scope or native tuple order")
    try:
        record = schema["maps"][map_id]
    except KeyError:
        raise BackgroundUnavailable(f"Map is absent from this background profile: {map_id}") from None
    if record.get("status") != "statically_verified_standard_mapping":
        raise BackgroundUnavailable("Map background is not statically verified")
    _rect(record["original_bounds_engine"])
    return record


def background_bounds(schema: dict, map_id: str) -> dict:
    """Return the original land PNG extent, independent of a shrunken play area."""
    record = _map(schema, map_id)
    return {**record["original_bounds_engine"], "kind": "original_png", "unit": "engine", "map_id": map_id}


def world_to_png(x, z, schema: dict, map_id: str) -> tuple[float, float]:
    """Continuous image-edge coordinates; +x is right, +z is up; no clamping."""
    record = _map(schema, map_id)
    min_x, max_x, min_z, max_z = _rect(record["original_bounds_engine"])
    width, height = record["minimap_dimensions_pixels"]
    x, z = _number(x, "x"), _number(z, "z")
    return (x - min_x) / (max_x - min_x) * width, (max_z - z) / (max_z - min_z) * height


def png_to_world(px, py, schema: dict, map_id: str) -> tuple[float, float]:
    record = _map(schema, map_id)
    min_x, max_x, min_z, max_z = _rect(record["original_bounds_engine"])
    width, height = record["minimap_dimensions_pixels"]
    px, py = _number(px, "px"), _number(py, "py")
    return min_x + px / width * (max_x - min_x), max_z - py / height * (max_z - min_z)


def viewport_from_configured_bounds(schema: dict, map_id: str, configured: dict) -> dict:
    """Confirm the native setter/readback cancellation for contained valid bounds.

    Lesta expands the requested box by two chunks and validates containment in
    the complete grid; Junk's default readback subtracts the same two chunks.
    A rejected box must never be silently clamped or treated as an update.
    """
    original = background_bounds(schema, map_id)
    min_x, max_x, min_z, max_z = _rect(configured)
    if min_x < original["min_x"] or max_x > original["max_x"] or min_z < original["min_z"] or max_z > original["max_z"]:
        raise BackgroundUnavailable("Configured border exceeds original playable extent; native setter would reject it")
    return {**configured, "kind": "playable", "unit": "engine", "native_viewport_equality_verified": True}


def png_crop_for_viewport(schema: dict, map_id: str, configured: dict) -> dict:
    bounds = viewport_from_configured_bounds(schema, map_id, configured)
    left, top = world_to_png(bounds["min_x"], bounds["max_z"], schema, map_id)
    right, bottom = world_to_png(bounds["max_x"], bounds["min_z"], schema, map_id)
    return {"left": left, "top": top, "right": right, "bottom": bottom, "coordinate_convention": "continuous_pixel_edges"}


def gui_background_transform(schema: dict, map_id: str, configured: dict, canvas_width, canvas_height) -> dict:
    """Equivalent unrotated MinimapSystem/Unbound scaling and translation."""
    original = background_bounds(schema, map_id)
    viewport = viewport_from_configured_bounds(schema, map_id, configured)
    width, height = _number(canvas_width, "canvas_width"), _number(canvas_height, "canvas_height")
    if width <= 0 or height <= 0:
        raise ValueError("Canvas dimensions must be positive")
    ow, oh = original["max_x"] - original["min_x"], original["max_z"] - original["min_z"]
    vw, vh = viewport["max_x"] - viewport["min_x"], viewport["max_z"] - viewport["min_z"]
    if not math.isclose(ow, oh) or not math.isclose(vw, vh):
        raise BackgroundUnavailable("Client GUI uses a scalar ratio; non-square maps require separate validation")
    ratio = ow / vw
    margin_x = (viewport["min_x"] - original["min_x"]) / ow
    margin_y = (original["max_z"] - viewport["max_z"]) / oh
    return {"land_width": width * ratio, "land_height": height * ratio,
            "land_left": -width * margin_x * ratio, "land_top": -height * margin_y * ratio,
            "water_width": width, "water_height": height, "ratio": ratio,
            "compositing": "preserve PNG alpha; land over water; clip to the viewport"}
