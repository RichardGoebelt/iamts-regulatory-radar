import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "update_radar.py"
spec = importlib.util.spec_from_file_location("radar_update", SCRIPT)
radar = importlib.util.module_from_spec(spec)
import sys
sys.modules[spec.name] = radar
spec.loader.exec_module(radar)


class RadarTests(unittest.TestCase):
    def test_status_rules(self):
        self.assertEqual(radar.classify_status("Proposed rule for automated driving systems"), "Draft")
        self.assertEqual(radar.classify_status("Final rule effective on 1 January"), "In force")
        self.assertEqual(radar.classify_status("UNECE working document on validation"), "In progress")
        self.assertEqual(radar.classify_status("Technical information"), "Status unclear")

    def test_change_labels(self):
        old = [{"id": "a", "contentHash": "1"}, {"id": "b", "contentHash": "2"}]
        fresh = [{"id": "a", "contentHash": "1"}, {"id": "b", "contentHash": "3"}, {"id": "c", "contentHash": "4"}]
        changes = {x["id"]: x["change"] for x in radar.apply_changes(fresh, old)}
        self.assertEqual(changes, {"a": "No change", "b": "Updated", "c": "New"})

    def test_html_parser(self):
        p = radar.parse_html('<table><tr><td>GRVA automated driving validation</td><td><a href="/doc.pdf">ECE/TRANS/WP.29/GRVA/2026/24</a></td></tr></table>', 'https://unece.org/event')
        self.assertEqual(len(p.rows), 1)
        self.assertIn("automated driving", p.rows[0].text)
        self.assertEqual(p.rows[0].anchors[0].url, "https://unece.org/doc.pdf")

    def test_china_title_is_english(self):
        title = radar.english_china_title("智能网联汽车 自动驾驶功能仿真试验方法及要求", "GB/T 47025-2026")
        self.assertIn("Simulation test methods", title)
        self.assertNotRegex(title, r"[\u4e00-\u9fff]")

    def test_priority(self):
        s = radar.score_item(
            "Draft automated driving validation and certification requirements",
            "Scenario-based testing and simulation for type approval",
            "May affect homologation and certification.",
            "Draft",
            "",
        )
        self.assertEqual(s["priority"], "High")
        self.assertGreaterEqual(s["total"], 5)

    def test_unavailable_source_retains_internal_state_but_is_not_published(self):
        original_sources = radar.SOURCES
        original_collect = radar.collect_source
        original_state = radar.STATE_FILE
        original_public = radar.PUBLIC_FILE
        try:
            with tempfile.TemporaryDirectory() as td:
                td = Path(td)
                radar.STATE_FILE = td / "state.json"
                radar.PUBLIC_FILE = td / "radar.json"
                source = {"id":"x","region":"USA","name":"Test source","page_url":"https://example.test","adapter":"official_pages"}
                radar.SOURCES = [source]
                previous_item = {"id":"old","contentHash":"abc","title":"Automated driving test","region":"USA","sourceId":"x","sourceName":"Test source","sourceUrl":"https://example.test/a","date":"","status":"In progress","summary":"","relevance":"","questions":"","tc":1,"urgency":1,"impact":2,"total":4,"priority":"Medium"}
                radar.write_json(radar.STATE_FILE, {"schemaVersion":1,"lastRun":"2026-08-01T00:00:00Z","sources":{"x":{"lastSuccessfulCheck":"2026-08-01T00:00:00Z","entries":[previous_item]}}})
                radar.collect_source = lambda src: {"source":src,"status":"unavailable","message":"Currently unavailable","entries":[],"elapsedMs":1}
                self.assertEqual(radar.run(), 0)
                state = radar.load_json(radar.STATE_FILE,{})
                published = radar.load_json(radar.PUBLIC_FILE,{})
                self.assertEqual(state["sources"]["x"]["entries"][0]["id"], "old")
                self.assertEqual(published["entries"], [])
                self.assertEqual(published["sources"][0]["status"], "unavailable")
        finally:
            radar.SOURCES = original_sources
            radar.collect_source = original_collect
            radar.STATE_FILE = original_state
            radar.PUBLIC_FILE = original_public


if __name__ == "__main__":
    unittest.main()
