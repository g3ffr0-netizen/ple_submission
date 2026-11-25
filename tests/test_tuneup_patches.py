"""Unit tests for the Run #30 Tune-Up Shop patches in serve.py and harness.py."""

from __future__ import annotations

import asyncio
import io
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from xml.sax.saxutils import escape as xml_escape

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import harness
import serve
import taskbridge


def _make_minimal_xlsx_bytes(sheets: dict[str, list[list[object]]]) -> bytes:
    """Create a valid minimal .xlsx workbook in memory without openpyxl dependency."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        sh_names = list(sheets.keys())
        ct_overrides = "\n".join(
            f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            for i in range(1, len(sh_names) + 1)
        )
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            f"{ct_overrides}</Types>",
        )
        wb_sheets = "\n".join(
            f'<sheet name="{xml_escape(name)}" sheetId="{i}" r:id="rId{i}"/>'
            for i, name in enumerate(sh_names, 1)
        )
        zf.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f"<sheets>{wb_sheets}</sheets></workbook>",
        )
        rels = "\n".join(
            f'<Relationship Id="rId{i}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{i}.xml"/>'
            for i in range(1, len(sh_names) + 1)
        )
        zf.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f"{rels}</Relationships>",
        )
        for i, name in enumerate(sh_names, 1):
            rows_xml: list[str] = []
            for r_idx, row in enumerate(sheets[name], 1):
                cells_xml: list[str] = []
                for c_idx, val in enumerate(row):
                    col_letter = chr(ord("A") + c_idx)
                    ref = f"{col_letter}{r_idx}"
                    txt = xml_escape("" if val is None else str(val))
                    cells_xml.append(f'<c r="{ref}" t="inlineStr"><is><t>{txt}</t></is></c>')
                rows_xml.append(f'<row r="{r_idx}">{"".join(cells_xml)}</row>')
            zf.writestr(
                f"xl/worksheets/sheet{i}.xml",
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                f"<sheetData>{''.join(rows_xml)}</sheetData></worksheet>",
            )
    return buf.getvalue()


def _make_minimal_docx_bytes(paragraphs: list[str]) -> bytes:
    """Create a valid minimal .docx archive with word/document.xml paragraphs."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>",
        )
        p_xml = "".join(
            f"<w:p><w:r><w:t>{xml_escape(p)}</w:t></w:r></w:p>" for p in paragraphs
        )
        zf.writestr(
            "word/document.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body>{p_xml}</w:body></w:document>",
        )
    return buf.getvalue()


def _run_audit_script(workdir: Path, **kwargs: object) -> dict:
    script = serve._build_programmatic_audit_script(**kwargs)  # type: ignore[arg-type]
    proc = subprocess.run(
        [sys.executable, "-"],
        input=script,
        cwd=workdir,
        capture_output=True,
        text=True,
        check=True,
    )
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith(serve.AUDIT_MARKER))
    return json.loads(line[len(serve.AUDIT_MARKER) :])


class _ScriptedClient:
    """Offline scripted AsyncOpenAI stand-in recording every request payload."""

    def __init__(self, handler):
        self._handler = handler
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append(
            {
                "model": kwargs.get("model"),
                "messages": json.loads(json.dumps(kwargs.get("messages") or [])),
                "has_tools": "tools" in kwargs,
            }
        )
        return self._handler(len(self.calls), kwargs)


def _msg_reply(content: str = "", tool_calls: list[dict] | None = None):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=msg, finish_reason="stop")],
        usage=None,
    )


class TestTuneupPatches(unittest.TestCase):
    def test_insertion_time_exec_spill_keeps_inline_compact_and_spills_full(self):
        class _SpillSession:
            def __init__(self):
                self.files: dict[str, bytes] = {}
                self.limits = {"exec_timeout_sec": 60}

            async def exec(self, command, timeout_sec=120):
                big = ("HEAD_LINE_MARKER\n" * 200) + ("MIDDLE_NOISE_12345\n" * 400) + ("TAIL_LINE_MARKER\n" * 100)
                return {"exit_code": 0, "output": big, "timed_out": False}

            async def write_file(self, path, data, append=False):
                raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
                self.files[path] = raw
                return len(raw)

        async def _run():
            sess = _SpillSession()
            res = await serve.run_tool(sess, {"name": "bash", "arguments": json.dumps({"command": "dump"})})  # type: ignore[arg-type]
            self.assertLessEqual(len(res), serve.EXEC_INLINE_CAP_CHARS)
            self.assertIn("/tmp/ple_exec_out_", res)
            self.assertIn("HEAD_LINE_MARKER", res)
            self.assertIn("TAIL_LINE_MARKER", res)
            self.assertEqual(len(sess.files), 1)
            spilled_text = next(iter(sess.files.values())).decode("utf-8")
            self.assertIn("MIDDLE_NOISE_12345", spilled_text)

        asyncio.run(_run())

    def test_prune_old_tool_outputs_respects_age_size_refs_and_idempotency(self):
        messages: list[dict] = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]
        old_big_unreferenced = ("UNREFERENCED_ALPHA_9991 " * 1000)  # > 20k chars
        old_big_referenced = ("KEEP_REF_TOKEN_884422 CRITICAL_FEEDER_Z9 " * 300)  # > 10k chars
        small_old = "short ok"
        for t in range(12):
            cid = f"call_{t}"
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": cid, "type": "function", "function": {"name": "bash", "arguments": "{}"}}],
                }
            )
            if t == 0:
                body = old_big_unreferenced
            elif t == 1:
                body = old_big_referenced
            elif t == 2:
                body = small_old
            elif t == 9:
                body = "recent " + ("RECENT_BIG_7733 " * 400)  # <= 8 turns old (age=2)
            elif t == 11:
                body = "Referencing KEEP_REF_TOKEN_884422 and CRITICAL_FEEDER_Z9 in recent turn"
            else:
                body = f"turn_{t}_ok"
            messages.append({"role": "tool", "tool_call_id": cid, "content": body})

        pruned, saved = serve.prune_old_tool_outputs(messages)
        self.assertEqual(pruned, 1)
        self.assertGreater(saved, 19_000)

        stub = messages[3]["content"]  # call_0 tool output
        self.assertTrue(stub.startswith("[pruned 24,000-char exec output"))
        self.assertLessEqual(len(stub), serve.PRUNE_STUB_CHARS + 20)

        # Old referenced output (call_1), old small output (call_2), recent big output (call_9) preserved!
        self.assertEqual(messages[5]["content"], old_big_referenced)
        self.assertEqual(messages[7]["content"], small_old)
        self.assertIn("RECENT_BIG_7733", messages[21]["content"])

        # Idempotent on second call
        self.assertEqual(serve.prune_old_tool_outputs(messages), (0, 0))

    def test_agent_coarse_checkpoint_pruning_and_duplicate_failing_bash_guard(self):
        class _LoopSession:
            workdir = "/app"
            limits = {"exec_timeout_sec": 60}
            final_message = ""
            cancelled = asyncio.Event()

            def __init__(self):
                self.logs: list[str] = []
                self.usages: list[tuple] = []
                self.bash_cmds: list[str] = []

            def time_left(self):
                return 3600.0

            async def exec(self, command, timeout_sec=120):
                self.bash_cmds.append(command)
                if "bad_script.py" in command:
                    return {"exit_code": 1, "output": "Traceback: SyntaxError", "timed_out": False}
                if "emit_big" in command:
                    return {"exit_code": 0, "output": "NOISE_CHUNK_4455 " * 300, "timed_out": False}  # ~5,100 chars
                return {"exit_code": 0, "output": "ok", "timed_out": False}

            async def write_file(self, path, data, append=False):
                return len(data)

            async def log(self, text):
                self.logs.append(text)

            async def usage(self, model, inp, out, cached=0):
                self.usages.append((model, inp, out, cached))

        def _handler(step_num, _kwargs):
            if step_num == 1:
                return _msg_reply(tool_calls=[{"id": "c1", "type": "function", "function": {"name": "bash", "arguments": '{"command":"emit_big"}'}}])
            if step_num == 2:
                return _msg_reply(tool_calls=[{"id": "c2", "type": "function", "function": {"name": "bash", "arguments": '{"command":"python3 bad_script.py"}'}}])
            if step_num == 3:
                # Immediately repeat exact same failing bash command -> must be short-circuited!
                return _msg_reply(tool_calls=[{"id": "c3", "type": "function", "function": {"name": "bash", "arguments": '{"command":"python3 bad_script.py"}'}}])
            if 4 <= step_num <= 13:
                return _msg_reply(tool_calls=[{"id": f"c{step_num}", "type": "function", "function": {"name": "bash", "arguments": f'{{"command":"echo step_{step_num}"}}'}}])
            return _msg_reply(content="done")

        async def _run():
            sess = _LoopSession()
            client = _ScriptedClient(_handler)
            cfg = {"model": "gemini-4-argon", "max_output_tokens": 1024, "fallback_models": [], "max_failures": 5}
            msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "run"}]
            out = await serve.agent(sess, msgs, serve.TOOLS, 0.0, client=client, runtime_cfg=cfg)  # type: ignore[arg-type]
            self.assertEqual(out, "done")
            # Duplicate failing bash command ran only ONCE inside container
            self.assertEqual(sess.bash_cmds.count("python3 bad_script.py"), 1)
            # Calls 2..12 kept prefix c1 UNMUTATED (prompt-cache preserved), while checkpoint at turn 12 pruned c1 for call 13+!
            call_12_tool1 = [m for m in client.calls[11]["messages"] if m.get("tool_call_id") == "c1"][0]["content"]
            call_13_tool1 = [m for m in client.calls[12]["messages"] if m.get("tool_call_id") == "c1"][0]["content"]
            self.assertIn("NOISE_CHUNK_4455", call_12_tool1)
            self.assertTrue(call_13_tool1.startswith("[pruned "))

        asyncio.run(_run())

    def test_dynamic_exclusion_rules_and_alphanumeric_register_ids(self):
        # 1. Seismic task that ALLOWS UFC 3-301-01 must NOT infer UFC as forbidden!
        seismic_task = "Use UFC 3-301-01 Chapter 12 and FEMA P-2082-1. Register R14, R17, R25, R29, R31, R32, R35."
        c_seis = json.dumps(serve._regex_extract_constraints(seismic_task, ""))
        excl_seis = serve.extract_exclusion_rules(c_seis, seismic_task, "")
        self.assertEqual(excl_seis["forbidden_citations"], [])
        rids_seis = serve._extract_required_register_ids(c_seis, seismic_task, "")
        for rid in ("R14", "R17", "R25", "R29", "R31", "R32", "R35"):
            self.assertIn(rid, rids_seis)

        # 2. Division-26 task that prohibits UFC citations + excludes section + requires alphanumeric R-ILLUM
        div26_task = (
            "Do not cite UFC or Unified Facilities Criteria documents in any edited section. "
            "Section 26 27 14.00 20 is not part of the project manual. "
            "Allowed sections: 26 05 73, 26 20 00, 26 24 13, 26 32 15, 26 36 23, 26 51 00, 01 33 00. "
            "Cover R-260573-02, R-260573-05, R-262413-01, and R-ILLUM in output/submission.zip."
        )
        c_div = json.dumps(serve._regex_extract_constraints(div26_task, ""))
        excl_div = serve.extract_exclusion_rules(c_div, div26_task, "")
        self.assertIn("UFC", excl_div["forbidden_citations"])
        self.assertIn("26 27 14.00 20", excl_div["disallowed_sections"])
        self.assertIn("26 20 00", excl_div["allowed_sections"])
        rids_div = serve._extract_required_register_ids(c_div, div26_task, "")
        for rid in ("R-260573-02", "R-260573-05", "R-262413-01", "R-ILLUM"):
            self.assertIn(rid, rids_div)

    def test_register_sheet_and_docx_pre_verifier_gate_block_and_recover(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            out_dir = workdir / "output"
            out_dir.mkdir(parents=True)

            # Build submission.zip with Register sheet missing R-ILLUM & R29 + flawed DOCX
            wb_bytes = _make_minimal_xlsx_bytes(
                {
                    "Sections": [["number", "title"], ["26 05 73", "POWER SYSTEM STUDIES"]],
                    "Register": [["requirement_id", "status"], ["R-260573-02", "covered"], ["R17", "covered"]],
                }
            )
            bad_docx = _make_minimal_docx_bytes(
                [
                    "2.1 GENERAL: Provide lighting panels [_____] or Option A as indicated.",
                    "2.2 CABLES: Category 5 UTPas indicated cables per UFC 3-520-01.",
                    "2.3 REF: Provide in accordance with Section 01 33 00, SUBMITTAL PROCEDURES and Section 26 27 14.00 20.",
                ]
            )
            with zipfile.ZipFile(out_dir / "submission.zip", "w") as zf:
                zf.writestr("design_basis.xlsx", wb_bytes)
                zf.writestr("specs/26 20 00.docx", bad_docx)

            res_bad = _run_audit_script(
                workdir,
                required_register_ids=["R-260573-02", "R-ILLUM", "R17", "R29"],
                target_deliverables=["output/submission.zip", "output/missing_summary.csv"],
                forbidden_citations=["UFC"],
                allowed_sections=["01 33 00", "26 20 00"],
                disallowed_sections=["26 27 14.00 20"],
                required_scope_exclusions=["Submetering excluded from Division 26"],
                enforce_deliverables=True,
            )
            err_blob = "\n".join(res_bad["errors"])
            blocker_blob = "\n".join(res_bad["pre_verifier_blockers"])
            self.assertIn("missing", err_blob.lower())
            self.assertIn("R-ILLUM", err_blob)
            self.assertIn("R29", err_blob)
            self.assertIn("missing_summary.csv", err_blob)
            self.assertIn("[_____]", blocker_blob)
            self.assertIn("UTPas indicated", blocker_blob)
            self.assertIn("UFC 3-520-01", blocker_blob)
            self.assertIn("01 33 00,", blocker_blob)
            self.assertIn("26 27 14.00 20", blocker_blob)
            self.assertIn("Submetering excluded from Division 26", blocker_blob)

            # harness.evaluate_pre_pass_gate blocks PASS when errors exist
            ok_gate, merged = harness.evaluate_pre_pass_gate(
                "PASS",
                audit_errors=res_bad["errors"],
            )
            self.assertFalse(ok_gate)
            self.assertTrue(len(merged) >= 5)

            # Fix all issues -> 0 errors, 0 pre_verifier_blockers, evaluate_pre_pass_gate passes!
            (out_dir / "missing_summary.csv").write_text("id,val\nA,1\n", encoding="utf-8")
            wb_good = _make_minimal_xlsx_bytes(
                {
                    "Sections": [["number", "title"], ["26 05 73", "POWER SYSTEM STUDIES"]],
                    "Register": [
                        ["requirement_id", "status"],
                        ["R-260573-02", "covered"],
                        ["R-ILLUM", "covered"],
                        ["R17", "covered"],
                        ["R29", "covered"],
                    ],
                }
            )
            good_docx = _make_minimal_docx_bytes(
                [
                    "2.1 GENERAL: Submetering excluded from Division 26 per SOW.",
                    "2.2 CABLES: Category 5 UTP cables per TIA-568.",
                    "2.3 REF: Provide in accordance with Section 01 33 00 SUBMITTAL PROCEDURES.",
                ]
            )
            with zipfile.ZipFile(out_dir / "submission.zip", "w") as zf:
                zf.writestr("design_basis.xlsx", wb_good)
                zf.writestr("specs/26 20 00.docx", good_docx)

            res_good = _run_audit_script(
                workdir,
                required_register_ids=["R-260573-02", "R-ILLUM", "R17", "R29"],
                target_deliverables=["output/submission.zip", "output/missing_summary.csv"],
                forbidden_citations=["UFC"],
                allowed_sections=["01 33 00", "26 20 00"],
                disallowed_sections=["26 27 14.00 20"],
                required_scope_exclusions=["Submetering excluded from Division 26"],
                enforce_deliverables=True,
            )
            self.assertEqual(res_good["errors"], [])
            self.assertEqual(res_good["pre_verifier_blockers"], [])
            self.assertEqual(harness.evaluate_pre_pass_gate("PASS", audit_errors=res_good["errors"]), (True, []))

    def test_solve_skips_verifier_call_when_pre_verifier_blocker_fires(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            out_dir = workdir / "output"
            out_dir.mkdir()
            with zipfile.ZipFile(out_dir / "submission.zip", "w") as zf:
                zf.writestr(
                    "specs/26 20 00.docx",
                    _make_minimal_docx_bytes(["Broken blank [_____] and UFC 3-520-01 citation."]),
                )

            class _PreBlockSession:
                instruction = (
                    "Update specs/26 20 00.docx in /app/output/submission.zip. "
                    "Do not cite UFC or Unified Facilities Criteria. Register R-ILLUM."
                )
                mcp_servers: list[dict] = []
                limits = {"exec_timeout_sec": 60}

                def __init__(self):
                    self.workdir = str(workdir)
                    self.logs: list[str] = []
                    self.final_message = ""
                    self.cancelled = asyncio.Event()
                    self.fixed = False

                def time_left(self):
                    return 3600.0

                async def exec(self, command, timeout_sec=120):
                    if "__PLE_XLSX_BASELINE__" in command:
                        return {"exit_code": 0, "output": "", "timed_out": False}
                    if "__PLE_AUDIT__" in command:
                        res = _run_audit_script(
                            workdir,
                            required_register_ids=["R-ILLUM"],
                            forbidden_citations=["UFC"],
                        )
                        return {"exit_code": 0, "output": serve.AUDIT_MARKER + json.dumps(res), "timed_out": False}
                    # Fix step invoked by Worker
                    if "fix_submission" in command:
                        with zipfile.ZipFile(out_dir / "submission.zip", "w") as zf:
                            zf.writestr(
                                "specs/26 20 00.docx",
                                _make_minimal_docx_bytes(["Clean spec covering R-ILLUM per NFPA 70."]),
                            )
                        self.fixed = True
                        return {"exit_code": 0, "output": "fixed", "timed_out": False}
                    return {"exit_code": 0, "output": "ok", "timed_out": False}

                async def write_file(self, path, data, append=False):
                    return len(data)

                async def read_file(self, path):
                    return b""

                async def log(self, text):
                    self.logs.append(text)

                async def usage(self, model, inp, out, cached=0):
                    pass

            verifier_calls = []

            def _solve_handler(_idx, kwargs):
                msgs = kwargs.get("messages") or []
                sys_txt = str(msgs[0].get("content") or "") if msgs else ""
                if "check an engineer's work" in sys_txt.lower():
                    verifier_calls.append(len(msgs))
                    return _msg_reply(content="PASS")
                if "extract strict engineering constraints" in sys_txt.lower():
                    return _msg_reply(content='{"deliverables": ["output/submission.zip"], "forbidden_citations": ["UFC"], "required_register_ids": ["R-ILLUM"]}')
                if "A reviewer checked your files" in str(msgs[-1].get("content") or "") and not any(
                    m.get("role") == "tool" and "fixed" in str(m.get("content") or "") for m in msgs
                ):
                    return _msg_reply(tool_calls=[{"id": "fix1", "type": "function", "function": {"name": "bash", "arguments": '{"command":"fix_submission"}'}}])
                return _msg_reply(content="Completed cleanly.")

            async def _run():
                sess = _PreBlockSession()
                client = _ScriptedClient(_solve_handler)
                ans = await serve.solve(sess, client=client)  # type: ignore[arg-type]
                self.assertEqual(ans, "Completed cleanly.")
                self.assertTrue(sess.fixed)
                # Round 1 bypassed VERIFIER completely; Round 2 called VERIFIER once -> PASS!
                self.assertEqual(len(verifier_calls), 1)
                self.assertTrue(any("pre-verifier gate blocked" in lg for lg in sess.logs))

            asyncio.run(_run())

    def test_read_file_dedup_cache_returns_stub_when_unchanged_and_reloads_on_edit_or_prune(self):
        class _ReadCacheSession:
            workdir = "/app"
            limits = {"exec_timeout_sec": 60}
            final_message = ""
            cancelled = asyncio.Event()

            def __init__(self):
                self.logs: list[str] = []
                self.files: dict[str, bytes] = {
                    "/app/agent_view.json": b'{"id": "p6_002", "payload": "' + b"X" * 5000 + b'"}',
                }

            def time_left(self):
                return 3600.0

            async def read_file(self, path):
                return self.files[path]

            async def write_file(self, path, data, append=False):
                raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
                self.files[path] = (self.files.get(path, b"") + raw) if append else raw
                return len(raw)

            async def exec(self, command, timeout_sec=120):
                return {"exit_code": 0, "output": "ok", "timed_out": False}

            async def log(self, text):
                self.logs.append(text)

            async def usage(self, model, inp, out, cached=0):
                pass

        sess = _ReadCacheSession()

        def _handler(step_idx, _kwargs):
            if step_idx == 1:
                return _msg_reply(
                    tool_calls=[{"id": "r1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "agent_view.json"}'}}]
                )
            if step_idx == 2:
                # Identical unchanged re-read -> should hit dedup cache stub
                return _msg_reply(
                    tool_calls=[{"id": "r2", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "agent_view.json"}'}}]
                )
            if step_idx == 3:
                # Overwrite file via write_file -> invalidates cache
                return _msg_reply(
                    tool_calls=[{"id": "w1", "type": "function", "function": {"name": "write_file", "arguments": '{"path": "agent_view.json", "content": "{\\"updated\\": true}"}'}}]
                )
            if step_idx == 4:
                # Re-read after write -> must return fresh full content, NOT cache stub
                return _msg_reply(
                    tool_calls=[{"id": "r3", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "agent_view.json"}'}}]
                )
            if step_idx <= 14:
                return _msg_reply(
                    tool_calls=[{"id": f"b{step_idx}", "type": "function", "function": {"name": "bash", "arguments": '{"command": "echo tick"}'}}]
                )
            if step_idx == 15:
                # After PRUNE_CHECKPOINT_EVERY=12 compacted the >4000 char initial read, reading again or after prune check
                return _msg_reply(content="done")
            return _msg_reply(content="done")

        async def _run():
            client = _ScriptedClient(_handler)
            msgs = serve.conversation("sys {workdir}", sess, "task")
            out = await serve.agent(
                sess,
                msgs,
                serve.TOOLS,
                0.0,
                client=client,
                runtime_cfg={"model": "test", "max_output_tokens": 256, "max_failures": 3},
            )
            self.assertEqual(out, "done")
            # Call 3 saw tool r2's cache stub instead of repeating 5KB
            r2_msg = next(m for m in client.calls[2]["messages"] if m.get("tool_call_id") == "r2")
            self.assertIn("is unchanged since your earlier read", r2_msg["content"])
            # Call 5 saw fresh content after write_file
            r3_msg = next(m for m in client.calls[4]["messages"] if m.get("tool_call_id") == "r3")
            self.assertIn('{"updated": true}', r3_msg["content"])

        asyncio.run(_run())

        # Also verify direct _evict behavior when r1 was compacted in prune_old_tool_outputs
        async def _run_prune_evict():
            cache: dict = {}
            sess2 = _ReadCacheSession()
            fn = {"name": "read_file", "arguments": '{"path": "agent_view.json"}'}
            res1 = await serve.run_tool(sess2, dict(fn), read_file_cache=cache, current_call_id="call_read1")  # type: ignore[arg-type]
            self.assertIn("payload", res1)
            res2 = await serve.run_tool(sess2, dict(fn), read_file_cache=cache, current_call_id="call_read2")  # type: ignore[arg-type]
            self.assertIn("is unchanged since your earlier read", res2)
            # Simulate external modification (e.g. bash sed) without write_file tool
            sess2.files["/app/agent_view.json"] = b'{"external": "edit"}'
            res3 = await serve.run_tool(sess2, dict(fn), read_file_cache=cache, current_call_id="call_read3")  # type: ignore[arg-type]
            self.assertEqual(res3, '{"external": "edit"}')

        asyncio.run(_run_prune_evict())

    def test_mep_connectivity_gate_blocks_unfed_and_disconnected_busway(self):
        hints = serve.extract_mep_service_entry_hints(
            "Route E-101 busway service entry at (200, 4910) mm and M-101 chilled water entry at (0, 12300) mm."
        )
        self.assertIn("busway", hints)
        self.assertEqual(hints["busway"][0], [0.2, 4.91])
        self.assertIn("chw_pipe", hints)
        self.assertEqual(hints["chw_pipe"][0], [0.0, 12.3])

        fake_ifc_pkg = '''
class _FakeBox:
    def __init__(self, x0, y0, z0, x1, y1, z1):
        corners = [
            (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
            (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1),
        ]
        self.verts = [c for pt in corners for c in pt]
        self.faces = [0,1,2, 0,2,3, 4,5,6, 4,6,7, 0,1,5, 0,5,4, 2,3,7, 2,7,6]

class _FakeShape:
    def __init__(self, box):
        self.geometry = box

class _FakeProd:
    def __init__(self, pid, cls, name, obj_type, pset_props, box):
        self._id = pid
        self._cls = cls
        self.Name = name
        self.ObjectType = obj_type
        self.GlobalId = f"GID_{pid}"
        self.Representation = True
        self.IsDefinedBy = []
        self._psets = {"Pset_Test": pset_props} if pset_props else {}
        self._box = box
    def id(self): return self._id
    def is_a(self, target=None):
        if target is None: return self._cls
        return self._cls == target

class _FakeModel:
    def __init__(self, prods):
        self._prods = prods
    def by_type(self, t):
        if t == "IfcProduct": return list(self._prods)
        return []

class _UtilElem:
    @staticmethod
    def get_psets(prod):
        return prod._psets

class _Util:
    element = _UtilElem()

class _Geom:
    class settings:
        USE_WORLD_COORDS = "use-world-coords"
        def set(self, k, v): pass
    @staticmethod
    def create_shape(_settings, prod):
        return _FakeShape(_FakeBox(*prod._box))

import json, pathlib
_CFG = json.loads(pathlib.Path("fake_scene.json").read_text())
def open(_path):
    return _FakeModel([_FakeProd(i+1, r["cls"], r["name"], r["obj"], r["pset"], r["box"]) for i, r in enumerate(_CFG["elems"])])
geom = _Geom()
util = _Util()
'''

        def _run_with_scene(elems_cfg: list[dict], recon: dict | None = None) -> dict:
            with tempfile.TemporaryDirectory() as tmp:
                wdir = Path(tmp)
                pkg = wdir / "ifcopenshell"
                pkg.mkdir()
                (pkg / "__init__.py").write_text(fake_ifc_pkg, encoding="utf-8")
                (pkg / "geom.py").write_text(
                    "from ifcopenshell import _Geom as _G\n"
                    "settings = _G.settings\n"
                    "create_shape = _G.create_shape\n",
                    encoding="utf-8",
                )
                (wdir / "fake_scene.json").write_text(json.dumps({"elems": elems_cfg}), encoding="utf-8")
                out_dir = wdir / "output"
                out_dir.mkdir()
                (out_dir / "model.ifc").write_text("ISO-10303-21;\nEND-ISO-10303-21;\n", encoding="utf-8")
                if recon is not None:
                    (out_dir / "reconciliation.json").write_text(json.dumps(recon), encoding="utf-8")
                return _run_audit_script(
                    wdir,
                    task="Route E-101 busway service entry at (200, 4910) mm and deliver output/model.ifc.",
                )

        # Case A: Unfed busway (far from (200, 4910)) + disconnected second busway segment + unserved Row B
        bad_elems = [
            {"cls": "IfcWall", "name": "WALL-W", "obj": "", "pset": {}, "box": (-0.2, 0.0, 0.0, 0.0, 20.0, 3.0)},
            {"cls": "IfcCableCarrierSegment", "name": "BW-01", "obj": "Busway", "pset": {"Service": "POWER"}, "box": (0.05, 12.60, -0.20, 20.0, 12.73, -0.05)},
            {"cls": "IfcCableCarrierSegment", "name": "BW-ISO", "obj": "Busway", "pset": {"Service": "POWER"}, "box": (15.0, 2.0, -0.20, 18.0, 2.13, -0.05)},
            {"cls": "IfcFurniture", "name": "RACK-B01", "obj": "Rack", "pset": {"Row": "B"}, "box": (3.0, 5.5, 0.0, 3.6, 6.5, 2.0)},
            {"cls": "IfcFurniture", "name": "RACK-B02", "obj": "Rack", "pset": {"Row": "B"}, "box": (4.0, 5.5, 0.0, 4.6, 6.5, 2.0)},
        ]
        bad_res = _run_with_scene(bad_elems)
        blockers_txt = "\n".join(bad_res["pre_verifier_blockers"])
        self.assertIn("disconnected MEP busway", blockers_txt)
        self.assertIn("unfed", blockers_txt)
        self.assertIn("4910", blockers_txt)
        self.assertIn("Row B", blockers_txt)

        # Case B: Single connected busway starting at (0.10, 4.91) within 2500mm of (0.20, 4.91) & covering Row B
        good_elems = [
            {"cls": "IfcWall", "name": "WALL-W", "obj": "", "pset": {}, "box": (-0.2, 0.0, 0.0, 0.0, 20.0, 3.0)},
            {"cls": "IfcCableCarrierSegment", "name": "BW-01", "obj": "Busway", "pset": {"Service": "POWER"}, "box": (0.10, 4.85, -0.20, 12.0, 4.98, -0.05)},
            {"cls": "IfcFurniture", "name": "RACK-B01", "obj": "Rack", "pset": {"Row": "B"}, "box": (3.0, 5.5, 0.0, 3.6, 6.5, 2.0)},
            {"cls": "IfcFurniture", "name": "RACK-B02", "obj": "Rack", "pset": {"Row": "B"}, "box": (4.0, 5.5, 0.0, 4.6, 6.5, 2.0)},
        ]
        good_res = _run_with_scene(good_elems)
        self.assertEqual(good_res["pre_verifier_blockers"], [])
        self.assertEqual(good_res["errors"], [])
        self.assertTrue(good_res["mep_summary"]["busway"]["perimeter_endpoints_mm"])

    def test_drawing_extracted_service_entries_and_non_pathway_pset_isolation(self) -> None:
        """Drawing-extracted entries (E-101/M-101, CONSTRAINTS.json, reconciliation.json, solution.py) are captured and gated."""
        # 1. Multi-source extraction from plan + CONSTRAINTS.json (when task prompt itself has no coordinates)
        ext = serve.extract_mep_service_entry_hints(
            "Build Data Hall 1 coordination model and route overhead MEP from the 7-sheet drawing set.",
            plan="- BW-1 busway (E-101): West wall service entry at X = 200 mm, Y = 4910 mm.\n- CT-1 telecom cable tray enters at [0, 12470] mm.",
            constraints_json=json.dumps(
                {
                    "deliverables": ["output/solution.py", "output/model.ifc"],
                    "service_entries_mm": {
                        "busway": [200, 4910],
                        "cable_tray": {"x_mm": 0, "y_mm": 12470},
                        "chw_pipe": [0, 12470],
                    },
                }
            ),
        )
        self.assertEqual(ext["busway"], [[0.2, 4.91]])
        self.assertEqual(ext["cable_tray"], [[0.0, 12.47]])
        self.assertEqual(ext["chw_pipe"], [[0.0, 12.47]])

        fake_ifc_pkg = '''
class _FakeBox:
    def __init__(self, x0, y0, z0, x1, y1, z1):
        corners = [
            (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
            (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1),
        ]
        self.verts = [c for pt in corners for c in pt]
        self.faces = [0,1,2, 0,2,3, 4,5,6, 4,6,7, 0,1,5, 0,5,4, 2,3,7, 2,7,6]

class _FakeShape:
    def __init__(self, box):
        self.geometry = box

class _FakeProd:
    def __init__(self, pid, cls, name, obj_type, pset_props, box):
        self._id = pid
        self._cls = cls
        self.Name = name
        self.ObjectType = obj_type
        self.GlobalId = f"GID_{pid}"
        self.Representation = True
        self.IsDefinedBy = []
        self._psets = {"Pset_Test": pset_props} if pset_props else {}
        self._box = box
    def id(self): return self._id
    def is_a(self, target=None):
        if target is None: return self._cls
        return self._cls == target

class _FakeModel:
    def __init__(self, prods):
        self._prods = prods
    def by_type(self, t):
        if t == "IfcProduct": return list(self._prods)
        return []

class _UtilElem:
    @staticmethod
    def get_psets(prod):
        return prod._psets

class _Util:
    element = _UtilElem()

class _Geom:
    class settings:
        USE_WORLD_COORDS = "use-world-coords"
        def set(self, k, v): pass
    @staticmethod
    def create_shape(_settings, prod):
        return _FakeShape(_FakeBox(*prod._box))

import json, pathlib
_CFG = json.loads(pathlib.Path("fake_scene.json").read_text())
def open(_path):
    return _FakeModel([_FakeProd(i+1, r["cls"], r["name"], r["obj"], r["pset"], r["box"]) for i, r in enumerate(_CFG["elems"])])
geom = _Geom()
util = _Util()
'''

        def _run_offset_case(
            elems_cfg: list[dict],
            *,
            recon: dict | None = None,
            solution_py: str | None = None,
            plan: str = "",
            constraints_json: str = "",
            with_sheets: bool = True,
        ) -> dict:
            with tempfile.TemporaryDirectory() as tmp:
                wdir = Path(tmp)
                pkg = wdir / "ifcopenshell"
                pkg.mkdir()
                (pkg / "__init__.py").write_text(fake_ifc_pkg, encoding="utf-8")
                (pkg / "geom.py").write_text(
                    "from ifcopenshell import _Geom as _G\n"
                    "settings = _G.settings\n"
                    "create_shape = _G.create_shape\n",
                    encoding="utf-8",
                )
                (wdir / "fake_scene.json").write_text(json.dumps({"elems": elems_cfg}), encoding="utf-8")
                if with_sheets:
                    inp_dir = wdir / "input"
                    inp_dir.mkdir()
                    (inp_dir / "E-101.png").write_bytes(b"\x89PNG\r\n\x1a\n")
                    (inp_dir / "M-101.png").write_bytes(b"\x89PNG\r\n\x1a\n")
                out_dir = wdir / "output"
                out_dir.mkdir()
                (out_dir / "model.ifc").write_text("ISO-10303-21;\nEND-ISO-10303-21;\n", encoding="utf-8")
                if recon is not None:
                    (out_dir / "reconciliation.json").write_text(json.dumps(recon), encoding="utf-8")
                if solution_py is not None:
                    (out_dir / "solution.py").write_text(solution_py, encoding="utf-8")
                return _run_audit_script(
                    wdir,
                    task="Build Data Hall 1 coordination model and route MEP from input drawings.",
                    plan=plan,
                    constraints_json=constraints_json,
                )

        # Slab-bounded hall (no IfcWall!), plus Sprinklers with ClearanceToTrayMm & CRAHs with ServicePipeSize
        # AND a segment named BW-ENTRY-N at Y=12.47m (wrongly placed near top-west instead of (0.20, 4.91)m!)
        wrong_offset_elems = [
            {"cls": "IfcSlab", "name": "FLOOR-1", "obj": "AccessFloor", "pset": {}, "box": (0.0, 0.0, -0.4, 21.6, 14.4, 0.0)},
            {"cls": "IfcFireSuppressionTerminal", "name": "SPK-A01", "obj": "Sprinkler", "pset": {"ClearanceToTrayMm": 520}, "box": (5.0, 5.0, 3.2, 5.1, 5.1, 3.3)},
            {"cls": "IfcFireSuppressionTerminal", "name": "SPK-A02", "obj": "Sprinkler", "pset": {"ClearanceToTrayMm": 520}, "box": (9.0, 5.0, 3.2, 9.1, 5.1, 3.3)},
            {"cls": "IfcUnitaryEquipment", "name": "CRAH-01", "obj": "CRAH", "pset": {"ServicePipeSize": "DN80"}, "box": (1.0, 11.5, 0.0, 2.0, 12.5, 2.2)},
            {"cls": "IfcCableCarrierSegment", "name": "CT-ENTRY", "obj": "CableTray", "pset": {"Service": "TELECOM"}, "box": (0.05, 12.40, 2.62, 10.0, 12.55, 2.74)},
            {"cls": "IfcCableCarrierSegment", "name": "BW-ENTRY-N", "obj": "Busway", "pset": {"Service": "POWER"}, "box": (0.10, 12.40, 3.15, 10.0, 12.55, 3.30)},
            {"cls": "IfcPipeSegment", "name": "CHW-ENTRY", "obj": "ChilledWater", "pset": {"Service": "CHILLED_WATER"}, "box": (0.05, 11.80, 2.80, 10.0, 11.95, 2.95)},
        ]

        # 2a. E-101/M-101 exist, Worker wrote NO service_entries_mm anywhere -> blocked!
        res_missing = _run_offset_case(wrong_offset_elems, recon={"project": "Meridian"})
        miss_txt = "\n".join(res_missing["pre_verifier_blockers"])
        self.assertIn("no drawing-extracted service entry coordinate", miss_txt)
        self.assertNotIn("disconnected MEP cable_tray", miss_txt)  # Sprinklers NOT misclassified as cable_tray!
        self.assertNotIn("disconnected MEP chw_pipe", miss_txt)  # CRAHs NOT misclassified as chw_pipe!

        # 2b. CONSTRAINTS.json (or reconciliation.json) extracted busway (200, 4910) from E-101,
        # while BW-ENTRY-N was routed to (100, 12470) -> blocked as unfed at (200, 4910) (BW-ENTRY-N does NOT self-satisfy port_elems!)
        c_json = json.dumps(
            {
                "service_entries_mm": {
                    "busway": [200, 4910],
                    "cable_tray": [0, 12470],
                    "chw_pipe": [0, 12000],
                }
            }
        )
        res_unfed = _run_offset_case(wrong_offset_elems, constraints_json=c_json)
        unfed_txt = "\n".join(res_unfed["pre_verifier_blockers"])
        self.assertIn("busway unfed", unfed_txt)
        self.assertIn("(200, 4910)", unfed_txt)

        # 2c. Worker falsely wrote (0, 12470) in reconciliation.json while Planner/CONSTRAINTS extracted (200, 4910):
        # emits both conflict warning AND co-location warning for Verifier!
        res_conflict = _run_offset_case(
            wrong_offset_elems,
            recon={
                "service_entries_mm": {
                    "BW-1": {"xy_mm": [0, 12470]},
                    "CT-1": [0, 12470],
                    "CHW-1": [0, 12000],
                }
            },
            constraints_json=c_json,
        )
        warn_txt = "\n".join(res_conflict["warnings"])
        self.assertIn("service-entry coordinate conflict for busway", warn_txt)
        self.assertIn("share nearly identical declared service-entry coordinates", warn_txt)

        # 2d. Fixed routing: BW-ENTRY routed to (200, 4910) & recorded in reconciliation.json -> 0 blockers, slab perimeter endpoints reported!
        fixed_elems = list(wrong_offset_elems[:-2]) + [
            {"cls": "IfcCableCarrierSegment", "name": "BW-ENTRY", "obj": "Busway", "pset": {"Service": "POWER"}, "box": (0.20, 4.85, 3.15, 10.0, 4.98, 3.30)},
            {"cls": "IfcPipeSegment", "name": "CHW-ENTRY", "obj": "ChilledWater", "pset": {"Service": "CHILLED_WATER"}, "box": (0.05, 11.80, 2.80, 10.0, 11.95, 2.95)},
        ]
        res_fixed = _run_offset_case(
            fixed_elems,
            recon={
                "reconciled": {
                    "service_entries_mm": {
                        "busway": [200, 4910],
                        "cable_tray": [0, 12470],
                        "chw_pipe": [0, 12000],
                    }
                }
            },
            constraints_json=c_json,
        )
        self.assertEqual(res_fixed["pre_verifier_blockers"], [])
        self.assertEqual(res_fixed["errors"], [])
        self.assertTrue(res_fixed["mep_summary"]["busway"]["perimeter_endpoints_mm"])

    def test_openai_api_status_error_mock_request_has_headers_for_sdk_v2(self) -> None:
        """Regression test for GitHub Actions Unit Tests (#63-#69) with openai v2 _error_request(request.headers)."""
        import openai

        try:
            import httpx

            req: object = httpx.Request("POST", "https://api.example/v1/chat/completions")
        except ImportError:
            req = SimpleNamespace(method="POST", url="https://api.example/v1/chat/completions", headers={})
        resp = SimpleNamespace(status_code=429, headers={"Retry-After": "2"}, request=req)
        err = openai.APIStatusError("quota 429", response=resp, body=None)  # type: ignore[arg-type]
        self.assertTrue(hasattr(err.request, "headers"))
        self.assertFalse(any(k in err.request.headers for k in ("authorization", "api-key")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
