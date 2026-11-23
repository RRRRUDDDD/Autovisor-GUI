import unittest

from modules.html_utils import extract_plain_text, has_meaningful_image


class ExtractPlainTextTests(unittest.TestCase):
    def test_strips_tags_and_unwraps_entities(self):
        html = "<div><p>已知&amp;求：&lt;x&gt;</p><p>选&nbsp;项</p></div>"
        self.assertEqual(extract_plain_text(html), "已知&求：<x> 选 项")

    def test_collapses_whitespace_and_ignores_scripts(self):
        html = (
            "<style>.a{color:red}</style>"
            "<script>var x = 1;</script>"
            "<p>第一行<br/>第二行\t 第三行\u3000结束</p>"
        )
        self.assertEqual(extract_plain_text(html), "第一行 第二行 第三行 结束")

    def test_handles_empty_and_none(self):
        self.assertEqual(extract_plain_text(""), "")
        self.assertEqual(extract_plain_text(None), "")

    def test_broken_html_does_not_raise(self):
        self.assertEqual(extract_plain_text("<p>未闭合<div>文本"), "未闭合 文本")


class MeaningfulImageTests(unittest.TestCase):
    def test_detects_img_tag_case_insensitively(self):
        self.assertTrue(has_meaningful_image('<p>题目<img src="q.png"/></p>'))
        self.assertTrue(has_meaningful_image("<IMG SRC='a.jpg'>"))
        self.assertTrue(has_meaningful_image('<img src="data:image/png;base64,AAAA">'))

    def test_plain_text_and_spacer_gif_have_no_meaningful_image(self):
        self.assertFalse(has_meaningful_image("<p>纯文字题干</p>"))
        self.assertFalse(has_meaningful_image('<img src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7">'))
        self.assertFalse(has_meaningful_image('<img>'))
        self.assertFalse(has_meaningful_image(None))


if __name__ == "__main__":
    unittest.main()