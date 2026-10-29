import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
CUES = "".join(f"{i}\n00:00:0{i},000 --> 00:00:0{i},500\nLine number {i} is here.\n\n" for i in range(1, 6))


class MaintenanceEncodingScanTests(unittest.TestCase):
    def test_library_scan_classifies_every_srt_in_one_helper_run(self):
        maintenance = (SCRIPTS / "maintenance.sh").read_text(encoding="utf-8")
        match = re.search(r'srt_encoding\.py\)" check --limit (\d+)', maintenance)
        self.assertIsNotNone(match, "maintenance.sh no longer pre-filters with srt_encoding.py check")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = []
            for i in range(250):  # more than the audit's sample of 200
                path = root / f"Clean{i}.en.srt"
                path.write_text(CUES, encoding="utf-8")
                paths.append(path)
            problems = {root / "Bom.en.srt": ("utf-8-bom", b"\xef\xbb\xbf" + CUES.encode("utf-8"))}
            if os.name == "posix":  # Windows names cannot hold '|'
                problems[root / "Pipe | Show.en.srt"] = ("utf-8-bom", b"\xef\xbb\xbf" + CUES.encode("utf-8"))
            problems[root / "Latin.fr.srt"] = ("non-utf-8", (CUES + "Café\n").encode("latin-1"))
            for path, (_, data) in problems.items():
                path.write_bytes(data)
            paths += list(problems)

            listing = b"".join(os.fsencode(p) + b"\0" for p in paths)
            result = subprocess.run([sys.executable, str(SCRIPTS / "srt_encoding.py"), "check", "--limit", match.group(1)],
                                    input=listing, capture_output=True, timeout=60)

        self.assertEqual(result.returncode, 0, result.stderr)
        records = result.stdout.split(b"\0")
        self.assertEqual(records.pop(), b"")
        # maintenance.sh splits each record with IFS='|' read -r -d '' ENC SRT.
        fields = [record.partition(b"|")[::2] for record in records]
        self.assertEqual(fields[-1], (b"checked", str(len(paths)).encode()))
        self.assertEqual(fields[:-1], [(kind.encode(), os.fsencode(p)) for p, (kind, _) in problems.items()])


if __name__ == "__main__":
    unittest.main()
