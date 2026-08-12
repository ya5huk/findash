"""Unit tests for inline-script-safe JSON serialization."""

import json
import unittest

from findash_core.script_json import json_for_script


class ScriptJsonTests(unittest.TestCase):
    def test_round_trips_unicode_and_nested_values(self):
        value = {"merchant": "בית קפה", "values": [1, None, True]}
        self.assertEqual(json.loads(json_for_script(value)), value)

    def test_script_end_tag_cannot_terminate_the_container(self):
        value = {"merchant": "</script><script>alert(1)</script>"}
        payload = json_for_script(value)
        self.assertNotIn("<", payload)
        self.assertNotIn(">", payload)
        self.assertNotIn("</script", payload.lower())
        self.assertEqual(json.loads(payload), value)

    def test_html_comment_ampersand_and_js_line_separators_are_escaped(self):
        value = "<!-- a & b -->\u2028next\u2029line"
        payload = json_for_script(value)
        self.assertNotIn("<!--", payload)
        self.assertNotIn("&", payload)
        self.assertNotIn("\u2028", payload)
        self.assertNotIn("\u2029", payload)
        self.assertIn("\\u2028", payload)
        self.assertIn("\\u2029", payload)
        self.assertEqual(json.loads(payload), value)


if __name__ == "__main__":
    unittest.main()
