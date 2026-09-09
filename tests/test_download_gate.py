# ---------------- 下载身份闸（v2.9.6）----------------
# 事故背景：OA fallback 把几百 MB 的临床数据"论文集"当 sensor 论文入库，RAG 卡死。
# 闸的设计：全文（前 6000 字符）token 与标题覆盖率 >=60% 放行；无大小限制
# （合集含目标文即放行）；PDF 提取失败不拦（扫描版宁可放过）。

import importlib.util
import os

import pytest

pytest.importorskip("fitz")

SPEC = importlib.util.spec_from_file_location(
    "tool", os.path.join(os.path.dirname(__file__), "..", "tool.py")
)
tool_mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool_mod)
Tools = tool_mod.Tools


def _pdf(text: str) -> bytes:
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    # insert_text 每页容量有限，多铺几行
    y = 72
    for line in [text[i:i + 80] for i in range(0, len(text), 80)][:40]:
        page.insert_text((72, y), line)
        y += 18
    return doc.tobytes()


class TestTitleTokens:
    def test_english_stops_removed(self):
        tok = Tools._title_tokens("Effect of the Glucose Sensor on Blood")
        assert "glucose" in tok and "sensor" in tok and "blood" in tok
        assert "of" not in tok and "the" not in tok and "on" not in tok

    def test_chinese_split_per_char(self):
        tok = Tools._title_tokens("连续血糖监测研究")
        assert {"连", "续", "血", "糖"} <= tok
        assert "研" in tok

    def test_numbers_kept(self):
        assert "2024" in Tools._title_tokens("COVID-19 vaccine 2024")

    def test_empty_returns_empty(self):
        assert Tools._title_tokens("!@# ...") == set()


def _extracted(text: str) -> str:
    """模拟 _pdf_to_text 之后的全文页文本（直接给 str，省去 fitz 往返）。"""
    return text


class TestTitleInPdf:
    TITLE = "Highly accurate protein structure prediction with AlphaFold"

    def test_same_paper_passes(self):
        body = (
            "Highly accurate protein structure prediction with AlphaFold. "
            "John Jumper et al. We introduce a transformer. " * 30
        )
        ok, ratio = Tools._title_in_pdf(_extracted(body), self.TITLE)
        assert ok and ratio >= 0.6

    def test_wrong_document_fails(self):
        # 临床数据论文集：标题 token 全不出现
        body = ("Clinical validation dataset collection. Cohort enrollment and "
                "laboratory procedures appendix. " * 60)
        ok, ratio = Tools._title_in_pdf(_extracted(body), self.TITLE)
        assert not ok and ratio < 0.6

    def test_collection_containing_target_passes(self):
        # 论文集首页含目标文标题 → 放行（用户要求：合集含目标文即可）
        body = ("Proceedings volume. " + self.TITLE + ". Also other studies. " * 20)
        ok, ratio = Tools._title_in_pdf(_extracted(body), self.TITLE)
        assert ok

    def test_scanned_pdf_no_tokens_not_blocked(self):
        # 全是符号/空 token → 无法校验，不拦
        ok, _ = Tools._title_in_pdf("· · · — —", self.TITLE)
        assert ok

    def test_empty_title_not_blocked(self):
        ok, _ = Tools._title_in_pdf("anything", "   ")
        assert ok


class TestVerifyGate:
    def _tools(self):
        t = Tools()
        # 不让任何上传真的发生
        t._upload_pdf = lambda *a, **k: "uploaded"
        return t

    def test_gate_raises_on_mismatch(self):
        t = self._tools()
        wrong = _pdf("Clinical dataset collection appendix. " * 50)
        with pytest.raises(RuntimeError, match="身份校验未通过"):
            t._verify_downloaded_pdf(wrong, "Deep Learning for Protein Folding", "test")

    def test_gate_passes_on_match(self):
        t = self._tools()
        body = "Deep learning for protein folding. We train a network. " * 30
        t._verify_downloaded_pdf(_pdf(body), "Deep Learning for Protein Folding", "test")

    def test_gate_passes_on_extraction_failure(self):
        t = self._tools()
        # 非 PDF 字节 → _pdf_to_text 抛异常 → 不拦
        t._verify_downloaded_pdf(b"not a pdf at all", "Some Title", "test")

    def test_path1_returns_error_json(self):
        """路径1（pdf_url 直下）被闸拦下时返回结构化 error 而不是静默落 fallback。"""
        import asyncio

        t = self._tools()
        t.valves.shared_download_dir = "/tmp"

        class _R:
            content = _pdf("Unrelated clinical dataset appendix. " * 50)
            status_code = 200

            def raise_for_status(self):
                return None

        import requests as rq
        orig = rq.get
        rq.get = lambda *a, **k: _R()
        try:
            out = asyncio.run(t.download_paper_to_knowledge(
                title="A Wireless Glucose Sensor",
                pdf_url="https://example.org/wrong.pdf",
            ))
        finally:
            rq.get = orig
        assert "身份校验未通过" in out

    def test_path2_endpoint_success(self):
        """v2.9.7 路径2 默认走 papers-service 端点：200 PDF → 上传。"""
        import asyncio

        t = self._tools()
        uploaded = []
        t._upload_pdf = lambda *a, **k: uploaded.append(a) or "✅ ok"

        class _R:
            status_code = 200
            content = _pdf("Glucose sensor wireless implantable paper " * 30)
            headers = {"X-Download-Via": "unpaywall"}

            def json(self):
                raise ValueError("binary")

        import requests as rq
        orig = rq.post
        rq.post = lambda url, **k: _R()
        try:
            out = asyncio.run(t.download_paper_to_knowledge(
                title="A Wireless Glucose Sensor",
                source="crossref", paper_id="10.1/x", doi="10.1/x",
            ))
        finally:
            rq.post = orig
        assert "✅ ok" in out
        assert len(uploaded) == 1

    def test_path2_endpoint_gate_reject_returns_detail(self):
        """papers-service 服务端闸拒（404+attempts）→ 结构化 error 带 attempts。"""
        import asyncio

        t = self._tools()

        class _R:
            status_code = 404
            headers = {}

            def json(self):
                return {"detail": "no PDF obtained", "attempts": ["unpaywall: no OA URL"]}

        import requests as rq
        orig = rq.post
        rq.post = lambda url, **k: _R()
        try:
            out = asyncio.run(t.download_paper_to_knowledge(
                title="Another Sensor Paper",
                source="crossref", paper_id="10.1/x", doi="10.1/x",
            ))
        finally:
            rq.post = orig
        assert "完整 fallback 链均未获取到 PDF" in out
        assert "unpaywall" in out

    def test_path2_backend_fallback_when_valve_empty(self, tmp_path):
        """download_fallback_url 留空 → 回退 mcpo 落盘共享卷模式（含本地身份闸）。"""
        import asyncio

        t = self._tools()
        t.valves.download_fallback_url = ""
        t.valves.shared_download_dir = str(tmp_path)
        local = tmp_path / "downloaded.pdf"
        local.write_bytes(_pdf("Unrelated proceedings volume. " * 50))

        uploaded = []
        t._upload_pdf = lambda *a, **k: uploaded.append(a) or "uploaded"

        def _fake_call(tool, args, timeout=180, _retried=False):
            assert tool == "download_with_fallback"
            return str(local)

        t._papers_call = _fake_call
        out = asyncio.run(t.download_paper_to_knowledge(
            title="Another Sensor Paper",
            source="crossref",
            paper_id="10.1/x",
            doi="10.1/x",
        ))
        assert "身份校验未通过" in out
        assert not uploaded  # 没有上传
        assert not local.exists()  # 拒收文件已清理

    def test_path2_backend_fallback_success_uploads(self, tmp_path):
        import asyncio

        t = self._tools()
        t.valves.download_fallback_url = ""
        t.valves.shared_download_dir = str(tmp_path)
        local = tmp_path / "downloaded.pdf"
        local.write_bytes(_pdf("Deep learning for protein folding network. " * 30))
        uploaded = []
        t._upload_pdf = lambda *a, **k: uploaded.append(a) or "✅ ok"
        t._papers_call = lambda tool, args, timeout=180, _retried=False: str(local)
        out = asyncio.run(t.download_paper_to_knowledge(
            title="Deep Learning for Protein Folding",
            source="crossref", paper_id="10.1/x", doi="10.1/x",
        ))
        assert "✅ ok" in out
        assert len(uploaded) == 1
