"""Bounded container-only pickle decoding for the isolated replay worker.

Only selected replay_unpack module bindings are replaced. The standard library
pickle module and unrelated callers are never monkey-patched.
"""
from __future__ import annotations

from collections import OrderedDict
import importlib
import io
import pickle
import pickletools
import pkgutil
from threading import RLock


MAX_PICKLE_BYTES = 8 * 1024 * 1024
MAX_PICKLE_OPCODES = 1_000_000
MAX_MEMO_INDEX = 250_000
MAX_VALUE_NODES = 1_000_000
MAX_VALUE_DEPTH = 100
CONTROLLER_MODULE = "replay_unpack.clients.wows.versions.15_3_0.battle_controller"
CORE_PACKAGE = "replay_unpack.core.entity_def.data_types"
SAFE_GLOBALS = {
    ("builtins", "set"): set, ("__builtin__", "set"): set,
    ("builtins", "frozenset"): frozenset, ("__builtin__", "frozenset"): frozenset,
    ("collections", "OrderedDict"): OrderedDict,
}
_CAMOUFLAGE_RECORD = object()
_PLAYER_MODE_RECORD = object()
WIRE_RECORD_GLOBALS = {
    ("CamouflageInfo", "CamouflageInfo"): _CAMOUFLAGE_RECORD,
    ("PlayerModeDef", "PlayerMode"): _PLAYER_MODE_RECORD,
}
FORBIDDEN_OPCODES = frozenset({
    "EXT1", "EXT2", "EXT4", "PERSID", "BINPERSID", "INST", "OBJ",
    "NEWOBJ_EX", "NEXT_BUFFER", "READONLY_BUFFER",
})
_LOCK = RLock()
_STATISTICS = {"loads": 0, "rejected": 0, "largest_blob_bytes": 0, "globals_seen": set()}


class UnsafeReplayPickle(pickle.UnpicklingError):
    pass


def _inspect(data: bytes):
    if not data:
        raise UnsafeReplayPickle("Empty replay pickle")
    if len(data) > MAX_PICKLE_BYTES:
        raise UnsafeReplayPickle("Replay pickle exceeds the blob size limit")
    count = 0
    last_opcode = None
    last_position = -1
    try:
        for opcode, argument, position in pickletools.genops(data):
            count += 1
            if count > MAX_PICKLE_OPCODES:
                raise UnsafeReplayPickle("Replay pickle exceeds the opcode limit")
            name = opcode.name
            if name in FORBIDDEN_OPCODES:
                raise UnsafeReplayPickle(f"Forbidden replay pickle opcode: {name}")
            if name == "GLOBAL":
                pair = tuple(str(argument).split(" ", 1))
                if pair not in SAFE_GLOBALS and pair not in WIRE_RECORD_GLOBALS:
                    raise UnsafeReplayPickle(f"Forbidden replay pickle global: {argument}")
            if name in {"PUT", "BINPUT", "LONG_BINPUT", "GET", "BINGET", "LONG_BINGET"}:
                if not 0 <= int(argument) <= MAX_MEMO_INDEX:
                    raise UnsafeReplayPickle("Replay pickle memo index exceeds the limit")
            if name == "MEMOIZE" and count > MAX_MEMO_INDEX:
                raise UnsafeReplayPickle("Replay pickle memo population exceeds the limit")
            last_opcode, last_position = name, position
    except (ValueError, UnicodeError) as error:
        raise UnsafeReplayPickle(f"Malformed replay pickle: {error}") from error
    if last_opcode != "STOP" or last_position != len(data) - 1:
        raise UnsafeReplayPickle("Expected one complete replay pickle without trailing data")


def _basic_graph(value):
    """Bound expanded containers, rejecting cycles and callable/class results."""
    pending = [(value, 0, False)]
    active = set()
    nodes = 0
    while pending:
        item, depth, leaving = pending.pop()
        if leaving:
            active.remove(id(item))
            continue
        nodes += 1
        if nodes > MAX_VALUE_NODES or depth > MAX_VALUE_DEPTH:
            raise UnsafeReplayPickle("Decoded replay pickle exceeds the container budget")
        kind = type(item)
        if kind in {type(None), bool, int, float, str, bytes, bytearray}:
            continue
        if kind not in {dict, list, tuple, set, frozenset, OrderedDict}:
            raise UnsafeReplayPickle(f"Non-container replay pickle value: {kind.__name__}")
        if id(item) in active:
            raise UnsafeReplayPickle("Cyclic replay pickle containers are not allowed")
        if len(item) > MAX_VALUE_NODES - nodes:
            raise UnsafeReplayPickle("Decoded replay pickle exceeds the container budget")
        active.add(id(item))
        pending.append((item, depth, True))
        children = [part for pair in item.items() for part in pair] if kind in {dict, OrderedDict} else item
        pending.extend((part, depth + 1, False) for part in children)


class RestrictedUnpickler(pickle._Unpickler):
    """No global resolution except inert, explicitly named built-in containers.

    REDUCE accepts only safe built-in constructors. Two exact legacy wire
    records are projected directly into tuple/dict data by custom opcodes;
    their client classes, __new__ and __setstate__ are never loaded or called.
    """
    def __init__(self, file, *, fix_imports=True, encoding="ASCII", errors="strict", buffers=None):
        if buffers is not None:
            raise UnsafeReplayPickle("Out-of-band pickle buffers are not supported")
        data = file.read(MAX_PICKLE_BYTES + 1)
        if not isinstance(data, (bytes, bytearray)):
            raise UnsafeReplayPickle("Replay pickle stream must be binary")
        data = bytes(data)
        with _LOCK:
            _STATISTICS["loads"] += 1
            _STATISTICS["largest_blob_bytes"] = max(_STATISTICS["largest_blob_bytes"], len(data))
        try:
            _inspect(data)
        except Exception:
            with _LOCK:
                _STATISTICS["rejected"] += 1
            raise
        self._buffer = io.BytesIO(data)
        self._player_mode_dict_ids = set()
        super().__init__(self._buffer, fix_imports=fix_imports, encoding=encoding, errors=errors)

    def find_class(self, module, name):
        pair = (module, name)
        if pair not in SAFE_GLOBALS and pair not in WIRE_RECORD_GLOBALS:
            raise UnsafeReplayPickle(f"Forbidden replay pickle global: {module}.{name}")
        with _LOCK:
            _STATISTICS["globals_seen"].add(".".join(pair))
        return SAFE_GLOBALS[pair] if pair in SAFE_GLOBALS else WIRE_RECORD_GLOBALS[pair]

    def persistent_load(self, persistent_id):
        raise UnsafeReplayPickle("Persistent replay pickle IDs are not supported")

    def load_reduce(self):
        if len(self.stack) < 2:
            raise UnsafeReplayPickle("Malformed REDUCE stack")
        args, constructor = self.stack.pop(), self.stack.pop()
        if not any(constructor is allowed for allowed in (set, frozenset, OrderedDict)) or type(args) is not tuple:
            raise UnsafeReplayPickle("REDUCE callable is not an approved container constructor")
        _basic_graph(args)
        self.append(constructor(*args))

    def load_newobj(self):
        if len(self.stack) < 2:
            raise UnsafeReplayPickle("Malformed NEWOBJ stack")
        args, record = self.stack.pop(), self.stack.pop()
        if record is _CAMOUFLAGE_RECORD and type(args) is tuple and len(args) == 2 and all(type(value) is int for value in args):
            self.append(args)
        elif record is _PLAYER_MODE_RECORD and args == ():
            value = {}
            self._player_mode_dict_ids.add(id(value))
            self.append(value)
        else:
            raise UnsafeReplayPickle("NEWOBJ is restricted to the two explicit data-only wire records")

    def load_build(self):
        if len(self.stack) < 2:
            raise UnsafeReplayPickle("Malformed BUILD stack")
        state = self.stack.pop()
        value = self.stack[-1]
        if type(value) is not dict or id(value) not in self._player_mode_dict_ids or type(state) is not dict:
            raise UnsafeReplayPickle("BUILD is restricted to PlayerMode data projection")
        normalized = {}
        for key, field in state.items():
            key = key.decode("ascii") if type(key) is bytes else key
            if key not in {"playerModeType", "observedTeamId"} or type(field) is not int:
                raise UnsafeReplayPickle("Unexpected PlayerMode state field or type")
            normalized[key] = field
        value.update(normalized)

    dispatch = pickle._Unpickler.dispatch.copy()
    dispatch[pickle.REDUCE[0]] = load_reduce
    dispatch[pickle.NEWOBJ[0]] = load_newobj
    dispatch[pickle.BUILD[0]] = load_build

    def load(self):
        try:
            value = super().load()
            _basic_graph(value)
            return value
        except Exception as error:
            with _LOCK:
                _STATISTICS["rejected"] += 1
            if isinstance(error, UnsafeReplayPickle):
                raise
            raise UnsafeReplayPickle(f"Invalid container-only replay pickle: {type(error).__name__}: {error}") from error


def restricted_loads(data, /, *, fix_imports=True, encoding="ASCII", errors="strict", buffers=None):
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("Replay pickle loads requires a bytes-like object")
    if len(data) > MAX_PICKLE_BYTES:
        raise UnsafeReplayPickle("Replay pickle exceeds the blob size limit")
    return RestrictedUnpickler(io.BytesIO(data), fix_imports=fix_imports,
                               encoding=encoding, errors=errors, buffers=buffers).load()


def restricted_load(file, *, fix_imports=True, encoding="ASCII", errors="strict", buffers=None):
    return RestrictedUnpickler(file, fix_imports=fix_imports, encoding=encoding, errors=errors, buffers=buffers).load()


class _PickleProxy:
    __slots__ = ()
    loads = staticmethod(restricted_loads)
    load = staticmethod(restricted_load)
    Unpickler = RestrictedUnpickler
    UnpicklingError = pickle.UnpicklingError
    PickleError = pickle.PickleError


_PROXY = _PickleProxy()


def pickle_guard_statistics():
    with _LOCK:
        return {**_STATISTICS, "globals_seen": sorted(_STATISTICS["globals_seen"])}


def install_replay_pickle_guard():
    """Install before parsing, inside the one-replay worker process only."""
    controller = importlib.import_module(CONTROLLER_MODULE)
    package = importlib.import_module(CORE_PACKAGE)
    modules = [controller, package]
    if hasattr(package, "__path__"):
        modules.extend(importlib.import_module(info.name) for info in pkgutil.walk_packages(package.__path__, package.__name__ + "."))
    bindings = {}
    with _LOCK:
        for module in modules:
            guarded = []
            for name, value in list(vars(module).items()):
                replacement = None
                if value is pickle or value is _PROXY:
                    replacement = _PROXY
                elif value is pickle.loads or value is restricted_loads:
                    replacement = restricted_loads
                elif value is pickle.load or value is restricted_load:
                    replacement = restricted_load
                elif value is pickle.Unpickler or value is RestrictedUnpickler:
                    replacement = RestrictedUnpickler
                if replacement is not None:
                    setattr(module, name, replacement)
                    guarded.append(name)
            if guarded:
                bindings[module.__name__] = sorted(guarded)
    if CONTROLLER_MODULE not in bindings:
        raise RuntimeError("The fixed replay controller has no guardable pickle binding; dependency layout changed")
    return {"scope": "isolated worker; fixed 15_3_0 controller and core data_types bindings",
            "bindings": bindings, "inspected_modules": sorted(module.__name__ for module in modules),
            "max_pickle_bytes": MAX_PICKLE_BYTES, "max_pickle_opcodes": MAX_PICKLE_OPCODES,
            "max_memo_index": MAX_MEMO_INDEX,
            "allowed_globals": sorted(".".join(pair) for pair in SAFE_GLOBALS),
            "wire_record_projections": {"CamouflageInfo.CamouflageInfo": "tuple(id, schemeId); integer fields only",
                                        "PlayerModeDef.PlayerMode": "dict(playerModeType, observedTeamId); integer fields only"},
            "stdlib_pickle_replaced": False}
