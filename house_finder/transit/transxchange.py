"""TransXChange timetable parser.

BODS publishes bus timetables as TransXChange XML (individually or zipped).
To answer "which stops does route U1 serve?" we only need a slice of the
document:

* ``StopPoints/AnnotatedStopPointRef`` — stop reference, name and sometimes
  coordinates (BODS datasets often omit coordinates, in which case NaPTAN is
  used instead).
* ``Services/Service/Lines/Line`` — maps a line id to its public ``LineName``.
* ``VehicleJourneys/VehicleJourney`` — links a ``LineRef`` to the
  ``JourneyPattern`` it operates.
* ``JourneyPattern`` — lists the ``JourneyPatternSection`` s it is made of.
* ``JourneyPatternSection`` — ordered ``JourneyPatternTimingLink`` s whose
  ``From``/``To`` stop refs give the stop sequence.

A ``RouteSection``/``RouteLink`` fallback covers the rarer documents that omit
journey-pattern sections.

The parser streams with ``ElementTree.iterparse`` so multi-megabyte files stay
in reasonable memory.
"""

from __future__ import annotations

import io
import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Iterator

import xml.etree.ElementTree as ET

log = logging.getLogger(__name__)

NS = "{http://www.transxchange.org.uk/}"


def _local(tag: str) -> str:
    return tag[len(NS):] if tag.startswith(NS) else tag


def normalise_line(value: str) -> str:
    """Normalise a line name for comparison (case/space insensitive)."""
    return " ".join((value or "").split()).upper()


@dataclass
class ParsedStop:
    ref: str
    name: str = ""
    latitude: float | None = None
    longitude: float | None = None
    indicator: str = ""
    locality: str = ""


@dataclass
class ParsedLine:
    line_name: str
    stops: list[str] = field(default_factory=list)


@dataclass
class ParsedTimetable:
    stops: dict[str, ParsedStop] = field(default_factory=dict)
    lines: dict[str, ParsedLine] = field(default_factory=dict)
    all_line_names: set[str] = field(default_factory=set)

    def line_stops(self, line_name: str) -> list[str]:
        line = self.lines.get(normalise_line(line_name))
        return list(line.stops) if line else []


def _to_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value.strip())
    except (TypeError, ValueError):
        return None


def _open(source) -> tuple:
    """Return ``(stream, owns_stream)`` for a path/bytes/file object."""
    if isinstance(source, (bytes, bytearray)):
        return io.BytesIO(source), True
    if hasattr(source, "read"):
        return source, False
    return open(source, "rb"), True


def parse_transxchange(
    source, target_lines: Iterable[str] | None = None
) -> ParsedTimetable:
    """Parse TransXChange ``source`` and extract per-line stop sequences.

    ``source`` may be a path, raw ``bytes`` or a binary file object (e.g. a zip
    member). ``target_lines`` limits the work to the named lines; ``None``
    extracts every line in the document.
    """
    wanted = {normalise_line(x) for x in target_lines} if target_lines else None

    stops: dict[str, ParsedStop] = {}
    line_ids: dict[str, str] = {}  # line id -> LineName
    jp_line: dict[str, set[str]] = defaultdict(set)  # jp id -> {line name}
    jp_sections: dict[str, list[str]] = {}  # jp id -> [section id]
    jp_route: dict[str, str] = {}  # jp id -> route id
    jp_order: list[str] = []  # journey-pattern ids in document order
    jps_stops: dict[str, list[str]] = {}  # section id -> [stop ref]
    # RouteSection / RouteLink fallback
    route_sections: dict[str, list[str]] = {}
    section_links: dict[str, list[str]] = {}
    links_stops: dict[str, list[str]] = {}

    stream, owns = _open(source)
    try:
        for _event, elem in ET.iterparse(stream, events=("end",)):
            tag = _local(elem.tag)

            if tag == "AnnotatedStopPointRef":
                ref = (elem.findtext(NS + "StopPointRef") or "").strip()
                if ref and ref not in stops:
                    loc = elem.find(NS + "Location")
                    stops[ref] = ParsedStop(
                        ref=ref,
                        name=(elem.findtext(NS + "CommonName") or "").strip(),
                        latitude=_to_float(loc.findtext(NS + "Latitude")) if loc is not None else None,
                        longitude=_to_float(loc.findtext(NS + "Longitude")) if loc is not None else None,
                        indicator=(elem.findtext(NS + "Indicator") or "").strip(),
                        locality=(elem.findtext(NS + "LocalityName") or "").strip(),
                    )
                elem.clear()

            elif tag == "Line":
                lid = elem.get("id")
                name = (elem.findtext(NS + "LineName") or "").strip()
                if lid and name:
                    line_ids[lid] = name
                elem.clear()

            elif tag == "VehicleJourney":
                lref = (elem.findtext(NS + "LineRef") or "").strip()
                jpref = (elem.findtext(NS + "JourneyPatternRef") or "").strip()
                if lref and jpref and lref in line_ids:
                    jp_line[jpref].add(normalise_line(line_ids[lref]))
                elem.clear()

            elif tag == "JourneyPattern":
                jid = elem.get("id")
                if jid:
                    jp_sections[jid] = [
                        e.text.strip()
                        for e in elem.findall(NS + "JourneyPatternSectionRefs")
                        if e.text and e.text.strip()
                    ]
                    rref = elem.findtext(NS + "RouteRef")
                    if rref and rref.strip():
                        jp_route[jid] = rref.strip()
                    jp_order.append(jid)
                elem.clear()

            elif tag == "JourneyPatternSection":
                jid = elem.get("id")
                refs: list[str] = []
                for link in elem.findall(NS + "JourneyPatternTimingLink"):
                    for endpoint in ("From", "To"):
                        node = link.find(NS + endpoint)
                        if node is None:
                            continue
                        ref = node.findtext(NS + "StopPointRef")
                        if ref and ref.strip():
                            refs.append(ref.strip())
                if jid:
                    jps_stops[jid] = refs
                elem.clear()

            elif tag == "Route":
                rid = elem.get("id")
                if rid:
                    route_sections[rid] = [
                        e.text.strip()
                        for e in elem.findall(NS + "RouteSectionRef")
                        if e.text and e.text.strip()
                    ]
                elem.clear()

            elif tag == "RouteSection":
                sid = elem.get("id")
                if sid:
                    section_links[sid] = [
                        e.text.strip()
                        for e in elem.findall(NS + "RouteLinkRef")
                        if e.text and e.text.strip()
                    ]
                elem.clear()

            elif tag == "RouteLink":
                lid = elem.get("id")
                if lid:
                    refs = []
                    for endpoint in ("From", "To"):
                        node = elem.find(NS + endpoint)
                        if node is None:
                            continue
                        ref = node.findtext(NS + "StopPointRef")
                        if ref and ref.strip():
                            refs.append(ref.strip())
                    links_stops[lid] = refs
                elem.clear()

    finally:
        if owns:
            stream.close()

    all_lines = {normalise_line(n) for n in line_ids.values()}
    # If no LineRef mapping exists (some single-line documents), treat the
    # document's line(s) as owning every journey pattern.
    if not jp_line and len(all_lines) <= 1:
        only = next(iter(all_lines), "")
        for jid in jp_order:
            jp_line[jid].add(only)

    def fallback_stops(jid: str) -> list[str]:
        route = jp_route.get(jid)
        if not route:
            return []
        refs: list[str] = []
        for section in route_sections.get(route, []):
            for link in section_links.get(section, []):
                refs.extend(links_stops.get(link, []))
        return refs

    by_line: dict[str, list[str]] = defaultdict(list)
    seen: dict[str, set[str]] = defaultdict(set)
    for jid in jp_order:
        line_names = jp_line.get(jid)
        if not line_names:
            continue
        section_refs: list[str] = []
        for section in jp_sections.get(jid, []):
            section_refs.extend(jps_stops.get(section, []))
        if not section_refs:
            section_refs = fallback_stops(jid)
        if not section_refs:
            continue
        for name in line_names:
            if wanted is not None and name not in wanted:
                continue
            for ref in section_refs:
                if ref not in seen[name]:
                    seen[name].add(ref)
                    by_line[name].append(ref)

    display = {normalise_line(n): n for n in line_ids.values()}
    lines = {
        name: ParsedLine(line_name=display.get(name, name), stops=refs)
        for name, refs in by_line.items()
    }
    return ParsedTimetable(stops=stops, lines=lines, all_line_names=all_lines)


def line_names_in_file(source) -> set[str]:
    """Cheaply scan ``source`` for ``<LineName>`` values without full parsing.

    Used to skip BODS dataset members that cannot contain a requested route.
    Tolerates namespace prefixes (e.g. ``<txc:LineName>``).
    """
    stream, owns = _open(source)
    try:
        data = stream.read()
    finally:
        if owns:
            stream.close()
    names: set[str] = set()
    marker = b"LineName>"
    start = 0
    while True:
        i = data.find(marker, start)
        if i == -1:
            break
        start = i + len(marker)
        # Skip the closing tag ``</LineName>``.
        if i > 0 and data[i - 1: i] == b"/":
            continue
        # The value follows the ">" that ends the open tag.
        gt = data.find(b">", i)
        if gt == -1:
            break
        lt = data.find(b"<", gt)
        if lt == -1:
            break
        raw = data[gt + 1: lt]
        names.add(normalise_line(raw.decode("utf-8", "ignore")))
        start = lt
    return names


def iter_zip_xml(payload: bytes) -> Iterator[tuple[str, bytes]]:
    """Yield ``(member_name, bytes)`` for XML members of a BODS zip payload."""
    import zipfile

    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            if not info.filename.lower().endswith(".xml"):
                continue
            yield os.path.basename(info.filename), zf.read(info)
