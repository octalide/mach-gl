#!/usr/bin/env python3
# gen.py: emit the generated mach-gl binding layers from the Khronos registry.
#
# tools/gl.xml is a verbatim snapshot of xml/gl.xml from KhronosGroup/OpenGL-Registry
# pinned at commit 77ccc142a506fdba4b56e41aa884e20bc060ec17:
#   https://raw.githubusercontent.com/KhronosGroup/OpenGL-Registry/77ccc142a506fdba4b56e41aa884e20bc060ec17/xml/gl.xml
#
# usage:
#   tools/gen.py            regenerate src/c.mach, src/enums.mach, src/cmd.mach, src/gl.mach
#   tools/gen.py check      regenerate to memory and diff against the committed sources;
#                           exit nonzero (and print a unified diff) on any drift
#
# stdlib only; output is deterministic so the committed sources and the registry
# pin cannot drift apart.

import difflib
import os
import re
import sys
import xml.etree.ElementTree as ET

REGISTRY_COMMIT = "77ccc142a506fdba4b56e41aa884e20bc060ec17"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
XML = os.path.join(ROOT, "tools", "gl.xml")
SRC = os.path.join(ROOT, "src")

# mach reserved keywords (grammar.md); a generated identifier matching one
# exactly is renamed with a trailing underscore.
KEYWORDS = set(
    "asm brk cnt def ext fin for fun fwd if nil or pub rec ret test uni use val var".split()
)

# C base type -> mach scalar type, per the README type table.
SCALAR = {
    "GLenum": "u32",
    "GLbitfield": "u32",
    "GLuint": "u32",
    "GLint": "i32",
    "GLsizei": "i32",
    "GLboolean": "u8",
    "GLubyte": "u8",
    "GLchar": "u8",
    "GLbyte": "i8",
    "GLshort": "i16",
    "GLushort": "u16",
    "GLhalf": "u16",
    "GLfloat": "f32",
    "GLclampf": "f32",
    "GLdouble": "f64",
    "GLclampd": "f64",
    "GLint64": "i64",
    "GLuint64": "u64",
    "GLintptr": "i64",
    "GLsizeiptr": "i64",
    "GLsync": "ptr",
}

# GLDEBUGPROC expands to its callback signature.
DEBUGPROC = "fun(u32, u32, u32, u32, i32, *u8, ptr)"


def text_minus_name(elem):
    # the C declaration of a <proto>/<param>, with the trailing <name> token removed.
    s = "".join(elem.itertext())
    nm = elem.find("name").text
    idx = s.rfind(nm)
    s = s[:idx] + s[idx + len(nm) :]
    return re.sub(r"\s+", " ", s).strip()


def base_and_depth(s):
    depth = s.count("*")
    m = re.search(r"(GL\w+|void)", s)
    return m.group(1), depth


def map_c(s):
    # C type string -> raw-layer mach type.
    s = s.strip()
    if s == "GLDEBUGPROC":
        return DEBUGPROC
    base, depth = base_and_depth(s)
    if depth == 0:
        if base == "void":
            return ""
        return SCALAR[base]
    if base == "void":
        return "ptr" if depth == 1 else "*" * (depth - 1) + "ptr"
    return "*" * depth + SCALAR[base]


def map_cmd(s, is_return):
    # C type string -> idiomatic-layer mach type: bool for GLboolean, str for a
    # single const GLchar* name argument, otherwise C-faithful.
    s = s.strip()
    if s == "GLboolean":
        return "bool"
    if not is_return and s == "const GLchar *":
        return "str"
    return map_c(s)


def ident(name):
    return name + "_" if name in KEYWORDS else name


def enum_name(c_name):
    # strip the GL_ prefix; prefix a leading underscore if the result starts with a digit.
    n = c_name[3:] if c_name.startswith("GL_") else c_name
    if n and n[0].isdigit():
        n = "_" + n
    return n


def snake(c_name):
    # strip the leading gl prefix, then insert an underscore before any uppercase
    # letter that follows a lowercase letter; digits never introduce a break.
    s = c_name[2:]
    out = []
    for i, ch in enumerate(s):
        if ch.isupper() and i > 0 and s[i - 1].islower():
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


def parse():
    root = ET.parse(XML).getroot()

    cmd_order = []
    cmd_set = set()
    enum_set = set()
    for feat in root.findall("feature"):
        if feat.get("api") != "gl":
            continue
        if float(feat.get("number")) > 4.6:
            continue
        for req in feat.findall("require"):
            if req.get("profile") not in (None, "core"):
                continue
            for c in req.findall("command"):
                n = c.get("name")
                if n not in cmd_set:
                    cmd_set.add(n)
                    cmd_order.append(n)
            for e in req.findall("enum"):
                enum_set.add(e.get("name"))
        for rem in feat.findall("remove"):
            if rem.get("profile") not in (None, "core"):
                continue
            for c in rem.findall("command"):
                if c.get("name") in cmd_set:
                    cmd_set.discard(c.get("name"))
                    cmd_order.remove(c.get("name"))
            for e in rem.findall("enum"):
                enum_set.discard(e.get("name"))

    commands = {}
    for c in root.iter("command"):
        proto = c.find("proto")
        if proto is None or proto.find("name") is None:
            continue
        commands[proto.find("name").text] = c

    cmds = []
    for cn in cmd_order:
        c = commands[cn]
        proto = c.find("proto")
        ret = text_minus_name(proto)
        params = []
        for p in c.findall("param"):
            params.append((ident(p.find("name").text), text_minus_name(p)))
        cmds.append((cn, ret, params))

    enum_defs = {}
    for enums in root.findall("enums"):
        for e in enums.findall("enum"):
            enum_defs.setdefault(e.get("name"), (e.get("value"), e.get("type")))
    enums = []
    for name in sorted(enum_set, key=enum_name):
        value, typ = enum_defs[name]
        width = "u64" if (typ == "ull" or int(value, 0) > 0xFFFFFFFF) else "u32"
        enums.append((enum_name(name), value, width))

    return cmds, enums


def fun_type(ret, params):
    args = ", ".join(map_c(t) for _, t in params)
    r = map_c(ret)
    return "fun({})".format(args) + (" " + r if r else "")


def gen_c(cmds):
    out = []
    out.append("# raw loaded-pointer layer for the OpenGL core profile (generated)")
    out.append("#")
    out.append("# one pub var function pointer per core command, with its C name and a")
    out.append("# C-faithful signature. every pointer is nil until load resolves it through a")
    out.append("# caller-supplied proc loader; a command the running context does not export")
    out.append("# stays nil, the same contract as in C.")
    out.append("#")
    out.append("# generated by tools/gen.py from the pinned tools/gl.xml. do not edit by hand.")
    out.append("")
    for cn, ret, params in cmds:
        out.append("# loaded pointer for the GL command {}".format(cn))
        out.append("pub var {}: {} = nil;".format(cn, fun_type(ret, params)))
    out.append("")
    out.append("# resolve every core command through loader")
    out.append("# ---")
    out.append("# loader: maps a command name to its address, or nil if unsupported")
    out.append("# ret:    the number of commands resolved; unresolved pointers stay nil")
    out.append("pub fun load(loader: fun(*u8) ptr) i64 {")
    out.append("    var n: i64 = 0;")
    for cn, ret, params in cmds:
        out.append('    {} = loader("{}"):~{};'.format(cn, cn, fun_type(ret, params)))
        out.append("    if ({} != nil) {{ n = n + 1; }}".format(cn))
    out.append("    ret n;")
    out.append("}")
    out.append("")
    out.append("fun load_nil(name: *u8) ptr {")
    out.append("    ret nil;")
    out.append("}")
    out.append("")
    out.append("var load_calls: i64 = 0;")
    out.append("")
    out.append("fun load_count(name: *u8) ptr {")
    out.append("    load_calls = load_calls + 1;")
    out.append("    ret (?load_calls)::ptr;")
    out.append("}")
    out.append("")
    out.append("# a nil loader resolves nothing and leaves the table nil.")
    out.append('test "c: load with a nil loader resolves nothing" {')
    out.append("    val n: i64 = load(load_nil);")
    out.append("    if (n != 0) { ret 1; }")
    out.append("    if (glClear != nil) { ret 1; }")
    out.append("    ret 0;")
    out.append("}")
    out.append("")
    out.append("# a stub loader that answers every name resolves the whole table; the count")
    out.append("# equals the number of loader calls, i.e. the generated command count.")
    out.append('test "c: load with a stub loader resolves every command" {')
    out.append("    load_calls = 0;")
    out.append("    val n: i64 = load(load_count);")
    out.append("    if (n != load_calls) { ret 1; }")
    out.append("    if (n <= 0) { ret 1; }")
    out.append("    ret 0;")
    out.append("}")
    return "\n".join(out) + "\n"


def gen_enums(enums):
    out = []
    out.append("# core OpenGL enumerant constants (generated)")
    out.append("#")
    out.append("# every core enum with the GL_ prefix stripped; u32 unless the registry tags")
    out.append("# the value as 64-bit. a name that would start with a digit gets a leading _.")
    out.append("#")
    out.append("# generated by tools/gen.py from the pinned tools/gl.xml. do not edit by hand.")
    out.append("")
    for name, value, width in enums:
        out.append("pub val {}: {} = {};".format(name, width, value))
    out.append("")
    out.append('test "enums: bitfield and primitive values match the registry" {')
    out.append("    if (COLOR_BUFFER_BIT != 0x4000) { ret 1; }")
    out.append("    if (DEPTH_BUFFER_BIT != 0x0100) { ret 1; }")
    out.append("    if (TRIANGLES != 4) { ret 1; }")
    out.append("    if (FALSE != 0) { ret 1; }")
    out.append("    if (TRUE != 1) { ret 1; }")
    out.append("    if (FLOAT != 0x1406) { ret 1; }")
    out.append("    ret 0;")
    out.append("}")
    out.append("")
    out.append('test "enums: 64-bit special value keeps its width" {')
    out.append("    if (TIMEOUT_IGNORED != 0xFFFFFFFFFFFFFFFF) { ret 1; }")
    out.append("    ret 0;")
    out.append("}")
    return "\n".join(out) + "\n"


def gen_cmd(cmds):
    out = []
    out.append("# idiomatic layer for the OpenGL core profile (generated)")
    out.append("#")
    out.append("# snake_case wrappers over gl.c, one per command. GLboolean becomes bool and a")
    out.append("# single const GLchar* name argument becomes str at the boundary; buffers, data")
    out.append("# pointers, and scalar out-params keep their C-faithful pointer types.")
    out.append("#")
    out.append("# generated by tools/gen.py from the pinned tools/gl.xml. do not edit by hand.")
    out.append("")
    out.append("use gl.c;")
    out.append("use gl.enums;")
    out.append("use std.types.bool.bool;")
    out.append("use std.types.string.str;")
    out.append("")
    for cn, ret, params in cmds:
        sig = ", ".join("{}: {}".format(n, map_cmd(t, False)) for n, t in params)
        rty = map_cmd(ret, True)
        decl = "pub fun {}({})".format(snake(cn), sig)
        decl += " {}".format(rty) if rty else ""
        args = ", ".join(n for n, _ in params)
        out.append("# wraps {}".format(cn))
        out.append(decl + " {")
        call = "c.{}({})".format(cn, args)
        if rty == "":
            out.append("    {};".format(call))
        elif map_cmd(ret, True) == "bool":
            out.append("    ret {} != 0;".format(call))
        else:
            out.append("    ret {};".format(call))
        out.append("}")
    out.append("")
    out.append("# query the GL major and minor version, valid after load")
    out.append("# ---")
    out.append("# major: receives the major version")
    out.append("# minor: receives the minor version")
    out.append("pub fun version(major: *i32, minor: *i32) {")
    out.append("    get_integerv(enums.MAJOR_VERSION, major);")
    out.append("    get_integerv(enums.MINOR_VERSION, minor);")
    out.append("}")
    out.append("")
    out.append("var abi_x: i32 = 0;")
    out.append("var abi_y: i32 = 0;")
    out.append("var abi_w: i32 = 0;")
    out.append("var abi_h: i32 = 0;")
    out.append("")
    out.append("fun abi_fake(x: i32, y: i32, width: i32, height: i32) {")
    out.append("    abi_x = x;")
    out.append("    abi_y = y;")
    out.append("    abi_w = width;")
    out.append("    abi_h = height;")
    out.append("}")
    out.append("")
    out.append("# pin the loaded-pointer call ABI: a Mach fake stands in for glScissor and the")
    out.append("# wrapper must deliver every argument through the table without a GL context.")
    out.append('test "cmd: loaded-pointer call ABI" {')
    out.append("    c.glScissor = abi_fake;")
    out.append("    scissor(11, 22, 33, 44);")
    out.append("    if (abi_x != 11) { ret 1; }")
    out.append("    if (abi_y != 22) { ret 1; }")
    out.append("    if (abi_w != 33) { ret 1; }")
    out.append("    if (abi_h != 44) { ret 1; }")
    out.append("    ret 0;")
    out.append("}")
    return "\n".join(out) + "\n"


def gen_gl(cmds, enums):
    out = []
    out.append("# the flat public surface of the OpenGL bindings (generated)")
    out.append("#")
    out.append("# re-exports every enum and wrapper under one namespace so a bare `use gl;`")
    out.append("# reaches the whole API as gl.load(), gl.clear(), gl.COLOR_BUFFER_BIT. the raw")
    out.append("# table stays reachable as gl.c.glClear for anyone who wants C names.")
    out.append("#")
    out.append("# generated by tools/gen.py from the pinned tools/gl.xml. do not edit by hand.")
    out.append("")
    out.append("use gl.enums;")
    out.append("use gl.cmd;")
    out.append("# links the startup entrypoint so `mach test` over this library produces a binary.")
    out.append("use std.runtime;")
    out.append("")
    out.append("fwd gl.c;")
    out.append("fwd gl.c.load;")
    for name, _, _ in enums:
        out.append("fwd enums.{};".format(name))
    for name in sorted(snake(cn) for cn, _, _ in cmds):
        out.append("fwd cmd.{};".format(name))
    out.append("fwd cmd.version;")
    return "\n".join(out) + "\n"


def render(cmds, enums):
    return {
        "c.mach": gen_c(cmds),
        "enums.mach": gen_enums(enums),
        "cmd.mach": gen_cmd(cmds),
        "gl.mach": gen_gl(cmds, enums),
    }


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "gen"
    cmds, enums = parse()
    files = render(cmds, enums)

    if mode == "check":
        drift = False
        for name, content in files.items():
            path = os.path.join(SRC, name)
            current = ""
            if os.path.exists(path):
                with open(path) as f:
                    current = f.read()
            if current != content:
                drift = True
                diff = difflib.unified_diff(
                    current.splitlines(keepends=True),
                    content.splitlines(keepends=True),
                    fromfile="src/" + name,
                    tofile="src/" + name + " (regenerated)",
                )
                sys.stderr.writelines(diff)
        if drift:
            sys.stderr.write("\nsrc is out of date; run tools/gen.py\n")
            sys.exit(1)
        return

    if mode != "gen":
        sys.stderr.write("usage: tools/gen.py [gen|check]\n")
        sys.exit(2)

    os.makedirs(SRC, exist_ok=True)
    for name, content in files.items():
        with open(os.path.join(SRC, name), "w") as f:
            f.write(content)
    sys.stderr.write(
        "generated {} commands, {} enums\n".format(len(cmds), len(enums))
    )


if __name__ == "__main__":
    main()
