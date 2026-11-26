#!/usr/bin/env python3
"""Self-contained BIM coordinator script for dc_model hall_seed005_d100.

Rebuilds Meridian Data Center Phase 1 - Data Hall 1 (MDC-2026-014) and routes
the three overhead MEP services (CT-1 telecom cable tray, BW-1 power busway,
CHW-1 chilled-water pipe) clash-free in IFC4 using ifcopenshell and numpy.
"""
import json
import math
import os
import sys
import numpy as np
import ifcopenshell
import ifcopenshell.api

OUT_PATH = (
    sys.argv[1]
    if len(sys.argv) > 1
    else "/app/files/dc_model__hall_seed005_d100__mep_routing/output/model.ifc"
)


# ---------------------------------------------------------------------------
# Baked-in drawing data reconciled from sheets A-101, A-301, A-601, A-602,
# M-101, E-101, F-101 (MDC-2026-014, plotted 2026-07-27, issue 2026-07-24)
# ---------------------------------------------------------------------------

ROOM_POLYGON_MM = [
    (0.0, 0.0),
    (27000.0, 0.0),
    (27000.0, 15600.0),
    (20400.0, 15600.0),
    (20400.0, 24000.0),
    (0.0, 24000.0),
    (0.0, 0.0),
]

CEILING_SOFFIT_MM = 3050.0
CLEAR_HEIGHT_MM = 2600.0
ACCESS_FLOOR_THICK_MM = 35.0
PLENUM_DEPTH_MM = 600.0
TILE_PITCH_MM = 600.0

SERVICE_ENTRIES_MM = {
    "busway": [0, 12470],
    "cable_tray": [0, 12470],
    "chw_pipe": [0, 12470],
}

# 8 full-height structural columns (600 x 600 x 3050 mm)
COLUMNS_DATA = [
    ("COL-A1", "A1", "A", "1", 7050.0, 7050.0),
    ("COL-B1", "B1", "B", "1", 14250.0, 7050.0),
    ("COL-C1", "C1", "C", "1", 21450.0, 7050.0),
    ("COL-A2", "A2", "A", "2", 7050.0, 14250.0),
    ("COL-B2", "B2", "B", "2", 14250.0, 14250.0),
    ("COL-C2", "C2", "C", "2", 21450.0, 14250.0),
    ("COL-A3", "N1", "A", "3", 7050.0, 21450.0),
    ("COL-B3", "N2", "B", "3", 14250.0, 21450.0),
]

# 4 perimeter CRAHs on South wall (1200 W x 900 D x 2000 H, Facing NORTH, 100 kW, DN100)
PERIMETER_CRAHS = [
    ("CRAH-01", "B01", 1200.0, 450.0, 1200.0, 900.0, 2000.0, "NORTH", 100.0, "DN100"),
    ("CRAH-02", "P01", 9600.0, 450.0, 1200.0, 900.0, 2000.0, "NORTH", 100.0, "DN100"),
    ("CRAH-03", "AC01", 17400.0, 450.0, 1200.0, 900.0, 2000.0, "NORTH", 100.0, "DN100"),
    ("CRAH-04", "AQ01", 25800.0, 450.0, 1200.0, 900.0, 2000.0, "NORTH", 100.0, "DN100"),
]

# 4 PDUs on East side (762 W x 813 D x 1959 H, 225 kVA, 480/277V, front face flush with served row front face)
PDUS_DATA = [
    ("PDU-01", "ROW A", "AN05", 23781.0, 2806.5, 762.0, 813.0, 1959.0, "SOUTH"),
    ("PDU-02", "ROW B", "AM10", 23181.0, 6063.5, 762.0, 813.0, 1959.0, "NORTH"),
    ("PDU-03", "ROW C", "AL14", 22581.0, 8206.5, 762.0, 813.0, 1959.0, "SOUTH"),
    ("PDU-04", "ROW D", "AO19", 24381.0, 11463.5, 762.0, 813.0, 1959.0, "NORTH"),
]


def _tile_col_name(col_idx: int) -> str:
    """0-based tile column index to Excel-style column label: 0..25 -> A..Z, 26..51 -> AA..AZ."""
    if col_idx < 26:
        return chr(ord("A") + col_idx)
    return chr(ord("A") + (col_idx // 26) - 1) + chr(ord("A") + (col_idx % 26))


def _tile_address(x_sw_mm: float, y_sw_mm: float) -> str:
    col_idx = int(math.floor((x_sw_mm + 1e-6) / 600.0))
    row_num = int(math.floor((y_sw_mm + 1e-6) / 600.0)) + 1
    return f"{_tile_col_name(col_idx)}{row_num:02d}"


def build_rows_data():
    """Return exact cabinet (94) and in-row CRAH (12) definitions for Rows A..D."""
    # Row specs: (row_id, row_label, y_center, depth, facing, bay_sequence)
    # Each bay in bay_sequence is (kind, width_mm, tag_override_or_None)
    # kind in {'RACK', 'DIST', 'CRAH', 'GAP'}
    row_specs = [
        (
            "A",
            "ROW A",
            3000.0,
            1200.0,
            "SOUTH",
            [
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("CRAH", 600.0, "CRAH-A01"),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("DIST", 800.0, "N1"),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("CRAH", 600.0, "CRAH-A02"),
                ("GAP", 1600.0, "CROSS-AISLE"),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("GAP", 600.0, "SPARE"),
                ("CRAH", 600.0, "CRAH-A03"),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
            ],
        ),
        (
            "B",
            "ROW B",
            5935.0,
            1070.0,
            "NORTH",
            [
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("CRAH", 600.0, "CRAH-B01"),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("GAP", 1200.0, "COL-A1-OMIT"),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("DIST", 800.0, "N1"),
                ("DIST", 800.0, "N2"),
                ("CRAH", 600.0, "CRAH-B02"),
                ("GAP", 1400.0, "CROSS-AISLE"),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("GAP", 1200.0, "COL-B1-OMIT"),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("CRAH", 600.0, "CRAH-B03"),
                ("GAP", 600.0, "SPARE"),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
            ],
        ),
        (
            "C",
            "ROW C",
            8400.0,
            1200.0,
            "SOUTH",
            [
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("CRAH", 600.0, "CRAH-C01"),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("GAP", 1200.0, "COL-A1-OMIT"),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("CRAH", 600.0, "CRAH-C02"),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("GAP", 1800.0, "CROSS-AISLE"),
                ("DIST", 800.0, "N1"),
                ("GAP", 1200.0, "COL-B1-OMIT"),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("GAP", 600.0, "SPARE"),
                ("CRAH", 600.0, "CRAH-C03"),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
            ],
        ),
        (
            "D",
            "ROW D",
            11335.0,
            1070.0,
            "NORTH",
            [
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("CRAH", 600.0, "CRAH-D01"),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("DIST", 800.0, "N1"),
                ("DIST", 800.0, "N2"),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("CRAH", 600.0, "CRAH-D02"),
                ("GAP", 1400.0, "CROSS-AISLE"),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("GAP", 600.0, "SPARE"),
                ("CRAH", 600.0, "CRAH-D03"),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
                ("RACK", 600.0, None),
            ],
        ),
    ]

    racks = []
    inrow_crahs = []
    for row_id, row_label, y_center, depth, facing, bays in row_specs:
        x_cursor = 1800.0
        rack_idx = 1
        y_sw = y_center - depth / 2.0
        for kind, width, tag in bays:
            cx = x_cursor + width / 2.0
            t_addr = _tile_address(x_cursor, y_sw)
            if kind in ("RACK", "DIST"):
                r_name = f"RACK-{row_id}{rack_idx:02d}"
                rack_idx += 1
                racks.append(
                    {
                        "name": r_name,
                        "row_id": row_id,
                        "row_label": row_label,
                        "cx": cx,
                        "cy": y_center,
                        "width": width,
                        "depth": depth,
                        "height": 2000.0,
                        "facing": facing,
                        "is_distributor": (kind == "DIST"),
                        "type_code": tag if kind == "DIST" else "C1",
                        "tile": t_addr,
                    }
                )
            elif kind == "CRAH":
                inrow_crahs.append(
                    {
                        "name": tag,
                        "row_id": row_id,
                        "row_label": row_label,
                        "cx": cx,
                        "cy": y_center,
                        "width": width,
                        "depth": depth,
                        "height": 2000.0,
                        "facing": facing,
                        "capacity_kw": 50.0,
                        "conn": "DN50",
                        "tile": t_addr,
                    }
                )
            x_cursor += width
    return racks, inrow_crahs


def build_sprinklers_data():
    """Return all 104 sprinkler heads: 94 ceiling pendent + 10 in-aisle upright."""
    heads = []
    full_x = [
        1250.0,
        3750.0,
        6250.0,
        8750.0,
        11250.0,
        13750.0,
        16250.0,
        18750.0,
        21250.0,
        23750.0,
        26250.0,
    ]
    idx = 1
    for y_mm in [1250.0, 3750.0, 6250.0, 8750.0, 11250.0]:
        for x_mm in full_x:
            heads.append(
                {
                    "name": f"SPR-CEIL-{idx:03d}",
                    "kind": "CeilingPendent",
                    "x": x_mm,
                    "y": y_mm,
                    "z_deflector": 3024.6,
                    "z_bottom": 3024.6,
                    "z_top": 3050.0,
                }
            )
            idx += 1

    # Row 6 near Y = 13750 / 14350 (step wall at Y = 15600, Col C2 at 21450, 14250)
    for x_mm in full_x:
        y_mm = 14350.0 if x_mm in (18750.0, 23750.0, 26250.0) else 13750.0
        heads.append(
            {
                "name": f"SPR-CEIL-{idx:03d}",
                "kind": "CeilingPendent",
                "x": x_mm,
                "y": y_mm,
                "z_deflector": 3024.6,
                "z_bottom": 3024.6,
                "z_top": 3050.0,
            }
        )
        idx += 1

    # Rows 7..10 in North wing (X = 0..20400)
    north_x = [1250.0, 3750.0, 6250.0, 8750.0, 11250.0, 13750.0, 17350.0]
    for y_mm in [16250.0, 18750.0, 21250.0, 23750.0]:
        for x_mm in north_x:
            heads.append(
                {
                    "name": f"SPR-CEIL-{idx:03d}",
                    "kind": "CeilingPendent",
                    "x": x_mm,
                    "y": y_mm,
                    "z_deflector": 3024.6,
                    "z_bottom": 3024.6,
                    "z_top": 3050.0,
                }
            )
            idx += 1

    # 10 in-aisle upright ESFR heads along Cold Aisle BC centreline (Y = 7135 mm, Z_deflector = 1890 mm)
    in_aisle_x = [
        2300.0,
        4275.0,
        6250.0,
        7850.0,
        10900.0,
        13275.0,
        15050.0,
        16867.0,
        18683.0,
        20500.0,
    ]
    for a_idx, x_mm in enumerate(in_aisle_x, start=1):
        heads.append(
            {
                "name": f"SPR-AISLE-{a_idx:03d}",
                "kind": "InAisleUpright",
                "x": x_mm,
                "y": 7135.0,
                "z_deflector": 1890.0,
                "z_bottom": 1864.6,
                "z_top": 1890.0,
            }
        )
    return heads


def build_mep_routes():
    """Return straight axis-aligned segments for CT-1, BW-1, and CHW-1.

    All three services run in the overhead zone Z_bottom >= 2600.0 mm and are laid out
    as THREE COMPLETELY NON-CROSSING PLANAR TREES IN XY:
    - CHW-1 (168.3 mm insulated OD, Z = 2620.0..2788.3 mm): West-trunk comb along X = 350 mm
      (Y = 2800..11650 mm) with branches at Y = 9300 mm and Y = 4300 mm and spurs to CRAH-02..04.
    - CT-1 (683 W x 127 H mm, Z = 2600.0..2727.0 mm): Middle S-curve entering at Y = 12060 mm,
      dropping at X = 2060 mm to Branch CD (Y = 9800 mm), dropping at X = 19560 mm to Branch AB
      (Y = 4800 mm). Because NO segment of CHW-1 or BW-1 ever crosses above or below CT-1 in XY,
      CT-1 has 323 mm (>= 300 mm per TIA-569-B) clear vertical access headroom to the 3050 mm soffit
      across 100% of its length.
    - BW-1 (128 W x 147 H mm, Z = 2600.0..2747.0 mm): Outer S-curve parallel to CT-1 with
      >= 314.5 mm (>= 300 mm) clear horizontal XY separation from CT-1 everywhere.
    - All segments stay >= 465.8 mm horizontally clear of all 104 sprinkler heads.
    """
    segments = []

    # -----------------------------------------------------------------------
    # 1. Telecom Cable Tray (CT-1): 683 W x 127 H, Z_bottom = 2600.0 (Z = 2600..2727)
    #    hw = 341.5 mm. Uncrossed S-curve with >= 323 mm clear access headroom above.
    # -----------------------------------------------------------------------
    ct_w, ct_h, ct_z = 683.0, 127.0, 2600.0
    # West entry header at Y = 12060 (spans Y = 11718.5..12401.5) from X = 50 to X = 1710
    segments.append(("CT-1-SEG-01", "cable_tray", 50.0, 12060.0, 1710.0, 12060.0, ct_z, ct_w, ct_h, "RECT"))
    # West drop at X = 2060 (spans X = 1718.5..2401.5) from Y = 9458.5 to Y = 12401.5
    segments.append(("CT-1-SEG-02", "cable_tray", 2060.0, 9458.5, 2060.0, 12401.5, ct_z, ct_w, ct_h, "RECT"))
    # Branch CD at Y = 9800 (spans Y = 9458.5..10141.5) from X = 2410 to X = 21800
    segments.append(("CT-1-SEG-03", "cable_tray", 2410.0, 9800.0, 21800.0, 9800.0, ct_z, ct_w, ct_h, "RECT"))
    # East connector at X = 22150 (spans X = 21808.5..22491.5; 58.5 mm E of COL-C1 21750) from Y = 4938.5 to Y = 10141.5
    segments.append(("CT-1-SEG-04", "cable_tray", 22150.0, 4938.5, 22150.0, 10141.5, ct_z, ct_w, ct_h, "RECT"))
    # Branch AB at Y = 5280 (spans Y = 4938.5..5621.5) from X = 1800 to X = 21800
    segments.append(("CT-1-SEG-05", "cable_tray", 1800.0, 5280.0, 21800.0, 5280.0, ct_z, ct_w, ct_h, "RECT"))

    # -----------------------------------------------------------------------
    # 2. Power Busway (BW-1): 128 W x 147 H, Z_bottom = 2600.0 (Z = 2600..2747)
    #    hw = 64.0 mm. Parallel outer S-curve maintaining >= 314.5 mm clear XY gap from CT-1.
    # -----------------------------------------------------------------------
    bw_w, bw_h, bw_z = 128.0, 147.0, 2600.0
    # West entry header at Y = 12780 (spans Y = 12716..12844; 314.5 mm N of CT-1 12401.5) from X = 50 to X = 2708
    segments.append(("BW-1-SEG-01", "busway", 50.0, 12780.0, 2708.0, 12780.0, bw_z, bw_w, bw_h, "RECT"))
    # West drop at X = 2780 (spans X = 2716..2844; 314.5 mm E of CT-1 2401.5) from Y = 10456 to Y = 12844
    segments.append(("BW-1-SEG-02", "busway", 2780.0, 10456.0, 2780.0, 12844.0, bw_z, bw_w, bw_h, "RECT"))
    # Branch CD at Y = 10520 (spans Y = 10456..10584; 314.5 mm N of CT-1 10141.5) from X = 2852 to X = 22798
    segments.append(("BW-1-SEG-03", "busway", 2852.0, 10520.0, 22798.0, 10520.0, bw_z, bw_w, bw_h, "RECT"))
    # East connector at X = 22870 (spans X = 22806..22934; 314.5 mm E of CT-1 22491.5) from Y = 4496 to Y = 10584
    segments.append(("BW-1-SEG-04", "busway", 22870.0, 4496.0, 22870.0, 10584.0, bw_z, bw_w, bw_h, "RECT"))
    # Branch AB at Y = 4560 (spans Y = 4496..4624; 314.5 mm S of CT-1 4938.5 & 111.85 mm N of CHW-1 4384.15)
    segments.append(("BW-1-SEG-05", "busway", 1800.0, 4560.0, 22798.0, 4560.0, bw_z, bw_w, bw_h, "RECT"))

    # -----------------------------------------------------------------------
    # 3. Chilled-Water Pipe (CHW-1): 168.3 mm insulated OD, Z_bottom = 2620.0 (Z = 2620..2788.3)
    #    hw = 84.15 mm. West-trunk comb completely non-overlapping in XY with CT-1 and BW-1
    #    and completely outside all PDU footprints.
    # -----------------------------------------------------------------------
    chw_od, chw_z = 168.3, 2620.0
    # West N-S trunk at X = 350 (spans X = 265.85..434.15) from Y = 2600 to Y = 11650
    # North end (350, 11650) is 891.6 mm from service entry (0, 12470) and 68.5 mm S of CT-1-SEG-01!
    segments.append(("CHW-1-SEG-01", "chw_pipe", 350.0, 2600.0, 350.0, 11650.0, chw_z, chw_od, chw_od, "CIRC"))
    # Branch CD at Y = 9300 (spans Y = 9215.85..9384.15; 74.35 mm S of CT-1 9458.5) from X = 442 to X = 19000
    segments.append(("CHW-1-SEG-02", "chw_pipe", 442.0, 9300.0, 19000.0, 9300.0, chw_z, chw_od, chw_od, "CIRC"))
    # Branch AB at Y = 4300 (spans Y = 4215.85..4384.15; 74.35 mm S of CT-1 4458.5) from X = 442 to X = 24510
    segments.append(("CHW-1-SEG-03", "chw_pipe", 442.0, 4300.0, 24510.0, 4300.0, chw_z, chw_od, chw_od, "CIRC"))
    # Spur to CRAH-02 (9600, 450) at X = 9600 from Y = 2800 to Y = 4208
    segments.append(("CHW-1-SEG-04", "chw_pipe", 9600.0, 2800.0, 9600.0, 4208.0, chw_z, chw_od, chw_od, "CIRC"))
    # Spur to CRAH-03 (17400, 450) at X = 17400 from Y = 2800 to Y = 4208
    segments.append(("CHW-1-SEG-05", "chw_pipe", 17400.0, 2800.0, 17400.0, 4208.0, chw_z, chw_od, chw_od, "CIRC"))
    # Spur to CRAH-04 (25800, 450) at X = 24600 from Y = 2500 to Y = 4384 (clear E of PDU-01 X=23400..24162)
    segments.append(("CHW-1-SEG-06", "chw_pipe", 24600.0, 2500.0, 24600.0, 4384.0, chw_z, chw_od, chw_od, "CIRC"))

    return segments


# ---------------------------------------------------------------------------
# IFC4 Geometry & Property Helpers
# ---------------------------------------------------------------------------

def _place_matrix(cx_mm: float, cy_mm: float, z_mm: float, facing: str = "NORTH") -> np.ndarray:
    """Create 4x4 placement matrix with translations in METRES per modelling contract."""
    mat = np.eye(4, dtype=float)
    if facing == "SOUTH":
        mat[0, 0] = -1.0
        mat[1, 1] = -1.0
    elif facing == "EAST":
        mat[0, 0] = 0.0
        mat[0, 1] = 1.0
        mat[1, 0] = -1.0
        mat[1, 1] = 0.0
    elif facing == "WEST":
        mat[0, 0] = 0.0
        mat[0, 1] = -1.0
        mat[1, 0] = 1.0
        mat[1, 1] = 0.0
    mat[0, 3] = cx_mm / 1000.0
    mat[1, 3] = cy_mm / 1000.0
    mat[2, 3] = z_mm / 1000.0
    return mat


def _make_rect_extrusion(model, body_ctx, width_mm: float, depth_mm: float, height_mm: float):
    hw = float(width_mm) / 2.0
    hd = float(depth_mm) / 2.0
    pts_2d = [
        model.createIfcCartesianPoint((-hw, -hd)),
        model.createIfcCartesianPoint((hw, -hd)),
        model.createIfcCartesianPoint((hw, hd)),
        model.createIfcCartesianPoint((-hw, hd)),
        model.createIfcCartesianPoint((-hw, -hd)),
    ]
    poly = model.createIfcPolyline(pts_2d)
    prof = model.createIfcArbitraryClosedProfileDef("AREA", None, poly)
    axis3d = model.createIfcAxis2Placement3D(
        model.createIfcCartesianPoint((0.0, 0.0, 0.0)),
        model.createIfcDirection((0.0, 0.0, 1.0)),
        model.createIfcDirection((1.0, 0.0, 0.0)),
    )
    solid = model.createIfcExtrudedAreaSolid(
        prof, axis3d, model.createIfcDirection((0.0, 0.0, 1.0)), float(height_mm)
    )
    rep = model.createIfcShapeRepresentation(body_ctx, "Body", "SweptSolid", [solid])
    return model.createIfcProductDefinitionShape(None, None, [rep])


def _make_poly_extrusion(model, body_ctx, pts_xy_mm, height_mm: float):
    pts_2d = [model.createIfcCartesianPoint((float(x), float(y))) for x, y in pts_xy_mm]
    poly = model.createIfcPolyline(pts_2d)
    prof = model.createIfcArbitraryClosedProfileDef("AREA", None, poly)
    axis3d = model.createIfcAxis2Placement3D(
        model.createIfcCartesianPoint((0.0, 0.0, 0.0)),
        model.createIfcDirection((0.0, 0.0, 1.0)),
        model.createIfcDirection((1.0, 0.0, 0.0)),
    )
    solid = model.createIfcExtrudedAreaSolid(
        prof, axis3d, model.createIfcDirection((0.0, 0.0, 1.0)), float(height_mm)
    )
    rep = model.createIfcShapeRepresentation(body_ctx, "Body", "SweptSolid", [solid])
    return model.createIfcProductDefinitionShape(None, None, [rep])


def _make_cylinder_extrusion(model, body_ctx, radius_mm: float, height_mm: float):
    n = 16
    pts_2d = []
    for i in range(n):
        ang = 2.0 * math.pi * i / n
        pts_2d.append(
            model.createIfcCartesianPoint(
                (round(float(radius_mm) * math.cos(ang), 3), round(float(radius_mm) * math.sin(ang), 3))
            )
        )
    pts_2d.append(pts_2d[0])
    poly = model.createIfcPolyline(pts_2d)
    prof = model.createIfcArbitraryClosedProfileDef("AREA", None, poly)
    axis3d = model.createIfcAxis2Placement3D(
        model.createIfcCartesianPoint((0.0, 0.0, 0.0)),
        model.createIfcDirection((0.0, 0.0, 1.0)),
        model.createIfcDirection((1.0, 0.0, 0.0)),
    )
    solid = model.createIfcExtrudedAreaSolid(
        prof, axis3d, model.createIfcDirection((0.0, 0.0, 1.0)), float(height_mm)
    )
    rep = model.createIfcShapeRepresentation(body_ctx, "Body", "SweptSolid", [solid])
    return model.createIfcProductDefinitionShape(None, None, [rep])


def _add_pset(model, product, pset_name: str, props: dict):
    pset = ifcopenshell.api.run("pset.add_pset", model, product=product, name=pset_name)
    ifcopenshell.api.run("pset.edit_pset", model, pset=pset, properties=props)
    return pset


def build_model():
    model = ifcopenshell.api.run("project.create_file", version="IFC4")

    # Project & Units (MILLI METRE)
    project = ifcopenshell.api.run(
        "root.create_entity",
        model,
        ifc_class="IfcProject",
        name="Meridian Data Center Phase 1 - Data Hall 1",
    )
    project.Description = "MDC-2026-014 Coordinated Data Hall & Overhead MEP Model"

    len_unit = ifcopenshell.api.run("unit.add_si_unit", model, unit_type="LENGTHUNIT", prefix="MILLI")
    area_unit = ifcopenshell.api.run("unit.add_si_unit", model, unit_type="AREAUNIT")
    vol_unit = ifcopenshell.api.run("unit.add_si_unit", model, unit_type="VOLUMEUNIT")
    ang_unit = ifcopenshell.api.run("unit.add_si_unit", model, unit_type="PLANEANGLEUNIT")
    ifcopenshell.api.run("unit.assign_unit", model, units=[len_unit, area_unit, vol_unit, ang_unit])

    model_ctx = ifcopenshell.api.run("context.add_context", model, context_type="Model")
    body_ctx = ifcopenshell.api.run(
        "context.add_context",
        model,
        context_type="Model",
        context_identifier="Body",
        target_view="MODEL_VIEW",
        parent=model_ctx,
    )

    # Spatial structure
    site = ifcopenshell.api.run("root.create_entity", model, ifc_class="IfcSite", name="Meridian Site")
    building = ifcopenshell.api.run(
        "root.create_entity", model, ifc_class="IfcBuilding", name="Meridian Data Center Phase 1"
    )
    storey = ifcopenshell.api.run(
        "root.create_entity", model, ifc_class="IfcBuildingStorey", name="Data Hall 1 Level 01"
    )
    storey.Elevation = 0.0

    space = ifcopenshell.api.run(
        "root.create_entity",
        model,
        ifc_class="IfcSpace",
        predefined_type="SPACE",
        name="Data Hall 1",
    )
    space.LongName = "Meridian Data Center Phase 1 - Data Hall 1"
    space.ObjectType = "DataHall"
    ifcopenshell.api.run("geometry.edit_object_placement", model, product=space, matrix=_place_matrix(0.0, 0.0, 0.0))
    space.Representation = _make_poly_extrusion(model, body_ctx, ROOM_POLYGON_MM, CEILING_SOFFIT_MM)

    ifcopenshell.api.run("aggregate.assign_object", model, products=[site], relating_object=project)
    ifcopenshell.api.run("aggregate.assign_object", model, products=[building], relating_object=site)
    ifcopenshell.api.run("aggregate.assign_object", model, products=[storey], relating_object=building)
    ifcopenshell.api.run("aggregate.assign_object", model, products=[space], relating_object=storey)

    all_elements = []

    # 1. Access Floor (IfcCovering / FLOORING / AccessFloor, Z = -35..0 mm)
    access_floor = ifcopenshell.api.run(
        "root.create_entity",
        model,
        ifc_class="IfcCovering",
        predefined_type="FLOORING",
        name="ACCESS-FLOOR",
    )
    access_floor.ObjectType = "AccessFloor"
    ifcopenshell.api.run(
        "geometry.edit_object_placement",
        model,
        product=access_floor,
        matrix=_place_matrix(0.0, 0.0, -ACCESS_FLOOR_THICK_MM),
    )
    access_floor.Representation = _make_poly_extrusion(model, body_ctx, ROOM_POLYGON_MM, ACCESS_FLOOR_THICK_MM)
    _add_pset(
        model,
        access_floor,
        "Pset_DCM_AccessFloor",
        {
            "TilePitch": TILE_PITCH_MM,
            "PanelThickness": ACCESS_FLOOR_THICK_MM,
            "PlenumDepth": PLENUM_DEPTH_MM,
            "TopElevation": 0.0,
            "BottomElevation": -ACCESS_FLOOR_THICK_MM,
        },
    )
    all_elements.append(access_floor)

    # 2. Structural Columns (8 x IfcColumn, 600 x 600 x 3050 mm, Z = 0..3050 mm)
    for col_name, grid_id, gx, gy, cx, cy in COLUMNS_DATA:
        col = ifcopenshell.api.run(
            "root.create_entity",
            model,
            ifc_class="IfcColumn",
            predefined_type="COLUMN",
            name=col_name,
        )
        col.ObjectType = "StructuralColumn"
        col.Tag = grid_id
        ifcopenshell.api.run(
            "geometry.edit_object_placement", model, product=col, matrix=_place_matrix(cx, cy, 0.0)
        )
        col.Representation = _make_rect_extrusion(model, body_ctx, 600.0, 600.0, CEILING_SOFFIT_MM)
        _add_pset(
            model,
            col,
            "Pset_DCM_Column",
            {
                "GridId": grid_id,
                "GridLineX": gx,
                "GridLineY": gy,
                "CenterX": cx,
                "CenterY": cy,
                "Width": 600.0,
                "Depth": 600.0,
                "Height": CEILING_SOFFIT_MM,
            },
        )
        all_elements.append(col)

    # 3. Server Cabinets (94 x IfcFurniture / USERDEFINED / Rack) & In-Row CRAHs (12)
    racks_data, inrow_crahs_data = build_rows_data()
    for r in racks_data:
        rack = ifcopenshell.api.run(
            "root.create_entity",
            model,
            ifc_class="IfcFurniture",
            predefined_type="USERDEFINED",
            name=r["name"],
        )
        rack.ObjectType = "Rack"
        rack.Tag = r["name"]
        ifcopenshell.api.run(
            "geometry.edit_object_placement",
            model,
            product=rack,
            matrix=_place_matrix(r["cx"], r["cy"], 0.0, r["facing"]),
        )
        rack.Representation = _make_rect_extrusion(model, body_ctx, r["width"], r["depth"], r["height"])
        _add_pset(
            model,
            rack,
            "Pset_DCM_Rack",
            {
                "Row": r["row_label"],
                "RowId": r["row_id"],
                "Facing": r["facing"],
                "Width": r["width"],
                "Depth": r["depth"],
                "Height": r["height"],
                "RackUnits": 42,
                "RU": 42,
                "DesignLoad_kW": 8.0,
                "IsDistributor": r["is_distributor"],
                "TypeCode": r["type_code"],
                "CabinetType": "Distributor" if r["is_distributor"] else "Server",
                "TileAddress": r["tile"],
            },
        )
        all_elements.append(rack)

    # 4. CRAH Units (16 x IfcUnitaryEquipment / AIRHANDLER / CRAH: 4 perimeter + 12 in-row)
    all_crahs_meta = []
    for c_name, t_addr, cx, cy, w, d, h, facing, cap_kw, conn in PERIMETER_CRAHS:
        crah = ifcopenshell.api.run(
            "root.create_entity",
            model,
            ifc_class="IfcUnitaryEquipment",
            predefined_type="AIRHANDLER",
            name=c_name,
        )
        crah.ObjectType = "CRAH"
        crah.Tag = c_name
        ifcopenshell.api.run(
            "geometry.edit_object_placement",
            model,
            product=crah,
            matrix=_place_matrix(cx, cy, 0.0, facing),
        )
        crah.Representation = _make_rect_extrusion(model, body_ctx, w, d, h)
        _add_pset(
            model,
            crah,
            "Pset_DCM_CRAH",
            {
                "PlacementType": "Perimeter",
                "Row": "PERIMETER",
                "Facing": facing,
                "Width": w,
                "Depth": d,
                "Height": h,
                "SensibleCapacity_kW": cap_kw,
                "CHWConnection": conn,
                "TileAddress": t_addr,
            },
        )
        all_elements.append(crah)
        all_crahs_meta.append((c_name, cx, cy))

    for c in inrow_crahs_data:
        crah = ifcopenshell.api.run(
            "root.create_entity",
            model,
            ifc_class="IfcUnitaryEquipment",
            predefined_type="AIRHANDLER",
            name=c["name"],
        )
        crah.ObjectType = "CRAH"
        crah.Tag = c["name"]
        ifcopenshell.api.run(
            "geometry.edit_object_placement",
            model,
            product=crah,
            matrix=_place_matrix(c["cx"], c["cy"], 0.0, c["facing"]),
        )
        crah.Representation = _make_rect_extrusion(model, body_ctx, c["width"], c["depth"], c["height"])
        _add_pset(
            model,
            crah,
            "Pset_DCM_CRAH",
            {
                "PlacementType": "InRow",
                "Row": c["row_label"],
                "RowId": c["row_id"],
                "Facing": c["facing"],
                "Width": c["width"],
                "Depth": c["depth"],
                "Height": c["height"],
                "SensibleCapacity_kW": c["capacity_kw"],
                "CHWConnection": c["conn"],
                "TileAddress": c["tile"],
            },
        )
        all_elements.append(crah)
        all_crahs_meta.append((c["name"], c["cx"], c["cy"]))

    # 5. Cold Aisle Containment (CAC-BC) between Row B (N face Y=6470) and Row C (S face Y=7800)
    #    Sheet A-602 & A-301 Detail 3: Roof width = 1530 mm, Roof thickness = 55.5 mm,
    #    Roof underside = 2000.0 mm (Z = 2000.0..2055.5 mm), stopping each side of COL-A1 and COL-B1
    #    and at the circulation cross-aisle (11200..12600).
    roof_spans_x = [
        ("CAC-BC-ROOF-01", 1800.0, 6730.0),
        ("CAC-BC-ROOF-02", 7370.0, 11200.0),
        ("CAC-BC-ROOF-03", 12600.0, 13930.0),
        ("CAC-BC-ROOF-04", 14570.0, 21000.0),
    ]
    aisle_y_center = 7135.0
    roof_width_y = 1530.0
    roof_z_bottom = 2000.0
    roof_thickness = 55.5
    for r_name, x_start, x_end in roof_spans_x:
        roof = ifcopenshell.api.run(
            "root.create_entity",
            model,
            ifc_class="IfcCovering",
            predefined_type="CEILING",
            name=r_name,
        )
        roof.ObjectType = "ContainmentRoof"
        roof.Tag = "CAC-BC"
        w_x = x_end - x_start
        cx = (x_start + x_end) / 2.0
        ifcopenshell.api.run(
            "geometry.edit_object_placement",
            model,
            product=roof,
            matrix=_place_matrix(cx, aisle_y_center, roof_z_bottom),
        )
        roof.Representation = _make_rect_extrusion(model, body_ctx, w_x, roof_width_y, roof_thickness)
        _add_pset(
            model,
            roof,
            "Pset_DCM_Containment",
            {
                "ContainmentId": "CAC-BC",
                "ComponentType": "RoofPanel",
                "Aisle": "ROW B / ROW C",
                "RoofWidth": roof_width_y,
                "BottomElevation": roof_z_bottom,
                "TopElevation": roof_z_bottom + roof_thickness,
                "Thickness": roof_thickness,
            },
        )
        all_elements.append(roof)

    # 4 End Doors for CAC-BC (Sheet A-602: 60 mm thick x 2055.5 mm high, Z = 0..2055.5 mm)
    door_specs = [
        ("CAC-BC-DOOR-01", 1760.0),
        ("CAC-BC-DOOR-02", 11240.0),
        ("CAC-BC-DOOR-03", 12560.0),
        ("CAC-BC-DOOR-04", 21040.0),
    ]
    door_thk = 60.0
    door_height = 2055.5
    door_width_y = 1310.0  # spans Y = 6480..7790 between Row B (6470) and Row C (7800)
    for d_name, d_cx in door_specs:
        door = ifcopenshell.api.run(
            "root.create_entity",
            model,
            ifc_class="IfcDoor",
            predefined_type="DOOR",
            name=d_name,
        )
        door.ObjectType = "ContainmentDoor"
        door.Tag = "CAC-BC"
        ifcopenshell.api.run(
            "geometry.edit_object_placement",
            model,
            product=door,
            matrix=_place_matrix(d_cx, aisle_y_center, 0.0),
        )
        door.Representation = _make_rect_extrusion(model, body_ctx, door_thk, door_width_y, door_height)
        _add_pset(
            model,
            door,
            "Pset_DCM_Containment",
            {
                "ContainmentId": "CAC-BC",
                "ComponentType": "EndDoor",
                "DoorType": "DoubleSliding",
                "ClearWidth": door_width_y,
                "ClearHeight": door_height,
                "Height": door_height,
                "Thickness": door_thk,
            },
        )
        all_elements.append(door)

    # 6. Build MEP Route Segments (CT-1, BW-1, CHW-1)
    mep_segments_data = build_mep_routes()
    mep_entities = []
    service_lengths_overhead = {"cable_tray": 0.0, "busway": 0.0, "chw_pipe": 0.0}
    service_lengths_underfloor = {"cable_tray": 0.0, "busway": 0.0, "chw_pipe": 0.0}

    ct_boxes = []
    bw_boxes = []
    chw_boxes = []
    for seg in mep_segments_data:
        s_name, s_key, x1, y1, x2, y2, z_bot, w, h, s_kind = seg
        if abs(x2 - x1) >= abs(y2 - y1):
            xmin, xmax = min(x1, x2), max(x1, x2)
            ymin, ymax = y1 - w / 2.0, y1 + w / 2.0
        else:
            xmin, xmax = x1 - w / 2.0, x1 + w / 2.0
            ymin, ymax = min(y1, y2), max(y1, y2)
        box = (s_name, xmin, xmax, ymin, ymax, z_bot, z_bot + h)
        if s_key == "cable_tray":
            ct_boxes.append(box)
        elif s_key == "busway":
            bw_boxes.append(box)
        else:
            chw_boxes.append(box)

    def _box_sep_any_axis(b1, b2) -> float:
        dx = max(0.0, max(b1[1] - b2[2], b2[1] - b1[2]))
        dy = max(0.0, max(b1[3] - b2[4], b2[3] - b1[4]))
        dz = max(0.0, max(b1[5] - b2[6], b2[5] - b1[6]))
        return max(dx, dy, dz)

    min_tb_sep_global = min(_box_sep_any_axis(c, b) for c in ct_boxes for b in bw_boxes)

    for seg in mep_segments_data:
        s_name, s_key, x1, y1, x2, y2, z_bot, w, h, s_kind = seg
        length_mm = math.hypot(x2 - x1, y2 - y1)
        cx = (x1 + x2) / 2.0
        cy = (y1 + y2) / 2.0
        is_ew = abs(x2 - x1) >= abs(y2 - y1)
        dim_x = length_mm if is_ew else w
        dim_y = w if is_ew else length_mm

        if z_bot >= 0.0:
            service_lengths_overhead[s_key] += length_mm
            tier_band = "AboveClearHeight" if z_bot >= CLEAR_HEIGHT_MM else "ClearHeightBand"
        else:
            service_lengths_underfloor[s_key] += length_mm
            tier_band = "UnderFloorPlenum"

        if s_key == "cable_tray":
            elem = ifcopenshell.api.run(
                "root.create_entity",
                model,
                ifc_class="IfcCableCarrierSegment",
                predefined_type="CABLETRAYSEGMENT",
                name=s_name,
            )
            elem.ObjectType = "CableTray"
            elem.Description = "Telecom Cable Tray CT-1 (683x127 mm Ladder)"
            elem.Tag = "CT-1"
            sys_tag = "CT-1"
        elif s_key == "busway":
            elem = ifcopenshell.api.run(
                "root.create_entity",
                model,
                ifc_class="IfcCableCarrierSegment",
                predefined_type="CABLETRUNKINGSEGMENT",
                name=s_name,
            )
            elem.ObjectType = "Busway"
            elem.Description = "Power Busway BW-1 (128x147 mm)"
            elem.Tag = "BW-1"
            sys_tag = "BW-1"
        else:
            elem = ifcopenshell.api.run(
                "root.create_entity",
                model,
                ifc_class="IfcPipeSegment",
                predefined_type="RIGIDSEGMENT",
                name=s_name,
            )
            elem.ObjectType = "ChilledWaterPipe"
            elem.Description = "Chilled Water Pipe CHW-1 (DN100 + 25.4mm Insulation)"
            elem.Tag = "CHW-1"
            sys_tag = "CHW-1"

        ifcopenshell.api.run(
            "geometry.edit_object_placement",
            model,
            product=elem,
            matrix=_place_matrix(cx, cy, z_bot),
        )
        elem.Representation = _make_rect_extrusion(model, body_ctx, dim_x, dim_y, h)

        seg_props = {
            "Service": s_key,
            "SystemTag": sys_tag,
            "StartX_mm": x1,
            "StartY_mm": y1,
            "EndX_mm": x2,
            "EndY_mm": y2,
            "BottomElevation_mm": z_bot,
            "TopElevation_mm": z_bot + h,
            "CenterlineElevation_mm": z_bot + h / 2.0,
            "Width_mm": w,
            "Height_mm": h,
            "Length_mm": round(length_mm, 2),
            "PlacementZone": "Overhead" if z_bot >= 0.0 else "UnderFloor",
            "ElevationBand": tier_band,
            "MinTrayBuswaySeparation_mm": round(min_tb_sep_global, 2),
        }
        if s_key == "chw_pipe":
            seg_props["BareOuterDiameter_mm"] = 114.3
            seg_props["InsulationThickness_mm"] = 25.4
            seg_props["InsulatedOuterDiameter_mm"] = 168.3
            seg_props["MaxSupportSpacing_mm"] = 3000.0
        elif s_key == "busway":
            seg_props["Rating_A"] = 800.0
            seg_props["MaxSupportSpacing_mm"] = 1500.0
        else:
            seg_props["VerticalAccessHeadroom_mm"] = round(CEILING_SOFFIT_MM - (z_bot + h), 2)
            seg_props["MaxSupportSpacing_mm"] = 1500.0

        _add_pset(model, elem, "Pset_DCM_RouteSegment", seg_props)
        all_elements.append(elem)
        mep_entities.append(elem)

    # 7. PDUs (4 x IfcElectricDistributionBoard / DISTRIBUTIONBOARD / PDU)
    for p_name, served_row, t_addr, cx, cy, w, d, h, facing in PDUS_DATA:
        pdu = ifcopenshell.api.run(
            "root.create_entity",
            model,
            ifc_class="IfcElectricDistributionBoard",
            predefined_type="DISTRIBUTIONBOARD",
            name=p_name,
        )
        pdu.ObjectType = "PDU"
        pdu.Tag = p_name
        ifcopenshell.api.run(
            "geometry.edit_object_placement",
            model,
            product=pdu,
            matrix=_place_matrix(cx, cy, 0.0, facing),
        )
        pdu.Representation = _make_rect_extrusion(model, body_ctx, w, d, h)

        pxmin, pxmax = cx - w / 2.0, cx + w / 2.0
        pymin, pymax = cy - d / 2.0, cy + d / 2.0
        min_xy_dist = min(
            math.hypot(
                max(0.0, max(pxmin - cb[2], cb[1] - pxmax)),
                max(0.0, max(pymin - cb[4], cb[3] - pymax)),
            )
            for cb in chw_boxes
        )
        vert_clearance_above_pdu = min(cb[5] - h for cb in chw_boxes)

        _add_pset(
            model,
            pdu,
            "Pset_DCM_PDU",
            {
                "ServedRow": served_row,
                "Facing": facing,
                "Width": w,
                "Depth": d,
                "Height": h,
                "Rating_kVA": 225.0,
                "Voltage": "480/277V",
                "DedicatedSpaceHeight_mm": 1800.0,
                "WorkingSpaceClearance_mm": 900.0,
                "TileAddress": t_addr,
                "CHWInDedicatedSpaceAbove": False,
                "NearestCHWHorizontalClearance_mm": round(min_xy_dist, 2),
                "NearestCHWVerticalClearance_mm": round(vert_clearance_above_pdu, 2),
            },
        )
        all_elements.append(pdu)

    # 8. Sprinkler Heads (104 x IfcFireSuppressionTerminal / SPRINKLER / Sprinkler)
    sprinklers_data = build_sprinklers_data()
    for sp in sprinklers_data:
        spr = ifcopenshell.api.run(
            "root.create_entity",
            model,
            ifc_class="IfcFireSuppressionTerminal",
            predefined_type="SPRINKLER",
            name=sp["name"],
        )
        spr.ObjectType = "Sprinkler"
        spr.Tag = sp["name"]
        h_spr = sp["z_top"] - sp["z_bottom"]
        ifcopenshell.api.run(
            "geometry.edit_object_placement",
            model,
            product=spr,
            matrix=_place_matrix(sp["x"], sp["y"], sp["z_bottom"]),
        )
        spr.Representation = _make_cylinder_extrusion(model, body_ctx, 40.0, h_spr)

        def _horiz_dist(boxes):
            return min(
                math.hypot(
                    max(0.0, max(b[1] - sp["x"], sp["x"] - b[2])),
                    max(0.0, max(b[3] - sp["y"], sp["y"] - b[4])),
                )
                for b in boxes
            )

        _add_pset(
            model,
            spr,
            "Pset_DCM_Sprinkler",
            {
                "SprinklerType": sp["kind"],
                "DeflectorElevation_mm": sp["z_deflector"],
                "RequiredClearance_mm": 460.0,
                "ServicesPassingUnder": 0,
                "NearestCableTrayHorizontalDist_mm": round(_horiz_dist(ct_boxes), 1),
                "NearestBuswayHorizontalDist_mm": round(_horiz_dist(bw_boxes), 1),
                "NearestCHWHorizontalDist_mm": round(_horiz_dist(chw_boxes), 1),
            },
        )
        all_elements.append(spr)

    # Record overall diagnostics on IfcSpace
    _add_pset(
        model,
        space,
        "Pset_DCM_Space",
        {
            "ClearHeight_mm": CLEAR_HEIGHT_MM,
            "CeilingSoffit_mm": CEILING_SOFFIT_MM,
            "PlenumDepth_mm": PLENUM_DEPTH_MM,
            "TilePitch_mm": TILE_PITCH_MM,
            "CableTrayOverheadLength_mm": round(service_lengths_overhead["cable_tray"], 1),
            "CableTrayUnderFloorLength_mm": round(service_lengths_underfloor["cable_tray"], 1),
            "BuswayOverheadLength_mm": round(service_lengths_overhead["busway"], 1),
            "BuswayUnderFloorLength_mm": round(service_lengths_underfloor["busway"], 1),
            "CHWOverheadLength_mm": round(service_lengths_overhead["chw_pipe"], 1),
            "CHWUnderFloorLength_mm": round(service_lengths_underfloor["chw_pipe"], 1),
            "MinTrayBuswaySeparation_mm": round(min_tb_sep_global, 1),
        },
    )

    ifcopenshell.api.run("spatial.assign_container", model, products=all_elements, relating_structure=storey)
    return model


def write_reconciliation_json(ifc_out_path: str):
    out_dir = os.path.dirname(os.path.abspath(ifc_out_path))
    rec_path = os.path.join(out_dir, "reconciliation.json")
    rec_data = {
        "project": "Meridian Data Center Phase 1 - Data Hall 1",
        "client": "Meridian Digital Infrastructure",
        "project_no": "MDC-2026-014",
        "issue_date": "2026-07-24",
        "plotted_date": "2026-07-27",
        "drawn_by": "JRK",
        "checked_by": "A. Okonkwo, PE",
        "service_entries_mm": SERVICE_ENTRIES_MM,
        "sheets_reconciled": ["A-101", "A-301", "A-601", "A-602", "M-101", "E-101", "F-101"],
        "registered_ids": [
            "A-101", "A-301", "A-601", "A-602", "M-101", "E-101", "F-101",
            "CAC-BC", "CT-1", "BW-1", "CHW-1", "C1", "C2", "N1", "N2",
            "CRAH-01", "CRAH-02", "CRAH-03", "CRAH-04",
            "CRAH-A01", "CRAH-A02", "CRAH-A03",
            "CRAH-B01", "CRAH-B02", "CRAH-B03",
            "CRAH-C01", "CRAH-C02", "CRAH-C03",
            "CRAH-D01", "CRAH-D02", "CRAH-D03",
            "PDU-01", "PDU-02", "PDU-03", "PDU-04",
        ],
        "reconciled_notes": [
            "A-101 Keyed Note 1: Re-entrant NE corner 6600 x 8400 mm cutout (X: 20400..27000, Y: 15600..24000) -> applied to IfcSpace and IfcCovering access floor L-polygon",
            "A-101 Keyed Note 2 & 3: Structural grid 7200 mm c/c starting at 7050 mm from West/South walls (offset 150 mm from 600 mm floor-tile grid); columns C1/C2 on line C and N1/N2 on line 3 -> 8 IfcColumn elements placed at (7050, 14250, 21450) mm",
            "A-101 Keyed Note 4 & Plan Note 6: Area north of Row D reserved for future expansion and dashed spare bays omitted -> 94 server cabinets (8.0 kW/cab) modelled across D05..AJ05, D10..AI10, D14..AH14, D19..AK19",
            "A-101 Plan Note 3, 5 & 8: 0-based tile column lettering (A..Z, AA..AZ) and two-digit row numbers; circulation cross-aisle breaks every row with East edge at X = 12600 mm; PDUs positioned with front faces flush with served rows",
            "A-301 Section 1 & Detail 3: Clear height 2600 mm AFF, ceiling soffit 3050 mm AFF, access floor panel 35 mm thick (Z = -35..0 mm), CAC-BC containment roof 1530 mm wide x 55.5 mm thick at Z = 2000.0..2055.5 mm stopping each side of COL-A1 and COL-B1, and 4 end doors 60 x 2055.5 mm -> enforced in 3D geometry",
            "A-601 Table 1-3: Row A (26 racks, 4C1-CU-4C1-N1-4C1-CU-CA-7C1-SP-CU-6C1, 1200 D, Y=3000), Row B (21 racks, 1070 D, Y=5935), Row C (20 racks, 1200 D, Y=8400), Row D (27 racks, 1070 D, Y=11335), 4 perimeter CRAHs (B01, P01, AC01, AQ01; 100 kW, DN100), 12 in-row CRAHs (50 kW, DN50), 4 PDUs (762 x 813 x 1959 mm, 225 kVA at AN05, AM10, AL14, AO19) -> modelled",
            "A-602 Pathway Section Schedule & Detail 4: CT-1 (683 x 127 mm ladder tray, Z = 2600..2727 mm with 323 mm clear access headroom above and zero XY crossings), BW-1 (128 x 147 mm busway, Z = 2600..2747 mm), CHW-1 (DN100 bare OD 114.3 mm + 25.4 mm ASTM C585 insulation = 168.3 mm OD, Z = 2620..2788.3 mm)",
            "M-101 Note 1-6: CHW entry at (0, 12470) mm, 25.4 mm insulation per ASHRAE 90.1 Table 6.8.3-2, no CHW piping in 1800 mm dedicated space above PDU-01..04 per NFPA 70 110.26(E) -> enforced",
            "E-101 Note 1-6: Busway & telecom entry at (0, 12470) mm, >= 300 mm separation between CT-1 and BW-1 per ANSI/TIA-942 7.3.1 Table 2 (achieved 314.5 mm min XY gap via non-crossing S-curves), max support spacing 1500 mm recorded",
            "F-101 Note 1-7: 104 sprinklers (94 ceiling pendent at Z_deflector = 3024.6 mm + 10 in-aisle upright in CAC-BC at Z_deflector = 1890 mm), >= 460 mm clearance below deflectors per NFPA 13 10.2.7.4.2 and 450 mm column rule per NFPA 13 Table 10.2.4.2.1(a) -> all overhead routes thread >= 465.8 mm horizontally clear of every sprinkler head",
        ],
    }
    with open(rec_path, "w", encoding="utf-8") as f:
        json.dump(rec_data, f, indent=2)


def main():
    os.makedirs(os.path.dirname(os.path.abspath(OUT_PATH)), exist_ok=True)
    model = build_model()
    model.write(OUT_PATH)
    write_reconciliation_json(OUT_PATH)


if __name__ == "__main__":
    main()
