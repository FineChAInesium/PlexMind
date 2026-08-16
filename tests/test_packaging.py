import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def test_api_badge_uses_liveness_state_not_dependency_health(self):
        dashboard = (ROOT / "plexmind" / "app" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("const plexOk = apiOnline;", dashboard)
        self.assertNotIn("const plexOk = h && h.status === 'ok';", dashboard)

    def test_only_one_script_source_tree_exists(self):
        self.assertTrue((ROOT / "scripts" / "lib.sh").is_file())
        self.assertFalse((ROOT / "plexmind" / "scripts" / "lib.sh").exists())

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

    def test_documented_script_configuration_is_wired(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        for key in (
            "ENABLE_WATERMARK", "WATERMARK_SEARCH", "KNOWN_BILINGUAL_TITLES",
            "KNOWN_ENGLISH_REALITY_TITLES", "SUBTITLE_FILE_MODE", "PGS_CLEANUP_ALLOW_UNKNOWN",
        ):
            self.assertIn(f"- {key}=", compose)

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
