# -*- coding: utf-8 -*-
import io
import os
import tempfile
import unittest
import zipfile

import requests

from modules.ppt_extractor import (
    PptExtractor,
    download_and_extract,
    download_pptx,
    extract_pptx,
    extract_pptx_bytes,
    select_ppt_resources,
    truncate_text,
)

NS = (
    'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
)


def paragraph(text):
    return f"<a:p><a:r><a:t>{text}</a:t></a:r></a:p>"


def table(rows):
    out = ["<a:tbl>"]
    for row in rows:
        out.append("<a:tr>")
        for cell in row:
            out.append(
                "<a:tc><a:txBody>"
                + paragraph(cell)
                + "</a:txBody></a:tc>"
            )
        out.append("</a:tr>")
    out.append("</a:tbl>")
    return "".join(out)


def slide_xml(*paragraphs, rows=None):
    body = "".join(paragraph(text) for text in paragraphs)
    shape = f"<p:sp><p:txBody>{body}</p:txBody></p:sp>"
    table_shape = ""
    if rows:
        table_shape = (
            "<p:graphicFrame><a:graphic><a:graphicData>"
            + table(rows)
            + "</a:graphicData></a:graphic></p:graphicFrame>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f"<p:sld {NS}><p:cSld><p:spTree>"
        f"{shape}{table_shape}"
        "</p:spTree></p:cSld></p:sld>"
    )


def make_pptx(slides):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for number, xml in slides.items():
            archive.writestr(f"ppt/slides/slide{number}.xml", xml)
    return buffer.getvalue()


def ppt_resource(url, *, completed=True, name="课件"):
    return {
        "resourcesDetail": {
            "resourcesType": 1,
            "resourcesDistributeType": 4,
            "resourcesUrl": url,
            "resourcesName": name,
            "studyStatus": 1 if completed else 0,
        }
    }


class FakeDownloadResponse:
    def __init__(self, data=b"", *, headers=None, error=None, chunks=None):
        self.data = data
        self.headers = headers or {}
        self.error = error
        self.chunks = chunks
        self.closed = False

    def raise_for_status(self):
        if self.error is not None:
            raise self.error

    def iter_content(self, chunk_size=65536):
        if self.chunks is not None:
            yield from self.chunks
        elif self.data:
            yield self.data

    def close(self):
        self.closed = True


class FakeDownloadSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class ExtractionTests(unittest.TestCase):
    def test_extracts_paragraphs_tables_and_numeric_slide_order(self):
        data = make_pptx(
            {
                10: slide_xml("第十页标题"),
                2: slide_xml("第二页标题", "要点一", rows=[["名称", "数值"], ["甲", "1"]]),
            }
        )
        text = extract_pptx_bytes(data)
        self.assertIn("[幻灯片 2]", text)
        self.assertIn("[幻灯片 10]", text)
        self.assertLess(text.index("[幻灯片 2]"), text.index("[幻灯片 10]"))
        self.assertIn("第二页标题", text)
        self.assertIn("名称 | 数值", text)
        self.assertIn("甲 | 1", text)

    def test_extract_pptx_reads_file(self):
        with tempfile.NamedTemporaryFile(suffix=".pptx", delete=False) as file:
            file.write(make_pptx({1: slide_xml("文件课件")}))
            path = file.name
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        self.assertIn("文件课件", extract_pptx(path))

    def test_corrupt_and_non_zip_fall_back_to_empty(self):
        self.assertEqual(extract_pptx_bytes(b"plain text"), "")
        self.assertEqual(extract_pptx_bytes(b"PK\x03\x04broken"), "")
        self.assertEqual(extract_pptx("missing-file.pptx"), "")

    def test_slide_with_dtd_is_skipped(self):
        unsafe = (
            '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]>'
            f"<p:sld {NS}><p:cSld><p:spTree><p:sp><p:txBody>"
            "<a:p><a:r><a:t>&lol;</a:t></a:r></a:p>"
            "</p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
        )
        self.assertEqual(extract_pptx_bytes(make_pptx({1: unsafe})), "")

    def test_truncate_text_respects_limit(self):
        self.assertEqual(truncate_text("abcdef", 3), "abc")
        self.assertEqual(truncate_text("abcdef", 0), "")


class ResourceSelectionTests(unittest.TestCase):
    def test_filters_types_and_prefers_completed(self):
        resources = [
            ppt_resource("https://example.com/unfinished.pptx", completed=False),
            {"resourcesDetail": {"resourcesType": 2, "resourcesDistributeType": 4,
                                 "resourcesUrl": "https://example.com/video.mp4"}},
            {"resourcesDetail": {"resourcesType": 1, "resourcesDistributeType": 5,
                                 "resourcesUrl": "https://example.com/other.pptx"}},
            {"resourcesDetail": {"resourcesType": 1, "resourcesDistributeType": 4,
                                 "resourcesUrl": ""}},
            ppt_resource("https://example.com/done.pptx", completed=True, name="已完成"),
        ]
        selected = select_ppt_resources(resources)
        self.assertEqual(len(selected), 2)
        self.assertTrue(selected[0]["completed"])
        self.assertEqual(selected[0]["name"], "已完成")


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for name in os.listdir(self.temp_dir):
            try:
                os.remove(os.path.join(self.temp_dir, name))
            except OSError:
                pass
        try:
            os.rmdir(self.temp_dir)
        except OSError:
            pass

    def test_download_extracts_and_removes_temp_file(self):
        data = make_pptx({1: slide_xml("下载课件")})
        session = FakeDownloadSession(FakeDownloadResponse(data))
        text = download_and_extract(
            "https://example.com/lesson.pptx",
            session=session,
            temp_dir=self.temp_dir,
        )
        self.assertIn("下载课件", text)
        self.assertEqual(os.listdir(self.temp_dir), [])
        self.assertTrue(session.response.closed)

    def test_failed_download_leaves_no_temp_file(self):
        session = FakeDownloadSession(requests.RequestException("boom"))
        text = download_and_extract(
            "https://example.com/lesson.pptx",
            session=session,
            temp_dir=self.temp_dir,
        )
        self.assertEqual(text, "")
        self.assertEqual(os.listdir(self.temp_dir), [])

    def test_non_zip_download_is_ignored(self):
        session = FakeDownloadSession(FakeDownloadResponse(b"PDF-1.7 data"))
        text = download_and_extract(
            "https://example.com/lesson.pptx",
            session=session,
            temp_dir=self.temp_dir,
        )
        self.assertEqual(text, "")
        self.assertEqual(os.listdir(self.temp_dir), [])

    def test_oversized_download_is_rejected(self):
        response = FakeDownloadResponse(
            headers={"Content-Length": "999999"},
            chunks=[b"PK\x03\x04"],
        )
        self.assertIsNone(
            download_pptx(
                "https://example.com/lesson.pptx",
                session=FakeDownloadSession(response),
                max_bytes=10,
            )
        )

    def test_extractor_caches_successful_parse(self):
        data = make_pptx({1: slide_xml("缓存课件")})
        session = FakeDownloadSession(FakeDownloadResponse(data))
        extractor = PptExtractor(session=session, temp_dir=self.temp_dir)
        resources = [ppt_resource("https://example.com/cache.pptx")]
        first = extractor.collect(resources)
        second = extractor.collect(resources)
        self.assertEqual(first, second)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(os.listdir(self.temp_dir), [])

    def test_extractor_skips_legacy_and_splits_budget(self):
        session = FakeDownloadSession(FakeDownloadResponse(b"unused"))
        extractor = PptExtractor(
            session=session,
            temp_dir=self.temp_dir,
            total_char_limit=15,
            per_ppt_char_limit=10,
        )
        legacy = [ppt_resource("https://example.com/old.ppt")]
        self.assertEqual(extractor.collect(legacy), [])
        self.assertEqual(session.calls, [])

        extractor.extract_url = (
            lambda url, referer="": "A" * 10 if url.endswith("a.pptx") else "B" * 10
        )
        materials = extractor.collect(
            [
                ppt_resource("https://example.com/a.pptx"),
                ppt_resource("https://example.com/b.pptx"),
            ]
        )
        self.assertEqual([len(item["content"]) for item in materials], [10, 5])


if __name__ == "__main__":
    unittest.main()