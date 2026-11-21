import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

class TestSidebarCollapse(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        cls.css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
        cls.js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    def test_sidebar_toggle_button(self):
        self.assertIn('id="sidebar-toggle-btn"', self.html)
        self.assertIn('class="sidebar-toggle-btn"', self.html)
        self.assertIn('aria-label="Toggle sidebar"', self.html)
        self.assertIn('aria-expanded="true"', self.html)

    def test_brand_logo_full_and_monogram(self):
        self.assertIn('vs-logo-full', self.html)
        self.assertIn('vs-logo-monogram', self.html)
        self.assertIn('logo.png', self.html)
        self.assertIn('logo Monogram.png', self.html)

    def test_nav_buttons_tooltips(self):
        for page in ["dashboard", "clients", "folders", "save", "pdf-studio", "vs-ai", "drive", "backup", "automation", "activity", "setup"]:
            self.assertIn(f'data-page="{page}"', self.html)
            self.assertIn(f'data-t=', self.html)

    def test_css_rules(self):
        self.assertIn("--sidebar-w-expanded: 240px;", self.css)
        self.assertIn("--sidebar-w-collapsed: 68px;", self.css)
        self.assertIn("transition: grid-template-columns", self.css)
        self.assertIn(".app.sidebar-collapsed", self.css)
        self.assertIn(".vs-logo-monogram", self.css)
        self.assertIn(".app.sidebar-collapsed nav button span", self.css)

    def test_js_logic(self):
        self.assertIn("initSidebarCollapse", self.js)
        self.assertIn("vs_sidebar_collapsed", self.js)
        self.assertIn("window.toggleSidebar", self.js)
        self.assertIn("localStorage.setItem('vs_sidebar_collapsed'", self.js)

if __name__ == "__main__":
    unittest.main()
