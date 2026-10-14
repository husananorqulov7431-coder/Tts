import unittest

from processor import _atempo_chain, _clean_caption


class ProcessorHelpersTest(unittest.TestCase):
    def test_caption_cleanup_removes_markup_and_fixes_line_breaks(self) -> None:
        self.assertEqual(_clean_caption(r"<i>Salom</i>\N dunyo"), "Salom dunyo")

    def test_atempo_chain_handles_normal_speed(self) -> None:
        self.assertEqual(_atempo_chain(1.5), "atempo=1.50000")

    def test_atempo_chain_splits_large_factors(self) -> None:
        self.assertEqual(_atempo_chain(4.5), "atempo=2.0,atempo=2.0,atempo=1.12500")


if __name__ == "__main__":
    unittest.main()
