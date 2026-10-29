import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


class LibPortabilityTests(unittest.TestCase):
    def test_python_helpers_used_by_lib_are_copied_into_the_image(self):
        lib = (SCRIPTS / "lib.sh").read_text(encoding="utf-8")
        dockerfile = (SCRIPTS / "Dockerfile").read_text(encoding="utf-8")
        helpers = set(re.findall(r"script_helper ([a-z_]+\.py)", lib))
        self.assertLessEqual({"dedup_subs.py", "pgs_cleanup.py", "srt_encoding.py"}, helpers)
        for helper in sorted(helpers):
            self.assertTrue((SCRIPTS / helper).is_file(), helper)
            self.assertRegex(dockerfile, rf"(?m)^COPY [^\n]*(\\\n[^\n]*)*\b{re.escape(helper)}\b", helper)

    def test_scripts_avoid_gnu_only_flags_missing_from_busybox(self):
        # The scripts image is Alpine: chmod/chown/grep come from BusyBox and
        # neither xxd nor GNU iconv is installed.
        forbidden = {
            "chmod/chown --reference": r"\b(?:chmod|chown)\s+--reference",
            "grep -P": r"\bgrep\s+-[a-zA-Z]*P",
            "xxd": r"(?m)^[^#\n]*\bxxd\b",
            "iconv": r"(?m)^[^#\n]*\biconv\b",
        }
        found = []
        for name in ("lib.sh", "watermark.sh", "transcribe.sh", "maintenance.sh"):
            text = (SCRIPTS / name).read_text(encoding="utf-8")
            found += [f"{name}: {label}" for label, pattern in forbidden.items() if re.search(pattern, text)]
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
