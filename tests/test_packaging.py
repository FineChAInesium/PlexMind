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


if __name__ == "__main__":
    unittest.main()
