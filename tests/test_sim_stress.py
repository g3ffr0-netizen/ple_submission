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
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Optional
from unittest.mock import patch
from xml.sax.saxutils import escape as xml_escape

import openai

import harness
import serve
import taskbridge

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_audit(
    workdir: Path,
    env: Optional[Dict[str, str]] = None,
    baseline_xlsx: Optional[Dict[str, Any]] = None,
    baseline_ifc_gids: Optional[Iterable[str]] = None,
    baseline_files: Optional[Dict[str, Any]] = None,
    required_register_ids: Optional[Iterable[str]] = None,
    target_deliverables: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Run serve._PROGRAMMATIC_AUDIT_SCRIPT in a temp workdir and parse its JSON report."""
    proc = subprocess.run(
        [sys.executable, "-"],
        input=serve._build_programmatic_audit_script(
            baseline_xlsx=baseline_xlsx,
            baseline_ifc_gids=baseline_ifc_gids,
            baseline_files=baseline_files,
            required_register_ids=required_register_ids,
            target_deliverables=target_deliverables,
        ),
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

        class _Prop:
            def __init__(self, name, value):
                self.Name = name
                self.NominalValue = type("_NomVal", (), {"wrappedValue": value})()

        class _Pset:
            def __init__(self, name, kv_dict):
                self.Name = name
                self.HasProperties = [_Prop(k, v) for k, v in kv_dict.items()]

        class _RelDefines:
            def __init__(self, pset):
                self.RelatingPropertyDefinition = pset

        class _Entity:
            def __init__(self, eid, cls, gid, name="", obj_type="", repr_=True, psets=None):
                self._eid = eid
                self._cls = cls
                self.GlobalId = gid
                self.Name = name
                self.ObjectType = obj_type
                self.Representation = repr_
                self.IsDecomposedBy = []
                self.ContainsElements = []
                self.IsDefinedBy = [
                    _RelDefines(_Pset(pname, pdict))
                    for pname, pdict in (psets or {}).items()
                ]

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
                        psets=e.get("psets"),
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

    @staticmethod
    def _extract_heredoc(command: str, tag: str) -> Optional[str]:
        lines = command.splitlines()
        start_marker = f"<<'{tag}'"
        for idx, ln in enumerate(lines):
            if start_marker in ln:
                for end_idx in range(idx + 1, len(lines)):
                    if lines[end_idx].strip() == tag:
                        return "\n".join(lines[idx + 1 : end_idx])
        return None

    async def exec(self, command: str, timeout_sec: int = 120) -> Dict[str, Any]:
        script_body = (
            self._extract_heredoc(command, "__PLE_XLSX_BASELINE__")
            or self._extract_heredoc(command, "__PLE_AUDIT__")
        )
        res = subprocess.run(
            [sys.executable, "-"] if script_body is not None else [sys.executable, "-c", "print('ok')"],
            input=script_body,
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
            self.assertIn("tray-stack vertical headroom 170.0mm < 300mm", bad_errs)
            self.assertIn("overhead clear height 2420.0mm < 2600mm", bad_warns)
            self.assertIn("overhead clear height 2350.0mm < 2600mm", bad_warns)

            clean_scene = {
                "elements": [
                    {"id": 1, "cls": "IfcSlab", "gid": "SLAB_1", "name": "FLOOR-SLAB", "box": [0.0, 20.0, 0.0, 20.0, -0.05, 0.0]},
                    {"id": 2, "cls": "IfcElectricDistributionBoard", "gid": "PDU_1", "name": "PDU-01", "obj_type": "PDU", "box": [14.0, 15.0, 1.0, 2.0, 0.0, 2.0]},
                    {"id": 3, "cls": "IfcPipeSegment", "gid": "CHW_1", "name": "CHW-SUP-01", "box": [0.0, 2.0, 2.0, 2.2, 2.80, 2.95]},
                    {"id": 4, "cls": "IfcPipeSegment", "gid": "CHW_2", "name": "CHW-SUP-02", "box": [2.008, 4.0, 2.0, 2.2, 2.80, 2.95]},
                    {"id": 5, "cls": "IfcFlowSegment", "gid": "COND_A", "name": "CONDUIT-A", "box": [5.0, 7.0, 4.0, 4.1, 2.80, 2.90]},
                    {"id": 6, "cls": "IfcFlowSegment", "gid": "COND_B", "name": "CONDUIT-B", "box": [7.008, 9.0, 4.0, 4.1, 2.80, 2.90]},
                    {"id": 7, "cls": "IfcDuctSegment", "gid": "DUCT_1", "name": "DUCT-01", "box": [1.0, 2.0, 4.0, 4.4, 2.80, 3.00]},
                    {"id": 8, "cls": "IfcColumn", "gid": "COL_B2", "name": "COL-B2", "box": [1.0, 1.5, 4.45, 4.8, 0.0, 4.0]},
                    {"id": 9, "cls": "IfcCableCarrierSegment", "gid": "CT_LOW", "name": "CT-LOWER", "box": [8.0, 10.0, 6.0, 6.5, 2.85, 2.95]},
                    {"id": 10, "cls": "IfcFlowSegment", "gid": "BW_UP", "name": "BW-UPPER", "obj_type": "Busway", "box": [8.0, 10.0, 6.0, 6.5, 3.30, 3.40]},
                    {"id": 11, "cls": "IfcFireSuppressionTerminal", "gid": "SPR_4", "name": "SPR-04", "obj_type": "Sprinkler", "box": [11.0, 11.2, 8.0, 8.2, 2.75, 2.85]},
                    {"id": 12, "cls": "IfcPipeSegment", "gid": "CHW_LOW", "name": "CHW-LOW", "box": [4.008, 6.0, 2.0, 2.2, 2.75, 2.90]},
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

    def test_mep_floating_drop_proxy_connectivity_and_pset_separation(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env = _write_mock_ifcopenshell_pkg(tmp_path / "pkgs")

            bad_scene = {
                "elements": [
                    {"id": 1, "cls": "IfcSlab", "gid": "SLAB_1", "name": "FLOOR-SLAB", "box": [0.0, 20.0, 0.0, 20.0, -0.05, 0.0]},
                    {"id": 2, "cls": "IfcCableCarrierSegment", "gid": "CT_1", "name": "CT-1-SEG-01", "box": [0.0, 2.0, 0.0, 0.4, 2.60, 2.70]},
                    {"id": 3, "cls": "IfcCableCarrierSegment", "gid": "CT_2", "name": "CT-1-SEG-02", "box": [2.008, 4.0, 0.0, 0.4, 2.60, 2.70]},
                    # Floating Termination proxy tagged with Pset_DCM_Drop SystemTag=CT-1, 260mm below CT-1 (>250mm tol!)
                    {
                        "id": 4,
                        "cls": "IfcBuildingElementProxy",
                        "gid": "DROP_CT_1",
                        "name": "DROP-CT-1-ROW-A",
                        "obj_type": "Termination",
                        "psets": {"Pset_DCM_Drop": {"SystemTag": "CT-1"}},
                        "box": [0.5, 0.8, 0.1, 0.3, 2.20, 2.34],
                    },
                    {"id": 5, "cls": "IfcFlowSegment", "gid": "BW_1", "name": "BW-1-SEG-01", "obj_type": "Busway", "box": [0.0, 2.0, 0.80, 1.10, 3.00, 3.10]},
                    {"id": 6, "cls": "IfcFlowSegment", "gid": "BW_2", "name": "BW-1-SEG-02", "obj_type": "Busway", "box": [2.008, 4.0, 0.80, 1.10, 3.00, 3.10]},
                    # Floating Termination proxy whose Name uses Pset SystemTag=BW-1 (no "busway" in Name): 260mm below BW-1 AND 266.5mm from CT-1
                    {
                        "id": 7,
                        "cls": "IfcBuildingElementProxy",
                        "gid": "DROP_BW_1",
                        "name": "TERM-DROP-01",
                        "obj_type": "Termination",
                        "psets": {"Pset_DCM_Drop": {"SystemTag": "BW-1"}},
                        "box": [1.0, 1.2, 0.6665, 0.75, 2.50, 2.74],
                    },
                    # Non-MEP neutral proxy without matching Name or Pset tag must be skipped cleanly
                    {"id": 8, "cls": "IfcBuildingElementProxy", "gid": "MISC_1", "name": "WALL-BRACKET-1", "box": [15.0, 16.0, 15.0, 16.0, 2.0, 2.2]},
                ]
            }
            bad_dir = tmp_path / "mep_drop_bad"
            bad_dir.mkdir()
            (bad_dir / "mep_drop_trap.ifc").write_text(
                f"ISO-10303-21;\n/* SCENE_JSON: {json.dumps(bad_scene)} */\nEND-ISO-10303-21;",
                encoding="utf-8",
            )
            bad = _run_audit(bad_dir, env=env)
            bad_errs = "\n".join(bad["errors"])
            self.assertIn("disconnected MEP cable_tray network", bad_errs)
            self.assertIn("disconnected MEP busway network", bad_errs)
            self.assertIn("tray-busway clearance 266.5mm < 300mm", bad_errs)
            self.assertNotIn("WALL-BRACKET-1", bad_errs)

            clean_scene = {
                "elements": [
                    {"id": 1, "cls": "IfcSlab", "gid": "SLAB_1", "name": "FLOOR-SLAB", "box": [0.0, 20.0, 0.0, 20.0, -0.05, 0.0]},
                    {"id": 2, "cls": "IfcCableCarrierSegment", "gid": "CT_1", "name": "CT-1-SEG-01", "box": [0.0, 2.0, 0.0, 0.4, 3.00, 3.10]},
                    {"id": 3, "cls": "IfcCableCarrierSegment", "gid": "CT_2", "name": "CT-1-SEG-02", "box": [2.008, 4.0, 0.0, 0.4, 3.00, 3.10]},
                    # Connected inline fitting within exact 250.0mm horizontal gap (4.25 - 4.00 = 0.250m)
                    {"id": 4, "cls": "IfcCableCarrierFitting", "gid": "DROP_OK", "name": "CT-1-DROP", "box": [4.25, 4.55, 0.0, 0.4, 3.00, 3.10]},
                    # Neutral ServiceDropTermination proxy without CT-1/BW-1 tag does not fragment CT-1
                    {
                        "id": 5,
                        "cls": "IfcBuildingElementProxy",
                        "gid": "SD_1",
                        "name": "SERVICE-DROP-A",
                        "obj_type": "ServiceDropTermination",
                        "psets": {"Pset_DCM_ServiceDrop": {"Panel": "PDU-1"}},
                        "box": [0.5, 0.8, 0.1, 0.3, 2.20, 2.40],
                    },
                    {"id": 6, "cls": "IfcFlowSegment", "gid": "BW_1", "name": "BW-1-SEG-01", "obj_type": "Busway", "box": [0.0, 2.0, 0.75, 1.05, 3.00, 3.10]},
                ]
            }
            clean_dir = tmp_path / "mep_drop_clean"
            clean_dir.mkdir()
            (clean_dir / "mep_drop_clean.ifc").write_text(
                f"ISO-10303-21;\n/* SCENE_JSON: {json.dumps(clean_scene)} */\nEND-ISO-10303-21;",
                encoding="utf-8",
            )
            clean = _run_audit(clean_dir, env=env)
            self.assertEqual(clean["errors"], [])
            self.assertEqual(clean["warnings"], [])

    def test_tray_stack_headroom_error_and_exact_300mm_epsilon_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env = _write_mock_ifcopenshell_pkg(tmp_path / "pkgs")

            bad_scene = {
                "elements": [
                    {"id": 1, "cls": "IfcCableCarrierSegment", "gid": "CT_BASE", "name": "CT-1-BASE", "box": [0.0, 4.0, 0.0, 0.5, 3.00, 3.10]},
                    # 72.0mm vertical overlap headroom above CHW-1
                    {"id": 2, "cls": "IfcPipeSegment", "gid": "CHW_72", "name": "CHW-1-72", "box": [0.5, 1.5, 0.0, 0.5, 2.80, 2.928]},
                    # 299.0mm vertical headroom below BW-1 (must be caught in errors, not warnings)
                    {"id": 3, "cls": "IfcFlowSegment", "gid": "BW_299", "name": "BW-1-299", "obj_type": "Busway", "box": [2.0, 3.0, 0.0, 0.5, 3.399, 3.50]},
                ]
            }
            bad_dir = tmp_path / "stack_bad"
            bad_dir.mkdir()
            (bad_dir / "stack_bad.ifc").write_text(
                f"ISO-10303-21;\n/* SCENE_JSON: {json.dumps(bad_scene)} */\nEND-ISO-10303-21;",
                encoding="utf-8",
            )
            bad = _run_audit(bad_dir, env=env)
            bad_errs = "\n".join(bad["errors"])
            self.assertIn("tray-stack vertical headroom 72.0mm < 300mm", bad_errs)
            self.assertIn("tray-stack vertical headroom 299.0mm < 300mm", bad_errs)
            self.assertFalse(any("tray-stack vertical headroom" in w for w in bad["warnings"]))

            # Exact 300.0mm (with IEEE-754 0.2999999999999998 subtraction) must pass under 1e-3mm epsilon
            clean_scene_v = {
                "elements": [
                    {"id": 1, "cls": "IfcCableCarrierSegment", "gid": "CT_LOW", "name": "CT-LOWER", "box": [0.0, 2.0, 0.0, 0.5, 2.85, 2.95]},
                    {"id": 2, "cls": "IfcFlowSegment", "gid": "BW_EXACT", "name": "BW-UPPER", "obj_type": "Busway", "box": [0.0, 2.0, 0.0, 0.5, 3.25, 3.35]},
                ]
            }
            clean_scene_h = {
                "elements": [
                    {"id": 3, "cls": "IfcCableCarrierSegment", "gid": "CT_SIDE", "name": "CT-LOWER-SIDE", "box": [0.0, 2.0, 1.0, 1.5, 2.85, 2.95]},
                    {"id": 4, "cls": "IfcFlowSegment", "gid": "BW_SIDE", "name": "BW-SIDE", "obj_type": "Busway", "box": [0.0, 2.0, 1.80, 2.1, 2.85, 2.95]},
                ]
            }
            clean_dir = tmp_path / "stack_clean"
            clean_dir.mkdir()
            (clean_dir / "stack_clean_v.ifc").write_text(
                f"ISO-10303-21;\n/* SCENE_JSON: {json.dumps(clean_scene_v)} */\nEND-ISO-10303-21;",
                encoding="utf-8",
            )
            (clean_dir / "stack_clean_h.ifc").write_text(
                f"ISO-10303-21;\n/* SCENE_JSON: {json.dumps(clean_scene_h)} */\nEND-ISO-10303-21;",
                encoding="utf-8",
            )
            clean = _run_audit(clean_dir, env=env)
            self.assertEqual(clean["errors"], [])
            self.assertEqual(clean["warnings"], [])

    def test_xlsx_audit_skips_unmodified_baseline_inputs_and_catches_modified_or_new(self):
        import stat

        defective_sheet = _make_ooxml_xlsx(
            {
                "Summary": [
                    ["Array", "Status"],
                    ["Cravenwood", "Carried", "EXTRA_SHIFTED_COL"],
                    ["Darnwick", "=1/0 (#REF!)"],
                ]
            }
        )
        clean_sheet = _make_ooxml_xlsx(
            {
                "Schedule": [
                    ["device_id", "register_id", "qty"],
                    ["INV-1", "REG-1", 4],
                    ["TOTAL", "", 4],
                ]
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            ro_input = workdir / "DER_Program_Contact_Directory.xlsx"
            untouched_equip = workdir / "Equipment" / "2025-06_Supplier_Lead_Time_Report.xlsx"
            untouched_equip.parent.mkdir(parents=True, exist_ok=True)
            ro_input.write_bytes(defective_sheet)
            untouched_equip.write_bytes(defective_sheet)
            os.chmod(ro_input, stat.S_IREAD)
            try:
                base_proc = subprocess.run(
                    [sys.executable, "-"],
                    input=serve._XLSX_BASELINE_SCRIPT,
                    cwd=workdir,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                base_line = next(
                    ln
                    for ln in base_proc.stdout.splitlines()
                    if ln.startswith(serve.XLSX_BASELINE_MARKER)
                )
                baseline_xlsx = json.loads(base_line[len(serve.XLSX_BASELINE_MARKER) :])
                self.assertIn("DER_Program_Contact_Directory.xlsx", baseline_xlsx)
                self.assertIn("Equipment/2025-06_Supplier_Lead_Time_Report.xlsx", baseline_xlsx)

                # With baseline snapshot, untouched input spreadsheets produce 0 errors
                filtered = _run_audit(workdir, baseline_xlsx=baseline_xlsx)
                self.assertEqual(filtered["errors"], [])
                self.assertEqual(filtered["warnings"], [])
                self.assertEqual(
                    set(filtered["skipped_unmodified_xlsx"]),
                    {
                        "DER_Program_Contact_Directory.xlsx",
                        "Equipment/2025-06_Supplier_Lead_Time_Report.xlsx",
                    },
                )

                # Without baseline snapshot (legacy standalone call), defects remain caught
                unfiltered = _run_audit(workdir)
                unfiltered_errs = "\n".join(unfiltered["errors"])
                self.assertIn("DER_Program_Contact_Directory.xlsx", unfiltered_errs)
                self.assertIn("Equipment/2025-06_Supplier_Lead_Time_Report.xlsx", unfiltered_errs)

                # Modifying one baseline XLSX in-place and adding a new defective XLSX deliverable:
                # read-only baseline stays skipped, modified & new deliverables are strictly audited
                modified_xlsx = _make_ooxml_xlsx(
                    {
                        "Orders": [
                            ["item_id", "qty"],
                            ["EQ-1", "#REF!"],
                        ]
                    }
                )
                new_bad_xlsx = _make_ooxml_xlsx(
                    {
                        "Takeoff": [
                            ["device_id", "qty"],
                            ["D1", 10, "SHIFTED"],
                            ["", 20],
                        ]
                    }
                )
                untouched_equip.write_bytes(modified_xlsx)
                (workdir / "output").mkdir(exist_ok=True)
                (workdir / "output" / "Takeoff.xlsx").write_bytes(new_bad_xlsx)

                after_mod = _run_audit(workdir, baseline_xlsx=baseline_xlsx)
                errs_text = "\n".join(after_mod["errors"])
                self.assertEqual(after_mod["skipped_unmodified_xlsx"], ["DER_Program_Contact_Directory.xlsx"])
                self.assertNotIn("DER_Program_Contact_Directory.xlsx", errs_text)
                self.assertIn("Equipment/2025-06_Supplier_Lead_Time_Report.xlsx[Orders]", errs_text)
                self.assertIn("output/Takeoff.xlsx[Takeoff]", errs_text)

                # Cleanly repairing both resolves all audit errors while read-only input remains skipped
                untouched_equip.write_bytes(clean_sheet)
                (workdir / "output" / "Takeoff.xlsx").write_bytes(clean_sheet)
                repaired = _run_audit(workdir, baseline_xlsx=baseline_xlsx)
                self.assertEqual(repaired["errors"], [])
            finally:
                if ro_input.exists():
                    os.chmod(ro_input, stat.S_IREAD | stat.S_IWRITE)


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

                    if sys_msg.startswith("You extract strict engineering constraints"):
                        return _reply(json.dumps({"source": "llm_extractor", "deliverables": ["takeoff.csv"]}))

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

                    if sys_msg.startswith("You extract strict engineering constraints"):
                        return _reply(json.dumps({"source": "llm_extractor", "deliverables": ["takeoff.csv"]}))

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
        def _mock_http_response(status_code: int, headers: Optional[Dict[str, str]] = None) -> Any:
            try:
                import httpx

                return httpx.Response(
                    status_code,
                    headers=dict(headers or {}),
                    request=httpx.Request("POST", "https://api.example/v1/chat/completions"),
                )
            except ImportError:
                return SimpleNamespace(
                    status_code=status_code,
                    headers=dict(headers or {}),
                    history=[],
                    next_request=None,
                    request=SimpleNamespace(method="POST", url="https://api.example/v1/chat/completions", headers={}),
                )

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
                            r429 = _mock_http_response(429, headers={"Retry-After": "9"})
                            raise openai.APIStatusError("rate limited", response=r429, body=None)
                        if self.planner_calls == 2:
                            r503 = _mock_http_response(503)
                            raise openai.APIStatusError("service unavailable", response=r503, body=None)
                        return serve._DryRunObject(
                            {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Plan ready."}}]}
                        )

                    if sys_msg.startswith("You extract strict engineering constraints"):
                        return serve._DryRunObject(
                            {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps({"source": "llm_extractor", "deliverables": ["takeoff.csv"]})}}]}
                        )

                    if sys_msg.startswith("You check an engineer's work"):
                        self.verifier_calls += 1
                        self.verifier_models.append(model_used)
                        if self.verifier_calls == 1:
                            r503 = _mock_http_response(503)
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
            response = _mock_http_response(503)

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

    def test_contradictory_cross_store_comment_prefilter_and_readback_gate(self):
        # Part A: Pure regex pre-filter checks
        self.assertTrue(
            serve.detect_contradictory_comment_statuses("Darnwick: pre-marked carried, final call not carried")
        )
        self.assertTrue(
            serve.detect_contradictory_comment_statuses("Lockmere: check pass after audit failed")
        )
        self.assertTrue(
            serve.detect_contradictory_comment_statuses("Marden: approved for batch, rejected pending signature")
        )
        self.assertEqual(
            serve.detect_contradictory_comment_statuses("Darnwick: NOT CARRIED — Witness test required"),
            [],
        )
        self.assertEqual(
            serve.detect_contradictory_comment_statuses("Record passed/failed and carried/not carried calls for each array."),
            [],
        )

        # Part B: Full solve() cross-store readback & contradictory comment blocking
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            comments: List[str] = []

            class _FakeMCPServer:
                name = "apps"

                async def call(self, tname: str, args: Dict[str, Any]) -> str:
                    req = args.get("request") if isinstance(args.get("request"), dict) else args
                    if tname == "openproject_mcp_add_comment":
                        comments.append(str(req.get("comment") or ""))
                        return json.dumps({"success": True, "activity_id": 61 + len(comments)})
                    if tname == "openproject_mcp_get_work_package":
                        return json.dumps({"id": 45, "status": "Closed"})
                    if tname == "openproject_mcp_list_comments":
                        return json.dumps({"comments": comments})
                    return json.dumps({"ok": True})

            fake_srv = _FakeMCPServer()
            fake_registry = {
                "apps__openproject_mcp_add_comment": (fake_srv, {"name": "openproject_mcp_add_comment", "inputSchema": {"type": "object"}}),
                "apps__openproject_mcp_get_work_package": (fake_srv, {"name": "openproject_mcp_get_work_package", "inputSchema": {"type": "object"}}),
                "apps__openproject_mcp_list_comments": (fake_srv, {"name": "openproject_mcp_list_comments", "inputSchema": {"type": "object"}}),
            }
            fake_defs = [
                {"type": "function", "function": {"name": k, "description": v["name"], "parameters": v["inputSchema"]}}
                for k, (_, v) in fake_registry.items()
            ]

            async def _fake_connect_mcp(_session: Any):
                return fake_registry, fake_defs, "\nMCP tools exposed."

            class _CrossStoreClient:
                def __init__(self) -> None:
                    self.verify_round = 0
                    self.fix_prompts: List[str] = []
                    self.fix_done = False
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
                        return _reply("Plan: record batch calls in OpenProject WP #45.")
                    if sys_msg.startswith("You extract strict engineering constraints"):
                        return _reply(json.dumps({"source": "llm_extractor", "deliverables": ["package.txt"]}))

                    if sys_msg.startswith("You check an engineer's work"):
                        if not tool_msgs:
                            self.verify_round += 1
                            if self.verify_round in (1, 2):
                                # Rounds 1 & 2 skip list_comments and only call get_work_package!
                                return _reply(
                                    None,
                                    [
                                        {
                                            "id": f"v_get_{self.verify_round}",
                                            "type": "function",
                                            "function": {
                                                "name": "apps__openproject_mcp_get_work_package",
                                                "arguments": json.dumps({"request": {"work_package_id": 45}}),
                                            },
                                        }
                                    ],
                                )
                            # Round 3 reads back list_comments properly
                            return _reply(
                                None,
                                [
                                    {
                                        "id": "v_list_3",
                                        "type": "function",
                                        "function": {
                                            "name": "apps__openproject_mcp_list_comments",
                                            "arguments": json.dumps({"request": {"work_package_id": 45}}),
                                        },
                                    }
                                ],
                            )
                        return _reply("PASS")

                    if any("A reviewer checked your files" in t for t in user_texts):
                        last_u = str(msgs[-1].get("content") or "")
                        if last_u.startswith("A reviewer checked your files"):
                            self.fix_prompts.append(last_u)
                            if not self.fix_done:
                                self.fix_done = True
                                return _reply(
                                    None,
                                    [
                                        {
                                            "id": "fix_clean_comment",
                                            "type": "function",
                                            "function": {
                                                "name": "apps__openproject_mcp_add_comment",
                                                "arguments": json.dumps(
                                                    {
                                                        "request": {
                                                            "work_package_id": 45,
                                                            "comment": "Darnwick: NOT CARRIED",
                                                        }
                                                    }
                                                ),
                                            },
                                        }
                                    ],
                                )
                        return _reply("Amended OpenProject WP #45 comment cleanly.")

                    if not tool_msgs:
                        return _reply(
                            None,
                            [
                                {
                                    "id": "work_bad_comment",
                                    "type": "function",
                                    "function": {
                                        "name": "apps__openproject_mcp_add_comment",
                                        "arguments": json.dumps(
                                            {
                                                "request": {
                                                    "work_package_id": 45,
                                                    "comment": "Darnwick: pre-marked carried, actually not carried",
                                                }
                                            }
                                        ),
                                    },
                                },
                                {
                                    "id": "work_write_pkg",
                                    "type": "function",
                                    "function": {
                                        "name": "write_file",
                                        "arguments": json.dumps({"path": "package.txt", "content": "Darnwick: NOT CARRIED\n"}),
                                    },
                                },
                            ],
                        )
                    return _reply("Posted WP #45 comment.")

            sess = _LocalReplaySession(workdir, "Record Darnwick status in package.txt and WP #45.")
            client = _CrossStoreClient()
            with patch.dict(
                os.environ,
                {
                    "MODEL_NAME": "gemini-4-argon",
                    "OPENAI_BASE_URL": "http://proxy.local/v1",
                    "OPENAI_API_KEY": "k",
                    "MAX_FIX_ROUNDS": "3",
                },
                clear=False,
            ), patch.object(serve, "connect_mcp", side_effect=_fake_connect_mcp):
                ans = asyncio.run(
                    serve.solve(
                        sess,  # type: ignore[arg-type]
                        client=client,
                        config_path=str(REPO_ROOT / "submission.yaml"),
                    )
                )

            self.assertEqual(ans, "Amended OpenProject WP #45 comment cleanly.")
            # Blocked R1 (contradictory comment + missing comment readback), blocked R2 (missing comment readback), passed R3
            self.assertEqual(client.verify_round, 3)
            self.assertIn("contradictory status (carried vs not/non-carried)", client.fix_prompts[0])
            self.assertIn("did not call an MCP comment/journal readback tool", client.fix_prompts[0])
            self.assertEqual(comments[-1], "Darnwick: NOT CARRIED")

    def test_tool_circuit_breaker_zero_error_batch_recovery_regression(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            (workdir / "ok.txt").write_text("healthy", encoding="utf-8")

            class _MixedBatchClient:
                def __init__(self) -> None:
                    self.turn = 0
                    self.seen_messages: List[List[Dict[str, Any]]] = []
                    self.chat = self
                    self.completions = self

                async def create(self, **kwargs: Any) -> serve._DryRunObject:
                    self.turn += 1
                    msgs = kwargs["messages"]
                    self.seen_messages.append(copy.deepcopy(msgs))
                    if self.turn == 1:
                        # 3 failing calls reaching max_failures=3, followed by 1 successful call in same batch!
                        return serve._DryRunObject(
                            {
                                "choices": [
                                    {
                                        "finish_reason": "tool_calls",
                                        "message": {
                                            "role": "assistant",
                                            "content": None,
                                            "tool_calls": [
                                                {"id": "b1", "type": "function", "function": {"name": "read_file", "arguments": "{bad1"}},
                                                {"id": "b2", "type": "function", "function": {"name": "read_file", "arguments": "{bad2"}},
                                                {"id": "b3", "type": "function", "function": {"name": "read_file", "arguments": "{bad3"}},
                                                {"id": "ok1", "type": "function", "function": {"name": "read_file", "arguments": json.dumps({"path": "ok.txt"})}},
                                            ],
                                        },
                                    }
                                ]
                            }
                        )
                    if self.turn == 2:
                        # 1 failing call (<3): failure_count should be 1, must NOT trigger 0-error breaker
                        return serve._DryRunObject(
                            {
                                "choices": [
                                    {
                                        "finish_reason": "tool_calls",
                                        "message": {
                                            "role": "assistant",
                                            "content": None,
                                            "tool_calls": [
                                                {"id": "b4", "type": "function", "function": {"name": "read_file", "arguments": "{bad4"}},
                                            ],
                                        },
                                    }
                                ]
                            }
                        )
                    return serve._DryRunObject(
                        {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Batch recovered cleanly."}}]}
                    )

            sess = _LocalReplaySession(workdir, "Test mixed batch recovery.")
            breaker = serve.CircuitBreaker(max_failures=3)
            rt = {"model": "gemini-4-argon", "max_output_tokens": 1024, "effort": "high", "cache_mark": False, "max_failures": 3}
            client = _MixedBatchClient()
            ans = asyncio.run(
                serve.agent(
                    sess,  # type: ignore[arg-type]
                    serve.conversation(serve.WORKER, sess, "Test mixed batch."),  # type: ignore[arg-type]
                    serve.TOOLS,
                    0.0,
                    client=client,
                    runtime_cfg=rt,
                    breaker=breaker,
                    final=True,
                )
            )
            self.assertEqual(ans, "Batch recovered cleanly.")
            self.assertFalse(any("0 consecutive tool errors" in entry for entry in sess.logs))
            self.assertFalse(any("tool circuit breaker tripped" in entry for entry in sess.logs))
            self.assertFalse(
                any(
                    m.get("role") == "user" and "Consecutive tool calls failed" in str(m.get("content") or "")
                    for m in client.seen_messages[-1]
                )
            )
            self.assertEqual(breaker.failure_count, 1)

            # Confirm a pure failing batch without trailing success still trips accurately at 3 consecutive errors
            class _PureFailClient:
                def __init__(self) -> None:
                    self.turn = 0
                    self.chat = self
                    self.completions = self

                async def create(self, **kwargs: Any) -> serve._DryRunObject:
                    self.turn += 1
                    if self.turn == 1:
                        return serve._DryRunObject(
                            {
                                "choices": [
                                    {
                                        "finish_reason": "tool_calls",
                                        "message": {
                                            "role": "assistant",
                                            "content": None,
                                            "tool_calls": [
                                                {"id": "p1", "type": "function", "function": {"name": "read_file", "arguments": "{bad1"}},
                                                {"id": "p2", "type": "function", "function": {"name": "read_file", "arguments": "{bad2"}},
                                                {"id": "p3", "type": "function", "function": {"name": "read_file", "arguments": "{bad3"}},
                                            ],
                                        },
                                    }
                                ]
                            }
                        )
                    return serve._DryRunObject(
                        {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Recovered after trip."}}]}
                    )

            sess2 = _LocalReplaySession(workdir, "Test pure failing batch.")
            ans2 = asyncio.run(
                serve.agent(
                    sess2,  # type: ignore[arg-type]
                    serve.conversation(serve.WORKER, sess2, "Test pure failing batch."),  # type: ignore[arg-type]
                    serve.TOOLS,
                    0.0,
                    client=_PureFailClient(),
                    runtime_cfg=rt,
                    breaker=serve.CircuitBreaker(max_failures=3),
                    final=True,
                )
            )
            self.assertEqual(ans2, "Recovered after trip.")
            self.assertTrue(any("tool circuit breaker tripped after 3 consecutive tool errors" in entry for entry in sess2.logs))

    def test_regex_extractor_linear_time_no_redos(self):
        import time

        t0 = time.monotonic()
        res_hyphens = serve._regex_extract_constraints("-" * 50_000 + " /app/output/plan.csv", "")
        res_nines = serve._regex_extract_constraints("9" * 50_000 + " >= 300 mm and 50% and 100 m2", "")
        elapsed = time.monotonic() - t0

        self.assertLess(elapsed, 0.5)
        self.assertIn("/app/output/plan.csv", res_hyphens["file_paths"])
        self.assertIn(">= 300 mm", res_nines["numeric_thresholds"])
        self.assertIn("50%", res_nines["numeric_thresholds"])
        self.assertIn("100 m2", res_nines["numeric_thresholds"])

    def test_e2e_extractor_429_regex_fallback_and_worker_429_retry_to_pass(self):
        empty_fb = serve._regex_extract_constraints("Summarize general engineering notes.", "")
        self.assertEqual(empty_fb["source"], "regex_fallback")
        self.assertEqual(empty_fb["entities"], [])
        self.assertEqual(empty_fb["note"], "no_constraints_found")

        def _mock_http_response(status_code: int, headers: Optional[Dict[str, str]] = None) -> Any:
            try:
                import httpx

                return httpx.Response(
                    status_code,
                    headers=dict(headers or {}),
                    request=httpx.Request("POST", "https://api.example/v1/chat/completions"),
                )
            except ImportError:
                return SimpleNamespace(
                    status_code=status_code,
                    headers=dict(headers or {}),
                    history=[],
                    next_request=None,
                    request=SimpleNamespace(method="POST", url="https://api.example/v1/chat/completions", headers={}),
                )

        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            sleeps: List[float] = []

            async def _fast_sleep(sec: float) -> None:
                sleeps.append(sec)

            clean_csv = "device_id,register,qty\nCT-1,REG-1,10\nTOTAL,,10\n"

            class _E2EStormClient:
                def __init__(self) -> None:
                    self.extractor_attempts = 0
                    self.worker_attempts = 0
                    self.worker_prompt_seen = ""
                    self.verifier_prompt_seen = ""
                    self.chat = self
                    self.completions = self

                async def create(self, **kwargs: Any) -> serve._DryRunObject:
                    msgs = kwargs["messages"]
                    sys_msg = str(msgs[0].get("content") or "")
                    user_texts = [str(m.get("content") or "") for m in msgs if m.get("role") == "user"]
                    tool_msgs = [m for m in msgs if m.get("role") == "tool"]

                    if sys_msg.startswith("You plan an engineering task"):
                        return serve._DryRunObject(
                            {
                                "choices": [
                                    {
                                        "finish_reason": "stop",
                                        "message": {
                                            "role": "assistant",
                                            "content": "Plan: validate cable_tray and busway thresholds >= 300 mm and write /app/output/package.csv.",
                                        },
                                    }
                                ]
                            }
                        )

                    if sys_msg.startswith("You extract strict engineering constraints"):
                        self.extractor_attempts += 1
                        r429 = _mock_http_response(429)
                        raise openai.APIStatusError("extractor quota 429", response=r429, body=None)

                    if sys_msg.startswith("You check an engineer's work"):
                        self.verifier_prompt_seen = user_texts[0]
                        if not tool_msgs:
                            return serve._DryRunObject(
                                {
                                    "choices": [
                                        {
                                            "finish_reason": "tool_calls",
                                            "message": {
                                                "role": "assistant",
                                                "content": None,
                                                "tool_calls": [
                                                    {
                                                        "id": "v_read_pkg",
                                                        "type": "function",
                                                        "function": {"name": "read_file", "arguments": json.dumps({"path": "package.csv"})},
                                                    }
                                                ],
                                            },
                                        }
                                    ]
                                }
                            )
                        return serve._DryRunObject(
                            {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "PASS"}}]}
                        )

                    # WORKER receives CONSTRAINTS.json, hits 1x 429, retries cleanly, writes package.csv
                    self.worker_attempts += 1
                    if not self.worker_prompt_seen:
                        self.worker_prompt_seen = user_texts[0]
                    if self.worker_attempts == 1:
                        r429 = _mock_http_response(429, headers={"Retry-After": "3"})
                        raise openai.APIStatusError("worker quota 429", response=r429, body=None)
                    if not tool_msgs:
                        return serve._DryRunObject(
                            {
                                "choices": [
                                    {
                                        "finish_reason": "tool_calls",
                                        "message": {
                                            "role": "assistant",
                                            "content": None,
                                            "tool_calls": [
                                                {
                                                    "id": "w_write_pkg",
                                                    "type": "function",
                                                    "function": {
                                                        "name": "write_file",
                                                        "arguments": json.dumps({"path": "package.csv", "content": clean_csv}),
                                                    },
                                                }
                                            ],
                                        },
                                    }
                                ]
                            }
                        )
                    return serve._DryRunObject(
                        {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Delivered clean package.csv."}}]}
                    )

            task_prompt = (
                "Write /app/output/package.csv for cable_tray clearances (>= 300 mm separation, "
                "<= 250 mm joint tolerance) as of 2025-07-14. If disposition is pending signature, "
                "the releasable array list must be empty."
            )
            sess = _LocalReplaySession(workdir, task_prompt)
            client = _E2EStormClient()
            with patch.dict(
                os.environ,
                {
                    "MODEL_NAME": "google-cloud/gemini-4-argon",
                    "FALLBACK_MODELS": "google-cloud/gemini-3.1-pro-preview",
                    "OPENAI_BASE_URL": "http://proxy.local/v1",
                    "OPENAI_API_KEY": "k",
                    "MAX_EXTRACT_TURNS": "99",
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

            self.assertEqual(ans, "Delivered clean package.csv.")
            self.assertEqual(client.extractor_attempts, 2)
            self.assertEqual(client.worker_attempts, 3)
            self.assertEqual(sleeps, [2.0, 5.0])
            self.assertTrue(any("falling back to regex constraint extraction" in entry for entry in sess.logs))
            for injected in (client.worker_prompt_seen, client.verifier_prompt_seen):
                self.assertIn("CONSTRAINTS.json", injected)
                self.assertIn('"source": "regex_fallback"', injected)
                self.assertIn("/app/output/package.csv", injected)
                self.assertIn(">= 300 mm", injected)
                self.assertIn("2025-07-14", injected)
                self.assertIn("cable_tray", injected)
                self.assertIn("busway", injected)
                self.assertIn("empty", injected)
            self.assertEqual(_run_audit(workdir)["errors"], [])

    def test_e2e_solve_skips_untouched_input_xlsx_audits_modified_xlsx_and_includes_mep_efficiency_guidance(self):
        import stat

        self.assertIn("keep runs compact and direct", serve.WORKER)
        self.assertIn("minimal local lateral offsets", serve.WORKER)
        self.assertIn("perimeter detours", serve.WORKER)

        ro_defective_xlsx = _make_ooxml_xlsx(
            {
                "Contact Directory": [
                    ["Name"],
                    ["Alice", "alice@example.com", "SHIFTED_COL"],
                    ["Bob", "#REF!"],
                ]
            }
        )
        clean_schedule_xlsx = _make_ooxml_xlsx(
            {
                "Schedule": [
                    ["device_id", "qty"],
                    ["INV-1", 4],
                    ["TOTAL", 4],
                ]
            }
        )
        broken_schedule_xlsx = _make_ooxml_xlsx(
            {
                "Schedule": [
                    ["device_id", "qty"],
                    ["INV-1", "#REF!"],
                ]
            }
        )

        # Sub-case A: untouched read-only defective input XLSX is skipped; clean WORKER + PASS needs 0 FIX rounds
        with tempfile.TemporaryDirectory() as tmp_a:
            dir_a = Path(tmp_a)
            ro_a = dir_a / "DER_Program_Contact_Directory.xlsx"
            ro_a.write_bytes(ro_defective_xlsx)
            os.chmod(ro_a, stat.S_IREAD)
            try:
                class _CleanClientA:
                    def __init__(self) -> None:
                        self.verifier_calls = 0
                        self.chat = self
                        self.completions = self

                    async def create(self, **kwargs: Any) -> serve._DryRunObject:
                        msgs = kwargs["messages"]
                        sys_msg = str(msgs[0].get("content") or "")
                        tool_msgs = [m for m in msgs if m.get("role") == "tool"]
                        if sys_msg.startswith("You plan an engineering task"):
                            return serve._DryRunObject(
                                {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Plan: write report.csv."}}]}
                            )
                        if sys_msg.startswith("You extract strict engineering constraints"):
                            return serve._DryRunObject(
                                {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps({"deliverables": ["report.csv"]})}}]}
                            )
                        if sys_msg.startswith("You check an engineer's work"):
                            self.verifier_calls += 1
                            return serve._DryRunObject(
                                {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "PASS"}}]}
                            )
                        if not tool_msgs:
                            return serve._DryRunObject(
                                {
                                    "choices": [
                                        {
                                            "finish_reason": "tool_calls",
                                            "message": {
                                                "role": "assistant",
                                                "content": None,
                                                "tool_calls": [
                                                    {
                                                        "id": "wa_csv",
                                                        "type": "function",
                                                        "function": {
                                                            "name": "write_file",
                                                            "arguments": json.dumps(
                                                                {"path": "report.csv", "content": "device_id,qty\nINV-1,4\nTOTAL,4\n"}
                                                            ),
                                                        },
                                                    }
                                                ],
                                            },
                                        }
                                    ]
                                }
                            )
                        return serve._DryRunObject(
                            {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Report delivered."}}]}
                        )

                sess_a = _LocalReplaySession(dir_a, "Generate clean report.csv without altering input spreadsheets.")
                client_a = _CleanClientA()
                ans_a = asyncio.run(
                    serve.solve(
                        sess_a,  # type: ignore[arg-type]
                        client=client_a,
                        config_path=str(REPO_ROOT / "submission.yaml"),
                    )
                )
                self.assertEqual(ans_a, "Report delivered.")
                self.assertEqual(client_a.verifier_calls, 1)
                self.assertFalse(any("audit: errors=" in ln for ln in sess_a.logs))
            finally:
                if ro_a.exists():
                    os.chmod(ro_a, stat.S_IREAD | stat.S_IWRITE)

        # Sub-case B: modified deliverable Schedule.xlsx with #REF! is caught, read-only input skipped, FIX gets exact error + MEP efficiency note
        with tempfile.TemporaryDirectory() as tmp_b:
            dir_b = Path(tmp_b)
            ro_b = dir_b / "DER_Program_Contact_Directory.xlsx"
            sched_b = dir_b / "Schedule.xlsx"
            ro_b.write_bytes(ro_defective_xlsx)
            sched_b.write_bytes(clean_schedule_xlsx)
            os.chmod(ro_b, stat.S_IREAD)
            try:
                class _ModifiedXlsxClientB:
                    def __init__(self) -> None:
                        self.verifier_calls = 0
                        self.fix_prompt = ""
                        self.chat = self
                        self.completions = self

                    async def create(self, **kwargs: Any) -> serve._DryRunObject:
                        msgs = kwargs["messages"]
                        sys_msg = str(msgs[0].get("content") or "")
                        user_msgs = [str(m.get("content") or "") for m in msgs if m.get("role") == "user"]
                        tool_msgs = [m for m in msgs if m.get("role") == "tool"]
                        if sys_msg.startswith("You plan an engineering task"):
                            return serve._DryRunObject(
                                {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Plan: update Schedule.xlsx."}}]}
                            )
                        if sys_msg.startswith("You extract strict engineering constraints"):
                            return serve._DryRunObject(
                                {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps({"deliverables": ["Schedule.xlsx"]})}}]}
                            )
                        if sys_msg.startswith("You check an engineer's work"):
                            self.verifier_calls += 1
                            return serve._DryRunObject(
                                {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "PASS"}}]}
                            )
                        if len(user_msgs) >= 2 and any("found these gaps" in u for u in user_msgs):
                            self.fix_prompt = user_msgs[-1]
                            if not any(m.get("tool_call_id") == "fix_sched" for m in tool_msgs):
                                sched_b.write_bytes(clean_schedule_xlsx)
                                return serve._DryRunObject(
                                    {
                                        "choices": [
                                            {
                                                "finish_reason": "tool_calls",
                                                "message": {
                                                    "role": "assistant",
                                                    "content": None,
                                                    "tool_calls": [
                                                        {
                                                            "id": "fix_sched",
                                                            "type": "function",
                                                            "function": {
                                                                "name": "bash",
                                                                "arguments": json.dumps({"command": "echo repaired Schedule.xlsx"}),
                                                            },
                                                        }
                                                    ],
                                                },
                                            }
                                        ]
                                    }
                                )
                            return serve._DryRunObject(
                                {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Repaired Schedule.xlsx cleanly."}}]}
                            )

                        # WORKER corrupts Schedule.xlsx in place while leaving DER_Program_Contact_Directory.xlsx untouched
                        if not tool_msgs:
                            sched_b.write_bytes(broken_schedule_xlsx)
                            return serve._DryRunObject(
                                {
                                    "choices": [
                                        {
                                            "finish_reason": "tool_calls",
                                            "message": {
                                                "role": "assistant",
                                                "content": None,
                                                "tool_calls": [
                                                    {
                                                        "id": "work_sched",
                                                        "type": "function",
                                                        "function": {
                                                            "name": "bash",
                                                            "arguments": json.dumps({"command": "echo updated Schedule.xlsx"}),
                                                        },
                                                    }
                                                ],
                                            },
                                        }
                                    ]
                                }
                            )
                        return serve._DryRunObject(
                            {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Updated Schedule.xlsx."}}]}
                        )

                sess_b = _LocalReplaySession(dir_b, "Update Schedule.xlsx without modifying DER_Program_Contact_Directory.xlsx.")
                client_b = _ModifiedXlsxClientB()
                ans_b = asyncio.run(
                    serve.solve(
                        sess_b,  # type: ignore[arg-type]
                        client=client_b,
                        config_path=str(REPO_ROOT / "submission.yaml"),
                    )
                )
                self.assertEqual(ans_b, "Repaired Schedule.xlsx cleanly.")
                self.assertEqual(client_b.verifier_calls, 2)
                self.assertIn("Schedule.xlsx[Schedule] R2C2: Excel error/NaN token '#REF!'", client_b.fix_prompt)
                self.assertNotIn("DER_Program_Contact_Directory.xlsx", client_b.fix_prompt)
                self.assertIn("compact, minimal-length BIM/MEP routes", client_b.fix_prompt)
                self.assertTrue(any("audit(post-fix): errors=0 warnings=0" in ln for ln in sess_b.logs))
            finally:
                if ro_b.exists():
                    os.chmod(ro_b, stat.S_IREAD | stat.S_IWRITE)


class TestLayer3HardeningPRA(unittest.TestCase):
    def test_view_image_dedup_and_mtime_invalidation(self):
        self.assertEqual(serve.MAX_ACTIVE_IMAGES, 10)
        png_a = b"\x89PNG\r\n\x1a\n" + b"frame-alpha-001"
        png_b = b"\x89PNG\r\n\x1a\n" + b"frame-bravo-002"

        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            chart = workdir / "chart.png"
            chart.write_bytes(png_a)
            sess = _LocalReplaySession(workdir, "Inspect chart.png cleanly.")

            class _DedupVisionClient:
                def __init__(self) -> None:
                    self.calls = 0
                    self.tool_contents: List[str] = []
                    self.final_messages: List[Dict[str, Any]] = []
                    self.chat = self
                    self.completions = self

                async def create(self, **kwargs: Any) -> serve._DryRunObject:
                    self.calls += 1
                    msgs = kwargs["messages"]
                    sys_msg = str(msgs[0].get("content") or "")
                    if sys_msg.startswith("You plan an engineering task"):
                        return serve._DryRunObject(
                            {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Plan: inspect chart.png."}}]}
                        )
                    if sys_msg.startswith("You extract strict engineering constraints"):
                        return serve._DryRunObject(
                            {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps({"deliverables": []})}}]}
                        )
                    if sys_msg.startswith("You check an engineer's work"):
                        return serve._DryRunObject(
                            {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "PASS"}}]}
                        )

                    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
                    self.tool_contents = [str(m.get("content") or "") for m in tool_msgs]
                    if len(tool_msgs) == 0:
                        req_id = "vi_1"
                    elif len(tool_msgs) == 1:
                        req_id = "vi_2"
                    elif len(tool_msgs) == 2:
                        # Bump mtime_ns even with identical bytes -> must invalidate dedup cache
                        st = chart.stat()
                        os.utime(chart, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
                        req_id = "vi_3"
                    elif len(tool_msgs) == 3:
                        # Overwrite file with new PNG content -> must reload fresh
                        chart.write_bytes(png_b)
                        req_id = "vi_4"
                    else:
                        self.final_messages = copy.deepcopy(msgs)
                        return serve._DryRunObject(
                            {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Chart inspected."}}]}
                        )
                    return serve._DryRunObject(
                        {
                            "choices": [
                                {
                                    "finish_reason": "tool_calls",
                                    "message": {
                                        "role": "assistant",
                                        "content": None,
                                        "tool_calls": [
                                            {
                                                "id": req_id,
                                                "type": "function",
                                                "function": {"name": "view_image", "arguments": json.dumps({"path": "chart.png"})},
                                            }
                                        ],
                                    },
                                }
                            ]
                        }
                    )

            client = _DedupVisionClient()
            ans = asyncio.run(
                serve.solve(
                    sess,  # type: ignore[arg-type]
                    client=client,
                    config_path=str(REPO_ROOT / "submission.yaml"),
                )
            )
            self.assertEqual(ans, "Chart inspected.")
            self.assertEqual(len(client.tool_contents), 4)
            self.assertIn("loaded image chart.png", client.tool_contents[0])
            self.assertTrue(client.tool_contents[1].startswith("INFO:"))
            self.assertIn("already loaded", client.tool_contents[1])
            self.assertIn("loaded image chart.png", client.tool_contents[2])
            self.assertIn("loaded image chart.png", client.tool_contents[3])

            attached = [
                part["image_url"]["url"]
                for m in client.final_messages
                if m.get("role") == "user" and isinstance(m.get("content"), list)
                for part in m["content"]
                if isinstance(part, dict) and part.get("type") == "image_url"
            ]
            # Call 1 (png_a), Call 2 (deduped!), Call 3 (mtime bump -> png_a), Call 4 (new bytes -> png_b)
            self.assertEqual(len(attached), 3)
            self.assertNotEqual(attached[1], attached[2])

    def test_scoped_mcp_comment_readback_per_service(self):
        # Unit checks on check_mcp_readback_errors
        registry = {
            "apps__openproject_mcp_add_comment": (None, {}),
            "apps__openproject_mcp_get_work_package": (None, {}),
            "apps__wiki_js_mcp_create_comment": (None, {}),
            "apps__wiki_js_mcp_list_pages": (None, {}),
            "apps__wiki_js_mcp_list_comments": (None, {}),
        }
        # OpenProject has NO comment reader; get_work_package satisfies OpenProject readback (0 errors)
        self.assertEqual(
            serve.check_mcp_readback_errors(
                {("comment", "wp=45"): {"tool": "apps__openproject_mcp_add_comment", "arguments": {}}},
                ["apps__openproject_mcp_get_work_package"],
                registry,
            ),
            [],
        )
        # Writing OpenProject but only reading Wiki.js fails OpenProject readback
        op_miss = serve.check_mcp_readback_errors(
            {("comment", "wp=45"): {"tool": "apps__openproject_mcp_add_comment", "arguments": {}}},
            ["apps__wiki_js_mcp_list_comments"],
            registry,
        )
        self.assertEqual(len(op_miss), 1)
        self.assertIn("openproject", op_miss[0].lower())

        # Wiki.js HAS list_comments; reading only list_pages flags genuine missing comment readback
        wiki_miss = serve.check_mcp_readback_errors(
            {("comment", "page=10"): {"tool": "apps__wiki_js_mcp_create_comment", "arguments": {}}},
            ["apps__wiki_js_mcp_list_pages"],
            registry,
        )
        self.assertEqual(len(wiki_miss), 1)
        self.assertIn("did not call an MCP comment/journal readback tool", wiki_miss[0])

        # Full solve() Scenario A: OpenProject write + OpenProject get_work_package -> clean 1-round PASS
        # Full solve() Scenario B: Wiki.js write + skipping list_comments -> FIX gate -> Round 2 list_comments -> PASS
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)

            class _MultiServiceMCP:
                name = "apps"

                async def call(self, tname: str, args: Dict[str, Any]) -> str:
                    return json.dumps({"ok": True, "tool": tname, "args": args})

            fake_srv = _MultiServiceMCP()
            fake_reg = {k: (fake_srv, {"name": k.split("apps__", 1)[1], "inputSchema": {"type": "object"}}) for k in registry}
            fake_defs = [
                {"type": "function", "function": {"name": k, "description": k, "parameters": {"type": "object"}}}
                for k in fake_reg
            ]

            async def _fake_connect_mcp(_session: Any):
                return fake_reg, fake_defs, "\nMCP tools exposed."

            class _ScopedClient:
                def __init__(self, target_service: str) -> None:
                    self.target_service = target_service
                    self.verify_round = 0
                    self.fix_prompts: List[str] = []
                    self.chat = self
                    self.completions = self

                async def create(self, **kwargs: Any) -> serve._DryRunObject:
                    msgs = kwargs["messages"]
                    sys_msg = str(msgs[0].get("content") or "")
                    user_msgs = [str(m.get("content") or "") for m in msgs if m.get("role") == "user"]
                    tool_msgs = [m for m in msgs if m.get("role") == "tool"]

                    def _reply(txt: Optional[str], calls: Optional[List[Dict[str, Any]]] = None) -> serve._DryRunObject:
                        msg: Dict[str, Any] = {"role": "assistant", "content": txt}
                        if calls:
                            msg["tool_calls"] = calls
                        return serve._DryRunObject({"choices": [{"finish_reason": "tool_calls" if calls else "stop", "message": msg}]})

                    if sys_msg.startswith("You plan an engineering task"):
                        return _reply("Plan: update MCP comment.")
                    if sys_msg.startswith("You extract strict engineering constraints"):
                        return _reply(json.dumps({"deliverables": []}))
                    if sys_msg.startswith("You check an engineer's work"):
                        if not tool_msgs:
                            self.verify_round += 1
                            if self.target_service == "openproject":
                                rtool = "apps__openproject_mcp_get_work_package"
                            else:
                                rtool = "apps__wiki_js_mcp_list_pages" if self.verify_round == 1 else "apps__wiki_js_mcp_list_comments"
                            return _reply(
                                None,
                                [{"id": f"vr_{self.verify_round}", "type": "function", "function": {"name": rtool, "arguments": json.dumps({"request": {"id": 1}})}}],
                            )
                        return _reply("PASS")
                    if any("found these gaps" in u for u in user_msgs):
                        self.fix_prompts.append(user_msgs[-1])
                        return _reply("Verified Wiki.js records.")
                    if not tool_msgs:
                        wtool = "apps__openproject_mcp_add_comment" if self.target_service == "openproject" else "apps__wiki_js_mcp_create_comment"
                        return _reply(
                            None,
                            [{"id": "wr_1", "type": "function", "function": {"name": wtool, "arguments": json.dumps({"request": {"id": 45, "comment": "Cravenwood: CARRIED"}})}}],
                        )
                    return _reply("Updated MCP comment.")

            with patch.object(serve, "connect_mcp", side_effect=_fake_connect_mcp):
                sess_op = _LocalReplaySession(workdir, "Update WP #45 status.")
                client_op = _ScopedClient("openproject")
                ans_op = asyncio.run(serve.solve(sess_op, client=client_op, config_path=str(REPO_ROOT / "submission.yaml")))  # type: ignore[arg-type]
                self.assertEqual(ans_op, "Updated MCP comment.")
                self.assertEqual(client_op.verify_round, 1)
                self.assertEqual(client_op.fix_prompts, [])
                self.assertTrue(any("readback_errors=0 PASS" in ln for ln in sess_op.logs))

                sess_wk = _LocalReplaySession(workdir, "Update Wiki.js array comment.")
                client_wk = _ScopedClient("wiki_js")
                ans_wk = asyncio.run(serve.solve(sess_wk, client=client_wk, config_path=str(REPO_ROOT / "submission.yaml")))  # type: ignore[arg-type]
                self.assertEqual(ans_wk, "Verified Wiki.js records.")
                self.assertEqual(client_wk.verify_round, 2)
                self.assertEqual(len(client_wk.fix_prompts), 1)
                self.assertIn("did not call an MCP comment/journal readback tool", client_wk.fix_prompts[0])

    def test_mep_summary_and_baseline_diffed_clear_height_telemetry(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env = _write_mock_ifcopenshell_pkg(tmp_path / "pkgs")

            # Capture workspace baseline on input IFC before WORKER generates modified model
            case_dir = tmp_path / "case"
            (case_dir / "input").mkdir(parents=True)
            base_scene = {
                "elements": [
                    {"id": 1, "cls": "IfcFireSuppressionTerminal", "gid": "BASE_SPK_01", "name": "SPK-A01", "obj_type": "Sprinkler", "box": [2.0, 2.2, 4.0, 4.2, 1.89, 1.94]},
                ]
            }
            (case_dir / "input" / "baseline.ifc").write_text(
                f"ISO-10303-21;\n#10=IFCBUILDINGELEMENTPROXY('BASE_STEP_02',$,'EXIST-EQ',$,$,$,$,$,$);\n/* SCENE_JSON: {json.dumps(base_scene)} */\nEND-ISO-10303-21;",
                encoding="utf-8",
            )
            sess = _LocalReplaySession(case_dir, "Route MEP cleanly.")
            file_base, ifc_gids = asyncio.run(serve.capture_workspace_baseline(sess))  # type: ignore[arg-type]
            self.assertIn("input/baseline.ifc", file_base)
            self.assertIn("BASE_SPK_01", ifc_gids)
            self.assertIn("BASE_STEP_02", ifc_gids)

            # Now write deliverable output/model.ifc mixing baseline sprinkler + new routed MEP + new sprinkler
            (case_dir / "output").mkdir(parents=True)
            deliverable_scene = {
                "elements": [
                    {"id": 1, "cls": "IfcFireSuppressionTerminal", "gid": "BASE_SPK_01", "name": "SPK-A01", "obj_type": "Sprinkler", "box": [2.0, 2.2, 4.0, 4.2, 1.89, 1.94]},
                    {"id": 2, "cls": "IfcFireSuppressionTerminal", "gid": "NEW_SPK_02", "name": "SPK-NEW-02", "obj_type": "Sprinkler", "box": [4.0, 4.2, 4.0, 4.2, 1.95, 2.00]},
                    {"id": 3, "cls": "IfcCableCarrierSegment", "gid": "NEW_TRAY_1", "name": "CT-1-SEG-01", "box": [0.0, 6.0, 1.0, 1.6, -0.18, -0.05]},
                    {"id": 4, "cls": "IfcCableCarrierSegment", "gid": "NEW_TRAY_2", "name": "CT-1-SEG-02", "box": [6.005, 10.005, 1.0, 1.6, -0.18, -0.05]},
                    {"id": 5, "cls": "IfcDuctSegment", "gid": "NEW_DUCT_LOW", "name": "DUCT-LOW", "box": [12.0, 14.0, 1.0, 1.6, 2.35, 2.48]},
                    {"id": 6, "cls": "IfcCableCarrierSegment", "gid": "NEW_BUS_1", "name": "BW-1-SEG-01", "obj_type": "Busway", "box": [0.0, 8.0, 8.0, 8.2, -0.20, -0.06]},
                    {"id": 7, "cls": "IfcPipeSegment", "gid": "NEW_CHW_1", "name": "CHW-1-SEG-01", "box": [0.0, 12.0, 12.0, 12.2, -0.68, -0.51]},
                ]
            }
            (case_dir / "output" / "model.ifc").write_text(
                f"ISO-10303-21;\n/* SCENE_JSON: {json.dumps(deliverable_scene)} */\nEND-ISO-10303-21;",
                encoding="utf-8",
            )
            out_only = case_dir / "output"
            report = _run_audit(out_only, env=env, baseline_ifc_gids=ifc_gids)
            self.assertEqual(report["errors"], [])

            summary = report["mep_summary"]
            self.assertEqual(summary["busway"], {"length_m": 8.0, "segment_count": 1, "avg_segment_m": 8.0, "z_min": -0.2, "z_max": -0.06})
            self.assertEqual(summary["chw_pipe"], {"length_m": 12.0, "segment_count": 1, "avg_segment_m": 12.0, "z_min": -0.68, "z_max": -0.51})
            self.assertEqual(summary["cable_tray"]["segment_count"], 2)
            self.assertEqual(summary["cable_tray"]["length_m"], 10.0)
            self.assertEqual(summary["cable_tray"]["avg_segment_m"], 5.0)
            self.assertEqual(summary["cable_tray"]["z_min"], -0.18)
            self.assertEqual(summary["cable_tray"]["z_max"], -0.05)

            ch = report["clear_height"]
            term_by_gid = {item["gid"]: item for item in ch["terminals"]}
            mep_by_gid = {item["gid"]: item for item in ch["routed_mep"]}
            self.assertEqual(term_by_gid["BASE_SPK_01"]["origin"], "[BASELINE_INPUT]")
            self.assertEqual(term_by_gid["NEW_SPK_02"]["origin"], "[MODIFIED/NEW]")
            self.assertEqual(mep_by_gid["NEW_DUCT_LOW"]["origin"], "[MODIFIED/NEW]")

            warns = "\n".join(report["warnings"])
            self.assertIn("[terminals] [BASELINE_INPUT] overhead clear height 1890.0mm < 2600mm", warns)
            self.assertIn("[terminals] [MODIFIED/NEW] overhead clear height 1950.0mm < 2600mm", warns)
            self.assertIn("[routed_mep] [MODIFIED/NEW] overhead clear height 2350.0mm < 2600mm", warns)

    def test_universal_sha256_baseline_skips_unmodified_csv_ifc_and_audits_modified(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            env = _write_mock_ifcopenshell_pkg(tmp_path / "pkgs")
            workdir = tmp_path / "case"

            (workdir / "30_geotech" / "boring_logs").mkdir(parents=True)
            (workdir / "50_bim").mkdir(parents=True)
            (workdir / "40_inputs").mkdir(parents=True)

            # Defective pre-existing read-only workspace inputs (.csv, .ifc, .json, .zip)
            b01 = workdir / "30_geotech" / "boring_logs" / "B-01.csv"
            b01.write_text("depth_m,spt_n\n1.0,NaN,SHIFTED\n2.0,#REF!\n", encoding="utf-8")

            input_ifc_scene = {
                "elements": [
                    {"id": 1, "cls": "IfcFireSuppressionTerminal", "gid": "BASE_SPK_01", "name": "SPK-A01", "obj_type": "Sprinkler", "box": [2.0, 2.2, 4.0, 4.2, 1.89, 1.94]},
                    {"id": 2, "cls": "IfcWall", "gid": "BASE_WALL_01", "name": "WALL-01", "box": [2.1, 2.3, 4.0, 4.2, 1.80, 2.50]},
                ]
            }
            (workdir / "50_bim" / "BLDG4410-E_RevC.ifc").write_text(
                f"ISO-10303-21;\n/* SCENE_JSON: {json.dumps(input_ifc_scene)} */\nEND-ISO-10303-21;",
                encoding="utf-8",
            )
            (workdir / "40_inputs" / "raw_config.json").write_text("{not valid json", encoding="utf-8")
            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, "w") as zf:
                zf.writestr("bad.csv", "a,b\n1,2,3\n")
            (workdir / "40_inputs" / "archive.zip").write_bytes(zip_buf.getvalue())

            sess = _LocalReplaySession(workdir, "Audit only modified deliverables.")
            file_base, ifc_gids = asyncio.run(serve.capture_workspace_baseline(sess))  # type: ignore[arg-type]
            xlsx_only = asyncio.run(serve.capture_xlsx_baseline(sess))  # type: ignore[arg-type]
            self.assertEqual(xlsx_only, {})
            for expected_rel in (
                "30_geotech/boring_logs/B-01.csv",
                "50_bim/BLDG4410-E_RevC.ifc",
                "40_inputs/raw_config.json",
                "40_inputs/archive.zip",
            ):
                self.assertIn(expected_rel, file_base)
            self.assertIn("BASE_SPK_01", ifc_gids)

            # Directory-qualified output deliverable never un-skips same-named input IFC
            clean_report = _run_audit(
                workdir,
                env=env,
                baseline_files=file_base,
                baseline_ifc_gids=ifc_gids,
                target_deliverables=["output/BLDG4410-E_RevC.ifc"],
            )
            self.assertEqual(clean_report["errors"], [])
            self.assertEqual(clean_report["warnings"], [])
            self.assertEqual(clean_report["skipped_unmodified_xlsx"], [])
            self.assertEqual(
                set(clean_report["skipped_unmodified_inputs"]),
                {
                    "30_geotech/boring_logs/B-01.csv",
                    "50_bim/BLDG4410-E_RevC.ifc",
                    "40_inputs/raw_config.json",
                    "40_inputs/archive.zip",
                },
            )

            # Untouched file explicitly listed in target_deliverables IS still audited
            stub_report = _run_audit(
                workdir,
                env=env,
                baseline_files=file_base,
                baseline_ifc_gids=ifc_gids,
                target_deliverables=["30_geotech/boring_logs/B-01.csv"],
            )
            self.assertIn("30_geotech/boring_logs/B-01.csv", "\n".join(stub_report["errors"]))
            self.assertNotIn("30_geotech/boring_logs/B-01.csv", stub_report["skipped_unmodified_inputs"])
            self.assertEqual(
                serve._extract_target_deliverables(
                    json.dumps(
                        {
                            "deliverables": [
                                "/app/output/BLDG4410-E_RevC.ifc",
                                {"path": "takeoff.csv"},
                                "OpenProject work package #45",
                            ]
                        }
                    ),
                    workdir="/app",
                ),
                ["output/BLDG4410-E_RevC.ifc", "takeoff.csv"],
            )

            # Modify B-01.csv in place and write a new defective output/model.ifc (with BASE_SPK_01 copied)
            b01.write_text("depth_m,spt_n\n1.0,15,SHIFTED\n", encoding="utf-8")
            (workdir / "output").mkdir(parents=True)
            out_scene = {
                "elements": [
                    {"id": 1, "cls": "IfcFireSuppressionTerminal", "gid": "BASE_SPK_01", "name": "SPK-A01", "obj_type": "Sprinkler", "box": [2.0, 2.2, 4.0, 4.2, 1.89, 1.94]},
                    {"id": 2, "cls": "IfcCableCarrierSegment", "gid": "NEW_TRAY_CLASH", "name": "CT-1", "box": [2.05, 3.0, 4.0, 4.3, 1.89, 1.96]},
                ]
            }
            (workdir / "output" / "model.ifc").write_text(
                f"ISO-10303-21;\n/* SCENE_JSON: {json.dumps(out_scene)} */\nEND-ISO-10303-21;",
                encoding="utf-8",
            )

            after_mod = _run_audit(
                workdir,
                env=env,
                baseline_files=file_base,
                baseline_ifc_gids=ifc_gids,
            )
            self.assertEqual(
                set(after_mod["skipped_unmodified_inputs"]),
                {
                    "50_bim/BLDG4410-E_RevC.ifc",
                    "40_inputs/raw_config.json",
                    "40_inputs/archive.zip",
                },
            )
            errs_text = "\n".join(after_mod["errors"])
            self.assertIn("30_geotech/boring_logs/B-01.csv", errs_text)
            self.assertIn("output/model.ifc", errs_text)
            self.assertNotIn("BLDG4410-E_RevC.ifc", errs_text)
            self.assertNotIn("raw_config.json", errs_text)
            self.assertNotIn("archive.zip", errs_text)
            term_by_gid = {item["gid"]: item for item in after_mod["clear_height"]["terminals"]}
            self.assertEqual(term_by_gid["BASE_SPK_01"]["origin"], "[BASELINE_INPUT]")

    def test_constraint_register_coverage_audit_extractor_ids_and_turn_budget(self):
        import time

        # 1. EXTRACTOR / regex extraction of R*/H*/S* IDs, hard caps, ReDoS safety, and non-req context filtering
        t0 = time.monotonic()
        _ = serve._regex_extract_constraints("R-" * 20_000 + " -" * 20_000, "")
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 0.5)

        empty_fb = serve._regex_extract_constraints("Summarize general engineering notes.", "")
        self.assertEqual(empty_fb["required_register_ids"], [])
        self.assertEqual(empty_fb["hard_numeric_caps"], [])
        self.assertEqual(empty_fb["note"], "no_constraints_found")

        sample_task = (
            "Cover requirements R01..R03, R-260573-05, H01, and S08. "
            "Enforce <= 3% drift and <= 0.85 demand/capacity ratio. "
            "Do not confuse R30 insulation, H20 truck load, or S40 pipe schedule."
        )
        fb = serve._regex_extract_constraints(sample_task, "")
        self.assertEqual(
            fb["required_register_ids"],
            ["R01", "R02", "R03", "R-260573-05", "H01", "S08"],
        )
        self.assertIn("<= 3%", fb["hard_numeric_caps"])
        self.assertIn("<= 0.85", fb["hard_numeric_caps"])

        # Deterministic merge even when LLM extractor omits required_register_ids
        merged_json = serve._parse_or_fallback_constraints(
            json.dumps({"deliverables": ["constraint_register.csv"]}),
            "Ensure R-260573-05 and R14 are verified (cap <= 0.90).",
            "",
        )
        merged = json.loads(merged_json)
        self.assertEqual(merged["required_register_ids"], ["R-260573-05", "R14"])
        self.assertIn("<= 0.90", merged["hard_numeric_caps"])
        self.assertEqual(
            serve._extract_required_register_ids(merged_json),
            ["R-260573-05", "R14"],
        )

        # 2. Programmatic audit catches missing IDs in standalone CSV, zipped CSV, and XLSX
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            # Case A: standalone constraint_register.csv
            case_csv = root / "case_csv"
            case_csv.mkdir()
            reg_csv = case_csv / "constraint_register.csv"
            reg_csv.write_text(
                "requirement_id,status\nR01,PASS\nR02,PASS\n",
                encoding="utf-8",
            )
            csv_miss = _run_audit(
                case_csv,
                required_register_ids=["R01", "R02", "R03", "R-260573-05"],
            )
            self.assertTrue(csv_miss["errors"])
            self.assertIn("constraint_register missing 2 required ID(s)", csv_miss["errors"][0])
            self.assertIn("R03", csv_miss["errors"][0])
            self.assertIn("R-260573-05", csv_miss["errors"][0])

            reg_csv.write_text(
                "requirement_id,status\nR01,PASS\nR02,PASS\nR03,PASS\nR-260573-05,PASS\n",
                encoding="utf-8",
            )
            csv_ok = _run_audit(
                case_csv,
                required_register_ids=["R01", "R02", "R03", "R-260573-05"],
            )
            self.assertEqual(csv_ok["errors"], [])

            # Case B: ZIP-nested constraint_register.csv (and no false fail on unrelated S08)
            case_zip = root / "case_zip"
            (case_zip / "output").mkdir(parents=True)
            z_bad = io.BytesIO()
            with zipfile.ZipFile(z_bad, "w") as zf:
                zf.writestr(
                    "deliverables/constraint_register.csv",
                    "req_id,result\nR-260573-01,OK\n",
                )
            (case_zip / "output" / "submission.zip").write_bytes(z_bad.getvalue())
            zip_miss = _run_audit(
                case_zip,
                required_register_ids=["R-260573-01", "R-260573-05", "S08"],
            )
            self.assertEqual(len(zip_miss["errors"]), 1)
            self.assertIn("output/submission.zip!deliverables/constraint_register.csv", zip_miss["errors"][0])
            self.assertIn("R-260573-05", zip_miss["errors"][0])
            self.assertNotIn("S08", zip_miss["errors"][0])

            z_ok = io.BytesIO()
            with zipfile.ZipFile(z_ok, "w") as zf:
                zf.writestr(
                    "deliverables/constraint_register.csv",
                    "req_id,result\nR-260573-01,OK\nR-260573-05,OK\n",
                )
            (case_zip / "output" / "submission.zip").write_bytes(z_ok.getvalue())
            zip_ok = _run_audit(
                case_zip,
                required_register_ids=["R-260573-01", "R-260573-05", "S08"],
            )
            self.assertEqual(zip_ok["errors"], [])

            # Case C: constraint_register.xlsx missing R14
            case_xlsx = root / "case_xlsx"
            case_xlsx.mkdir()
            reg_xlsx = case_xlsx / "constraint_register.xlsx"
            reg_xlsx.write_bytes(
                _make_ooxml_xlsx(
                    {
                        "Register": [
                            ["id", "status"],
                            ["R01", "OK"],
                        ]
                    }
                )
            )
            xlsx_miss = _run_audit(case_xlsx, required_register_ids=["R01", "R14"])
            self.assertEqual(len(xlsx_miss["errors"]), 1)
            self.assertIn("R14", xlsx_miss["errors"][0])

            reg_xlsx.write_bytes(
                _make_ooxml_xlsx(
                    {
                        "Register": [
                            ["id", "status"],
                            ["R01", "OK"],
                            ["R14", "OK"],
                        ]
                    }
                )
            )
            xlsx_ok = _run_audit(case_xlsx, required_register_ids=["R01", "R14"])
            self.assertEqual(xlsx_ok["errors"], [])

        # 3. VERIFIER instructions and MAX_WORK_TURNS=75
        self.assertIn("governing scenario", serve.VERIFIER.lower())
        self.assertIn("near-boundary", serve.VERIFIER.lower())
        clean_env = dict(os.environ)
        clean_env.pop("MAX_WORK_TURNS", None)
        with patch.dict(os.environ, clean_env, clear=True):
            cfg = serve.resolve_runtime_config(str(REPO_ROOT / "submission.yaml"))
            self.assertEqual(cfg["max_work_turns"], 75)


if __name__ == "__main__":
    unittest.main(verbosity=2)
