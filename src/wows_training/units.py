"""Verified client-display distances for the pinned 15.9 world-coordinate domain.

This module has no game/runtime dependency. Native reciprocal exports remain
evidence; metric inversion divides by the verified forward factor to avoid
introducing its rounded float32 reciprocal error.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
import struct


CLIENT_VERSION = '15.9.0.13357625'
COORDINATE_KINDS = frozenset({'world_position', 'map_extent', 'decoded_minimap_position'})
NATIVE_EXPECTED = {
    'BW_TO_BALLISTIC': 30.0,
    'BALLISTIC_TO_BW': 0.03333333507180214,
    'BW_TO_SHIP': 15.0,
    'SHIP_TO_BW': 0.06666667014360428,
}


class UnitsUnavailable(ValueError):
    """Evidence is absent, from another build, or for another quantity domain."""


def _finite(value, name):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f'{name} must be a finite number')
    return float(value)


def validate_units_schema(schema, expected_version=CLIENT_VERSION, *, expected_executable_sha256=None,
                          expected_archive_sha256=None):
    if not isinstance(schema, dict):
        raise UnitsUnavailable('Unit evidence must be an object')
    if expected_version != CLIENT_VERSION or schema.get('client_version') != expected_version:
        raise UnitsUnavailable('Unit evidence does not match the verified client version')
    if schema.get('schema_version') != 1 or schema.get('game_execution') is not False:
        raise UnitsUnavailable('Unsupported unit evidence schema')
    executable = schema.get('executable', {})
    if not isinstance(executable, dict) or not isinstance(executable.get('version_info'), dict):
        raise UnitsUnavailable('Native executable version evidence is missing')
    raw_product_version = executable['version_info'].get('ProductVersion')
    product_version = raw_product_version.replace(',', '.') if isinstance(raw_product_version, str) else None
    if executable.get('build') != '13357625' or product_version != expected_version:
        raise UnitsUnavailable('Native executable version is not bound to the target build')
    for name, actual, expected in (
        ('executable', executable.get('sha256'), expected_executable_sha256),
        ('archive', schema.get('source_archive_sha256'), expected_archive_sha256),
    ):
        if not isinstance(actual, str) or re.fullmatch(r'[0-9a-fA-F]{64}', actual) is None:
            raise UnitsUnavailable(f'Missing {name} source hash')
        if expected is not None:
            if not isinstance(expected, str) or re.fullmatch(r'[0-9a-fA-F]{64}', expected) is None:
                raise UnitsUnavailable(f'Invalid expected {name} source hash')
            if actual.lower() != expected.lower():
                raise UnitsUnavailable(f'{name} source hash does not match the supplied evidence')
    flags = schema.get('verified', {})
    if not isinstance(flags, dict):
        raise UnitsUnavailable('Verification flags must be an object')
    for flag in ('native_name_value_binding', 'native_python_float', 'client_world_position_distance_usage', 'kilometer_relation'):
        if flags.get(flag) is not True:
            raise UnitsUnavailable(f'Required proof is missing: {flag}')
    native = schema.get('native_constants', {})
    if not isinstance(native, dict):
        raise UnitsUnavailable('Native constant bindings must be an object')
    for name, expected in NATIVE_EXPECTED.items():
        entry = native.get(name, {})
        if not isinstance(entry, dict):
            raise UnitsUnavailable(f'Native constant binding is invalid: {name}')
        value = entry.get('value')
        if type(value) not in (int, float) or not math.isfinite(value) or value != expected:
            raise UnitsUnavailable(f'Unsupported native constant value: {name}')
        if entry.get('module') != 'BigWorld' or entry.get('export_name') != name:
            raise UnitsUnavailable(f'Native name/value binding is absent: {name}')
        if entry.get('value_type') != 'Python float from native IEEE754 binary64 literal':
            raise UnitsUnavailable(f'Native constant type is unverified: {name}')
        try:
            literal = bytes.fromhex(entry['literal_bytes_le'])
            decoded = struct.unpack('<d', literal)[0]
        except (ValueError, KeyError, TypeError, struct.error) as exc:
            raise UnitsUnavailable(f'Invalid native literal evidence: {name}') from exc
        if decoded != value:
            raise UnitsUnavailable(f'Native literal differs from exported value: {name}')
    world = schema.get('world_distance', {})
    if not isinstance(world, dict):
        raise UnitsUnavailable('World distance evidence must be an object')
    allowed = world.get('allowed_coordinate_kinds')
    if not isinstance(allowed, (list, tuple)) or not all(isinstance(value, str) for value in allowed):
        raise UnitsUnavailable('World distance quantity domains must be explicit strings')
    if (world.get('native_export') != 'BW_TO_BALLISTIC'
            or type(world.get('meters_per_engine_unit')) not in (int, float)
            or world['meters_per_engine_unit'] != native['BW_TO_BALLISTIC']['value']
            or type(world.get('km_to_m')) not in (int, float) or world['km_to_m'] != 1000.0
            or set(allowed) != COORDINATE_KINDS):
        raise UnitsUnavailable('World distance scale or permitted quantity domain is invalid')
    return schema


def load_units_schema(path, expected_version=CLIENT_VERSION, *, expected_executable_sha256=None,
                      expected_archive_sha256=None):
    schema = json.loads(Path(path).read_text(encoding='utf-8'))
    return validate_units_schema(schema, expected_version,
                                 expected_executable_sha256=expected_executable_sha256,
                                 expected_archive_sha256=expected_archive_sha256)


def _distance_scale(schema, coordinate_kind):
    validate_units_schema(schema)
    if not isinstance(coordinate_kind, str) or coordinate_kind not in COORDINATE_KINDS:
        raise UnitsUnavailable(f'Conversion is not verified for {coordinate_kind!r}')
    return schema['world_distance']['meters_per_engine_unit']


def engine_distance_to_meters(schema, distance, *, coordinate_kind='world_position'):
    """Convert a nonnegative separation already expressed in BW world units."""
    distance = _finite(distance, 'distance')
    if distance < 0:
        raise ValueError('Distance must not be negative')
    result = distance * _distance_scale(schema, coordinate_kind)
    if not math.isfinite(result):
        raise ValueError('Converted distance is not finite')
    return result


def meters_to_engine_distance(schema, distance, *, coordinate_kind='world_position'):
    """Invert the display-distance scale; do not apply the ship-physics scale."""
    distance = _finite(distance, 'distance')
    if distance < 0:
        raise ValueError('Distance must not be negative')
    return distance / _distance_scale(schema, coordinate_kind)


def horizontal_world_distance_meters(schema, first, second):
    """Horizontal distance for two explicit (x,z) world-coordinate points."""
    if not isinstance(first, (tuple, list)) or not isinstance(second, (tuple, list)) or len(first) != 2 or len(second) != 2:
        raise ValueError('Expected two (x,z) coordinate pairs')
    x1, z1 = (_finite(value, 'first coordinate') for value in first)
    x2, z2 = (_finite(value, 'second coordinate') for value in second)
    return engine_distance_to_meters(schema, math.hypot(x2 - x1, z2 - z1))
