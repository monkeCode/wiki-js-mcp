"""File-like Wiki tools tested only with isolated definitions and fake requests."""

import copy
import json
import unittest
from unittest.mock import AsyncMock

from test_page_tools import PAGE, UPDATE_OK, load_definitions, single
from types import SimpleNamespace


class FileToolsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ns = load_definitions()
        self.client = SimpleNamespace(authenticate=AsyncMock(return_value=True),
                                      graphql_request=AsyncMock())
        self.ns["wikijs"] = self.client
        self.page = dict(copy.deepcopy(PAGE), content="# Title\n\nfirst\nsecond\n",
                         updatedAt="2026-10-10T12:00:00Z", contentType="markdown")

    async def call(self, name, **kwargs):
        return json.loads(await self.ns[name](**kwargs))

    async def test_read_default_numbered_source_and_metadata(self):
        self.client.graphql_request.return_value = single(self.page)
        result = await self.call("wikijs_read_page", page_id=7)
        self.assertEqual(result["content"], "1: # Title\n2: \n3: first\n4: second")
        for result_key, page_key in (("pageId", "id"), ("path", "path"), ("lastModified", "updatedAt"),
                                     ("contentType", "contentType")):
            self.assertEqual(result[result_key], self.page[page_key])
        self.assertEqual(result["totalLines"], 4)
        self.assertFalse(result["truncated"])
        self.assertIsNone(result["nextOffset"])

    async def test_read_ranges_and_eof(self):
        self.client.graphql_request.return_value = single(self.page)
        for offset, limit, content, next_offset in (
            (2, 2, "2: \n3: first", 4), (4, 20, "4: second", None), (5, 1, "", None), (99, 1, "", None),
        ):
            with self.subTest(offset=offset, limit=limit):
                result = await self.call("wikijs_read_page", page_id=7, offset=offset, limit=limit)
                self.assertEqual(result["content"], content)
                self.assertEqual(result["nextOffset"], next_offset)
                self.assertEqual(result["hasMore"], next_offset is not None)

    async def test_read_empty_and_crlf_source(self):
        for content, expected, count in (("", "", 0), ("one\r\ntwo\r\n", "1: one\n2: two", 2)):
            self.client.graphql_request.return_value = single(dict(self.page, content=content))
            result = await self.call("wikijs_read_page", page_id=7)
            self.assertEqual(result["content"], expected)
            self.assertEqual(result["totalLines"], count)

    async def test_read_returns_html_as_stored_not_markdown_conversion(self):
        self.client.graphql_request.return_value = single(dict(self.page, content="<p>Source</p>", contentType="html"))
        result = await self.call("wikijs_read_page", page_id=7)
        self.assertEqual(result["content"], "1: <p>Source</p>")
        self.assertEqual(result["contentType"], "html")

    async def test_read_invalid_ranges_before_authentication(self):
        for kwargs in ({"offset": 0}, {"offset": -1}, {"limit": 0}, {"limit": 2001}):
            self.assertIn("error", await self.call("wikijs_read_page", page_id=7, **kwargs))
        self.client.authenticate.assert_not_awaited()

    async def test_read_missing_page(self):
        self.client.graphql_request.return_value = single(None)
        self.assertIn("error", await self.call("wikijs_read_page", page_id=7))

    async def test_read_long_line_and_output_bounds(self):
        self.client.graphql_request.return_value = single(dict(self.page, content=("x" * 2100 + "\n") * 100))
        result = await self.call("wikijs_read_page", page_id=7)
        self.assertLessEqual(len(result["content"]), 64000)
        self.assertTrue(result["lineTruncated"])
        self.assertTrue(result["hasMore"])
        self.assertEqual(result["nextOffset"], len(result["content"].splitlines()) + 1)

    async def test_edit_unique_exact_multiline_preserves_metadata(self):
        self.client.graphql_request.side_effect = [single(self.page), UPDATE_OK]
        result = await self.call("wikijs_edit_page", page_id=7, old_string="first\nsecond", new_string="replacement\ntext")
        self.assertEqual(result["replacements"], 1)
        self.assertEqual(result["path"], PAGE["path"])
        variables = self.client.graphql_request.call_args.args[1]
        self.assertEqual(variables["content"], "# Title\n\nreplacement\ntext\n")
        for key, value in PAGE.items():
            if key != "content":
                self.assertEqual(variables[key], [tag["tag"] for tag in value] if key == "tags" else value)
        self.assertEqual(self.client.graphql_request.await_count, 2)

    async def test_edit_missing_and_ambiguous_matches_do_not_mutate(self):
        for old, content, replace_all in (("missing", "text", False), ("a", "a a", False), ("missing", "text", True)):
            with self.subTest(old=old, replace_all=replace_all):
                self.client.graphql_request.reset_mock()
                self.client.graphql_request.return_value = single(dict(self.page, content=content))
                result = await self.call("wikijs_edit_page", page_id=7, old_string=old, new_string="new", replace_all=replace_all)
                self.assertIn("error", result)
                self.assertEqual(self.client.graphql_request.await_count, 1)

    async def test_edit_empty_old_rejected_before_read(self):
        self.assertIn("error", await self.call("wikijs_edit_page", page_id=7, old_string="", new_string="new"))
        self.client.authenticate.assert_not_awaited()

    async def test_edit_replace_all_and_literal_not_regex(self):
        self.client.graphql_request.side_effect = [single(dict(self.page, content="a.* a.*\r\n")), UPDATE_OK]
        result = await self.call("wikijs_edit_page", page_id=7, old_string="a.*", new_string="b", replace_all=True)
        self.assertEqual(result["replacements"], 2)
        self.assertEqual(self.client.graphql_request.call_args.args[1]["content"], "b b\r\n")

    async def test_edit_empty_replacement_deletes_matched_text(self):
        self.client.graphql_request.side_effect = [single(dict(self.page, content="delete me\nkeep")), UPDATE_OK]
        await self.call("wikijs_edit_page", page_id=7, old_string="delete me", new_string="")
        self.assertEqual(self.client.graphql_request.call_args.args[1]["content"], "\nkeep")

    async def test_edit_rejects_empty_result_without_mutation(self):
        for new_string in ("", " \n\t"):
            self.client.graphql_request.reset_mock()
            self.client.graphql_request.return_value = single(dict(self.page, content="delete me"))
            result = await self.call("wikijs_edit_page", page_id=7, old_string="delete me", new_string=new_string)
            self.assertIn("nonempty page body", result["error"])
            self.assertEqual(self.client.graphql_request.await_count, 1)

    async def test_edit_noop_does_not_mutate(self):
        for content, replace_all in (("same", False), ("same same", True)):
            self.client.graphql_request.reset_mock()
            self.client.graphql_request.return_value = single(dict(self.page, content=content))
            result = await self.call("wikijs_edit_page", page_id=7, old_string="same", new_string="same", replace_all=replace_all)
            self.assertEqual(result["status"], "unchanged")
            self.assertEqual(result["replacements"], 0)
            self.assertEqual(result["lastModified"], self.page["updatedAt"])
            self.assertEqual(self.client.graphql_request.await_count, 1)

    async def test_edit_missing_page_and_lost_response(self):
        self.client.graphql_request.return_value = single(None)
        self.assertIn("error", await self.call("wikijs_edit_page", page_id=7, old_string="first", new_string="new"))
        self.client.graphql_request.reset_mock()
        self.client.graphql_request.side_effect = [single(self.page), TimeoutError()]
        self.assertIn("error", await self.call("wikijs_edit_page", page_id=7, old_string="first", new_string="new"))
        self.assertEqual(self.client.graphql_request.await_count, 2)

    def candidates(self, pages, total=None):
        self.ns["wikijs_list_pages"] = AsyncMock(return_value=json.dumps({
            "pages": pages, "total": len(pages) if total is None else total,
            "hasMore": total is not None and total > len(pages),
        }))

    async def test_grep_regex_line_numbers_filters_and_matching_lines_only(self):
        self.candidates([self.page])
        self.client.graphql_request.return_value = single(dict(self.page, content="hidden\nFirst 12\nFirst 34\nsecret nonmatching\n"))
        result = await self.call("wikijs_grep_pages", pattern=r"^First \d+$", locale="ru", path_prefix="docs/")
        self.assertEqual([match["lineNumber"] for match in result["matches"]], [2, 3])
        self.assertEqual(result["matches"][0]["pageId"], 7)
        self.assertEqual(result["matches"][0]["path"], PAGE["path"])
        self.assertNotIn("secret nonmatching", json.dumps(result))
        self.assertNotIn("hidden", json.dumps(result))
        self.assertFalse(result["truncated"])
        self.ns["wikijs_list_pages"].assert_awaited_once_with(locale="ru", path_prefix="docs/", limit=100)

    async def test_grep_case_sensitivity(self):
        self.candidates([self.page])
        self.client.graphql_request.return_value = single(dict(self.page, content="Alpha\nalpha"))
        for case_sensitive, expected in ((True, 1), (False, 2)):
            result = await self.call("wikijs_grep_pages", pattern="alpha", case_sensitive=case_sensitive)
            self.assertEqual(len(result["matches"]), expected)

    async def test_grep_invalid_regex_and_limits_before_network(self):
        self.candidates([])
        for kwargs in ({"pattern": "["}, {"pattern": "x", "limit": 0}, {"pattern": "x", "limit": 501}):
            self.assertIn("error", await self.call("wikijs_grep_pages", **kwargs))
        self.ns["wikijs_list_pages"].assert_not_awaited()
        self.client.graphql_request.assert_not_awaited()

    async def test_grep_result_limit_truncation_and_no_later_fetch(self):
        self.candidates([self.page, dict(self.page, id=8)])
        self.client.graphql_request.return_value = single(dict(self.page, content="hit\nhit\nhit"))
        result = await self.call("wikijs_grep_pages", pattern="hit", limit=2)
        self.assertEqual(len(result["matches"]), 2)
        self.assertTrue(result["truncated"])
        self.assertTrue(result["resultLimitReached"])
        self.assertEqual(self.client.graphql_request.await_count, 1)

    async def test_grep_exact_result_limit_not_truncated(self):
        self.candidates([self.page])
        self.client.graphql_request.return_value = single(dict(self.page, content="hit\nhit"))
        result = await self.call("wikijs_grep_pages", pattern="hit", limit=2)
        self.assertFalse(result["truncated"])

    async def test_grep_page_fetch_bound_real_list_filtering(self):
        pages = [dict(self.page, id=i, path=f"docs/{i}") for i in range(101)]
        async def request(query, variables):
            if "list(" in query:
                return {"data": {"pages": {"list": [dict(self.page, path="other", id=999)] + pages}}}
            return single(dict(self.page, id=variables["id"], content="nonmatching"))
        self.client.graphql_request.side_effect = request
        result = await self.call("wikijs_grep_pages", pattern="absent", path_prefix="docs/")
        self.assertEqual(result["pagesScanned"], 100)
        self.assertEqual(result["candidatePages"], 101)
        self.assertTrue(result["truncated"])
        self.assertEqual(self.client.graphql_request.await_count, 101)
        self.assertEqual(result["matches"], [])

    async def test_grep_skips_unavailable_oversized_and_moved_pages(self):
        self.candidates([dict(self.page, id=i) for i in range(4)])
        self.client.graphql_request.side_effect = [single(None), TimeoutError(),
            single(dict(self.page, content="x" * 1000001)), single(dict(self.page, path="other"))]
        result = await self.call("wikijs_grep_pages", pattern="x", path_prefix="docs/")
        self.assertEqual(result["skippedPageIds"], [0, 1, 2, 3])
        self.assertTrue(result["truncated"])
        self.assertEqual(result["matches"], [])

    async def test_grep_line_clipping_searches_whole_line(self):
        self.candidates([self.page])
        self.client.graphql_request.return_value = single(dict(self.page, content="x" * 2001 + "needle"))
        result = await self.call("wikijs_grep_pages", pattern="needle")
        self.assertEqual(len(result["matches"][0]["line"]), 2000)
        self.assertTrue(result["lineTruncated"])
        self.assertTrue(result["truncated"])

    async def test_grep_candidate_error_returned_without_content_fetch(self):
        self.ns["wikijs_list_pages"] = AsyncMock(return_value=json.dumps({"error": "Denied"}))
        result = await self.call("wikijs_grep_pages", pattern="x")
        self.assertEqual(result["error"], "Denied")
        self.client.graphql_request.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
