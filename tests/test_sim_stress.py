"""Fast (<5s, $0 cost) local two-layer simulation and replay stress-test suite."""

from __future__ import annotations

import asyncio
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch
from xml.sax.saxutils import escape as xml_escape

import httpx
import openai

import harness
import serve
import taskbridge

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_audit(workdir: Path, env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Run serve._PROGRAMMATIC_AUDIT_SCRIPT in a temp workdir and parse its JSON report."""
    proc = subprocess.run(
        [sys.executable, "-"],
        input=serve._build_programmatic_audit_script(),
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith(serve.AUDIT_MARKER))
    return json.loads(line[len(serve.AUDIT_MARKER) :])


def _write_mock_ifcopenshell_pkg(pkgdir: Path) -> Dict[str, str]:
    """Create a scene-JSON-driven mock ifcopenshell + geom.tree BVH package."""
    (pkgdir / "ifcopenshell").mkdir(parents=True, exist_ok=True)
    fake_ifc = textwrap.dedent(
        """
        import builtins
        import json

        class _Entity:
            def __init__(self, eid, cls, gid, name="", obj_type="", repr_=True):
                self._eid = eid
                self._cls = cls
                self.GlobalId = gid
                self.Name = name
                self.ObjectType = obj_type
                self.Representation = repr_
                self.IsDecomposedBy = []
                self.ContainsElements = []

            def id(self):
                return self._eid

            def is_a(self, target=None):
                return self._cls == target if target else self._cls

            def __str__(self):
                return f"#{self._eid}={self._cls}"

        class _RelConnects:
            def __init__(self, e1, e2):
                self.RelatingElement = e1
                self.RelatedElement = e2

        class _Model:
            def __init__(self, path):
                with builtins.open(path, "r", encoding="utf-8") as fh:
                    raw = fh.read()
                tok = "/* SCENE_JSON:"
                i0 = raw.find(tok)
                if i0 < 0:
                    scene = {}
                else:
                    i1 = raw.find("*/", i0 + len(tok))
                    scene = json.loads(raw[i0 + len(tok) : i1].strip())
                self._elems = {}
                self.buildings = []
                self.products = []
                for e in scene.get("elements", []):
                    ent = _Entity(
                        e["id"],
                        e["cls"],
                        e["gid"],
                        e.get("name", ""),
                        e.get("obj_type", ""),
                        bool(e.get("repr", True)),
                    )
                    ent._box = tuple(e["box"]) if "box" in e else None
                    self._elems[e["id"]] = ent
                    self.products.append(ent)
                for b in scene.get("buildings", []):
                    bent = _Entity(
                        b["id"],
                        "IfcBuilding",
                        b["gid"],
                        b.get("name", ""),
                        repr_=False,
                    )
                    children = [self._elems[cid] for cid in b.get("element_ids", []) if cid in self._elems]
                    bent.ContainsElements = [
                        type("_RelC", (), {"RelatedElements": children})()
                    ]
                    self.buildings.append(bent)
                    self.products.append(bent)
                self.connects = []
                for id1, id2 in scene.get("connects", []):
                    if id1 in self._elems and id2 in self._elems:
                        self.connects.append(_RelConnects(self._elems[id1], self._elems[id2]))

            def by_type(self, name):
                if name == "IfcBuilding":
                    return list(self.buildings)
                if name == "IfcProduct":
                    return list(self.products)
                if name == "IfcRelConnectsElements":
                    return list(self.connects)
                return []

        def open(path):
            return _Model(path)
        """
    ).lstrip()

    fake_geom = textwrap.dedent(
        """
        class settings:
            USE_WORLD_COORDS = "use-world-coords"

            def set(self, k, v):
                pass

        class _Shape:
            def __init__(self, eid, gid, verts, faces, box):
                self.id = eid
                self.guid = gid
                self.geometry = type("_Geo", (), {"verts": verts, "faces": faces})()
                self._box = box

        def create_shape(_settings, elem):
            box = getattr(elem, "_box", None)
            if box is None:
                raise ValueError("element has no bounding box")
            x0, x1, y0, y1, z0, z1 = box
            verts = [
                x0, y0, z0, x1, y0, z0, x1, y1, z0, x0, y1, z0,
                x0, y0, z1, x1, y0, z1, x1, y1, z1, x0, y1, z1,
            ]
            faces = [
                0, 1, 2, 0, 2, 3,
                4, 5, 6, 4, 6, 7,
                0, 1, 5, 0, 5, 4,
                1, 2, 6, 1, 6, 5,
                2, 3, 7, 2, 7, 6,
                3, 0, 4, 3, 4, 7,
            ]
            return _Shape(elem.id(), elem.GlobalId, verts, faces, box)

        class tree:
            def __init__(self):
                self._items = {}

            def add_element(self, shape):
                self._items[shape.id] = shape

            @staticmethod
            def _touches_or_overlaps(b1, b2, tol=1e-6):
                return (
                    min(b1[1], b2[1]) - max(b1[0], b2[0]) >= -tol
                    and min(b1[3], b2[3]) - max(b1[2], b2[2]) >= -tol
                    and min(b1[5], b2[5]) - max(b1[4], b2[4]) >= -tol
                )

            def select(self, elem):
                s1 = self._items.get(elem.id())
                if s1 is None:
                    return []
                return [
                    eid
                    for eid, s2 in self._items.items()
                    if eid != s1.id and self._touches_or_overlaps(s1._box, s2._box)
                ]

            def clash_intersection_many(self, set_a, set_b, tolerance=0.0, check_all=True, allow_touching=False):
                out = []
                b_ids = {e.id() for e in set_b}
                for ea in set_a:
                    s1 = self._items.get(ea.id())
                    if s1 is None:
                        continue
                    for eid_b in b_ids:
                        if eid_b == s1.id:
                            continue
                        s2 = self._items.get(eid_b)
                        if s2 and self._touches_or_overlaps(s1._box, s2._box):
                            out.append((s1.id, s2.id))
                return out
        """
    ).lstrip()

    (pkgdir / "ifcopenshell" / "__init__.py").write_text(fake_ifc, encoding="utf-8")
    (pkgdir / "ifcopenshell" / "geom.py").write_text(fake_geom, encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(pkgdir) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def _col_name(c_idx: int) -> str:
    """Convert 0-based column index to Excel letters (0 -> A, 25 -> Z, 26 -> AA)."""
    out: List[str] = []
    n = c_idx + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out.append(chr(ord("A") + rem))
    return "".join(reversed(out))


def _make_ooxml_xlsx(sheets: Dict[str, List[List[Any]]]) -> bytes:
    """Build a valid multi-sheet OOXML .xlsx archive using stdlib XML/zipfile only."""
    shared_list: List[str] = []
    shared_idx: Dict[str, int] = {}

    def _intern(s: str) -> int:
        if s not in shared_idx:
            shared_idx[s] = len(shared_list)
            shared_list.append(s)
        return shared_idx[s]

    sheet_entries: List[tuple[str, str]] = []
    for s_num, (sname, rows) in enumerate(sheets.items(), start=1):
        row_xml: List[str] = []
        for r_idx, row in enumerate(rows, start=1):
            cells_xml: List[str] = []
            for c_idx, cell in enumerate(row):
                if cell is None:
                    continue
                ref = f"{_col_name(c_idx)}{r_idx}"
                if isinstance(cell, dict):
                    formula = str(cell.get("formula") or "")
                    f_body = formula[1:] if formula.startswith("=") else formula
                    ctype = str(cell.get("type") or "")
                    val = cell.get("value")
                    f_xml = f"<f>{xml_escape(f_body)}</f>" if formula else ""
                    v_xml = f"<v>{xml_escape(str(val))}</v>" if val is not None else ""
                    t_attr = f' t="{xml_escape(ctype)}"' if ctype else ""
                    cells_xml.append(f'<c r="{ref}"{t_attr}>{f_xml}{v_xml}</c>')
                elif isinstance(cell, (int, float)) and not isinstance(cell, bool):
                    cells_xml.append(f'<c r="{ref}"><v>{cell}</v></c>')
                else:
                    si = _intern(str(cell))
                    cells_xml.append(f'<c r="{ref}" t="s"><v>{si}</v></c>')
            row_xml.append(f'<row r="{r_idx}">{"".join(cells_xml)}</row>')
        ws_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheetData>{"".join(row_xml)}</sheetData>'
            "</worksheet>"
        )
        sheet_entries.append((sname, ws_xml))

    si_xml = "".join(f'<si><t xml:space="preserve">{xml_escape(s)}</t></si>' for s in shared_list)
    sst_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        f'count="{len(shared_list)}" uniqueCount="{len(shared_list)}">{si_xml}</sst>'
    )
    wb_sheets = "".join(
        f'<sheet name="{xml_escape(sname)}" sheetId="{idx}" r:id="rId{idx}"/>'
        for idx, (sname, _) in enumerate(sheet_entries, start=1)
    )
    wb_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheets>{wb_sheets}</sheets>"
        "</workbook>"
    )
    rels_items = "".join(
        f'<Relationship Id="rId{idx}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        f'Target="worksheets/sheet{idx}.xml"/>'
        for idx in range(1, len(sheet_entries) + 1)
    ) + (
        f'<Relationship Id="rId{len(sheet_entries) + 1}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" '
        'Target="sharedStrings.xml"/>'
    )
    wb_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{rels_items}</Relationships>"
    )
    root_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    ct_sheets = "".join(
        f'<Override PartName="/xl/worksheets/sheet{idx}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for idx in range(1, len(sheet_entries) + 1)
    )
    ct_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-officedocument.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/sharedStrings.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
        f"{ct_sheets}</Types>"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", ct_xml)
        zf.writestr("_rels/.rels", root_rels_xml)
        zf.writestr("xl/workbook.xml", wb_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", wb_rels_xml)
        zf.writestr("xl/sharedStrings.xml", sst_xml)
        for idx, (_, ws_xml) in enumerate(sheet_entries, start=1):
            zf.writestr(f"xl/worksheets/sheet{idx}.xml", ws_xml)
    return buf.getvalue()


class _LocalReplaySession:
    """Async session double backed by a real temp directory and real programmatic audit."""

    mcp_servers: List[Dict[str, Any]] = []
    limits = {"exec_timeout_sec": 60}

    def __init__(self, workdir: Path, instruction: str = "Validate deliverables.") -> None:
        self.workdir = str(workdir)
        self._root = workdir
        self.instruction = instruction
        self.logs: List[str] = []
        self.usage_records: List[Dict[str, Any]] = []
        self.final_message = ""
        self.cancelled = asyncio.Event()

    def time_left(self) -> float:
        return 3600.0

    def _map_path(self, path: str) -> Path:
        norm = path.replace("\\", "/")
        root_norm = str(self._root).replace("\\", "/").rstrip("/")
        if norm.startswith(root_norm + "/"):
            rel = norm[len(root_norm) + 1 :]
        elif "/" in norm and (norm.startswith("/") or ":" in norm.split("/")[0]):
            rel = Path(norm).name
        else:
            rel = norm
        return self._root / rel

    async def exec(self, command: str, timeout_sec: int = 120) -> Dict[str, Any]:
        is_audit = "__PLE_AUDIT__" in command
        res = subprocess.run(
            [sys.executable, "-"] if is_audit else ["cmd", "/c", "echo ok"],
            input=serve._build_programmatic_audit_script() if is_audit else None,
            cwd=self._root,
            capture_output=True,
            text=True,
        )
        return {"exit_code": res.returncode, "output": res.stdout + res.stderr, "truncated": False}

    async def write_file(self, path: str, data: Any, append: bool = False) -> int:
        target = self._map_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        target.write_bytes((target.read_bytes() + raw) if append and target.exists() else raw)
        return len(raw)

    async def read_file(self, path: str) -> bytes:
        target = self._map_path(path)
        if not target.exists():
            raise taskbridge.TaskError(f"missing: {path}")
        return target.read_bytes()

    async def log(self, text: str) -> None:
        self.logs.append(text)

    async def usage(self, model: str, inp: int, out: int, cached: int = 0) -> None:
        self.usage_records.append({"model": model, "inp": inp, "out": out, "cached": cached})


# ---------------------------------------------------------------------------
# Layer 1: Synthetic Failure Trap Fixtures
# ---------------------------------------------------------------------------
class TestLayer1SyntheticFailureFixtures(unittest.TestCase):
    def test_mep_ifc_trap_fixture_catches_all_defects_and_clean_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env = _write_mock_ifcopenshell_pkg(tmp_path / "pkgs")

            bad_scene = {
                "elements": [
                    {"id": 1, "cls": "IfcSlab", "gid": "SLAB_1", "name": "FLOOR-SLAB", "box": [0.0, 20.0, 0.0, 20.0, -0.05, 0.0]},
                    {"id": 2, "cls": "IfcElectricDistributionBoard", "gid": "PDU_1", "name": "PDU-01", "obj_type": "PDU", "box": [14.0, 15.0, 1.0, 2.0, 0.0, 2.0]},
                    # 0mm butt-joint connected via IfcRelConnectsElements (must NOT be exempted)
                    {"id": 3, "cls": "IfcPipeSegment", "gid": "CHW_1", "name": "CHW-SUP-01", "box": [0.0, 2.0, 2.0, 2.2, 2.80, 2.95]},
                    {"id": 4, "cls": "IfcPipeSegment", "gid": "CHW_2", "name": "CHW-SUP-02", "box": [2.0, 4.0, 2.0, 2.2, 2.80, 2.95]},
                    # 1.2mm micro-clash (< 2mm) between two MEP conduits
                    {"id": 5, "cls": "IfcFlowSegment", "gid": "COND_A", "name": "CONDUIT-A", "box": [5.0, 7.0, 2.0, 2.1, 2.80, 2.90]},
                    {"id": 6, "cls": "IfcFlowSegment", "gid": "COND_B", "name": "CONDUIT-B", "box": [7.0012, 9.0, 2.0, 2.1, 2.80, 2.90]},
                    # 0.5mm micro-clash (< 1mm) between MEP duct and structural column
                    {"id": 7, "cls": "IfcDuctSegment", "gid": "DUCT_1", "name": "DUCT-01", "box": [1.0, 2.0, 4.0, 4.4, 2.80, 3.00]},
                    {"id": 8, "cls": "IfcColumn", "gid": "COL_B2", "name": "COL-B2", "box": [1.0, 1.5, 4.4005, 4.8, 0.0, 4.0]},
                    # <300mm tray-stack vertical headroom (170mm) using class detection without "tray" in Name
                    {"id": 9, "cls": "IfcCableCarrierSegment", "gid": "CT_LOW", "name": "CT-LOWER", "box": [8.0, 10.0, 6.0, 6.5, 2.85, 2.95]},
                    {"id": 10, "cls": "IfcCableCarrierSegment", "gid": "CT_UP", "name": "CT-UPPER", "box": [8.0, 10.0, 6.0, 6.5, 3.12, 3.22]},
                    # <2600mm overhead clear height
                    {"id": 11, "cls": "IfcFireSuppressionTerminal", "gid": "SPR_4", "name": "SPR-04", "obj_type": "Sprinkler", "box": [11.0, 11.2, 8.0, 8.2, 2.42, 2.52]},
                    {"id": 12, "cls": "IfcPipeSegment", "gid": "CHW_LOW", "name": "CHW-LOW", "box": [12.0, 13.0, 8.0, 8.2, 2.35, 2.50]},
                ],
                "connects": [[3, 4]],
            }
            bad_dir = tmp_path / "mep_bad_dir"
            bad_dir.mkdir()
            (bad_dir / "mep_trap.ifc").write_text(
                f"ISO-10303-21;\n/* SCENE_JSON: {json.dumps(bad_scene)} */\nEND-ISO-10303-21;",
                encoding="utf-8",
            )
            bad = _run_audit(bad_dir, env=env)
            bad_errs = "\n".join(bad["errors"])
            bad_warns = "\n".join(bad["warnings"])

            self.assertIn("CHW-SUP-01", bad_errs)
            self.assertIn("CHW-SUP-02", bad_errs)
            self.assertIn("CONDUIT-A", bad_errs)
            self.assertIn("CONDUIT-B", bad_errs)
            self.assertIn("DUCT-01", bad_errs)
            self.assertIn("COL-B2", bad_errs)
            self.assertNotIn("PDU-01", bad_errs)
            self.assertIn("tray-stack vertical headroom 170.0mm < 300mm", bad_warns)
            self.assertIn("overhead clear height 2420.0mm < 2600mm", bad_warns)
            self.assertIn("overhead clear height 2350.0mm < 2600mm", bad_warns)

            clean_scene = {
                "elements": [
                    {"id": 1, "cls": "IfcSlab", "gid": "SLAB_1", "name": "FLOOR-SLAB", "box": [0.0, 20.0, 0.0, 20.0, -0.05, 0.0]},
                    {"id": 2, "cls": "IfcElectricDistributionBoard", "gid": "PDU_1", "name": "PDU-01", "obj_type": "PDU", "box": [14.0, 15.0, 1.0, 2.0, 0.0, 2.0]},
                    {"id": 3, "cls": "IfcPipeSegment", "gid": "CHW_1", "name": "CHW-SUP-01", "box": [0.0, 2.0, 2.0, 2.2, 2.80, 2.95]},
                    {"id": 4, "cls": "IfcPipeSegment", "gid": "CHW_2", "name": "CHW-SUP-02", "box": [2.008, 4.0, 2.0, 2.2, 2.80, 2.95]},
                    {"id": 5, "cls": "IfcFlowSegment", "gid": "COND_A", "name": "CONDUIT-A", "box": [5.0, 7.0, 2.0, 2.1, 2.80, 2.90]},
                    {"id": 6, "cls": "IfcFlowSegment", "gid": "COND_B", "name": "CONDUIT-B", "box": [7.008, 9.0, 2.0, 2.1, 2.80, 2.90]},
                    {"id": 7, "cls": "IfcDuctSegment", "gid": "DUCT_1", "name": "DUCT-01", "box": [1.0, 2.0, 4.0, 4.4, 2.80, 3.00]},
                    {"id": 8, "cls": "IfcColumn", "gid": "COL_B2", "name": "COL-B2", "box": [1.0, 1.5, 4.45, 4.8, 0.0, 4.0]},
                    {"id": 9, "cls": "IfcCableCarrierSegment", "gid": "CT_LOW", "name": "CT-LOWER", "box": [8.0, 10.0, 6.0, 6.5, 2.85, 2.95]},
                    {"id": 10, "cls": "IfcCableCarrierSegment", "gid": "CT_UP", "name": "CT-UPPER", "box": [8.0, 10.0, 6.0, 6.5, 3.30, 3.40]},
                    {"id": 11, "cls": "IfcFireSuppressionTerminal", "gid": "SPR_4", "name": "SPR-04", "obj_type": "Sprinkler", "box": [11.0, 11.2, 8.0, 8.2, 2.75, 2.85]},
                    {"id": 12, "cls": "IfcPipeSegment", "gid": "CHW_LOW", "name": "CHW-LOW", "box": [12.0, 13.0, 8.0, 8.2, 2.75, 2.90]},
                ],
                "connects": [[3, 4]],
            }
            clean_dir = tmp_path / "mep_clean_dir"
            clean_dir.mkdir()
            (clean_dir / "mep_clean.ifc").write_text(
                f"ISO-10303-21;\n/* SCENE_JSON: {json.dumps(clean_scene)} */\nEND-ISO-10303-21;",
                encoding="utf-8",
            )
            clean = _run_audit(clean_dir, env=env)
            self.assertEqual(clean["errors"], [])
            self.assertEqual(clean["warnings"], [])

    def test_site_ifc_road_building_overlap_trap_and_clean_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env = _write_mock_ifcopenshell_pkg(tmp_path / "pkgs")

            bad_scene = {
                "buildings": [{"id": 1, "gid": "BLDG_DH01", "name": "DH-01", "element_ids": [2]}],
                "elements": [
                    {"id": 2, "cls": "IfcSlab", "gid": "DH01_SLAB", "name": "DH01-SLAB", "box": [10.0, 30.0, 10.0, 25.0, 0.0, 6.0]},
                    {"id": 3, "cls": "IfcRoad", "gid": "ROAD_N", "name": "ACCESS-ROAD-N", "box": [0.0, 35.0, 23.0, 29.0, 0.0, 0.2]},
                ],
            }
            bad_dir = tmp_path / "site_bad"
            bad_dir.mkdir()
            (bad_dir / "site_trap.ifc").write_text(
                f"ISO-10303-21;\n/* SCENE_JSON: {json.dumps(bad_scene)} */\nEND-ISO-10303-21;",
                encoding="utf-8",
            )
            bad = _run_audit(bad_dir, env=env)
            self.assertIn("road 'ACCESS-ROAD-N' intersects building 'DH-01 (site_trap.ifc)'", "\n".join(bad["errors"]))

            clean_scene = {
                "buildings": [{"id": 1, "gid": "BLDG_DH01", "name": "DH-01", "element_ids": [2]}],
                "elements": [
                    {"id": 2, "cls": "IfcSlab", "gid": "DH01_SLAB", "name": "DH01-SLAB", "box": [10.0, 30.0, 10.0, 25.0, 0.0, 6.0]},
                    {"id": 3, "cls": "IfcRoad", "gid": "ROAD_N", "name": "ACCESS-ROAD-N", "box": [0.0, 35.0, 28.0, 34.0, 0.0, 0.2]},
                ],
            }
            clean_dir = tmp_path / "site_clean"
            clean_dir.mkdir()
            (clean_dir / "site_clean.ifc").write_text(
                f"ISO-10303-21;\n/* SCENE_JSON: {json.dumps(clean_scene)} */\nEND-ISO-10303-21;",
                encoding="utf-8",
            )
            clean = _run_audit(clean_dir, env=env)
            self.assertEqual(clean["errors"], [])
            self.assertEqual(clean["warnings"], [])

    def test_takeoff_csv_and_xlsx_traps_catch_all_defects_and_clean_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bad_dir = tmp_path / "takeoff_bad"
            bad_dir.mkdir()

            (bad_dir / "device_takeoff.csv").write_text(
                "register,device_id,qty,kw,cost_usd,voltage_v\n"
                "REG-01,DEV-01,10,5,100,480\n"
                "REG-02,,20,10,200,480\n"
                "REG-03,DEV-03,30,15,300,480,SHIFTED_EXTRA\n"
                "REG-04,DEV-04,15,8,NaN,480\n"
                "REG-05,DEV-05,25,12,#REF!,480\n"
                "REG-06,DEV-06,10,5,50,480\n"
                "TOTAL,,95,22,400,480\n",
                encoding="utf-8",
            )
            bad_xlsx = _make_ooxml_xlsx(
                {
                    "Takeoff": [
                        ["register_id", "qty", "cost_usd", "voltage_v"],
                        ["REG-101", 10, 1000, 480],
                        ["", 20, {"formula": "=SUM(#REF!)", "value": "#REF!", "type": "e"}, 480],
                        ["REG-103", "NaN", 500, 480, "SHIFTED"],
                        ["Grand Total", 45, 2000, 480],
                    ]
                }
            )
            (bad_dir / "device_takeoff.xlsx").write_bytes(bad_xlsx)

            bad = _run_audit(bad_dir)
            errs = "\n".join(bad["errors"])

            self.assertIn("device_takeoff.csv: shifted/malformed CSV columns", errs)
            self.assertIn("device_takeoff.csv: invalid numeric/formula token 'NaN'", errs)
            self.assertIn("device_takeoff.csv: invalid numeric/formula token '#REF!'", errs)
            self.assertIn("device_takeoff.csv: column 'device_id' has missing/placeholder IDs", errs)
            self.assertIn("device_takeoff.csv: summary total mismatch in column 'qty'", errs)
            self.assertIn("device_takeoff.csv: summary total mismatch in column 'kw'", errs)

            self.assertIn("device_takeoff.xlsx[Takeoff]: shifted/malformed XLSX columns", errs)
            self.assertIn("Excel error/NaN token 'SUM(#REF!)'", errs)
            self.assertIn("Excel error/NaN token '#REF!'", errs)
            self.assertIn("Excel error/NaN token 'NaN'", errs)
            self.assertIn("device_takeoff.xlsx[Takeoff]: column 'register_id' has missing/placeholder IDs", errs)
            self.assertIn("device_takeoff.xlsx[Takeoff]: summary total mismatch in column 'qty'", errs)

            clean_dir = tmp_path / "takeoff_clean"
            clean_dir.mkdir()
            (clean_dir / "device_takeoff.csv").write_text(
                "register,device_id,qty,kw,voltage_v\n"
                "REG-01,DEV-01,10,5,480\n"
                "REG-02,DEV-02,20,10,480\n"
                "REG-03,DEV-03,30,15,480\n"
                "TOTAL,,60,30,480\n",
                encoding="utf-8",
            )
            clean_xlsx = _make_ooxml_xlsx(
                {
                    "Takeoff": [
                        ["register_id", "qty", "cost_usd", "voltage_v"],
                        ["REG-101", 10, 1000, 480],
                        ["REG-102", 20, 2000, 480],
                        ["Grand Total", {"formula": "=SUM(B2:B3)", "value": 30, "type": "n"}, 3000, 480],
                    ]
                }
            )
            (clean_dir / "device_takeoff.xlsx").write_bytes(clean_xlsx)

            clean = _run_audit(clean_dir)
            self.assertEqual(clean["errors"], [])
            self.assertEqual(clean["warnings"], [])


# ---------------------------------------------------------------------------
# Layer 2: Scripted Full Agent-Loop & Resilience Simulations
# ---------------------------------------------------------------------------
class TestLayer2ScriptedAgentLoopSimulation(unittest.TestCase):
    def test_truncated_verifier_premature_pass_blocked_and_triggers_fix(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            (workdir / "takeoff.csv").write_text(
                "device_id,register,qty\n"
                "D1,,10\n"
                "D2,REG-2,20\n"
                "TOTAL,,99\n",
                encoding="utf-8",
            )
            fixed_csv = "device_id,register,qty\nD1,REG-1,10\nD2,REG-2,20\nTOTAL,,30\n"

            class _TruncVerifierClient:
                def __init__(self) -> None:
                    self.verify_rounds = 0
                    self.fix_prompts: List[str] = []
                    self.fixed = False
                    self.chat = self
                    self.completions = self

                async def create(self, **kwargs: Any) -> serve._DryRunObject:
                    msgs = kwargs["messages"]
                    sys_msg = str(msgs[0].get("content") or "")
                    user_texts = [str(m.get("content") or "") for m in msgs if m.get("role") == "user"]
                    tool_msgs = [m for m in msgs if m.get("role") == "tool"]

                    def _reply(content: Optional[str], calls: Optional[List[Dict[str, Any]]] = None):
                        m: Dict[str, Any] = {"role": "assistant", "content": content}
                        if calls:
                            m["tool_calls"] = calls
                        return serve._DryRunObject({"choices": [{"finish_reason": "stop", "message": m}]})

                    if sys_msg.startswith("You plan an engineering task"):
                        return _reply("Plan: inspect and verify takeoff.csv.")

                    if sys_msg.startswith("You check an engineer's work"):
                        if not tool_msgs and len(user_texts) == 1:
                            self.verify_rounds += 1
                        if self.verify_rounds == 1:
                            if kwargs.get("tool_choice") == "none":
                                return _reply("PASS")
                            return _reply(
                                None,
                                [
                                    {
                                        "id": f"v_read_{len(tool_msgs)}",
                                        "type": "function",
                                        "function": {
                                            "name": "read_file",
                                            "arguments": json.dumps({"path": "takeoff.csv"}),
                                        },
                                    }
                                ],
                            )
                        return _reply("PASS")

                    if any("A reviewer checked your files" in t for t in user_texts):
                        last_msg = msgs[-1]
                        if last_msg.get("role") == "user" and str(last_msg.get("content") or "").startswith(
                            "A reviewer checked your files"
                        ):
                            self.fix_prompts.append(str(last_msg.get("content") or ""))
                        if not self.fixed:
                            self.fixed = True
                            return _reply(
                                None,
                                [
                                    {
                                        "id": "fix_write",
                                        "type": "function",
                                        "function": {
                                            "name": "write_file",
                                            "arguments": json.dumps({"path": "takeoff.csv", "content": fixed_csv}),
                                        },
                                    }
                                ],
                            )
                        return _reply("Fixed takeoff.csv completely.")

                    return _reply("Initial draft left takeoff.csv unfixed.")

            sess = _LocalReplaySession(workdir, "Fix takeoff.csv.")
            client = _TruncVerifierClient()
            with patch.dict(
                os.environ,
                {
                    "MODEL_NAME": "gemini-4-argon",
                    "OPENAI_BASE_URL": "http://proxy.local/v1",
                    "OPENAI_API_KEY": "k",
                    "MAX_FIX_ROUNDS": "2",
                    "MAX_VERIFY_TURNS": "3",
                    "MAX_FIX_TURNS": "5",
                },
                clear=False,
            ):
                ans = asyncio.run(
                    serve.solve(
                        sess,  # type: ignore[arg-type]
                        client=client,
                        config_path=str(REPO_ROOT / "submission.yaml"),
                    )
                )

            self.assertEqual(ans, "Fixed takeoff.csv completely.")
            self.assertEqual(client.verify_rounds, 2)
            self.assertTrue(any("verifier: truncated=True" in entry for entry in sess.logs))
            self.assertEqual(len(client.fix_prompts), 1)
            self.assertIn("missing/placeholder IDs", client.fix_prompts[0])
            self.assertIn("summary total mismatch in column 'qty'", client.fix_prompts[0])
            self.assertIn("Verifier ran out of turns/time", client.fix_prompts[0])
            self.assertEqual(_run_audit(workdir)["errors"], [])

    def test_persisting_audit_errors_after_fix1_stay_blocked_and_pass_exact_errors_to_fix2(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            (workdir / "takeoff.csv").write_text(
                "device_id,register,qty,kw\n"
                "D1,,10,5\n"
                "D2,REG-2,20,10\n"
                "D3,REG-3,30,#REF!\n"
                "TOTAL,,99,15\n",
                encoding="utf-8",
            )
            partial_fix_csv = (
                "device_id,register,qty,kw\n"
                "D1,REG-1,10,5\n"
                "D2,REG-2,20,10\n"
                "D3,REG-3,30,15\n"
                "TOTAL,,99,30\n"
            )
            final_clean_csv = (
                "device_id,register,qty,kw\n"
                "D1,REG-1,10,5\n"
                "D2,REG-2,20,10\n"
                "D3,REG-3,30,15\n"
                "TOTAL,,60,30\n"
            )

            class _MultiFixClient:
                def __init__(self) -> None:
                    self.verify_rounds = 0
                    self.fix_prompts: List[str] = []
                    self.fix_write_stage = 0
                    self.chat = self
                    self.completions = self

                async def create(self, **kwargs: Any) -> serve._DryRunObject:
                    msgs = kwargs["messages"]
                    sys_msg = str(msgs[0].get("content") or "")
                    user_texts = [str(m.get("content") or "") for m in msgs if m.get("role") == "user"]
                    tool_msgs = [m for m in msgs if m.get("role") == "tool"]

                    def _reply(content: Optional[str], calls: Optional[List[Dict[str, Any]]] = None):
                        m: Dict[str, Any] = {"role": "assistant", "content": content}
                        if calls:
                            m["tool_calls"] = calls
                        return serve._DryRunObject({"choices": [{"finish_reason": "stop", "message": m}]})

                    if sys_msg.startswith("You plan an engineering task"):
                        return _reply("Plan: inspect takeoff.csv.")

                    if sys_msg.startswith("You check an engineer's work"):
                        if not tool_msgs and len(user_texts) == 1:
                            self.verify_rounds += 1
                        return _reply("PASS")

                    if any("A reviewer checked your files" in t for t in user_texts):
                        last_msg = msgs[-1]
                        if last_msg.get("role") == "user" and str(last_msg.get("content") or "").startswith(
                            "A reviewer checked your files"
                        ):
                            self.fix_prompts.append(str(last_msg.get("content") or ""))
                            target_csv = partial_fix_csv if self.fix_write_stage == 0 else final_clean_csv
                            self.fix_write_stage += 1
                            return _reply(
                                None,
                                [
                                    {
                                        "id": f"fix_w_{self.fix_write_stage}",
                                        "type": "function",
                                        "function": {
                                            "name": "write_file",
                                            "arguments": json.dumps({"path": "takeoff.csv", "content": target_csv}),
                                        },
                                    }
                                ],
                            )
                        return _reply(f"Completed fix round {self.fix_write_stage}.")

                    return _reply("Worker completed initial pass.")

            sess = _LocalReplaySession(workdir, "Fix all errors in takeoff.csv.")
            client = _MultiFixClient()
            with patch.dict(
                os.environ,
                {
                    "MODEL_NAME": "gemini-4-argon",
                    "OPENAI_BASE_URL": "http://proxy.local/v1",
                    "OPENAI_API_KEY": "k",
                    "MAX_FIX_ROUNDS": "3",
                },
                clear=False,
            ):
                ans = asyncio.run(
                    serve.solve(
                        sess,  # type: ignore[arg-type]
                        client=client,
                        config_path=str(REPO_ROOT / "submission.yaml"),
                    )
                )

            self.assertEqual(ans, "Completed fix round 2.")
            self.assertEqual(client.verify_rounds, 3)
            self.assertEqual(len(client.fix_prompts), 2)
            # Round 1 sees all 3 errors
            self.assertIn("#REF!", client.fix_prompts[0])
            self.assertIn("missing/placeholder IDs", client.fix_prompts[0])
            self.assertIn("summary total mismatch in column 'qty'", client.fix_prompts[0])
            # Round 2 sees ONLY the persisting summary total mismatch
            self.assertIn("summary total mismatch in column 'qty' (row 5): reported 99 vs sum of 3 detail rows 60", client.fix_prompts[1])
            self.assertNotIn("#REF!", client.fix_prompts[1])
            self.assertNotIn("missing/placeholder IDs", client.fix_prompts[1])
            self.assertTrue(any("audit(post-fix): errors=0 warnings=0" in entry for entry in sess.logs))
            self.assertEqual(_run_audit(workdir)["errors"], [])

    def test_429_503_api_storms_fallback_rotation_and_malformed_tool_calls(self):
        req = httpx.Request("POST", "https://api.example/v1/chat/completions")
        clean_csv = "device_id,register,qty\nD1,REG-1,10\nD2,REG-2,20\nTOTAL,,30\n"

        # Part A: Full serve.solve() replay with 429 Retry-After, 503 storms, fallback rotation,
        # empty/whitespace replies, malformed tool_calls, and final clean PASS
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            async_sleeps: List[float] = []

            async def _fast_sleep(sec: float) -> None:
                async_sleeps.append(sec)

            class _StormSolveClient:
                def __init__(self) -> None:
                    self.planner_calls = 0
                    self.worker_calls = 0
                    self.verifier_calls = 0
                    self.planner_models: List[str] = []
                    self.verifier_models: List[str] = []
                    self.worker_messages: List[List[Dict[str, Any]]] = []
                    self.chat = self
                    self.completions = self

                async def create(self, **kwargs: Any) -> Any:
                    msgs = kwargs["messages"]
                    model_used = str(kwargs.get("model") or "")
                    sys_msg = str(msgs[0].get("content") or "")

                    if sys_msg.startswith("You plan an engineering task"):
                        self.planner_calls += 1
                        self.planner_models.append(model_used)
                        if self.planner_calls == 1:
                            r429 = httpx.Response(429, request=req, headers={"Retry-After": "9"})
                            raise openai.APIStatusError("rate limited", response=r429, body=None)
                        if self.planner_calls == 2:
                            r503 = httpx.Response(503, request=req)
                            raise openai.APIStatusError("service unavailable", response=r503, body=None)
                        return serve._DryRunObject(
                            {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Plan ready."}}]}
                        )

                    if sys_msg.startswith("You check an engineer's work"):
                        self.verifier_calls += 1
                        self.verifier_models.append(model_used)
                        if self.verifier_calls == 1:
                            r503 = httpx.Response(503, request=req)
                            raise openai.APIStatusError("verifier overloaded", response=r503, body=None)
                        return serve._DryRunObject(
                            {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "PASS"}}]}
                        )

                    # WORKER chaos sequence
                    self.worker_calls += 1
                    self.worker_messages.append(copy.deepcopy(msgs))
                    if self.worker_calls == 1:
                        return serve._DryRunObject({"choices": []})
                    if self.worker_calls == 2:
                        return serve._DryRunObject(
                            {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "   \n\t  "}}]}
                        )
                    if self.worker_calls == 3:
                        return serve._DryRunObject(
                            {
                                "choices": [
                                    {
                                        "finish_reason": "tool_calls",
                                        "message": {
                                            "role": "assistant",
                                            "content": None,
                                            "tool_calls": [
                                                None,
                                                {"id": "dup_1", "function": {"name": "read_file", "arguments": "{bad"}},
                                                {"id": "dup_1", "function": {"name": "bash", "arguments": '{"command": NaN}'}},
                                                {"id": "", "function": "not_a_dict"},
                                                {"function": {"name": "write_file", "arguments": "[1, 2]"}},
                                            ],
                                        },
                                    }
                                ]
                            }
                        )
                    if self.worker_calls == 4:
                        # Single-dict tool_calls payload instead of list
                        return serve._DryRunObject(
                            {
                                "choices": [
                                    {
                                        "finish_reason": "tool_calls",
                                        "message": {
                                            "role": "assistant",
                                            "content": None,
                                            "tool_calls": {
                                                "id": "single_dict_call",
                                                "function": {"name": "write_file", "arguments": json.dumps({"path": "takeoff.csv", "content": clean_csv})},
                                            },
                                        },
                                    }
                                ]
                            }
                        )
                    return serve._DryRunObject(
                        {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Clean takeoff.csv delivered."}}]}
                    )

            sess = _LocalReplaySession(workdir, "Create takeoff.csv under storm.")
            client = _StormSolveClient()
            with patch.dict(
                os.environ,
                {
                    "MODEL_NAME": "google-cloud/gemini-4-argon",
                    "FALLBACK_MODELS": "google-cloud/gemini-3.1-pro-preview,gemini-3.8-flash",
                    "OPENAI_BASE_URL": "http://proxy.local/v1",
                    "OPENAI_API_KEY": "k",
                    "MAX_FAILURES": "3",
                },
                clear=False,
            ), patch.object(serve.asyncio, "sleep", side_effect=_fast_sleep):
                ans = asyncio.run(
                    serve.solve(
                        sess,  # type: ignore[arg-type]
                        client=client,
                        config_path=str(REPO_ROOT / "submission.yaml"),
                    )
                )

            self.assertEqual(ans, "Clean takeoff.csv delivered.")
            self.assertEqual(client.planner_models, ["gemini-4-argon", "gemini-3.1-pro-preview", "gemini-3.8-flash"])
            self.assertEqual(client.verifier_models, ["gemini-4-argon", "gemini-3.1-pro-preview"])
            self.assertEqual(async_sleeps, [9.0, 15.0, 5.0])
            self.assertTrue(any("rotating fallback model: gemini-4-argon -> gemini-3.1-pro-preview" in entry for entry in sess.logs))
            self.assertTrue(any("empty choices" in entry for entry in sess.logs))
            self.assertTrue(any("tool circuit breaker tripped" in entry for entry in sess.logs))

            # Verify whitespace-only reply triggered nudge + malformed burst had 1-to-1 unique tool_call_id pairing
            msgs_after_burst = client.worker_messages[3]
            nudge_msgs = [m for m in msgs_after_burst if m.get("role") == "user" and m.get("content") == "Continue, and finish with your answer."]
            self.assertEqual(len(nudge_msgs), 1)
            burst_asst = next(m for m in msgs_after_burst if m.get("role") == "assistant" and isinstance(m.get("tool_calls"), list))
            burst_tools = [m for m in msgs_after_burst if m.get("role") == "tool"]
            burst_ids = [c["id"] for c in burst_asst["tool_calls"]]
            self.assertEqual(len(burst_ids), 5)
            self.assertEqual(len(set(burst_ids)), 5)
            self.assertEqual(burst_ids, [m["tool_call_id"] for m in burst_tools])
            self.assertTrue(all(m["content"].startswith("error:") for m in burst_tools))
            self.assertEqual(_run_audit(workdir)["errors"], [])

        # Part B: HarnessExecutionEngine API storm backoff & rotation verification
        sleeps: List[float] = []
        engine = harness.HarnessExecutionEngine(
            config_path=str(REPO_ROOT / "submission.yaml"),
            max_retries=5,
            retry_backoff_sec=0.5,
            sleep_fn=sleeps.append,
        )

        class _Rate429(Exception):
            status_code = 429
            retry_after = 2.5

        class _Srv503(Exception):
            status_code = 503
            response = httpx.Response(503, request=req)

        class _Srv500(Exception):
            status_code = 500

        attempts: List[str] = []

        def _storm_gen(_prompt: str, model_name: str, _max_tokens: int) -> str:
            attempts.append(model_name)
            idx = len(attempts)
            if idx == 1:
                raise _Rate429("429 quota")
            if idx == 2:
                raise _Srv503("503 overloaded")
            if idx == 3:
                raise _Srv500("500 internal error")
            if idx == 4:
                return "{bad json"
            return '{"ok": true}'

        res = engine.run_with_verification(
            prompt="Return valid JSON.",
            generate_fn=_storm_gen,
            verify_fn=harness.make_verify_fn(require_json=True),
        )
        self.assertTrue(res["ok"])
        self.assertEqual(len(attempts), 5)
        self.assertEqual(sleeps, [2.5, 1.0, 2.0])
        self.assertEqual([h.get("error_kind") for h in res["history"][:3]], ["rate_limit", "server_error", "server_error"])
        self.assertFalse(res["history"][3]["ok"])
        self.assertTrue(res["history"][4]["ok"])
        self.assertFalse(engine.locked)


if __name__ == "__main__":
    unittest.main(verbosity=2)
