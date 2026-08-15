import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from app import recommendation_jobs, scheduler


ROOT = Path(__file__).resolve().parents[1]


class BlastRadiusTests(unittest.TestCase):
    def test_remote_dispatch_has_one_upstream_request(self):
        source = (ROOT / "plexmind/app/main.py").read_text()
        block = source.split("async def _scripts_request", 1)[1].split("# ---------------------------------------------------------------------------", 1)[0]
        self.assertEqual(block.count("await client.request("), 1)

    def test_scheduler_uses_authoritative_launcher(self):
        calls = []

        async def launcher(job):
            calls.append(job)
            return {"status": "started"}

        scheduler.set_script_launcher(launcher)
        now = datetime.now(scheduler._script_schedule_timezone())
        with patch.object(scheduler, "gpu_info", return_value={"pct": 0, "vendor": "nvidia", "probe_error": None}), \
                patch.object(scheduler, "_script_window_key", return_value="test-window"), \
                patch.object(scheduler, "_SCRIPT_LAST_WINDOW", {}):
            asyncio.run(scheduler._script_window_tick("translate", "Translation", now.hour, now.hour + 1))
        self.assertEqual(calls, ["translate"])

    def test_gpu_telemetry_failure_is_not_assumed_idle(self):
        with patch.object(scheduler, "gpu_info", return_value={"pct": None, "vendor": None, "probe_error": "offline"}):
            with self.assertRaisesRegex(RuntimeError, "refusing to assume idle"):
                asyncio.run(scheduler._wait_for_idle_gpu())

    def test_recommendation_queue_survives_process_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(recommendation_jobs, "DATA_DIR", root), \
                    patch.object(recommendation_jobs, "JOB_FILE", root / "jobs.json"), \
                    patch.object(recommendation_jobs, "LOCK_FILE", root / "jobs.lock"):
                job_id = recommendation_jobs.create("test")
                claimed = recommendation_jobs.claim_next("worker:1")
                self.assertEqual(claimed[0], job_id)
                self.assertEqual(recommendation_jobs.recover_running("restart"), 1)
                self.assertEqual(recommendation_jobs.get(job_id)["status"], "pending")

    def test_release_has_no_floating_runtime_images(self):
        compose = (ROOT / "docker-compose.yml").read_text()
        setup = (ROOT / "setup.sh").read_text()
        self.assertNotIn("linuxserver/ffmpeg:latest", compose)
        self.assertNotIn("openai-whisper-asr-webservice:latest", setup)
        for dockerfile in (ROOT / "plexmind/Dockerfile", ROOT / "scripts/Dockerfile", ROOT / "scripts/Dockerfile.broker"):
            first = dockerfile.read_text().splitlines()[0]
            self.assertIn("@sha256:", first)

    def test_release_publishes_all_custom_images(self):
        workflow = (ROOT / ".github/workflows/docker-publish.yml").read_text()
        self.assertIn("plexmind-scripts", workflow)
        self.assertIn("plexmind-docker-broker", workflow)
        self.assertEqual(workflow.count("docker/build-push-action@"), 6)
        self.assertEqual(workflow.count("anchore/scan-action@"), 3)

    def test_broker_exposes_only_fixed_gpu_probe(self):
        source = (ROOT / "scripts/docker_broker.py").read_text()
        self.assertIn('parts[2] == "gpu"', source)
        self.assertIn('"--query-gpu=name,utilization.gpu,memory.total,memory.free"', source)
        self.assertNotIn("self.rfile.read", source)

    def test_api_compose_has_no_gpu_device_request(self):
        compose = (ROOT / "docker-compose.yml").read_text()
        api_block = compose.split("  plexmind:\n", 1)[1].split("  recommendation-worker:\n", 1)[0]
        self.assertNotIn("capabilities: [gpu]", api_block)

    def test_recommendation_generation_does_not_implicitly_sync(self):
        source = (ROOT / "plexmind/app/recommender.py").read_text()
        function = source.split("async def get_recommendations", 1)[1]
        self.assertNotIn("sync_to_plex", function)


if __name__ == "__main__":
    unittest.main()
