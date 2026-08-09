import unittest

from backend.app.services.alignment.sentence_splitter import (
    _align_clean,
    _split_for_readability,
    _subtitle_split_units,
)


class SentenceSplitterRegressionTests(unittest.TestCase):
    def split(self, text: str):
        return _split_for_readability(text, min_chars=10, max_chars=32)

    def assert_preserves_spoken_text(self, source: str, chunks: list[str]) -> None:
        self.assertEqual(
            _align_clean("".join(chunks)).lower(),
            _align_clean(source).lower(),
        )

    def assert_no_accidental_tiny_chunks(self, chunks: list[str]) -> None:
        tiny = [chunk for chunk in chunks if _subtitle_split_units(chunk) < 6.0]
        self.assertEqual(tiny, [])

    def test_english_titles_clocks_and_versions_remain_atomic(self):
        source = (
            "Mr. Smith met Prof. Wang at 3:45 p.m. "
            "They reviewed version v2.1.0 and compared it with the previous release."
        )
        chunks = self.split(source)
        self.assert_preserves_spoken_text(source, chunks)
        self.assert_no_accidental_tiny_chunks(chunks)
        joined = "\n".join(chunks)
        self.assertIn("Mr. Smith", joined)
        self.assertIn("Prof. Wang", joined)
        self.assertIn("3:45 p.m.", joined)
        self.assertIn("v2.1.0", joined)

    def test_english_reference_abbreviations_do_not_create_orphans(self):
        source = (
            "Dr. Chen fine-tunes a Transformer on 1.23 million samples. "
            "The CUDA 13.0 runtime processes tensors on an NVIDIA GPU. "
            "In Fig. 3, the API retries at 10:30 a.m. when the server is busy."
        )
        chunks = self.split(source)
        self.assert_preserves_spoken_text(source, chunks)
        self.assert_no_accidental_tiny_chunks(chunks)
        self.assertFalse(any(chunk in {"Dr", "Mr", "Ms", "Prof", "Fig"} for chunk in chunks))
        self.assertIn("Dr. Chen", "\n".join(chunks))
        self.assertIn("Fig. 3", "\n".join(chunks))
        self.assertIn("10:30", "\n".join(chunks))

    def test_mixed_technical_text_preserves_spacing_and_numeric_units(self):
        source = (
            "本系統先以 VoxCPM2 生成語音，RTF 約為 0.12。"
            "The next stage runs Qwen3 ForcedAligner and maps every token to a timestamp. "
            "當輸入包含 900px、1.23GB、CUDA 13.0 或 Dr. Chen 等字串時，"
            "小數點、時間 10:30 與縮寫都不能被當成句尾。"
        )
        chunks = self.split(source)
        self.assert_preserves_spoken_text(source, chunks)
        self.assert_no_accidental_tiny_chunks(chunks)
        joined = "\n".join(chunks)
        self.assertIn("Qwen3 ForcedAligner", joined)
        self.assertIn("1.23GB", joined)
        self.assertIn("CUDA 13.0", joined)
        self.assertIn("Dr. Chen", joined)
        self.assertIn("10:30", joined)

    def test_english_comma_spacing_is_not_destroyed(self):
        source = "First, load the model. Next, run inference. Finally, export the result."
        chunks = self.split(source)
        self.assert_preserves_spoken_text(source, chunks)
        joined = "\n".join(chunks)
        self.assertIn("First, load", joined)
        self.assertIn("Next, run", joined)
        self.assertIn("Finally, export", joined)

    def test_code_and_identifiers_remain_intact(self):
        source = (
            "請執行 `torch.compile(model)`，接著呼叫 main.py，"
            "再把 batch_size 設為 16，避免切壞程式碼或參數名稱。"
        )
        chunks = self.split(source)
        self.assert_preserves_spoken_text(source, chunks)
        joined = "\n".join(chunks)
        self.assertIn("`torch.compile(model)`", joined)
        self.assertIn("main.py", joined)
        self.assertIn("batch_size", joined)

    def test_initials_urls_ports_and_compact_colons_remain_atomic(self):
        source = (
            "J. Smith opens https://localhost:8000/api/status at 10:30. "
            "The response contains mode:fast and version v3.2.1."
        )
        chunks = self.split(source)
        self.assert_preserves_spoken_text(source, chunks)
        self.assert_no_accidental_tiny_chunks(chunks)
        joined = "\n".join(chunks)
        self.assertIn("J. Smith", joined)
        self.assertIn("https://localhost:8000/api/status", joined)
        self.assertIn("10:30", joined)
        self.assertIn("mode:fast", joined)

    def test_closing_quote_stays_with_quoted_sentence(self):
        source = (
            "教授說：「請先載入 CUDA 13.0 runtime，再執行 fine-tune。」"
            "接著系統會輸出結果，並保留原始時間軸。"
        )
        chunks = self.split(source)
        self.assert_preserves_spoken_text(source, chunks)
        for chunk in chunks:
            self.assertFalse(chunk.startswith(("」", "』", "”", "’")))

    def test_rebalancing_never_duplicates_text(self):
        sources = [
            "Wang at 3:45 p.m. They reviewed version v2.1.0 and compared it with the previous release.",
            "教授說：「請先載入 CUDA 13.0 runtime，再執行 fine-tune。」接著系統會輸出結果。",
            "今天介紹 transformer architecture 它透過 multi head attention 建模長距離關係。",
        ]
        for source in sources:
            with self.subTest(source=source):
                self.assert_preserves_spoken_text(source, self.split(source))


if __name__ == "__main__":
    unittest.main()
