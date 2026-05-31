import unittest

from core.hipHopProducer import HipHopAutoProject


class HipHopProducerRuntimeTests(unittest.TestCase):
    def test_translation_response_parser_accepts_fenced_json_array(self):
        content = '```json\n["你好", "世界"]\n```'

        self.assertEqual(
            HipHopAutoProject._parse_translation_response(content),
            ["你好", "世界"],
        )

    def test_translation_response_parser_accepts_wrapped_translations_object(self):
        content = '{"translations": ["第一行", "第二行"]}'

        self.assertEqual(
            HipHopAutoProject._parse_translation_response(content),
            ["第一行", "第二行"],
        )

    def test_translation_response_parser_accepts_tagged_lines(self):
        content = "<R1>第一句</R1>\n<R2>第二句</R2>"

        self.assertEqual(
            HipHopAutoProject._parse_translation_response(content),
            ["第一句", "第二句"],
        )

    def test_translation_response_parser_accepts_numbered_lines(self):
        content = "1. 第一行\n2. 第二行"

        self.assertEqual(
            HipHopAutoProject._parse_translation_response(content),
            ["第一行", "第二行"],
        )

    def test_missing_subtitles_filter_detection_is_specific(self):
        self.assertTrue(
            HipHopAutoProject._is_missing_subtitles_filter("No such filter: 'subtitles'")
        )
        self.assertFalse(
            HipHopAutoProject._is_missing_subtitles_filter("font file missing for subtitles render")
        )

    def test_js_runtime_env_parser_accepts_paths(self):
        self.assertEqual(
            HipHopAutoProject._parse_js_runtimes("node:/opt/homebrew/bin/node, deno"),
            {
                "node": {"path": "/opt/homebrew/bin/node"},
                "deno": {},
            },
        )


if __name__ == "__main__":
    unittest.main()
