import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "scripts" / "lib.sh"

# Language codes the Whisper ASR webservice accepts (whisper.tokenizer.LANGUAGES).
WHISPER_CODES = set("""
en zh de es ru ko fr ja pt tr pl ca nl ar sv it id hi fi vi he uk el ms cs ro da hu ta no th ur
hr bg lt la mi ml cy sk te fa lv bn sr az sl kn et mk br eu is hy ne mn bs kk sq sw gl mr pa si
km sn yo so af oc ka be tg sd gu am yi lo uz fo ht ps tk nn mt sa lb my bo tl mg as tt haw ln ha
ba jw su yue
""".split())


def lang_code_arms():
    text = LIB.read_text(encoding="utf-8")
    body = re.search(r"(?ms)^normalize_lang_code\(\) \{\n(.*?)^\}", text).group(1)
    return re.findall(r'(?m)^\s+([a-z|]+)\) echo "([a-z]*)" ;;$', body)


class NormalizeLangCodeTableTests(unittest.TestCase):
    def test_every_mapped_tag_is_a_code_whisper_accepts(self):
        arms = lang_code_arms()
        self.assertGreater(len(arms), 30)
        wrong = [(tags, code) for tags, code in arms if code and code not in WHISPER_CODES]
        self.assertEqual(wrong, [])

    def test_no_tag_is_listed_twice(self):
        # bash takes the first matching case arm, so a repeat would be dead code.
        tags = [tag for arm, _ in lang_code_arms() for tag in arm.split("|")]
        self.assertEqual(sorted({t for t in tags if tags.count(t) > 1}), [])

    def test_common_iso_639_2_tags_are_mapped(self):
        mapped = {tag: code for arm, code in lang_code_arms() for tag in arm.split("|")}
        for tag, code in {"swe": "sv", "hin": "hi", "dan": "da", "pol": "pl", "tur": "tr",
                          "heb": "he", "ara": "ar", "tha": "th", "nld": "nl", "dut": "nl"}.items():
            self.assertEqual(mapped.get(tag), code, tag)


if __name__ == "__main__":
    unittest.main()
