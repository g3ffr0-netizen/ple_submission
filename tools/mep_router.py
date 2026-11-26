"""Deterministic Manhattan trunk-and-tap MEP router for data-hall corridors."""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Dict, Iterable, List, Sequence, Tuple


@dataclass(frozen=True)
class KeepoutBox:
    x0: float
    y0: float
    x1: float
    y1: float


@dataclass(frozen=True)
class Segment3D:
    service: str
    kind: str  # "trunk", "tap", or "drop"
    start_mm: Tuple[float, float, float]
    end_mm: Tuple[float, float, float]
    width_mm: float
    height_mm: float

    @property
    def length_mm(self) -> float:
        dx = abs(self.end_mm[0] - self.start_mm[0])
        dy = abs(self.end_mm[1] - self.start_mm[1])
        dz = abs(self.end_mm[2] - self.start_mm[2])
        return dx + dy + dz


def _unique_sorted(values: Iterable[float], tol: float = 1.0) -> List[float]:
    out: List[float] = []
    for v in sorted(values):
        if not out or abs(v - out[-1]) > tol:
            out.append(v)
    return out


def _build_axis_spine(
    terminals: Sequence[Tuple[float, float, float]],
    axis: str,
    spine_coord: float,
) -> List[Tuple[Tuple[float, float], Tuple[float, float]]]:
    """Build non-overlapping 2D Manhattan spine + grouped perpendicular taps."""
    if not terminals:
        return []
    segs: List[Tuple[Tuple[float, float], Tuple[float, float]]] = []
    if axis == "x":
        xs = [t[0] for t in terminals]
        x_min, x_max = min(xs), max(xs)
        if x_max - x_min > 1.0:
            segs.append(((x_min, spine_coord), (x_max, spine_coord)))
        by_x: Dict[float, List[float]] = {}
        for x, y, _ in terminals:
            key = round(x, 1)
            by_x.setdefault(key, []).append(y)
        for x_key in sorted(by_x):
            ys = by_x[x_key]
            y_hi = max(ys)
            y_lo = min(ys)
            if y_hi > spine_coord + 1.0:
                segs.append(((x_key, spine_coord), (x_key, y_hi)))
            if y_lo < spine_coord - 1.0:
                segs.append(((x_key, spine_coord), (x_key, y_lo)))
    else:
        ys = [t[1] for t in terminals]
        y_min, y_max = min(ys), max(ys)
        if y_max - y_min > 1.0:
            segs.append(((spine_coord, y_min), (spine_coord, y_max)))
        by_y: Dict[float, List[float]] = {}
        for x, y, _ in terminals:
            key = round(y, 1)
            by_y.setdefault(key, []).append(x)
        for y_key in sorted(by_y):
            xs = by_y[y_key]
            x_hi = max(xs)
            x_lo = min(xs)
            if x_hi > spine_coord + 1.0:
                segs.append(((spine_coord, y_key), (x_hi, y_key)))
            if x_lo < spine_coord - 1.0:
                segs.append(((spine_coord, y_key), (x_lo, y_key)))
    return segs


def _detour_segment_2d(
    p0: Tuple[float, float],
    p1: Tuple[float, float],
    keepouts: Sequence[KeepoutBox],
    pad_mm: float,
) -> List[Tuple[Tuple[float, float], Tuple[float, float]]]:
    """Bypass localized rectangular keepouts with minimal orthogonal doglegs."""
    segs = [(p0, p1)]
    for ko in keepouts:
        rx0, ry0 = ko.x0 - pad_mm, ko.y0 - pad_mm
        rx1, ry1 = ko.x1 + pad_mm, ko.y1 + pad_mm
        next_segs: List[Tuple[Tuple[float, float], Tuple[float, float]]] = []
        for (ax, ay), (bx, by) in segs:
            if abs(ay - by) <= 1.0 and ry0 < ay < ry1:
                x_lo, x_hi = min(ax, bx), max(ax, bx)
                if x_lo < rx1 and x_hi > rx0:
                    y_det = ry0 if abs(ay - ry0) <= abs(ry1 - ay) else ry1
                    x_enter = rx0 if ax < bx else rx1
                    x_exit = rx1 if ax < bx else rx0
                    # Clamp if segment starts or ends inside expanded keepout
                    if rx0 <= ax <= rx1:
                        x_enter = ax
                    if rx0 <= bx <= rx1:
                        x_exit = bx
                    pts = [(ax, ay)]
                    if abs(x_enter - ax) > 1.0:
                        pts.append((x_enter, ay))
                    pts.append((x_enter, y_det))
                    pts.append((x_exit, y_det))
                    if not (rx0 <= bx <= rx1):
                        pts.append((x_exit, by))
                        pts.append((bx, by))
                    for i in range(len(pts) - 1):
                        if abs(pts[i][0] - pts[i + 1][0]) > 1.0 or abs(pts[i][1] - pts[i + 1][1]) > 1.0:
                            next_segs.append((pts[i], pts[i + 1]))
                    continue
            elif abs(ax - bx) <= 1.0 and rx0 < ax < rx1:
                y_lo, y_hi = min(ay, by), max(ay, by)
                if y_lo < ry1 and y_hi > ry0:
                    x_det = rx0 if abs(ax - rx0) <= abs(rx1 - ax) else rx1
                    y_enter = ry0 if ay < by else ry1
                    y_exit = ry1 if ay < by else ry0
                    if ry0 <= ay <= ry1:
                        y_enter = ay
                    if ry0 <= by <= ry1:
                        y_exit = by
                    pts = [(ax, ay)]
                    if abs(y_enter - ay) > 1.0:
                        pts.append((ax, y_enter))
                    pts.append((x_det, y_enter))
                    pts.append((x_det, y_exit))
                    if not (ry0 <= by <= ry1):
                        pts.append((bx, y_exit))
                        pts.append((bx, by))
                    for i in range(len(pts) - 1):
                        if abs(pts[i][0] - pts[i + 1][0]) > 1.0 or abs(pts[i][1] - pts[i + 1][1]) > 1.0:
                            next_segs.append((pts[i], pts[i + 1]))
                    continue
            next_segs.append(((ax, ay), (bx, by)))
        segs = next_segs
    return segs


def _choose_best_axis(terminals: Sequence[Tuple[float, float, float]]) -> Tuple[str, float]:
    xs = [t[0] for t in terminals]
    ys = [t[1] for t in terminals]
    x_med = float(median(xs))
    y_med = float(median(ys))
    x_segs = _build_axis_spine(terminals, "x", y_med)
    y_segs = _build_axis_spine(terminals, "y", x_med)
    len_x = sum(abs(b[0] - a[0]) + abs(b[1] - a[1]) for a, b in x_segs)
    len_y = sum(abs(b[0] - a[0]) + abs(b[1] - a[1]) for a, b in y_segs)
    return ("x", y_med) if len_x <= len_y else ("y", x_med)


def route_unstacked_mep(
    tray_terminals_mm: Sequence[Tuple[float, float, float]],
    busway_terminals_mm: Sequence[Tuple[float, float, float]],
    chw_terminals_mm: Sequence[Tuple[float, float, float]],
    keepouts_mm: Sequence[KeepoutBox] = (),
    *,
    tray_z_center_mm: float = 2673.5,   # Z_bot=2610, height=127 -> Z_top=2737 <= 2750 mm
    busway_z_center_mm: float = 2683.5, # Z_bot=2610, height=147 -> Z_top=2757 mm
    chw_z_center_mm: float = -480.0,
    tray_size_mm: Tuple[float, float] = (300.0, 127.0),
    busway_size_mm: Tuple[float, float] = (160.0, 147.0),
    chw_od_mm: float = 165.1,
    lateral_gap_mm: float = 350.0,
) -> Dict[str, object]:
    """Generate unstacked overhead cable_tray/busway lanes + under-floor chw_pipe trunk-and-taps."""
    segments: List[Segment3D] = []

    # 1. Overhead cable_tray and busway: choose optimal spine axis and offset busway laterally
    ref_terms = list(tray_terminals_mm) or list(busway_terminals_mm)
    if ref_terms:
        axis, spine_c = _choose_best_axis(ref_terms)
        lane_offset = (tray_size_mm[0] + busway_size_mm[0]) / 2.0 + max(300.0, lateral_gap_mm)

        for svc, terms, z_c, (w_mm, h_mm), offset in (
            ("cable_tray", tray_terminals_mm, tray_z_center_mm, tray_size_mm, 0.0),
            ("busway", busway_terminals_mm, busway_z_center_mm, busway_size_mm, lane_offset),
        ):
            if not terms:
                continue
            raw_2d = _build_axis_spine(terms, axis, spine_c + offset)
            pad = w_mm / 2.0 + 50.0
            for idx, (p0, p1) in enumerate(raw_2d):
                kind = "trunk" if idx == 0 else "tap"
                for a, b in _detour_segment_2d(p0, p1, keepouts_mm, pad):
                    segments.append(
                        Segment3D(
                            service=svc,
                            kind=kind,
                            start_mm=(round(a[0], 1), round(a[1], 1), z_c),
                            end_mm=(round(b[0], 1), round(b[1], 1), z_c),
                            width_mm=w_mm,
                            height_mm=h_mm,
                        )
                    )
            for tx, ty, tz in terms:
                if abs(tz - z_c) > 1.0:
                    segments.append(
                        Segment3D(
                            service=svc,
                            kind="drop",
                            start_mm=(round(tx, 1), round(ty, 1), z_c),
                            end_mm=(round(tx, 1), round(ty, 1), round(tz, 1)),
                            width_mm=w_mm,
                            height_mm=h_mm,
                        )
                    )

    # 2. Chilled water pipe in its terminal Z-band (under-floor -480 mm)
    if chw_terminals_mm:
        chw_axis, chw_spine = _choose_best_axis(chw_terminals_mm)
        raw_chw = _build_axis_spine(chw_terminals_mm, chw_axis, chw_spine)
        pad = chw_od_mm / 2.0 + 50.0
        for idx, (p0, p1) in enumerate(raw_chw):
            kind = "trunk" if idx == 0 else "tap"
            for a, b in _detour_segment_2d(p0, p1, keepouts_mm, pad):
                segments.append(
                    Segment3D(
                        service="chw_pipe",
                        kind=kind,
                        start_mm=(round(a[0], 1), round(a[1], 1), chw_z_center_mm),
                        end_mm=(round(b[0], 1), round(b[1], 1), chw_z_center_mm),
                        width_mm=chw_od_mm,
                        height_mm=chw_od_mm,
                    )
                )
        for tx, ty, tz in chw_terminals_mm:
            if abs(tz - chw_z_center_mm) > 1.0:
                segments.append(
                    Segment3D(
                        service="chw_pipe",
                        kind="drop",
                        start_mm=(round(tx, 1), round(ty, 1), chw_z_center_mm),
                        end_mm=(round(tx, 1), round(ty, 1), round(tz, 1)),
                        width_mm=chw_od_mm,
                        height_mm=chw_od_mm,
                    )
                )

    by_service_mm: Dict[str, float] = {}
    for seg in segments:
        by_service_mm[seg.service] = round(by_service_mm.get(seg.service, 0.0) + seg.length_mm, 1)

    return {
        "total_length_mm": round(sum(by_service_mm.values()), 1),
        "length_by_service_mm": by_service_mm,
        "segments": segments,
    }
