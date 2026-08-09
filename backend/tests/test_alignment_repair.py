import unittest

from backend.app.services.alignment.repair import (
    _align_clean,
    _int_to_zh,
    build_repaired_alignment_text,
    find_risky_alignment_units,
    make_repair_plan,
    validate_repaired_spans,
)


class AlignmentRepairRegressionTests(unittest.TestCase):
    def test_chinese_large_number_conversion_keeps_internal_zeroes(self):
        expected = {
            0: "零",
            10: "十",
            101: "一百零一",
            1001: "一千零一",
            10001: "一萬零一",
            10010: "一萬零一十",
            1_000_001: "一百萬零一",
            100_000_001: "一億零一",
        }
        for value, spoken in expected.items():
            with self.subTest(value=value):
                self.assertEqual(_int_to_zh(value), spoken)

    def test_rewrite_is_language_aware(self):
        source = "影像為900px，容量1.23GB，延遲120ms，準確率92.7%。"
        zh_text, zh_map = build_repaired_alignment_text(source, language="Chinese")
        en_text, en_map = build_repaired_alignment_text(source, language="English")
        self.assertIn("九百pixel", zh_text)
        self.assertIn("一點二三GB", zh_text)
        self.assertIn("百分之九十二點七", zh_text)
        self.assertIn("nine hundred pixels", en_text)
        self.assertIn("one point two three gigabytes", en_text)
        self.assertIn("ninety two point seven percent", en_text)
        self.assertEqual(len(zh_map), len(_align_clean(source)))
        self.assertEqual(len(en_map), len(_align_clean(source)))

    def test_identifiers_are_not_partially_rewritten(self):
        source = "保留 900pxValue、foo_120ms 與 model1.23GBeta，但改寫 900px。"
        repaired, char_map = build_repaired_alignment_text(source, language="Chinese")
        self.assertIn("900pxValue", repaired)
        self.assertIn("foo_120ms", repaired)
        self.assertIn("model1.23GBeta", repaired)
        self.assertIn("九百pixel", repaired)
        self.assertEqual(len(char_map), len(_align_clean(source)))

    def test_split_numeric_and_unit_tokens_can_trigger_repair(self):
        words = [
            {"text": "1.23", "start": 1.00, "end": 1.04},
            {"text": "GB", "start": 1.04, "end": 1.06},
            {"text": "資料", "start": 1.55, "end": 1.95},
        ]
        self.assertEqual(find_risky_alignment_units(words), [0, 1])
        plan = make_repair_plan("資料大小為1.23GB", words, language="Chinese")
        self.assertTrue(plan.enabled)
        self.assertIn("一點二三GB", plan.repaired_text)

    def test_natural_pause_after_healthy_unit_does_not_trigger_repair(self):
        words = [
            {"text": "900px", "start": 1.00, "end": 1.70},
            {"text": "接著", "start": 2.25, "end": 2.65},
        ]
        self.assertEqual(find_risky_alignment_units(words), [])

    def test_repair_validation_rejects_bad_second_pass(self):
        source = "資料大小為1.23GB"
        clean_len = len(_align_clean(source))
        good = [(i * 0.05, (i + 1) * 0.05) for i in range(clean_len)]
        ok, reason = validate_repaired_spans(
            source,
            good,
            audio_end=good[-1][1],
            text_match_ratio=0.95,
        )
        self.assertTrue(ok, reason)

        ok, reason = validate_repaired_spans(
            source,
            good,
            audio_end=good[-1][1],
            text_match_ratio=0.20,
        )
        self.assertFalse(ok)
        self.assertIn("text_match_too_low", reason)

        non_monotonic = list(good)
        non_monotonic[3] = (0.01, 0.02)
        ok, reason = validate_repaired_spans(
            source,
            non_monotonic,
            audio_end=good[-1][1],
            text_match_ratio=0.95,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "non_monotonic_span")


if __name__ == "__main__":
    unittest.main()
