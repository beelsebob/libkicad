"""libkicad's bridge to an installed ("stock") KiCad.

libkicad's stock backend (stock/libkicad_stock.cpp) runs this script with KiCad's own Python, so
board data comes from the user's KiCad install instead of a custom KiCad build:

    python kicad_bridge.py QUERY --board B --project P --output OUT.json [--layer NAME] [--kicad-cli EXE]

QUERY is one of:
  board       everything except whole-board geometry: layers, stackup, nets, net classes, colours,
              footprints with their pads, tracks, vias and zone outlines
  geometry    BoardGeometry: outline, copper per layer, solder-mask openings, silkscreen
  layer       BoardLayerGeometry for --layer
  simmodels   every schematic symbol's SPICE model, from kicad-cli's SPICE and XML netlists

The result is written to OUT.json as {"ok": true, "protocol": N, "result": ...} or
{"ok": false, "error": "..."}. This JSON is the contract with libkicad_stock.cpp: it mirrors what
the source backend (libkicad.cpp) computes from KiCad's C++ objects, and the KiCad calls below are
the same ones libkicad.cpp makes, through KiCad's pcbnew Python module. Positions are millimetres
relative to the board's auxiliary origin with Y pointing up, exactly as libkicad.cpp returns them.

KiCad's SWIG Python bindings are deprecated in favour of its IPC API. Only this script talks to
KiCad, so moving to the IPC API (once a KiCad release ships the headless `kicad-cli api-server`)
means reimplementing these queries here, against the same JSON contract.

Where KiCad 10's bindings expose an object only opaquely (net classes, colour themes, via padstack
overrides, the board stackup) the data is read from the project and board files instead, following
KiCad's own rules, as commented at each place.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import traceback
import xml.etree.ElementTree as ET

PROTOCOL = 1
INT_MAX = 2**31 - 1


class BridgeError(Exception):
    """An expected failure, reported to libkicad as its error message."""


# ---------------------------------------------------------------------------------------------
# Shared helpers

def kiround(value: float) -> int:
    """KiCad's KiROUND: round half away from zero."""
    return int(math.copysign(math.floor(abs(value) + 0.5), value))


def color_hex(r: float, g: float, b: float, a: float) -> str:
    """COLOR4D::ToHexString() for a colour given as 0-1 channels: always #RRGGBBAA."""
    return "#%02X%02X%02X%02X" % tuple(kiround(c * 255.0) for c in (r, g, b, a))


def parse_css_color(text: str | None):
    """A project-file colour ("rgb(r, g, b)" or "rgba(r, g, b, a)") as 0-1 channels, or None for
    KiCad's UNSPECIFIED colour (all zero) or anything unparseable."""
    if not text:
        return None
    match = re.fullmatch(r"\s*rgba?\(([^)]*)\)\s*", text)
    if not match:
        return None
    parts = [p.strip() for p in match.group(1).split(",")]
    try:
        r, g, b = (float(p) / 255.0 for p in parts[:3])
        a = float(parts[3]) if len(parts) > 3 else 1.0
    except (ValueError, IndexError):
        return None
    if r == 0 and g == 0 and b == 0 and a == 0:
        return None
    return (r, g, b, a)


def sexpr_block(text: str, start: int) -> str:
    """The balanced s-expression beginning at text[start] == '('."""
    depth = 0
    i = start
    in_string = False
    while i < len(text):
        c = text[i]
        if in_string:
            if c == "\\":
                i += 1
            elif c == '"':
                in_string = False
        elif c == '"':
            in_string = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
        i += 1
    return text[start:]


def sexpr_parse(text: str):
    """Parses one s-expression into nested lists of strings."""
    tokens = re.findall(r'\(|\)|"(?:\\.|[^"\\])*"|[^\s()"]+', text)
    stack: list[list] = [[]]
    for token in tokens:
        if token == "(":
            stack.append([])
        elif token == ")":
            done = stack.pop()
            stack[-1].append(done)
        elif token.startswith('"'):
            stack[-1].append(re.sub(r"\\(.)", r"\1", token[1:-1]))
        else:
            stack[-1].append(token)
    return stack[0][0] if stack[0] else []


def sexpr_find(node, key):
    """The first child list of `node` whose head is `key`."""
    for child in node[1:] if isinstance(node, list) else []:
        if isinstance(child, list) and child and child[0] == key:
            return child
    return None


def read_text(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


# ---------------------------------------------------------------------------------------------
# Net classes and net colours, from the project file (KiCad 10's bindings expose NETCLASS objects
# only opaquely). Mirrors NET_SETTINGS::GetEffectiveNetClass() and makeEffectiveNetclass().

# The parameters whose absence makes KiCad add Default to a net's constituent classes
# (NET_SETTINGS::addMissingDefaults()).
_REQUIRED_NETCLASS_PARAMETERS = ("clearance", "track_width", "via_diameter", "via_drill",
                                 "microvia_diameter", "microvia_drill", "diff_pair_width",
                                 "diff_pair_gap", "wire_width", "bus_width")


class NetSettings:
    def __init__(self, project_path: str):
        self.classes: dict[str, dict] = {}
        self.assignments: dict[str, list[str]] = {}
        self.patterns: list[tuple[list, str]] = []
        self.net_colors: dict[str, str] = {}
        self.has_project = os.path.isfile(project_path)
        if not self.has_project:
            return
        try:
            project = json.loads(read_text(project_path))
        except ValueError as error:
            raise BridgeError(f'Could not load the KiCad project file "{project_path}": {error}')
        settings = project.get("net_settings") or {}
        for entry in settings.get("classes") or []:
            if isinstance(entry, dict) and "name" in entry:
                self.classes[entry["name"]] = entry
        for net, names in (settings.get("netclass_assignments") or {}).items():
            if isinstance(names, str):
                names = [names]
            self.assignments[net] = list(names or [])
        for entry in settings.get("netclass_patterns") or []:
            pattern, netclass = entry.get("pattern"), entry.get("netclass")
            if pattern and netclass:
                self.patterns.append((self._matchers(pattern), netclass))
        self.net_colors = dict(settings.get("net_colors") or {})

    @staticmethod
    def _matchers(pattern: str) -> list:
        """EDA_COMBINED_MATCHER for CTX_NETCLASS: an anchored regex and an anchored wildcard."""
        matchers = []
        regex = pattern if pattern.startswith("^") else "^" + pattern
        regex = regex if regex.endswith("$") else regex + "$"
        try:
            matchers.append(re.compile(regex))
        except re.error:
            pass
        wildcard = "".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in pattern)
        matchers.append(re.compile("^" + wildcard + "$"))
        return matchers

    def _priority(self, name: str) -> int:
        if name == "Default":
            return int((self.classes.get("Default") or {}).get("priority", INT_MAX))
        if name in self.classes:
            return int(self.classes[name].get("priority", 0))
        return INT_MAX - 1  # An implicit class: assigned by a label but never configured.

    def _sorted(self, names) -> list[str]:
        return sorted(names, key=lambda n: (self._priority(n), n))

    def _resolved(self, net: str) -> list[str]:
        resolved = set(self.assignments.get(net) or [])
        for matchers, netclass in self.patterns:
            if any(m.match(net) for m in matchers):
                resolved.add(netclass)
        return self._sorted(resolved)

    def constituents(self, net: str) -> list[str]:
        """The names NETCLASS::GetConstituentNetclasses() reports for the net's effective class,
        in priority order."""
        if not net:
            return ["Default"]
        resolved = self._resolved(net)
        if not resolved:
            return ["Default"]
        present = set()
        for name in resolved:
            entry = self.classes.get("Default") if name == "Default" else self.classes.get(name)
            for key in _REQUIRED_NETCLASS_PARAMETERS:
                if entry is not None and entry.get(key) is not None:
                    present.add(key)
        if not all(key in present for key in _REQUIRED_NETCLASS_PARAMETERS) and "Default" not in resolved:
            resolved.append("Default")
        return resolved

    def class_color(self, net: str):
        """The effective class's PCB colour: the highest-priority resolved class that sets one."""
        resolved = self._resolved(net) if net else []
        for name in resolved or ["Default"]:
            color = parse_css_color((self.classes.get(name) or {}).get("pcb_color"))
            if color is not None:
                return color
        return None

    def user_class_names(self) -> list[str]:
        return sorted(name for name in self.classes if name != "Default")


# ---------------------------------------------------------------------------------------------
# KiCad's built-in default colour theme for board layers (common/settings/builtin_color_themes.h,
# s_defaultTheme), keyed by pcbnew layer constant name. KiCad 10's bindings expose COLOR_SETTINGS
# only opaquely; libkicad.cpp reads the active theme, which is this one unless the user changed it.

_DEFAULT_THEME = {
    "F_Cu": (200, 52, 52, 1), "In1_Cu": (127, 200, 127, 1), "In2_Cu": (206, 125, 44, 1),
    "In3_Cu": (79, 203, 203, 1), "In4_Cu": (219, 98, 139, 1), "In5_Cu": (167, 165, 198, 1),
    "In6_Cu": (40, 204, 217, 1), "In7_Cu": (232, 178, 167, 1), "In8_Cu": (242, 237, 161, 1),
    "In9_Cu": (141, 203, 129, 1), "In10_Cu": (237, 124, 51, 1), "In11_Cu": (91, 195, 235, 1),
    "In12_Cu": (247, 111, 142, 1), "In13_Cu": (167, 165, 198, 1), "In14_Cu": (40, 204, 217, 1),
    "In15_Cu": (232, 178, 167, 1), "In16_Cu": (242, 237, 161, 1), "In17_Cu": (237, 124, 51, 1),
    "In18_Cu": (91, 195, 235, 1), "In19_Cu": (247, 111, 142, 1), "In20_Cu": (167, 165, 198, 1),
    "In21_Cu": (40, 204, 217, 1), "In22_Cu": (232, 178, 167, 1), "In23_Cu": (242, 237, 161, 1),
    "In24_Cu": (237, 124, 51, 1), "In25_Cu": (91, 195, 235, 1), "In26_Cu": (247, 111, 142, 1),
    "In27_Cu": (167, 165, 198, 1), "In28_Cu": (40, 204, 217, 1), "In29_Cu": (232, 178, 167, 1),
    "In30_Cu": (242, 237, 161, 1), "B_Cu": (77, 127, 196, 1),
    "B_Adhes": (0, 0, 132, 1), "F_Adhes": (132, 0, 132, 1), "B_Paste": (0, 194, 194, 0.9),
    "F_Paste": (180, 160, 154, 0.9), "B_SilkS": (232, 178, 167, 1), "F_SilkS": (242, 237, 161, 1),
    "B_Mask": (2, 255, 238, 0.4), "F_Mask": (216, 100, 255, 0.4), "Dwgs_User": (194, 194, 194, 1),
    "Cmts_User": (89, 148, 220, 1), "Eco1_User": (180, 219, 210, 1), "Eco2_User": (216, 200, 82, 1),
    "Edge_Cuts": (208, 210, 205, 1), "Margin": (255, 38, 226, 1), "B_CrtYd": (38, 233, 255, 1),
    "F_CrtYd": (255, 38, 226, 1), "B_Fab": (88, 93, 132, 1), "F_Fab": (175, 175, 175, 1),
}
_USER_LAYER_CYCLE = [(194, 194, 194, 1), (89, 148, 220, 1), (180, 219, 210, 1), (216, 200, 82, 1)]
for _n in range(1, 46):
    _DEFAULT_THEME[f"User_{_n}"] = (232, 178, 167, 1) if _n == 9 else _USER_LAYER_CYCLE[(_n - 1) % 4]


# ---------------------------------------------------------------------------------------------
# Board queries

class Board:
    def __init__(self, board_path: str, project_path: str):
        import pcbnew  # Imported here: the simmodels query doesn't need it.
        self.pcbnew = pcbnew
        self.path = board_path
        self.project_path = project_path
        try:
            self.board = pcbnew.LoadBoard(board_path)
        except Exception as error:  # noqa: BLE001 - KiCad reports parse errors as exceptions
            raise BridgeError(f'Could not load the KiCad board file "{board_path}": {error}')
        if self.board is None:
            raise BridgeError("LoadBoard failed")
        self.ds = self.board.GetDesignSettings()
        aux = self.ds.GetAuxOrigin()
        self.ax, self.ay = aux.x, aux.y
        self.max_error = self.ds.m_MaxError
        scale = getattr(pcbnew, "pcbIUScale", None)
        self.iu_per_mm = float(getattr(scale, "IU_PER_MM", 0) or pcbnew.FromMM(1))
        self._text = None

    # -- units and shapes --

    def mm(self, iu) -> float:
        return iu / self.iu_per_mm

    def point(self, x, y):
        # Integer aux-origin offset first, then mm, then the Y flip (see libkicad.cpp's resolvePinRaw).
        return self.mm(x - self.ax), -self.mm(y - self.ay)

    def chain(self, chain) -> list[float]:
        flat: list[float] = []
        for i in range(chain.PointCount()):
            p = chain.CPoint(i)
            flat.extend(self.point(p.x, p.y))
        return flat

    def loops(self, polyset) -> list:
        """_forEachPolygonLoop: [hole, [x0, y0, x1, y1, ...]] for every contour of 3+ points."""
        out = []
        for oi in range(polyset.OutlineCount()):
            outline = polyset.COutline(oi)
            if outline.PointCount() >= 3:
                out.append([0, self.chain(outline)])
            for hi in range(polyset.HoleCount(oi)):
                hole = polyset.CHole(oi, hi)
                if hole.PointCount() >= 3:
                    out.append([1, self.chain(hole)])
        return out

    def layer_name(self, layer) -> str:
        return self.board.GetLayerName(layer)

    def text(self) -> str:
        if self._text is None:
            self._text = read_text(self.path)
        return self._text

    def pad_copper_layer(self, pad):
        copper = [layer for layer in pad.GetLayerSet().Seq() if self.pcbnew.IsCopperLayer(layer)]
        return copper[0] if copper else pad.GetLayer()

    def new_polyset(self):
        return self.pcbnew.SHAPE_POLY_SET()

    # -- the "board" query --

    def summary(self) -> dict:
        pcbnew = self.pcbnew
        board = self.board
        nets = NetSettings(self.project_path)

        net_names = []
        by_code = board.GetNetInfo().NetsByNetcode()
        for code in sorted(by_code.keys()):
            if code != 0:  # NETINFO_LIST::UNCONNECTED
                net_names.append(by_code[code].GetNetname())

        net_colors = []
        for name in net_names:
            color = parse_css_color(nets.net_colors.get(name))
            if color is None:
                color = nets.class_color(name)
            if color is not None:
                net_colors.append([name, color_hex(*color)])

        layers = []
        layer_colors = []
        theme = {getattr(pcbnew, key): value for key, value in _DEFAULT_THEME.items() if hasattr(pcbnew, key)}
        for layer in board.GetEnabledLayers().UIOrder():
            name = self.layer_name(layer)
            layers.append([name, bool(pcbnew.IsCopperLayer(layer)), layer in (pcbnew.F_Mask, pcbnew.B_Mask)])
            if layer in theme:
                r, g, b, a = theme[layer]
                layer_colors.append([name, color_hex(r / 255.0, g / 255.0, b / 255.0, a)])

        footprints = []
        pad_count = 0
        for footprint in board.GetFootprints():
            pads = []
            for pad in footprint.Pads():
                x, y = self.point(pad.GetPosition().x, pad.GetPosition().y)
                pads.append({
                    "num": pad.GetNumber(), "fn": pad.GetPinFunction(), "net": pad.GetNetname(),
                    "type": pad.GetPinType(), "x": x, "y": y,
                    "rot": pad.GetOrientation().AsDegrees(),
                    "layer": self.layer_name(self.pad_copper_layer(pad)),
                    "w": self.mm(pad.GetSizeX()), "h": self.mm(pad.GetSizeY()),
                    "attr": {pcbnew.PAD_ATTRIB_PTH: "pth", pcbnew.PAD_ATTRIB_NPTH: "npth"}.get(pad.GetAttribute(), "smd"),
                    "dw": self.mm(pad.GetDrillSizeX()), "dh": self.mm(pad.GetDrillSizeY()),
                })
            pad_count += len(pads)
            footprints.append({"ref": footprint.GetReference(), "value": footprint.GetValue(), "pads": pads})

        tracks, vias = self._tracks_and_vias()

        zones = []
        for zone in board.Zones():
            outline = zone.Outline()
            if outline is None:
                continue
            for layer in zone.GetLayerSet().CuStack():
                for oi in range(outline.OutlineCount()):
                    contour = outline.COutline(oi)
                    if contour.PointCount() >= 3:
                        zones.append({"net": zone.GetNetname(), "layer": self.layer_name(layer),
                                      "outline": self.chain(contour)})

        return {
            "counts": {"footprints": len(footprints), "tracks": len(list(board.GetTracks())),
                       "zones": len(list(board.Zones())), "pads": pad_count},
            "layers": layers,
            "layerColors": layer_colors,
            "stackup": self._stackup(),
            "nets": net_names,
            "hasProject": nets.has_project,
            "netClasses": nets.user_class_names(),
            "netConstituents": {name: nets.constituents(name) for name in net_names},
            "netColors": net_colors,
            "footprints": footprints,
            "tracks": tracks,
            "vias": vias,
            "zones": zones,
        }

    def _tracks_and_vias(self):
        pcbnew = self.pcbnew
        tracks = []
        vias = []
        overrides = self._via_overrides()
        rules = (self.ds.m_TentViasFront, self.ds.m_TentViasBack, self.ds.m_CoverViasFront, self.ds.m_CoverViasBack,
                 self.ds.m_PlugViasFront, self.ds.m_PlugViasBack, self.ds.m_FillVias, self.ds.m_CapVias)

        def segment(track, start, end, arc):
            x0, y0 = self.point(*start)
            x1, y1 = self.point(*end)
            tracks.append({"net": track.GetNetname(), "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                           "w": self.mm(track.GetWidth()), "layer": self.layer_name(track.GetLayer()),
                           "uuid": track.m_Uuid.AsString(), "arc": arc})

        for track in self.board.GetTracks():
            kind = track.GetClass()
            if kind == "PCB_VIA":
                x, y = self.point(track.GetPosition().x, track.GetPosition().y)
                flags = list(rules)
                for index, value in enumerate(overrides.get(track.m_Uuid.AsString(), [None] * 8)):
                    if value is not None:
                        flags[index] = value
                vias.append({"x": x, "y": y, "net": track.GetNetname(),
                             "w": self.mm(track.GetWidth(pcbnew.PADSTACK.ALL_LAYERS)),
                             "drill": self.mm(track.GetDrillValue()), "prot": [bool(f) for f in flags]})
            elif kind == "PCB_TRACK":
                segment(track, (track.GetStart().x, track.GetStart().y), (track.GetEnd().x, track.GetEnd().y), False)
            elif kind == "PCB_ARC":
                # Flattened exactly as libkicad.cpp's allTracksRaw() does: 5-degree pieces, exact
                # endpoints, interior points rounded (llround) to KiCad integer units.
                center = track.GetCenter()
                start = (track.GetStart().x, track.GetStart().y)
                end = (track.GetEnd().x, track.GetEnd().y)
                sweep = track.GetAngle().AsRadians()
                radius = track.GetRadius()
                if not math.isfinite(sweep) or not math.isfinite(radius) or radius <= 0:
                    segment(track, start, end, True)
                    continue
                pieces = max(1, int(math.ceil(abs(sweep) / (math.pi / 36.0))))
                start_angle = math.atan2(float(start[1] - center.y), float(start[0] - center.x))
                previous = start
                for piece in range(1, pieces + 1):
                    if piece == pieces:
                        current = end
                    else:
                        angle = start_angle + sweep * float(piece) / pieces
                        current = (kiround(center.x + radius * math.cos(angle)),
                                   kiround(center.y + radius * math.sin(angle)))
                    segment(track, previous, current, True)
                    previous = current
        return tracks, vias

    def _via_overrides(self) -> dict:
        """Per-via IPC-4761 overrides from the board file -- KiCad 10's bindings expose padstack
        optionals only opaquely. Order: tentedFront, tentedBack, coveredFront, coveredBack,
        pluggedFront, pluggedBack, filled, capped; None means "from rules"."""
        text = self.text()
        result = {}
        for match in re.finditer(r"\(via[\s)]", text):
            node = sexpr_parse(sexpr_block(text, match.start()))
            uuid = sexpr_find(node, "uuid")
            if not uuid or len(uuid) < 2:
                continue

            def value(entry):
                if entry is None or len(entry) < 2:
                    return None
                return {"yes": True, "no": False}.get(entry[1])

            def sides(key):
                entry = sexpr_find(node, key)
                if entry is None:
                    return [None, None]
                if len(entry) > 1 and not isinstance(entry[1], list):
                    # Older form: (tenting front back) names the tented sides.
                    words = entry[1:]
                    return ["front" in words, "back" in words]
                return [value(sexpr_find(entry, "front")), value(sexpr_find(entry, "back"))]

            flags = sides("tenting") + sides("covering") + sides("plugging")
            flags += [value(sexpr_find(node, "filling")), value(sexpr_find(node, "capping"))]
            result[uuid[1]] = flags
        return result

    def _stackup(self) -> list:
        """libkicad.cpp's _stackupLayers(), from the board file's (setup (stackup ...)) -- KiCad 10's
        bindings expose BOARD_STACKUP only opaquely -- or BOARD_STACKUP::BuildDefaultStackupList()
        when the board has none. Rows: [kind, name, thicknessMm, epsilonR, lossTangent]."""
        pcbnew = self.pcbnew
        text = self.text()
        match = re.search(r"\(stackup[\s)]", text)
        if match is None:
            return self._default_stackup()
        stackup = sexpr_parse(sexpr_block(text, match.start()))
        rows = []
        for entry in stackup[1:]:
            if not isinstance(entry, list) or not entry or entry[0] != "layer" or len(entry) < 2:
                continue
            name = entry[1]
            # Only the first sublayer counts, as BOARD_STACKUP_ITEM::GetThickness() etc. do.
            fields = []
            for child in entry[2:]:
                if isinstance(child, list) and child and child[0] == "addsublayer":
                    break
                fields.append(child)
            node = ["layer", name] + fields
            kind = (sexpr_find(node, "type") or [None, ""])[1]

            def number(key, default):
                found = sexpr_find(node, key)
                try:
                    return float(found[1]) if found and len(found) > 1 else default
                except ValueError:
                    return default

            lowered = kind.lower()
            if name.lower().startswith("dielectric"):
                index = re.sub(r"\D", "", name) or "0"
                rows.append(["core" if lowered == "core" else "prepreg", f"Dielectric {int(index)}",
                             number("thickness", 0.0), number("epsilon_r", 4.5), number("loss_tangent", 0.02)])
            elif lowered == "copper":
                layer = self.board.GetLayerID(name)
                rows.append(["copper", self.layer_name(layer) if layer >= 0 else name, number("thickness", 0.035), 0.0, 0.0])
            elif "solder mask" in lowered:
                layer = self.board.GetLayerID(name)
                top = layer == pcbnew.F_Mask if layer >= 0 else name.startswith("F")
                rows.append(["maskTop" if top else "maskBottom", self.layer_name(layer) if layer >= 0 else name,
                             number("thickness", 0.01), number("epsilon_r", 3.3), number("loss_tangent", 0.0)])
        return rows

    def _default_stackup(self) -> list:
        pcbnew = self.pcbnew
        enabled = self.board.GetEnabledLayers()
        copper = list(enabled.CuStack())
        masks = [layer for layer in (pcbnew.F_Mask, pcbnew.B_Mask) if enabled.Contains(layer)]
        copper_iu = round(0.035 * self.iu_per_mm)
        mask_iu = round(0.01 * self.iu_per_mm)
        dielectric_iu = self.ds.GetBoardThickness() - copper_iu * len(copper) - mask_iu * len(masks)
        dielectric_iu = int(dielectric_iu / max(1, len(copper) - 1))  # C++ integer division
        rows = []
        if pcbnew.F_Mask in masks:
            rows.append(["maskTop", self.layer_name(pcbnew.F_Mask), self.mm(mask_iu), 3.3, 0.0])
        for index, layer in enumerate(copper):
            rows.append(["copper", self.layer_name(layer), self.mm(copper_iu), 0.0, 0.0])
            if layer == pcbnew.B_Cu:
                break
            rows.append(["core" if index % 2 == 0 else "prepreg", f"Dielectric {index + 1}",
                         self.mm(dielectric_iu), 4.5, 0.02])
        if pcbnew.B_Mask in masks:
            rows.append(["maskBottom", self.layer_name(pcbnew.B_Mask), self.mm(mask_iu), 3.3, 0.0])
        return rows

    # -- geometry --

    def board_outline(self, required: bool):
        outline = self.new_polyset()
        if self.board.GetBoardPolygonOutlines(outline, False, None, False, False):
            return outline
        # libkicad.cpp retries with the board's own Edge.Cuts drawings, ignoring open footprint
        # graphics; KiCad 10's bindings don't expose ConvertOutlineToPolygon, so use it when present.
        convert = getattr(self.pcbnew, "ConvertOutlineToPolygon", None)
        shapes = [d.Cast() for d in self.board.GetDrawings()
                  if d.GetClass() == "PCB_SHAPE" and d.GetLayer() == self.pcbnew.Edge_Cuts]
        if convert is not None and shapes:
            outline = self.new_polyset()
            try:
                if convert(shapes, outline, self.max_error, self.board.GetOutlinesChainingEpsilon(), True, None, False):
                    return outline
            except Exception:  # noqa: BLE001
                pass
        if required:
            raise BridgeError("Board Edge.Cuts do not form valid closed polygons")
        return None

    def copper_on_layer(self, layer, out: list) -> None:
        """Tracks, pads, footprint zones and board zones on one copper layer, as libkicad.cpp."""
        name = self.layer_name(layer)

        def append(polyset, net, zone, ref="", pad=""):
            for hole, points in self.loops(polyset):
                out.append([net, name, zone, ref, pad, hole, points])

        for track in self.board.GetTracks():
            if not track.IsOnLayer(layer):
                continue
            polyset = self.new_polyset()
            track.TransformShapeToPolygon(polyset, layer, 0, self.max_error, self.pcbnew.ERROR_INSIDE)
            append(polyset, track.GetNetname(), 0)
        for footprint in self.board.GetFootprints():
            for pad in footprint.Pads():
                if not pad.FlashLayer(layer):
                    continue
                polyset = self.new_polyset()
                pad.TransformShapeToPolygon(polyset, layer, 0, self.max_error, self.pcbnew.ERROR_INSIDE)
                append(polyset, pad.GetNetname(), 0, footprint.GetReference(), pad.GetNumber())
            for zone in footprint.Zones():
                if not zone.GetLayerSet().Contains(layer):
                    continue
                polyset = self.new_polyset()
                zone.TransformSolidAreasShapesToPolygon(layer, polyset)
                append(polyset, zone.GetNetname(), 0 if zone.IsTeardropArea() else 1)
        for zone in self.board.Zones():
            if not zone.GetLayerSet().Contains(layer):
                continue
            polyset = self.new_polyset()
            zone.TransformSolidAreasShapesToPolygon(layer, polyset)
            append(polyset, zone.GetNetname(), 0 if zone.IsTeardropArea() else 1)

    def silkscreen(self, layer) -> list:
        """[footprintRef, hole, points] per contour; board artwork has an empty reference."""
        pcbnew = self.pcbnew
        out = []
        for footprint in self.board.GetFootprints():
            polyset = self.new_polyset()
            footprint.TransformFPShapesToPolySet(polyset, layer, 0, self.max_error, pcbnew.ERROR_INSIDE,
                                                 True, True, False)
            for hole, points in self.loops(polyset):
                out.append([footprint.GetReference(), hole, points])
        polyset = self.new_polyset()
        for drawing in self.board.GetDrawings():
            if not drawing.IsOnLayer(layer):
                continue
            item = drawing.Cast()
            kind = drawing.GetClass()
            args = (polyset, 0, self.max_error, pcbnew.ERROR_INSIDE)
            try:
                if kind in ("PCB_SHAPE", "PCB_BARCODE"):
                    item.TransformShapeToPolygon(polyset, layer, 0, self.max_error, pcbnew.ERROR_INSIDE)
                elif kind in ("PCB_TEXT", "PCB_FIELD"):
                    item.TransformTextToPolySet(*args)
                elif kind == "PCB_TEXTBOX":
                    pcbnew.PCB_SHAPE.TransformShapeToPolygon(item, polyset, layer, 0, self.max_error, pcbnew.ERROR_INSIDE)
                    item.TransformTextToPolySet(*args)
                elif kind == "PCB_TABLE":
                    item.TransformGraphicItemsToPolySet(polyset, self.max_error, pcbnew.ERROR_INSIDE, None)
                elif kind.startswith("PCB_DIM_"):
                    item.TransformShapeToPolygon(polyset, layer, 0, self.max_error, pcbnew.ERROR_INSIDE)
                    item.TransformTextToPolySet(*args)
            except (AttributeError, TypeError):
                continue  # An item type these bindings can't convert; libkicad.cpp would include it.
        for hole, points in self.loops(polyset):
            out.append(["", hole, points])
        return out

    def layer_contours(self, layer) -> list:
        polyset = self.new_polyset()
        self.board.ConvertBrdLayerToPolygonalContours(layer, polyset)
        return self.loops(polyset)

    def geometry(self) -> dict:
        pcbnew = self.pcbnew
        outline = self.loops(self.board_outline(required=True))
        if not any(hole == 0 for hole, _ in outline):
            raise BridgeError("Board has no closed Edge.Cuts outline")
        copper: list = []
        for layer in self.board.GetEnabledLayers().CuStack():
            self.copper_on_layer(layer, copper)
        return {
            "outline": outline,
            "copper": copper,
            "frontMask": self.layer_contours(pcbnew.F_Mask),
            "backMask": self.layer_contours(pcbnew.B_Mask),
            "frontSilk": self.silkscreen(pcbnew.F_SilkS),
            "backSilk": self.silkscreen(pcbnew.B_SilkS),
        }

    def layer_geometry(self, layer_name: str) -> dict:
        pcbnew = self.pcbnew
        layer = next((candidate for candidate in self.board.GetEnabledLayers().Seq()
                      if self.layer_name(candidate) == layer_name), None)
        if layer is None:
            raise BridgeError("Board layer is not enabled: " + layer_name)
        copper_layer = bool(pcbnew.IsCopperLayer(layer))
        outline = self.board_outline(required=False)
        result = {"layer": [layer_name, copper_layer, layer in (pcbnew.F_Mask, pcbnew.B_Mask)],
                  "boardOutline": self.loops(outline) if outline is not None else [],
                  "copper": [], "contours": []}
        if copper_layer:
            self.copper_on_layer(layer, result["copper"])
        else:
            result["contours"] = self.layer_contours(layer)
        return result


# ---------------------------------------------------------------------------------------------
# Simulation models, from kicad-cli's netlists. libkicad.cpp resolves them with eeschema's
# SIM_LIB_MGR, which has no Python bindings; kicad-cli's SPICE netlist is that same resolution
# written out. libkicad_stock.cpp then expands subcircuits and evaluates values with the same C++
# code libkicad.cpp uses (spice_subcircuit.cpp, spice_expression.cpp).

def spice_net_name(name: str) -> str:
    """NETLIST_EXPORTER_SPICE::ConvertToSpiceMarkup(), for plain (markup-free) net names."""
    for c in "%(),[]<>~ ":
        name = name.replace(c, "_")
    if name.endswith("/0") and not name.endswith("//0"):
        name = "0"
    if name.lower() == "/gnd":
        name = name[1:]
    if name.startswith("//"):
        name = name.replace("//", "/root/")
    return name


def spice_lines(text: str) -> list[str]:
    """Netlist cards with '+' continuations joined and comments dropped."""
    cards: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("*"):
            continue
        if line.startswith("+") and cards:
            cards[-1] += " " + line[1:].strip()
        else:
            cards.append(line)
    return cards


def subcircuits_in(paths: list[str]) -> dict[str, str]:
    """Every top-level .subckt ... .ends block in `paths` and the files they .include, by lower-case
    name -- the scope a library subcircuit's nested X instances resolve in."""
    found: dict[str, str] = {}
    seen: set[str] = set()
    pending = list(paths)
    while pending:
        path = os.path.normpath(pending.pop(0))
        if path in seen or not os.path.isfile(path):
            continue
        seen.add(path)
        lines = read_text(path).splitlines()
        depth = 0
        block: list[str] = []
        name = ""
        for line in lines:
            words = line.strip().split()
            head = words[0].lower() if words else ""
            if depth == 0 and head in (".include", ".lib", ".inc") and len(words) > 1:
                target = words[1].strip("\"'")
                pending.append(target if os.path.isabs(target) else os.path.join(os.path.dirname(path), target))
                continue
            if head == ".subckt":
                if depth == 0:
                    block = []
                    name = words[1].lower() if len(words) > 1 else ""
                depth += 1
            if depth > 0:
                block.append(line)
            if head == ".ends" and depth > 0:
                depth -= 1
                if depth == 0 and name and name not in found:
                    found[name] = "\n".join(block)
    return found


_TERMINALS = {"R": 2, "C": 2, "L": 2, "D": 2, "V": 2, "I": 2, "B": 2, "F": 2, "H": 2,
              "J": 3, "Q": 3, "Z": 3, "M": 4, "E": 4, "G": 4, "S": 4, "T": 4, "W": 2}


def excluded_references(schematic_paths: list[str]) -> set[str]:
    """References of symbols marked "Exclude from simulation" -- kicad-cli's XML netlist doesn't
    report that flag for the base design."""
    refs: set[str] = set()
    for path in schematic_paths:
        if not os.path.isfile(path):
            continue
        text = read_text(path)
        for match in re.finditer(r"\(symbol\s*\(lib_id", text):
            block = sexpr_block(text, match.start())
            if not re.search(r"\(exclude_from_sim\s+yes\)", block):
                continue
            refs.update(re.findall(r'\(reference\s+"([^"]+)"\)', block))
            refs.update(re.findall(r'\(property\s+"Reference"\s+"([^"]+)"', block))
    return refs


def run_kicad_cli(kicad_cli: str, args: list[str]) -> None:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    completed = subprocess.run([kicad_cli, *args], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                               creationflags=flags)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        raise BridgeError("kicad-cli " + " ".join(args[:3]) + " failed" + (": " + detail[-1] if detail else ""))


def sim_models(project_path: str, kicad_cli: str) -> dict:
    root = os.path.splitext(project_path)[0] + ".kicad_sch"
    if not os.path.isfile(root):
        raise BridgeError(f'The KiCad schematic file does not exist: "{root}"')
    with tempfile.TemporaryDirectory(prefix="libkicad-") as work:
        spice_path = os.path.join(work, "netlist.cir")
        xml_path = os.path.join(work, "netlist.xml")
        run_kicad_cli(kicad_cli, ["sch", "export", "netlist", "--format", "spice", "-o", spice_path, root])
        run_kicad_cli(kicad_cli, ["sch", "export", "netlist", "--format", "kicadxml", "-o", xml_path, root])
        spice = read_text(spice_path)
        xml = ET.parse(xml_path).getroot()

    # Symbols, their Sim.* fields, and which pins each net touches.
    components: dict[str, dict] = {}
    sheet_files = {root}
    project_dir = os.path.dirname(project_path)
    for comp in xml.iter("comp"):
        ref = comp.get("ref") or ""
        if not ref or ref.startswith("#") or ref in components:
            continue
        fields = {f.get("name"): (f.text or "") for f in comp.iter("field")}
        for prop in comp.iter("property"):
            if prop.get("name") == "Sheetfile" and prop.get("value"):
                sheet_files.add(os.path.join(project_dir, prop.get("value")))
        components[ref] = {"fields": fields, "pins": []}
    for net in xml.iter("net"):
        spice_name = spice_net_name(net.get("name") or "")
        for node in net.iter("node"):
            ref = node.get("ref")
            if ref in components:
                components[ref]["pins"].append((node.get("pin") or "", spice_name))

    includes: list[str] = []
    elements_by_ref: dict[str, list] = {}
    for card in spice_lines(spice):
        if card.startswith("."):
            words = card.split(None, 1)
            if words[0].lower() in (".include", ".lib", ".inc") and len(words) > 1:
                includes.append(words[1].strip().strip("\"'"))
            continue
        tokens = card.split()
        item = tokens[0]
        kind = item[0].upper()
        ref = item if item in components else item[1:] if item[1:] in components else None
        if ref is None:
            continue
        if len(tokens) == 2 and tokens[1].lower() == "__" + item.lower():
            # KiCad's placeholder for a symbol it has no model for ("U1 __U1": no terminals, no
            # .model card) -- SIM_MODEL::TYPE::NONE, which libkicad reports as NoModel.
            continue
        if kind == "X":
            # Nodes run up to the subcircuit name, the last token before any name=value parameter.
            params_at = next((i for i, t in enumerate(tokens) if "=" in t), len(tokens))
            node_count = max(0, params_at - 2)
        else:
            node_count = _TERMINALS.get(kind, 0)
        terminals = tokens[1:1 + node_count]
        prefix = card.split(None, 1 + node_count)
        value = prefix[1 + node_count] if len(prefix) > 1 + node_count else ""

        # A terminal's net names the symbol pin on it; libkicad reports pins, not nets.
        available = list(components[ref]["pins"])
        nodes = []
        for index, terminal in enumerate(terminals):
            pin = next((p for p in available if p[1] == terminal), None)
            if pin is not None:
                available.remove(pin)
                nodes.append(["pin", pin[0]])
            else:
                nodes.append(["internal", f"{ref}.nc.{index + 1}"])
        element = {"kind": kind, "name": ref, "nodes": nodes, "value": value}
        if kind == "X":
            words = value.split()
            element["subckt"] = words[0] if words else ""
            element["params"] = {w.split("=", 1)[0].lower(): w.split("=", 1)[1] for w in words[1:] if "=" in w}
        elements_by_ref.setdefault(ref, []).append(element)

    excluded = excluded_references(sorted(sheet_files))
    models = []
    for ref, component in components.items():
        fields = component["fields"]
        model = {"reference": ref, "status": "resolved", "deviceType": "", "modelName": "", "libraryPath": "",
                 "message": "", "elements": elements_by_ref.get(ref, [])}
        if ref in excluded or fields.get("Sim.Enable", "").strip() in ("0", "false", "no"):
            model["status"] = "excluded"
            model["elements"] = []
        elif not model["elements"]:
            model["status"] = "nomodel"
        else:
            first = model["elements"][0]["kind"]
            model["deviceType"] = fields.get("Sim.Device") or {"X": "SUBCKT"}.get(first, first)
            library = fields.get("Sim.Library", "")
            if library:
                model["modelName"] = fields.get("Sim.Name", "")
                base = os.path.basename(re.sub(r"\$\{[^}]*\}", "", library).replace("\\", "/"))
                model["libraryPath"] = next((p for p in includes if os.path.basename(p) == base), library)
        models.append(model)
    return {"models": models, "subcircuits": subcircuits_in(includes)}


# ---------------------------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="libkicad stock-KiCad bridge")
    parser.add_argument("query", choices=["board", "geometry", "layer", "simmodels"])
    parser.add_argument("--board", default="")
    parser.add_argument("--project", default="")
    parser.add_argument("--layer", default="")
    parser.add_argument("--kicad-cli", default="kicad-cli")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    if not os.environ.get("LIBKICAD_BRIDGE_VERBOSE"):
        # KiCad's C++ code prints warnings straight to the console; keep the caller's output clean.
        # Every result and error goes to --output instead.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)

    try:
        if args.query == "simmodels":
            result = sim_models(args.project, args.kicad_cli)
        else:
            board = Board(args.board, args.project)
            if args.query == "board":
                result = board.summary()
            elif args.query == "geometry":
                result = board.geometry()
            else:
                result = board.layer_geometry(args.layer)
        payload = {"ok": True, "protocol": PROTOCOL, "result": result}
    except BridgeError as error:
        payload = {"ok": False, "protocol": PROTOCOL, "error": str(error)}
    except Exception as error:  # noqa: BLE001 - reported to libkicad rather than lost
        payload = {"ok": False, "protocol": PROTOCOL, "error": f"{type(error).__name__}: {error}",
                   "traceback": traceback.format_exc()}

    temporary = args.output + ".tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except ValueError as error:  # A NaN or infinity somewhere in the result.
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump({"ok": False, "protocol": PROTOCOL, "error": f"Invalid number in result: {error}"}, handle)
    os.replace(temporary, args.output)
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
