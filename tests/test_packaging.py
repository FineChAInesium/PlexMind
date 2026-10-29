import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def compose_service(compose, name, next_name):
    """The text of one docker-compose.yml service, up to the service after it."""
    return compose.split(f"\n  {name}:\n", 1)[1].split(f"\n  {next_name}:\n", 1)[0]


class PackagingTests(unittest.TestCase):
    def test_api_badge_uses_liveness_state_not_dependency_health(self):
        dashboard = (ROOT / "plexmind" / "app" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("const plexOk = apiOnline;", dashboard)
        self.assertNotIn("const plexOk = h && h.status === 'ok';", dashboard)

    def test_only_one_script_source_tree_exists(self):
        self.assertTrue((ROOT / "scripts" / "lib.sh").is_file())
        self.assertFalse((ROOT / "plexmind" / "scripts" / "lib.sh").exists())

    def test_scripts_image_ships_every_script(self):
        dockerfile = (ROOT / "scripts" / "Dockerfile").read_text(encoding="utf-8")
        copied = set()
        for instruction in re.findall(r"(?m)^COPY ((?:[^\n]*\\\n)*[^\n]*)", dockerfile):
            copied.update(instruction.replace("\\\n", " ").split()[:-1])
        # docker_broker.py ships in the broker image instead.
        shipped = {p.name for p in (ROOT / "scripts").iterdir() if p.suffix in (".sh", ".py")} - {"docker_broker.py"}
        self.assertEqual(shipped - copied, set())
        self.assertIn("COPY docker_broker.py", (ROOT / "scripts" / "Dockerfile.broker").read_text(encoding="utf-8"))

    def test_api_image_does_not_package_media_mutation_scripts(self):
        dockerfile = (ROOT / "plexmind" / "Dockerfile").read_text()
        self.assertNotIn("COPY scripts/ ./scripts/", dockerfile)
        self.assertNotIn("ffmpeg", dockerfile)

    def test_unraid_api_template_preserves_least_privilege_boundary(self):
        template = (ROOT / "templates" / "PlexMind.xml").read_text()
        self.assertNotIn("<ContainerDir>/media/movies</ContainerDir>", template)
        self.assertNotIn("<ContainerDir>/media/tv</ContainerDir>", template)
        self.assertNotIn("<ContainerDir>/var/run/docker.sock</ContainerDir>", template)
        self.assertIn("<Name>SCRIPTS_API_URL</Name>", template)
        self.assertIn("<Name>DOCKER_BROKER_URL</Name>", template)

    def test_dashboard_api_key_is_not_persisted_in_browser_storage(self):
        dashboard = (ROOT / "plexmind" / "app" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('JSON.stringify({ apiBase: API_BASE })', dashboard)
        self.assertNotIn("apiBase: API_BASE, apiKey: API_KEY", dashboard)
        self.assertNotIn("sessionStorage.setItem('pm_api_key'", dashboard)

    def test_translation_targets_come_from_worker_configuration(self):
        dashboard = (ROOT / "plexmind" / "app" / "static" / "index.html").read_text(encoding="utf-8")
        controller = (ROOT / "scripts" / "control_server.py").read_text(encoding="utf-8")
        self.assertNotIn('id="target-langs" value="zh,es-MX"', dashboard)
        self.assertIn("health?.scripts?.target_languages", dashboard)
        self.assertIn('"target_languages": _configured_target_languages()', controller)

    def test_dashboard_runtime_configuration_avoids_stale_display_defaults(self):
        dashboard = (ROOT / "plexmind" / "app" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("Scheduled runs use 23:00-03:00", dashboard)
        self.assertIn("data.script_windows?.translate", dashboard)
        self.assertIn("data.cron_hour != null", dashboard)

    def test_storage_display_tolerates_media_filesystem_latency(self):
        dashboard = (ROOT / "plexmind" / "app" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("AbortSignal.timeout(10000)", dashboard)
        self.assertIn("storageReadingLoaded", dashboard)
        self.assertNotIn("storage-label').textContent = 'N/A'", dashboard)

    def test_documented_script_configuration_is_wired(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        for key in (
            "ENABLE_WATERMARK", "WATERMARK_SEARCH", "KNOWN_BILINGUAL_TITLES",
            "KNOWN_ENGLISH_REALITY_TITLES", "SUBTITLE_FILE_MODE", "PGS_CLEANUP_ALLOW_UNKNOWN",
        ):
            self.assertIn(f"- {key}=", compose)

    def test_new_runtime_settings_reach_their_services(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
        expectations = {
            ("plexmind", "recommendation-worker"): ("PLEXMIND_SECURE_COOKIE", "GPU_BUSY_SAMPLES", "GPU_SAMPLE_INTERVAL_SECONDS"),
            ("recommendation-worker", "scripts"): ("GPU_BUSY_SAMPLES", "GPU_SAMPLE_INTERVAL_SECONDS"),
            ("scripts", "ffmpeg"): ("TRANSLATION_BATCH_CUES",),
        }
        for (service, following), keys in expectations.items():
            block = compose_service(compose, service, following)
            for key in keys:
                self.assertIn(f"- {key}=${{{key}:-", block, f"{key} not passed to {service}")
                self.assertIn(f"\n{key}=", env_example, f"{key} not documented in .env.example")

    def test_compose_keeps_sidecar_ports_and_privileges_narrow(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
        # llama.cpp has no API key; PlexMind reaches both sidecars on the Docker network.
        self.assertIn('"${LLAMA_CPP_BIND_ADDRESS:-127.0.0.1}:${LLAMA_CPP_HOST_PORT:-11435}:8080"', compose)
        self.assertIn('"${WHISPER_BIND_ADDRESS:-127.0.0.1}:${WHISPER_HOST_PORT:-9001}:9000"', compose)
        self.assertIn("\nLLAMA_CPP_BIND_ADDRESS=127.0.0.1\n", env_example)
        self.assertIn("\nWHISPER_BIND_ADDRESS=127.0.0.1\n", env_example)
        self.assertNotIn(":-/tmp}:/tmp", compose)
        self.assertNotIn("\nWHISPER_TMP_DIR=/tmp\n", env_example)
        for service, following in (("docker-broker", "llama-cpp"), ("scripts", "ffmpeg")):
            self.assertIn("- no-new-privileges:true", compose_service(compose, service, following))
        # SUBTITLE_UID/GID ownership changes need CAP_CHOWN in the scripts worker.
        self.assertNotIn("cap_drop", compose_service(compose, "scripts", "ffmpeg"))

    def test_scripts_worker_outlasts_the_controller_drain(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        controller = (ROOT / "scripts" / "control_server.py").read_text(encoding="utf-8")
        drain = int(re.search(r"deadline = time\.time\(\) \+ (\d+)", controller).group(1))
        grace = re.search(r"\n    stop_grace_period: (\d+)s\n", compose_service(compose, "scripts", "ffmpeg"))
        self.assertIsNotNone(grace, "scripts service has no stop_grace_period")
        self.assertGreater(int(grace.group(1)), drain)
        self.assertIn("--stop-timeout=30", (ROOT / "README.md").read_text(encoding="utf-8"))

    def test_security_docs_match_http_only_session_contract(self):
        docs = "\n".join(
            (ROOT / name).read_text(encoding="utf-8")
            for name in ("README.md", "SECURITY.md")
        )
        self.assertNotIn("stores its API key in browser localStorage", docs)
        self.assertIn("HttpOnly", docs)

    def test_setup_has_read_only_preflight(self):
        setup = (ROOT / "setup.sh").read_text(encoding="utf-8")
        self.assertIn('mode="${1:-install}"', setup)
        self.assertIn('mode" == "--check', setup)
        self.assertIn("No configuration or services were changed", setup)


if __name__ == "__main__":
    unittest.main()
