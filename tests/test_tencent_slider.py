import unittest

from modules.slider import is_failure_tip
from modules.tencent_slider import (
    PIECE_OFFSET_LIMIT_PX,
    classify_drag_items,
    gap_to_drag_distance,
    locate_gap,
    parse_background_url,
    resolve_media_url,
)

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = None
    np = None


class BackgroundUrlTests(unittest.TestCase):
    def test_extracts_absolute_url(self):
        style = 'url("https://t.captcha.qq.com/cap_union_new_getcapbysig?x=1")'
        self.assertEqual(
            parse_background_url(style),
            "https://t.captcha.qq.com/cap_union_new_getcapbysig?x=1",
        )

    def test_extracts_relative_url_without_quotes(self):
        style = "url(/cap_union_new_getcapbysig?image=abc)"
        self.assertEqual(parse_background_url(style), "/cap_union_new_getcapbysig?image=abc")

    def test_returns_none_for_no_image(self):
        self.assertIsNone(parse_background_url("none"))
        self.assertIsNone(parse_background_url(""))

    def test_resolves_relative_against_frame_url(self):
        base = "https://t.captcha.qq.com/cap_union_new_show?aid=1"
        self.assertEqual(
            resolve_media_url(base, "/cap_union_new_getcapbysig?x=1"),
            "https://t.captcha.qq.com/cap_union_new_getcapbysig?x=1",
        )
        self.assertEqual(
            resolve_media_url(base, "https://cdn.example.com/a.png"),
            "https://cdn.example.com/a.png",
        )


class DragItemClassificationTests(unittest.TestCase):
    def test_sorts_track_thumb_and_missing_block_by_width(self):
        # 新版三件套宽度约为 280/54/50, 但位置随机。
        self.assertEqual(classify_drag_items([280, 54, 50]), (0, 1, 2))
        self.assertEqual(classify_drag_items([50, 280, 54]), (1, 2, 0))
        self.assertEqual(classify_drag_items([54, 50, 280]), (2, 0, 1))

    def test_needs_three_items(self):
        self.assertIsNone(classify_drag_items([280, 54]))


class DragDistanceTests(unittest.TestCase):
    def test_scales_gap_and_subtracts_piece_offset(self):
        # 672px 原图显示为 280px: 缺口 336 -> 140, 拼图初始偏移 10 -> 拖动 130。
        self.assertAlmostEqual(gap_to_drag_distance(336, 672, 280, 10), 130.0)

    def test_out_of_range_offset_is_ignored(self):
        self.assertAlmostEqual(gap_to_drag_distance(336, 672, 280, -20), 140.0)
        self.assertAlmostEqual(
            gap_to_drag_distance(336, 672, 280, PIECE_OFFSET_LIMIT_PX + 1), 140.0
        )

    def test_never_negative(self):
        self.assertEqual(gap_to_drag_distance(10, 672, 280, 60), 0.0)

    def test_invalid_width_returns_zero(self):
        self.assertEqual(gap_to_drag_distance(336, 0, 280, 0), 0.0)


class FailureTipTests(unittest.TestCase):
    def test_slider_hint_is_not_failure(self):
        self.assertFalse(is_failure_tip("拖动下方滑块完成拼图"))

    def test_retry_hint_is_failure(self):
        self.assertTrue(is_failure_tip("验证失败，请重试"))


@unittest.skipUnless(cv2 is not None and np is not None, "需要 opencv 与 numpy")
class LocateGapTests(unittest.TestCase):
    def test_matches_puzzle_silhouette(self):
        scale = 3
        width, height = 480, 270
        background = np.full((height, width, 3), 110, np.uint8)
        puzzle = np.zeros((40, 48), np.uint8)
        puzzle[8:32, 6:22] = 255
        puzzle[14:26, 22:36] = 255

        hole_x, hole_y = 217, 90
        region = background[hole_y : hole_y + 40, hole_x : hole_x + 48]
        region[puzzle > 0] = (40, 40, 40)

        piece = np.zeros((40, 48, 4), np.uint8)
        piece[:, :, :3] = (180, 160, 120)
        piece[:, :, 3] = puzzle

        gap_x, gap_y, score = locate_gap(background, piece, np, cv2)
        # 拼图块按 alpha 裁剪掉了 6px 左边距, 因此匹配点落在缺口左侧 6px 处。
        self.assertAlmostEqual(gap_x, hole_x + 6, delta=scale)
        self.assertAlmostEqual(gap_y, hole_y + 8, delta=scale)
        self.assertGreater(score, 0.2)

    def test_rejects_piece_larger_than_background(self):
        background = np.zeros((20, 20, 3), np.uint8)
        piece = np.zeros((40, 40, 3), np.uint8)
        with self.assertRaises(ValueError):
            locate_gap(background, piece, np, cv2)


if __name__ == "__main__":
    unittest.main()