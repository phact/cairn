"""
Cairn persistent tier-3 agent (gdb-Python, under rr replay).

Runs ONCE inside a warm gdb session connected to an rr replay server, then
serves many LOC requests over a control socket — restarting the (sub-second)
replay per request instead of relaunching gdb + reloading 248 MB of DWARF.
This turns ~2.2 s/request into ~100-300 ms.

Protocol (newline-delimited JSON over CAIRN_CTRL_PORT):
  request : {file, line, hit, instant|null, max}
  reply   : {function, lines:[{i,line,locals,fn}], reverse_step:{...}}  | {error}
  request : {cmd:"shutdown"}  ->  exits
"""

import gdb
import json
import os
import socket
import subprocess
import sys

CTRL_PORT = int(os.environ["CAIRN_CTRL_PORT"])
_CAIRN_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "cairn")
if _CAIRN_DIR not in sys.path:
    sys.path.insert(0, _CAIRN_DIR)
import decoders  # shared decode SPECS (JWT/epoch/name lists) — core-owned
# We do NOT truncate real values — the backend must not throw away bytes the UI
# might need (the UI ellipsizes for display). Only a generous safety ceiling
# guards against a pathological multi-MB buffer, and trips an honest flag.
MAX_VAL = int(os.environ.get("CAIRN_MAX_VAL", str(1 << 20)))   # 1 MiB
VAL_TRUNC = MAX_VAL

for cmd in ("set pagination off", "set print pretty off",
            "set print frame-arguments none", "set width 0",
            "set confirm off"):
    gdb.execute(cmd)


def load_rust_printers():
    """Load the Rust toolchain's gdb pretty-printers (what `rust-gdb` uses) so
    owned collections render as their CONTENTS — `Vec<SocketAddr>` becomes
    `[127.0.0.1:44107]`, not a RawVec struct. gdb's built-ins only cover
    &str/slices/primitives."""
    try:
        etc = os.environ.get("CAIRN_RUST_ETC")
        sysroot = os.environ.get("CAIRN_RUST_SYSROOT")
        if not etc:
            sysroot = subprocess.check_output(
                ["rustc", "--print", "sysroot"], text=True).strip()
            etc = os.path.join(sysroot, "lib", "rustlib", "etc")
        if etc not in sys.path:
            sys.path.insert(0, etc)
        if sysroot:
            try:
                gdb.execute(f"add-auto-load-safe-path {sysroot}")
            except gdb.error:
                pass
        import gdb_load_rust_pretty_printers  # noqa: F401  (registers on import)
    except Exception as e:
        try:
            print(f"[cairn] rust pretty-printers not loaded: {e}")
        except Exception:
            pass


load_rust_printers()
for sig in ("SIGPIPE", "SIG32", "SIG33", "SIG34", "SIGUSR1", "SIGUSR2"):
    try:
        gdb.execute(f"handle {sig} nostop noprint pass")
    except gdb.error:
        pass


def trunc(s):
    s = s.replace("\n", " ")
    return s if len(s) <= VAL_TRUNC else s[:VAL_TRUNC] + "…[clipped]"


import re as _re
_ADDR = _re.compile(r"^\s*0x[0-9a-fA-F]+\s*$")
_AGG = None


def _agg_codes():
    global _AGG
    if _AGG is None:
        _AGG = {gdb.TYPE_CODE_STRUCT, gdb.TYPE_CODE_UNION, gdb.TYPE_CODE_ENUM}
    return _AGG


def decode_value(val):
    """Decode a DWARF value into (display, ref). For pointer/reference types
    (e.g. a method's `&self`, a `*mut T`) `ref` is {kind,address} so the UI can
    badge it and `display` shows the pointee's CONTENTS when it's an aggregate
    (gdb otherwise prints the bare address). Guarded: a dangling pointer keeps
    the address as display. Non-pointers return ref=None."""
    s = str(val)
    ref = None
    try:
        t = val.type.strip_typedefs()
        code = t.code
        if code in (gdb.TYPE_CODE_REF, gdb.TYPE_CODE_PTR,
                    getattr(gdb, "TYPE_CODE_RVALUE_REF", -99)):
            kind = "ptr" if code == gdb.TYPE_CODE_PTR else "ref"
            try:
                address = hex(int(val))
            except Exception:
                address = s if _ADDR.match(s) else None
            ref = {"kind": kind, "address": address}
            # If gdb only gave us the address, show the pointee's contents.
            if _ADDR.match(s) and t.target().strip_typedefs().code in _agg_codes():
                try:
                    s = str(val.dereference())
                except Exception:
                    pass
    except Exception:
        pass
    return s, ref


# Structured decode bounds for the EAGER tree shipped with a LOC line. Kept
# modest so the initial payload is small and fast; anything clipped here is not
# discarded — it's marked `expandable` with a re-navigable `path`, and the UI
# fetches it on click (GET /frame/:id/value). Leaf VALUES are never truncated
# (only the MAX_VAL safety ceiling); we bound STRUCTURE, not data.
TREE_DEPTH = int(os.environ.get("CAIRN_TREE_DEPTH", "3"))
TREE_CHILDREN = int(os.environ.get("CAIRN_TREE_CHILDREN", "64"))

# Pointer targets worth following when resolving nested pointers. We deref to
# these; we do NOT chase void (`*mut ()`), function pointers, or vtables (no
# meaningful pointee), and null pointers render as "null".
_DEREFABLE = None


def _derefable_codes():
    global _DEREFABLE
    if _DEREFABLE is None:
        codes = {gdb.TYPE_CODE_STRUCT, gdb.TYPE_CODE_UNION, gdb.TYPE_CODE_ENUM,
                 gdb.TYPE_CODE_INT, gdb.TYPE_CODE_BOOL, gdb.TYPE_CODE_FLT,
                 gdb.TYPE_CODE_CHAR, gdb.TYPE_CODE_ARRAY}
        _DEREFABLE = codes
    return _DEREFABLE


def _scalarish(s):
    s = s.replace("\n", " ")
    return s if len(s) <= MAX_VAL else s[:MAX_VAL] + "…[clipped]"


def _summary(val):
    """A concise one-line summary that NEVER recurses into nested fields. For an
    aggregate without a pretty-printer, `str(val)` dumps the entire subtree (the
    sync-machinery wall), so we use the short type name + `{…}` instead and let
    the tree/lazy-expand carry the contents. Collections/scalars (which have a
    concise printer or repr) render normally."""
    try:
        t = val.type.strip_typedefs()
        if t.code in (gdb.TYPE_CODE_STRUCT, gdb.TYPE_CODE_UNION):
            vis = gdb.default_visualizer(val)
            if vis is not None:
                s = vis.to_string()
                if s is not None:
                    return _scalarish(str(s))
            return _short_type(str(t)) + " {…}"
    except Exception:
        pass
    return _scalarish(str(val))


_jwt_decode = decoders.jwt_decode


def _annotate_text(node, text):
    jwt = _jwt_decode(text)
    if jwt is not None:
        node["jwt"] = jwt


def _enum_node(val, t, depth_left, budget, path):
    """Render a Rust enum as its discriminant: `Option` -> `None` / `Some(x)`,
    `Result` -> `Ok(..)`/`Err(..)`, custom -> `Variant(..)`. Detected
    structurally: the active variant appears as the struct's single field, and
    enum variants are CamelCase (struct fields are snake_case). No type names
    hardcoded."""
    try:
        fields = [f for f in t.fields() if getattr(f, "name", None)]
    except Exception:
        return None
    if len(fields) != 1:
        return None
    variant = fields[0].name
    if not _re.match(r"[A-Z][A-Za-z0-9_]*$", variant):
        return None
    node = {"summary": variant, "variant": variant}
    try:
        pnode = decode_tree(val[variant], depth_left, budget, path)
    except Exception:
        return node
    ch = pnode.get("children")
    if ch:
        inner = (_flatten(ch[0]) if len(ch) == 1
                 else ", ".join(f"{c.get('name')}: {_flatten(c)}" for c in ch[:6]))
        node["summary"] = f"{variant}({inner})"
        node["children"] = ch
    elif pnode.get("summary") and not pnode["summary"].endswith("{…}"):
        node["summary"] = f"{variant}({pnode['summary']})"
    if pnode.get("expandable"):
        node["expandable"], node["path"] = True, pnode.get("path", path)
    return node


def _bytes_string(kids):
    """If a collection's children are all `u8` (0-255), decode them to the
    ASCII/UTF-8 string they spell — so `&[u8]{104,111,115,116}` reads as `"host"`
    instead of leaving the byte->char math to the human. None if not all bytes."""
    if not kids or len(kids) > 4096:
        return None
    out = bytearray()
    for k in kids:
        s = k.get("summary")
        if s is None or not s.lstrip("-").isdigit():
            return None
        v = int(s)
        if not (0 <= v <= 255):
            return None
        out.append(v)
    try:
        text = bytes(out).decode("utf-8")
    except Exception:
        return None
    if not all(c.isprintable() or c in "\t\n\r" for c in text):
        return None
    return f'"{text}" ({len(out)} bytes)'


def _flatten(node, depth=2):
    """One-line, wrapper-collapsed rendering of a tree node — the source for the
    inline `value` chip, so it agrees with the rail tree and shows no machinery."""
    if node.get("children") and depth > 0:
        if node.get("wrapper"):
            return node.get("summary") or node["wrapper"]
        head = _short_type(node.get("type", "")) if node.get("type") else ""
        inner = ", ".join(f"{c.get('name', '?')}: {_flatten(c, depth - 1)}"
                          for c in node["children"][:8])
        more = "…" if len(node["children"]) > 8 else ""
        return f"{head} {{{inner}{more}}}".strip()
    return node.get("summary") or node.get("type") or "?"


# Total nodes per eager tree. With lazy expansion this just bounds the FIRST
# payload; deeper structure is fetched on click, never lost.
MAX_NODES = int(os.environ.get("CAIRN_TREE_NODES", "800"))


def _has_children(val):
    """Would this value expand into children if decoded deeper? Used to mark a
    depth/budget-clipped node `expandable` (vs a true leaf)."""
    try:
        t = val.type.strip_typedefs()
        code = t.code
        if code in (gdb.TYPE_CODE_PTR, gdb.TYPE_CODE_REF):
            return int(val) != 0
        if code in (gdb.TYPE_CODE_STRUCT, gdb.TYPE_CODE_UNION, gdb.TYPE_CODE_ARRAY):
            return True
        vis = gdb.default_visualizer(val)
        return vis is not None and getattr(vis, "children", None) is not None
    except Exception:
        return False


def _try_buffer(val, t):
    """Render a `{ptr, len}` buffer (e.g. `bytes::Bytes`, custom slices) as its
    CONTENTS by reading `len` elements from `ptr` — instead of dereferencing the
    pointer to a single first element. u8 buffers become a string (if UTF-8) or a
    byte array. Returns a node, or None if this isn't a ptr+len buffer."""
    try:
        fnames = {f.name for f in t.fields() if getattr(f, "name", None)}
    except Exception:
        return None
    if "ptr" not in fnames or "len" not in fnames:
        return None
    try:
        ptr = val["ptr"]
        if ptr.type.strip_typedefs().code != gdb.TYPE_CODE_PTR:
            return None
        n = int(val["len"])
        addr = int(ptr)
    except Exception:
        return None
    if n < 0 or n > 65536 or addr == 0:          # sanity / safety bound
        return None
    elem = ptr.type.strip_typedefs().target().strip_typedefs()
    if elem.code in (gdb.TYPE_CODE_INT, gdb.TYPE_CODE_CHAR) and elem.sizeof == 1:
        try:
            mem = bytes(gdb.selected_inferior().read_memory(addr, n))
        except Exception:
            return None
        node = {"type": str(t), "synthesized": "bytes", "len": n}
        try:
            s = mem.decode("utf-8")
            printable = all(c.isprintable() or c in "\t\n\r" for c in s)
        except Exception:
            printable = False
        if printable:
            node["summary"] = '"' + _scalarish(s) + '"'
            _annotate_text(node, s)            # JWT-in-string -> claims
        else:
            shown = ", ".join(str(b) for b in mem[:64])
            node["summary"] = "[" + shown + ("…" if n > 64 else "") + "]"
        return node
    return None


# Machinery vs. signal — decided STRUCTURALLY, not by naming runtimes.
#
# In Rust, interior mutability is ONLY possible through `core::cell::UnsafeCell`.
# So a struct (with no pretty-printer) that contains an `UnsafeCell<T>` field IS
# an interior-mutability wrapper — a lock, atomic, RefCell, or any custom one —
# whose logical value is the guarded `T`; the sibling fields (semaphores,
# waitlists, poison flags, atomics) are machinery. We show `<TypeName>(<T>)` and
# elide the rest. This generalizes to std / tokio / parking_lot / async-std /
# hand-rolled locks present and future, because they all build on UnsafeCell.
# Only CORE LANGUAGE primitives are named below — stable, not library-specific.
_TRANSPARENT = ("UnsafeCell", "MaybeUninit", "ManuallyDrop")


def _short_type(tn):
    return tn.split("<", 1)[0].split("::")[-1]


def _cell_inner(val):
    """The single inner value of a transparent wrapper (UnsafeCell/MaybeUninit/
    ManuallyDrop) — its `value` field, else its sole field."""
    try:
        for nm in ("value", "0"):
            try:
                return val[nm]
            except Exception:
                pass
        fields = [f for f in val.type.strip_typedefs().fields()
                  if getattr(f, "name", None)]
        if len(fields) == 1:
            return val[fields[0].name]
    except Exception:
        pass
    return None


def _guarded_value(val, t):
    """The `T` inside an interior-mutability wrapper: the value of its
    `UnsafeCell<T>` field (the universal Rust marker for guarded data)."""
    try:
        fields = t.fields()
    except Exception:
        return None
    for f in fields:
        if not getattr(f, "name", None):
            continue
        try:
            fv = val[f.name]
            if _short_type(str(fv.type.strip_typedefs())) == "UnsafeCell":
                inner = _cell_inner(fv)
                if inner is not None:
                    return inner
        except Exception:
            continue
    return None


def _wrapper_node(val, t, depth_left, budget, path):
    """Collapse machinery to its logical value, by structure not by crate name.
    Returns a node, or None to decode `val` normally."""
    name = _short_type(str(t))
    if name == "PhantomData":
        return {"summary": "PhantomData"}              # zero-sized marker, elide
    if name in _TRANSPARENT:                            # wrapper IS its inner
        inner = _cell_inner(val)
        if inner is not None:
            return decode_tree(inner, depth_left, budget, path)
        return None
    guarded = _guarded_value(val, t)                   # struct around an UnsafeCell
    if guarded is not None:
        n = decode_tree(guarded, depth_left, budget, path)
        base = n.get("summary") or _short_type(n.get("type", "")) or ""
        n["wrapper"] = name
        n["summary"] = f"{name}({base})"
        return n
    return None


def decode_tree(val, depth_left=TREE_DEPTH, budget=None, path=None):
    """Decode a gdb.Value into a structured node — walked THROUGH the Rust
    pretty-printers (so a Vec exposes its elements, a struct its fields) and
    following pointers to their pointee. Leaf VALUES keep their full text.

    Structure is bounded by depth / width / a node budget; a clipped node is NOT
    a dead end — it carries `expandable: true` + `path` (the accessor list from
    the local root) so the UI can fetch it deeper via the value endpoint.

      node = {summary?, type?, ref?, children?: [{name, ...node}],
              expandable?, path?, truncated?}
    """
    if path is None:
        path = []
    if budget is None:
        budget = {"n": 0}
    budget["n"] += 1
    if budget["n"] > MAX_NODES:
        n = {"summary": _summary(val)}
        if _has_children(val):
            n["expandable"], n["path"] = True, path
        return n

    node = {}
    try:
        t = val.type.strip_typedefs()
        code = t.code
    except Exception:
        return {"summary": _summary(val)}

    # pointer/reference: record it, then RESOLVE it — follow the pointer to its
    # pointee (at any nesting depth). The deref is transparent to `path` (no
    # accessor added). Null -> "null"; void/function pointers keep the address.
    if code in (gdb.TYPE_CODE_PTR, gdb.TYPE_CODE_REF,
                getattr(gdb, "TYPE_CODE_RVALUE_REF", -99)):
        # Every pointer-like node carries an explicit `deref.status` — consumers
        # key on THAT, never on shape/children, because a null pointer and a
        # budget-stopped pointer look identical by shape. The four states are
        # reasoned about oppositely (see spec "Lossy values").
        kind = "ptr" if code == gdb.TYPE_CODE_PTR else "ref"
        try:
            addr_int = int(val)
        except Exception:
            addr_int = None
        address = hex(addr_int) if addr_int is not None else None
        ref = {"kind": kind, "address": address}
        if addr_int == 0:                              # nothing there — a fact
            return {"summary": "null", "ref": ref,
                    "deref": {"status": "null", "kind": kind}}
        try:
            target = t.target().strip_typedefs()
        except Exception:
            target = None
        if target is not None and target.code in _derefable_codes():
            if depth_left > 0:
                try:
                    inner = decode_tree(val.dereference(), depth_left - 1,
                                        budget, path)
                    inner["ref"] = ref
                    inner["deref"] = {"status": "expanded", "kind": kind,
                                      "addr": address}
                    return inner
                except gdb.MemoryError:                # tried, memory gone
                    return {"summary": address, "ref": ref,
                            "deref": {"status": "unreadable", "kind": kind,
                                      "addr": address,
                                      "reason": "memory not readable at this instant"}}
                except Exception:
                    pass
            # not walked — real, retrievable, just budget-stopped
            return {"summary": address, "ref": ref, "path": path,
                    "expandable": True,                # back-compat for current UI
                    "deref": {"status": "unexpanded", "kind": kind,
                              "addr": address, "reason": "depth_budget",
                              "path": path}}
        # opaque pointer (void `*mut ()`, function pointer, vtable) — no
        # meaningful pointee to follow; the address IS the value.
        return {"summary": address or _summary(val), "ref": ref,
                "deref": {"status": "expanded", "kind": kind, "addr": address,
                          "opaque": True}}

    # Use the pretty-printer (so Rust types render nicely + expose children).
    vis = None
    try:
        vis = gdb.default_visualizer(val)
    except Exception:
        vis = None
    if vis is not None:
        try:
            s = vis.to_string()
            if s is not None:
                node["summary"] = _scalarish(str(s))
                _annotate_text(node, str(s))   # JWT-in-string -> claims
        except Exception:
            pass
        children = getattr(vis, "children", None)
        if children is not None:
            if depth_left <= 0:
                node["expandable"], node["path"] = True, path
            else:
                kids, n = [], 0
                try:
                    for name, cval in children():
                        if n >= TREE_CHILDREN or budget["n"] >= MAX_NODES:
                            node["expandable"], node["path"] = True, path
                            break
                        try:
                            cp = path + [str(name)]
                            child = decode_tree(cval, depth_left - 1, budget, cp)
                            child["name"] = str(name)
                            _annotate_time(child, str(name))
                            kids.append(child)
                        except Exception:
                            pass
                        n += 1
                except Exception:
                    pass
                if kids:
                    node["children"] = kids
                    bs = _bytes_string(kids)   # u8 collection -> ASCII string
                    if bs is not None:
                        node["summary"] = bs
                        node["ascii"] = True
                        _annotate_text(node, bs)   # JWT-in-bytes -> claims
        if "summary" not in node and "children" not in node:
            node["summary"] = _summary(val)
        return node

    # Plain aggregate without a printer: walk DWARF fields.
    if code in (gdb.TYPE_CODE_STRUCT, gdb.TYPE_CODE_UNION):
        # Interior-mutability wrapper / transparent cell -> its logical value,
        # eliding the sync machinery (structural, runtime-agnostic).
        w = _wrapper_node(val, t, depth_left, budget, path)
        if w is not None:
            return w
        # {ptr,len} buffers (Bytes etc.) -> their contents, not a first byte.
        buf = _try_buffer(val, t)
        if buf is not None:
            return buf
        # Rust enum -> its discriminant (Option/Result/custom), not raw variant.
        en = _enum_node(val, t, depth_left, budget, path)
        if en is not None:
            return en
        node["type"] = str(t)
        if depth_left <= 0:
            node["summary"] = _summary(val)
            node["expandable"], node["path"] = True, path
            return node
        kids = []
        try:
            for f in t.fields():
                if not getattr(f, "name", None):
                    continue
                if "core::marker::PhantomData" in str(f.type):
                    continue                     # zero-sized marker, elide
                if budget["n"] >= MAX_NODES:
                    node["expandable"], node["path"] = True, path
                    break
                try:
                    cp = path + [f.name]
                    child = decode_tree(val[f], depth_left - 1, budget, cp)
                    child["name"] = f.name
                    _annotate_time(child, f.name)
                    kids.append(child)
                except Exception:
                    pass
        except Exception:
            pass
        if kids:
            node["children"] = kids
        elif "expandable" not in node:
            node["summary"] = _summary(val)
        return node

    # scalar / leaf
    node["summary"] = _summary(val)
    return node


# Epoch annotation is gated on the NAME, not the magnitude — the programmer's
# field name (`iat`, `exp`, `*_at`, `*_time`…) is the real source-level signal
# that an integer is a time. Range-only would mislabel non-timestamps (a
# semaphore's 1073741822 permits, a capacity, an id) as bogus dates — exactly
# the fabrication the grounding rule forbids. Additive: the raw value stays.
_timeish_name = decoders.timeish_name
_epoch_iso = decoders.epoch_iso


def _annotate_time(node, name):
    """Tag a scalar-integer leaf as a date IFF its name says it's a time and the
    value is a plausible epoch. Both required — name AND range — so non-time
    integers are never mislabeled."""
    if node.get("children"):
        return
    s = node.get("summary")
    if not (s and s.lstrip("-").isdigit() and _timeish_name(name)):
        return
    iso = _epoch_iso(int(s))
    if iso:
        node["as_time"] = iso


def _autoderef(val, hops=12):
    """Follow pointers to their pointee, mirroring decode_tree's transparent
    deref, so a path can be navigated through `&self`/Box/Arc-style indirection."""
    for _ in range(hops):
        try:
            t = val.type.strip_typedefs()
        except Exception:
            return val
        if t.code in (gdb.TYPE_CODE_PTR, gdb.TYPE_CODE_REF,
                      getattr(gdb, "TYPE_CODE_RVALUE_REF", -99)):
            try:
                if int(val) == 0:
                    return val
                if t.target().strip_typedefs().code in _derefable_codes():
                    val = val.dereference()
                    continue
            except Exception:
                return val
        return val
    return val


def _try_step(val, acc):
    """One accessor step on `val`: struct field, then visualizer child name.
    Returns (new_val, True) or (val, False)."""
    try:
        return val[acc], True                  # struct/tuple field by name
    except Exception:
        pass
    try:
        vis = gdb.default_visualizer(val)
    except Exception:
        vis = None
    ch = getattr(vis, "children", None) if vis else None
    if ch:
        try:
            for name, cval in ch():
                if str(name) == acc:
                    return cval, True
        except Exception:
            pass
    return val, False


def _unwrap_transparent(val):
    """One transparent hop, mirroring decode_tree's path-invisible layers:
    enum -> its active variant payload; interior-mutability wrapper -> the
    guarded value; UnsafeCell/MaybeUninit/ManuallyDrop -> the inner value.
    Returns (new_val, True) if a hop happened. decode_tree records NONE of these
    as path segments, so navigate must step through them implicitly — this is
    what async state machines (enum-of-suspend-variants) need."""
    try:
        t = val.type.strip_typedefs()
    except Exception:
        return val, False
    if t.code not in (gdb.TYPE_CODE_STRUCT, gdb.TYPE_CODE_UNION):
        return val, False
    name = _short_type(str(t))
    if name in _TRANSPARENT:
        inner = _cell_inner(val)
        if inner is not None:
            return inner, True
        return val, False
    guarded = _guarded_value(val, t)
    if guarded is not None:
        return guarded, True
    try:
        fields = [f for f in t.fields() if getattr(f, "name", None)]
    except Exception:
        return val, False
    if len(fields) == 1 and _re.match(r"[A-Z][A-Za-z0-9_]*$", fields[0].name):
        try:
            return val[fields[0].name], True   # enum -> active variant
        except Exception:
            pass
    return val, False


def navigate(rootval, accessors):
    """Re-walk an accessor path from a root local to the value it names — the
    inverse of the `path` decode_tree records on clipped nodes. Mirrors all of
    decode_tree's path-transparent layers: pointers (autoderef), enum variants,
    and sync-wrapper unwrapping. Without the unwrap hops, paths through async
    futures (`__awaitee` suspend-variant enums) and Options fail to re-walk."""
    val = rootval
    for acc in accessors:
        stepped = False
        for _ in range(12):                    # bounded transparent hops
            val = _autoderef(val)
            val, stepped = _try_step(val, acc)
            if stepped:
                break
            val, hopped = _unwrap_transparent(val)
            if not hopped:
                break
        if not stepped:
            raise KeyError(acc)
    return val


def depth(frame, cap=400):
    d, f = 0, frame
    while f is not None and d < cap:
        d += 1
        try:
            f = f.older()
        except Exception:
            break
    return d


def cur_line():
    try:
        sal = gdb.newest_frame().find_sal()
        return sal.line if sal else 0
    except gdb.error:
        return 0


def cur_fn():
    try:
        f = gdb.newest_frame().function()
        return f.name if f else "?"
    except gdb.error:
        return "?"


def _stamp_instant(node, instant):
    """A pointer's pointee is only meaningful at the instant it was read (it may
    be mutated/freed later), so an unexpanded node records its instant — the
    `cairn value --instant N` that re-reaches exactly this state."""
    if node.get("deref", {}).get("status") == "unexpanded":
        node["deref"]["instant"] = instant
    for c in node.get("children", []):
        _stamp_instant(c, instant)


def locals_now(instant=None):
    out = []
    try:
        fr = gdb.newest_frame()
        blk = fr.block()
    except (gdb.error, RuntimeError):
        return out
    seen = set()
    while blk is not None:
        for sym in blk:
            if not (sym.is_variable or sym.is_argument):
                continue
            if sym.name in seen:
                continue
            seen.add(sym.name)
            try:
                val = sym.value(fr)
                tree = decode_tree(val, path=[sym.name])  # wrapper-collapsing
                if instant is not None:
                    _stamp_instant(tree, instant)
                rec = {"name": sym.name,
                       "kind": "arg" if sym.is_argument else "local",
                       # inline chip and rail tree share one decoded value, so
                       # both collapse machinery identically
                       "value": trunc(_flatten(tree)),
                       "tree": tree}
                if tree.get("ref"):
                    rec["ref"] = tree["ref"]
                out.append(rec)
            except Exception:
                out.append({"name": sym.name, "value": "<unreadable>",
                            "kind": "arg" if sym.is_argument else "local"})
        if blk.function is not None:
            break
        blk = blk.superblock
    return out


def handle(req):
    file = req["file"]
    line = int(req["line"])
    hit = int(req.get("hit", 1))
    instant = req.get("instant")
    maxlines = int(req.get("max", 150))

    gdb.execute("delete")
    bp = gdb.Breakpoint(f"{file}:{line}", type=gdb.BP_BREAKPOINT)
    bp.silent = True
    # Restart the replay from the beginning. Under rr, `run` stops at the
    # process start (not the first breakpoint), so advance to the bp's first
    # hit, then to the requested activation.
    gdb.execute("run")
    if cur_line() != line:
        gdb.execute("continue")
    for _ in range(hit - 1):
        gdb.execute("continue")

    entry = gdb.newest_frame()
    entry_depth = depth(entry)
    fn = entry.function()
    fn_name = fn.name if fn else "?"

    def in_activation():
        try:
            fr = gdb.newest_frame()
            f2 = fr.function()
        except gdb.error:
            return False
        return f2 is not None and f2.name == fn_name and depth(fr) >= entry_depth

    lines = []
    last_i = 0
    for i in range(maxlines):
        lines.append({"i": i, "line": cur_line(), "locals": locals_now(i),
                      "fn": cur_fn()})
        last_i = i
        try:
            gdb.execute("next")
        except gdb.error:
            break
        if not in_activation():
            break

    reverse = {"supported": False}
    try:
        target_i = instant if (instant is not None and 0 <= instant < last_i) \
                   else max(0, last_i - 1)
        expected = lines[target_i]["line"] if lines else 0
        if expected and target_i < last_i:
            rbp = gdb.Breakpoint(f"{file}:{expected}", type=gdb.BP_BREAKPOINT)
            rbp.silent = True
            gdb.execute("reverse-continue")
            rbp.delete()
        rfn = gdb.newest_frame().function()
        rfn_name = rfn.name if rfn else "?"
        reverse = {
            "supported": True,
            "method": "reverse-continue to line breakpoint",
            "reread_instant": target_i,
            "reread_function": rfn_name,
            "reread_line": cur_line(),
            "expected_line": expected,
            "reread_locals": locals_now(),
            "matches_forward": (cur_line() == expected and rfn_name == fn_name),
        }
    except gdb.error as e:
        reverse = {"supported": False, "error": str(e)}

    return {"function": fn_name, "file": file, "entry_line": line, "hit": hit,
            "lines": lines, "reverse_step": reverse}


def resolve(req):
    """Lazy expansion: re-reach a frame's activation at a given instant, walk
    the accessor `path` from the named local, and decode that subtree deeper.
    Powers click-to-expand on a clipped tree node."""
    file = req["file"]
    line = int(req["line"])
    hit = int(req.get("hit", 1))
    instant = req.get("instant")
    path = req.get("path") or []
    depth_req = int(req.get("depth", TREE_DEPTH + 2))
    if not path:
        return {"error": "empty path"}

    gdb.execute("delete")
    bp = gdb.Breakpoint(f"{file}:{line}", type=gdb.BP_BREAKPOINT)
    bp.silent = True
    gdb.execute("run")
    if cur_line() != line:
        gdb.execute("continue")
    for _ in range(hit - 1):
        gdb.execute("continue")
    # Step to the same instant the clipped node came from, so a mutated local
    # has the value the UI showed (args are stable, so instant 0 is fine too).
    if instant:
        for _ in range(int(instant)):
            try:
                gdb.execute("next")
            except gdb.error:
                break
    bp.delete()

    try:
        rootval = gdb.newest_frame().read_var(path[0])
    except Exception as e:
        return {"error": f"local `{path[0]}` not in scope: {e}"}
    try:
        target = navigate(rootval, path[1:])
    except Exception as e:
        return {"error": f"could not navigate {path}: {e}"}
    return {"path": path, "tree": decode_tree(target, depth_left=depth_req,
                                              path=path)}


def serve():
    sock = socket.create_connection(("127.0.0.1", CTRL_PORT))
    buf = b""
    sock.sendall(b'{"ready":true}\n')
    while True:
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                return
            buf += chunk
        line, buf = buf.split(b"\n", 1)
        if not line.strip():
            continue
        req = json.loads(line)
        cmd = req.get("cmd")
        if cmd == "shutdown":
            return
        try:
            resp = resolve(req) if cmd == "resolve" else handle(req)
        except Exception as e:  # keep the session alive on a bad request
            resp = {"error": f"{type(e).__name__}: {e}"}
        sock.sendall((json.dumps(resp) + "\n").encode())


try:
    serve()
finally:
    gdb.execute("quit")
