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
        # The API badge follows the liveness probe; the Plex card follows Plex itself.
        self.assertIn("document.getElementById('stat-api').textContent = apiOnline", dashboard)
        self.assertIn("const plexOk = !!plex?.ready;", dashboard)
        self.assertNotIn("const plexOk = h && h.status === 'ok';", dashboard)

    def test_only_one_script_source_tree_exists(self):
        self.assertTrue((ROOT / "scripts" / "lib.sh").is_file())
        self.assertFalse((ROOT / "plexmind" / "scripts" / "lib.sh").exists())

    def test_image_ships_every_script(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        copied = set()
        for instruction in re.findall(r"(?m)^COPY ((?:[^\n]*\\\n)*[^\n]*)", dockerfile):
            copied.update(Path(src).name for src in instruction.replace("\\\n", " ").split()[:-1])
        shipped = {p.name for p in (ROOT / "scripts").iterdir() if p.suffix in (".sh", ".py")}
        self.assertEqual(shipped - copied, set())
        self.assertIn('CMD ["python3", "/app/supervisor.py"]', dockerfile)

    def test_unraid_template_is_the_whole_single_container(self):
        template = (ROOT / "templates" / "PlexMind.xml").read_text(encoding="utf-8")
        for path in ("/app/data", "/media/movies", "/media/tv", "/var/run/docker.sock"):
            self.assertIn(f"<ContainerDir>{path}</ContainerDir>", template)
        # The internal URLs and tokens are set by supervisor.py, not the user.
        for name in ("SCRIPTS_API_URL", "DOCKER_BROKER_URL", "PLEXMIND_CONTROL_TOKEN", "PLEXMIND_BROKER_TOKEN"):
            self.assertNotIn(f"<Name>{name}</Name>", template)
        self.assertIn("<ExtraParams>--stop-timeout=40</ExtraParams>", template)
        # A moving tag, so Unraid can tell when a newer image is published.
        self.assertIn("<Repository>ghcr.io/finechainesium/plexmind:latest</Repository>", template)

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
        self.assertIn("data.script_windows?.[job]", dashboard)
        self.assertIn("['transcribe', 'tx-window-label']", dashboard)
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
        env_example = (ROOT / "docs" / "CONFIGURATION.md").read_text(encoding="utf-8")
        block = compose_service(compose, "plexmind", "ffmpeg")
        for key in ("PLEXMIND_SECURE_COOKIE", "GPU_BUSY_SAMPLES", "GPU_SAMPLE_INTERVAL_SECONDS", "TRANSLATION_BATCH_CUES"):
            self.assertIn(f"- {key}=${{{key}:-", block, f"{key} not passed to plexmind")
            self.assertIn(f"\n{key}=", env_example, f"{key} not documented in docs/CONFIGURATION.md")

    def test_compose_keeps_sidecar_ports_and_privileges_narrow(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        env_example = (ROOT / "docs" / "CONFIGURATION.md").read_text(encoding="utf-8")
        # llama.cpp has no API key; PlexMind reaches both sidecars on the Docker network.
        self.assertIn('"${LLAMA_CPP_BIND_ADDRESS:-127.0.0.1}:${LLAMA_CPP_HOST_PORT:-11435}:8080"', compose)
        self.assertIn('"${WHISPER_BIND_ADDRESS:-127.0.0.1}:${WHISPER_HOST_PORT:-9001}:9000"', compose)
        self.assertIn("\nLLAMA_CPP_BIND_ADDRESS=127.0.0.1\n", env_example)
        self.assertIn("\nWHISPER_BIND_ADDRESS=127.0.0.1\n", env_example)
        self.assertNotIn(":-/tmp}:/tmp", compose)
        self.assertNotIn("\nWHISPER_TMP_DIR=/tmp\n", env_example)
        self.assertIn("- no-new-privileges:true", compose_service(compose, "plexmind", "ffmpeg"))
        # SUBTITLE_UID/GID ownership changes need CAP_CHOWN for the scripts.
        self.assertNotIn("cap_drop", compose_service(compose, "plexmind", "ffmpeg"))

    def test_container_outlasts_the_supervisor_and_controller_drains(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        controller = (ROOT / "scripts" / "control_server.py").read_text(encoding="utf-8")
        supervisor = (ROOT / "scripts" / "supervisor.py").read_text(encoding="utf-8")
        drain = int(re.search(r"deadline = time\.time\(\) \+ (\d+)", controller).group(1))
        supervisor_drain = int(re.search(r"(?m)^DRAIN_SECONDS = (\d+)", supervisor).group(1))
        broker_stop = int(re.search(r"(?m)^BROKER_STOP_SECONDS = (\d+)", supervisor).group(1))
        grace = re.search(r"\n    stop_grace_period: (\d+)s\n", compose_service(compose, "plexmind", "ffmpeg"))
        self.assertIsNotNone(grace, "plexmind service has no stop_grace_period")
        self.assertGreater(supervisor_drain, drain)
        self.assertGreater(int(grace.group(1)), supervisor_drain + broker_stop)
        self.assertIn("--stop-timeout=40", (ROOT / "docs" / "UPGRADING.md").read_text(encoding="utf-8"))

    def test_security_docs_match_http_only_session_contract(self):
        docs = "\n".join(
            (ROOT / name).read_text(encoding="utf-8")
            for name in ("README.md", "SECURITY.md")
        )
        self.assertNotIn("stores its API key in browser localStorage", docs)
        self.assertIn("HttpOnly", docs)

    def test_install_asks_for_no_tokens_or_keys(self):
        setup = (ROOT / "setup.sh").read_text(encoding="utf-8")
        template = (ROOT / "templates" / "PlexMind.xml").read_text(encoding="utf-8")
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertNotIn("read -rsp", setup)  # no hidden token/key prompts
        self.assertIn("/api/v2/pins", setup)
        for name in ("PLEX_TOKEN", "PLEX_URL", "PLEXMIND_API_KEY", "PLEXMIND_WEBHOOK_SECRET"):
            self.assertNotIn(f"<Name>{name}</Name>", template)
        self.assertIn("- PLEX_TOKEN=${PLEX_TOKEN:-}", compose)
        self.assertIn("- PLEXMIND_WEBHOOK_SECRET=${PLEXMIND_WEBHOOK_SECRET:-}", compose)

    def test_env_example_is_short_and_every_key_reaches_compose(self):
        example = (ROOT / ".env.example").read_text(encoding="utf-8")
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        keys = re.findall(r"(?m)^([A-Z][A-Z0-9_]*)=", example)
        self.assertLessEqual(len(keys), 10)
        for key in keys:
            self.assertIn("${" + key, compose, f"{key} in .env.example is not used by docker-compose.yml")

    def test_setup_downloads_are_checksummed(self):
        setup = (ROOT / "setup.sh").read_text(encoding="utf-8")
        shas = re.findall(r"MODEL_SHA=([0-9a-f]+)", setup)
        self.assertTrue(shas)
        self.assertTrue(all(len(sha) == 64 for sha in shas))
        self.assertIn('sha256sum "${file}.part"', setup)

    def test_repository_meets_community_apps_submission_rules(self):
        import xml.etree.ElementTree as ET
        self.assertIn("MIT License", (ROOT / "LICENSE").read_text(encoding="utf-8"))
        profile = ET.parse(ROOT / "ca_profile.xml").getroot()
        self.assertEqual(profile.tag, "CommunityApplications")
        self.assertTrue((profile.findtext("Profile") or "").strip())
        template = ET.parse(ROOT / "templates" / "PlexMind.xml").getroot()
        for tag in ("Name", "Repository", "Overview", "Support", "Project", "TemplateURL", "Icon"):
            self.assertTrue((template.findtext(tag) or "").strip(), f"template has no {tag}")

    def test_setup_has_read_only_preflight(self):
        setup = (ROOT / "setup.sh").read_text(encoding="utf-8")
        self.assertIn('mode="${1:-install}"', setup)
        self.assertIn('mode" == "--check', setup)
        self.assertIn("No configuration or services were changed", setup)


if __name__ == "__main__":
    unittest.main()
