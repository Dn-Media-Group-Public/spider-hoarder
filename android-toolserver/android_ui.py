"""Accessibility-tree outline for the Android tool server: parse uiautomator's hierarchy XML, pick the
nodes worth showing, hand out refs. No device access here, so it is tested on captured XML."""

import re
import xml.etree.ElementTree as ET
from collections import OrderedDict
from dataclasses import dataclass

BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
LABEL_CAP = 300  # chars of a node's text/description shown
FLAGS = ("clickable", "long-clickable", "checkable", "checked", "focused", "scrollable", "selected", "password")


class RefNotFound(Exception):
    pass


@dataclass(frozen=True)
class Node:
    cls: str
    rid: str
    text: str
    desc: str
    hint: str
    pkg: str
    bounds: tuple
    flags: frozenset
    depth: int

    @property
    def sig(self) -> tuple:
        return (self.cls, self.rid, self.text, self.desc)

    @property
    def center(self) -> tuple:
        l, t, r, b = self.bounds
        return ((l + r) // 2, (t + b) // 2)

    @property
    def label(self) -> str:
        s = self.text or self.desc or self.hint
        return s if len(s) <= LABEL_CAP else s[:LABEL_CAP] + "..."


def parse(xml: str) -> list:
    """All nodes of a uiautomator dump, in document order, that are on screen."""
    nodes: list = []

    def walk(el, depth):
        for child in el:
            if child.tag != "node":
                continue
            a = child.attrib
            m = BOUNDS.fullmatch(a.get("bounds", ""))
            if m and a.get("visible-to-user", "true") != "false":
                b = tuple(int(x) for x in m.groups())
                if b[2] > b[0] and b[3] > b[1]:
                    nodes.append(Node(
                        cls=a.get("class", ""), rid=a.get("resource-id", ""), text=a.get("text", ""),
                        desc=a.get("content-desc", ""), hint=a.get("hint", ""), pkg=a.get("package", ""), bounds=b,
                        flags=frozenset(f for f in FLAGS if a.get(f) == "true") | (
                            frozenset() if a.get("enabled", "true") == "true" else frozenset({"disabled"})),
                        depth=depth))
            walk(child, depth + 1)

    walk(ET.fromstring(xml), 0)
    return nodes


def interesting(n: Node) -> bool:
    """Worth a line in the outline: has a label or can be interacted with."""
    return bool(n.label) or bool(n.flags & {"clickable", "long-clickable", "checkable", "scrollable"}) \
        or n.cls.endswith("EditText")


class Refs:
    """ref -> the node as last seen. Numbers are never reused, so an old ref cannot silently point at a
    different element; resolving re-reads the screen and finds the node again by its identity."""

    def __init__(self, limit: int = 3000):
        self.n = 0
        self.limit = limit
        self.table: OrderedDict = OrderedDict()

    def assign(self, node: Node) -> str:
        self.n += 1
        ref = f"e{self.n}"
        self.table[ref] = node
        while len(self.table) > self.limit:
            self.table.popitem(last=False)
        return ref

    def resolve(self, ref: str, fresh: list) -> Node:
        old = self.table.get(ref)
        if old is None:
            raise RefNotFound(f"ref {ref!r} not found, take a new snapshot")
        same = [n for n in fresh if n.sig == old.sig]
        if not same:
            raise RefNotFound(f"ref {ref!r} is gone from the screen, take a new snapshot")
        ox, oy = old.center
        return min(same, key=lambda n: (n.center[0] - ox) ** 2 + (n.center[1] - oy) ** 2)


def short_id(rid: str) -> str:
    return rid.split(":id/", 1)[-1] if rid else ""


def line(n: Node, ref: str, indent: int) -> str:
    cls = n.cls.rsplit(".", 1)[-1] or "node"
    label = n.label.replace("\n", " ")
    parts = [f"{'  ' * indent}- {cls}"]
    if label:
        parts.append('"' + label.replace('"', "'") + '"')
    parts.append(f"[ref={ref}]")
    for f in sorted(n.flags):
        if f != "password":
            parts.append(f"[{f}]")
    if "password" in n.flags:
        parts.append("[password]")
    if n.rid:
        parts.append(f"id={short_id(n.rid)}")
    parts.append("@%d,%d" % n.center)
    return " ".join(parts)


def outline(nodes: list, refs: Refs) -> str:
    """Compact indented outline of the interesting nodes; indentation follows interesting ancestors only."""
    out, stack = [], []  # stack: depths of emitted ancestors
    for n in nodes:
        if not interesting(n):
            continue
        while stack and stack[-1] >= n.depth:
            stack.pop()
        out.append(line(n, refs.assign(n), len(stack)))
        stack.append(n.depth)
    return "\n".join(out) or "(nothing on screen)"


def find(nodes: list, refs: Refs, text=None, regex=None, resource_id=None, description=None, class_name=None) -> str:
    """Nodes matching ALL given criteria (text/description: case-insensitive substring)."""
    rx = re.compile(regex) if regex else None
    out = []
    for n in nodes:
        label = " ".join(x for x in (n.text, n.desc, n.hint) if x)
        if text and text.lower() not in label.lower():
            continue
        if rx and not rx.search(label):
            continue
        if resource_id and resource_id not in n.rid:
            continue
        if description and description.lower() not in n.desc.lower():
            continue
        if class_name and class_name not in n.cls:
            continue
        out.append(line(n, refs.assign(n), 0))
    return "\n".join(out) or "(no matches)"
