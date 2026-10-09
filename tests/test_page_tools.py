"""Offline tests: execute definitions only, never the server's import-time setup."""

import ast
import copy
import json
import logging
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
import unittest
from unittest.mock import AsyncMock


SOURCE = Path(__file__).resolve().parents[1] / "src" / "wiki_mcp_server.py"


def load_definitions():
    # The legacy module loads dotenv/settings, opens a DB and creates a client
    # at import time. Compile only the functions/class under test instead.
    tree = ast.parse(SOURCE.read_text())
    names = {
        "WikiJSClient", "_update_page", "wikijs_update_page", "wikijs_move_page",
        "wikijs_list_pages", "wikijs_list_tags", "wikijs_get_page_tags",
        "wikijs_add_page_tags", "wikijs_remove_page_tags", "wikijs_replace_page_tags",
        "wikijs_read_page", "wikijs_edit_page", "wikijs_grep_pages",
    }
    nodes = [node for node in tree.body if getattr(node, "name", None) in names]
    for node in nodes:
        if isinstance(node, ast.AsyncFunctionDef):
            node.decorator_list = []
        elif isinstance(node, ast.ClassDef):
            # Do not substitute a fake client constructor: omit it entirely.
            node.body = [method for method in node.body
                         if getattr(method, "name", "") not in {"__init__", "authenticate"}]
            for method in node.body:
                if isinstance(method, ast.AsyncFunctionDef):
                    method.decorator_list = []
    namespace = {
        "json": json, "List": List, "Dict": Dict, "Optional": Optional, "Any": Any,
        "re": re,
        "logger": logging.getLogger("offline-tests"),
        "httpx": SimpleNamespace(HTTPStatusError=type("HTTPStatusError", (Exception,), {}),
                                 RequestError=type("RequestError", (Exception,), {})),
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace


PAGE = {
    "id": 7, "path": "docs/original", "locale": "ru", "title": "Original",
    "content": "<p>Original source</p>", "description": "Keep description",
    "editor": "ckeditor", "isPrivate": True, "isPublished": True,
    "publishStartDate": "2026-01-01T00:00:00Z",
    "publishEndDate": "2027-01-01T00:00:00Z",
    "scriptCss": ".example { color: red; }", "scriptJs": "void 0;",
    "tags": [{"tag": "keep"}, {"tag": "old"}],
}


def single(page):
    return {"data": {"pages": {"single": copy.deepcopy(page)}}}


UPDATE_OK = {"data": {"pages": {"update": {
    "responseResult": {"succeeded": True},
    "page": {"id": 7, "title": "Updated", "updatedAt": "now"},
}}}}
MOVE_OK = {"data": {"pages": {"move": {"responseResult": {"succeeded": True}}}}}


class PageToolsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ns = load_definitions()
        self.client = SimpleNamespace(authenticate=AsyncMock(return_value=True),
                                      graphql_request=AsyncMock())
        self.ns["wikijs"] = self.client

    async def call(self, name, **kwargs):
        return json.loads(await self.ns[name](**kwargs))

    def update_variables(self):
        return self.client.graphql_request.call_args.args[1]

    def assert_preserved(self, variables, changed=()):
        for key, value in PAGE.items():
            if key not in changed:
                expected = [tag["tag"] for tag in value] if key == "tags" else value
                self.assertEqual(variables[key], expected, key)

    async def test_title_only_with_empty_content_preserves_all_metadata(self):
        self.client.graphql_request.side_effect = [single(PAGE), UPDATE_OK]
        result = await self.call("wikijs_update_page", page_id=7, title="Renamed", content="", locale="")
        self.assertEqual(result["status"], "updated")
        self.assertEqual(self.update_variables()["title"], "Renamed")
        self.assert_preserved(self.update_variables(), changed={"title"})
        query, variables = self.client.graphql_request.call_args.args
        for field in ("publishStartDate", "publishEndDate", "scriptCss", "scriptJs", "editor"):
            self.assertIn(f"{field}: ${field}", query)
            self.assertIn(field, self.client.graphql_request.call_args_list[0].args[0])

    async def test_empty_title_and_content_keep_current(self):
        self.client.graphql_request.side_effect = [single(PAGE), UPDATE_OK]
        await self.call("wikijs_update_page", page_id=7, title="", content="")
        self.assert_preserved(self.update_variables())

    async def test_explicit_metadata_and_content_changes(self):
        self.client.graphql_request.side_effect = [single(PAGE), UPDATE_OK]
        await self.call("wikijs_update_page", page_id=7, content="new source", locale="en",
                        description="", tags=[], is_published=False)
        variables = self.update_variables()
        self.assert_preserved(variables, {"content", "locale", "description", "tags", "isPublished"})
        self.assertEqual(variables["content"], "new source")
        self.assertEqual(variables["locale"], "en")
        self.assertEqual(variables["description"], "")
        self.assertEqual(variables["tags"], [])
        self.assertIs(variables["isPublished"], False)

    async def test_tag_operations_preserve_unrelated_fields(self):
        cases = [
            ("wikijs_add_page_tags", ["keep", "new", "new"], ["keep", "old", "new"]),
            ("wikijs_remove_page_tags", ["old", "absent"], ["keep"]),
            ("wikijs_replace_page_tags", ["new", "new"], ["new"]),
            ("wikijs_replace_page_tags", [], []),
            ("wikijs_add_page_tags", [], ["keep", "old"]),
            ("wikijs_remove_page_tags", [], ["keep", "old"]),
        ]
        for name, tags, expected in cases:
            with self.subTest(name=name, tags=tags):
                self.client.graphql_request.reset_mock()
                self.client.graphql_request.side_effect = [single(PAGE), UPDATE_OK]
                result = await self.call(name, page_id=7, tags=tags)
                self.assertEqual(result["tags"], expected)
                self.assertEqual(self.update_variables()["tags"], expected)
                self.assert_preserved(self.update_variables(), {"tags"})
                self.assertEqual(self.client.graphql_request.await_count, 2)

    async def test_missing_update_page_does_not_mutate(self):
        self.client.graphql_request.return_value = single(None)
        self.assertIn("error", await self.call("wikijs_update_page", page_id=7, title="New"))
        self.assertEqual(self.client.graphql_request.await_count, 1)

    async def test_incomplete_metadata_does_not_mutate(self):
        page = copy.deepcopy(PAGE)
        del page["scriptJs"]
        self.client.graphql_request.return_value = single(page)
        self.assertIn("error", await self.call("wikijs_add_page_tags", page_id=7, tags=["new"]))
        self.assertEqual(self.client.graphql_request.await_count, 1)

    async def test_update_failure_is_returned(self):
        self.client.graphql_request.side_effect = [single(PAGE), {
            "data": {"pages": {"update": {"responseResult": {"succeeded": False, "message": "Denied"}}}}
        }]
        result = await self.call("wikijs_update_page", page_id=7, title="New")
        self.assertIn("Denied", result["error"])

    async def test_update_transport_failure_is_not_retried(self):
        self.client.graphql_request.side_effect = [single(PAGE), TimeoutError()]
        self.assertIn("error", await self.call("wikijs_update_page", page_id=7, title="New"))
        self.assertEqual(self.client.graphql_request.await_count, 2)

    async def test_move_native_and_verified_by_same_id(self):
        target = dict(PAGE, path="docs/new", locale="en")
        self.client.graphql_request.side_effect = [single(PAGE), MOVE_OK, single(target)]
        result = await self.call("wikijs_move_page", page_id=7, destination_path="docs/new", destination_locale="en")
        self.assertTrue(result["verified"])
        self.assertEqual(result["pageId"], 7)
        mutation, variables = self.client.graphql_request.call_args_list[1].args
        self.assertIn("move(id: $id", mutation)
        self.assertNotIn("create(", mutation)
        self.assertNotIn("delete(", mutation)
        self.assertEqual(variables, {"id": 7, "destinationPath": "docs/new", "destinationLocale": "en"})
        self.assertEqual(self.client.graphql_request.call_args.args[1], {"id": 7})

    async def test_move_ambiguous_response_verified_without_retry(self):
        self.client.graphql_request.side_effect = [single(PAGE), TimeoutError(), single(dict(PAGE, path="new"))]
        result = await self.call("wikijs_move_page", page_id=7, destination_path="new")
        self.assertEqual(result["status"], "moved")
        self.assertTrue(result["verified"])
        self.assertEqual(self.client.graphql_request.await_count, 3)
        self.assertEqual(self.client.graphql_request.call_args_list[1].args[1]["destinationLocale"], "ru")

    async def test_move_unconfirmed_outcomes_do_not_retry(self):
        for verification in (single(PAGE), single(None), TimeoutError(), single(dict(PAGE, id=8, path="new")), single({"id": 7})):
            with self.subTest(verification=verification):
                self.client.graphql_request.reset_mock()
                self.client.graphql_request.side_effect = [single(PAGE), TimeoutError(), verification]
                result = await self.call("wikijs_move_page", page_id=7, destination_path="new")
                self.assertFalse(result["verified"])
                self.assertFalse(result["retrySafe"])
                self.assertEqual(self.client.graphql_request.await_count, 3)

    async def test_move_success_response_still_requires_verification(self):
        self.client.graphql_request.side_effect = [single(PAGE), MOVE_OK, single(PAGE)]
        result = await self.call("wikijs_move_page", page_id=7, destination_path="new")
        self.assertFalse(result["verified"])
        self.assertIn("error", result)

    async def test_move_repeated_call_checks_destination_before_mutating(self):
        self.client.graphql_request.return_value = single(PAGE)
        result = await self.call("wikijs_move_page", page_id=7, destination_path=PAGE["path"])
        self.assertEqual(result["status"], "unchanged")
        self.assertEqual(self.client.graphql_request.await_count, 1)

    async def test_move_missing_page_does_not_mutate(self):
        self.client.graphql_request.return_value = single(None)
        self.assertIn("error", await self.call("wikijs_move_page", page_id=7, destination_path="new"))
        self.assertEqual(self.client.graphql_request.await_count, 1)

    async def test_move_invalid_path_does_not_contact_server(self):
        for path in ("", "/new", "new/"):
            self.assertIn("error", await self.call("wikijs_move_page", page_id=7, destination_path=path))
        self.client.authenticate.assert_not_awaited()

    async def test_list_pages_filters_before_bounding_output(self):
        pages = [dict(PAGE, id=1, path="docs/a", tags=["keep"]),
                 dict(PAGE, id=2, path="other", tags=[]),
                 dict(PAGE, id=3, path="docs/b", tags=["old"]),
                 dict(PAGE, id=4, path="docs/c", locale="en", tags=[])]
        self.client.graphql_request.return_value = {"data": {"pages": {"list": pages}}}
        result = await self.call("wikijs_list_pages", locale="ru", path_prefix="docs/", limit=1, offset=1)
        self.assertEqual([page["id"] for page in result["pages"]], [3])
        self.assertEqual(result["total"], 2)
        self.assertFalse(result["hasMore"])
        query, variables = self.client.graphql_request.call_args.args
        self.assertEqual(variables, {"locale": "ru"})
        self.assertNotIn("content", query.replace("contentType", ""))
        self.assertNotIn("single(", query)
        self.assertIn("tags", query)
        self.assertNotIn("tags {", query)
        self.assertNotIn("limit:", query)

    async def test_list_pages_all_locales_and_has_more(self):
        self.client.graphql_request.return_value = {"data": {"pages": {"list": [PAGE, dict(PAGE, id=8, locale="en")]}}}
        result = await self.call("wikijs_list_pages", limit=1)
        self.assertTrue(result["hasMore"])
        self.assertEqual(result["total"], 2)
        self.assertEqual(self.client.graphql_request.call_args.args[1], {"locale": None})

    async def test_list_limits_rejected_before_authentication(self):
        for name in ("wikijs_list_pages", "wikijs_list_tags"):
            for kwargs in ({"limit": 0}, {"limit": 501}, {"offset": -1}):
                self.assertIn("error", await self.call(name, **kwargs))
        self.client.authenticate.assert_not_awaited()

    async def test_native_tags_catalog_sorted_and_bounded(self):
        tags = [{"id": 2, "tag": "z", "title": "Z"}, {"id": 1, "tag": "a", "title": "A"}]
        self.client.graphql_request.return_value = {"data": {"pages": {"tags": tags}}}
        result = await self.call("wikijs_list_tags", limit=1)
        self.assertEqual(result["tags"], [tags[1]])
        self.assertTrue(result["hasMore"])
        self.assertIn("pages { tags {", self.client.graphql_request.call_args.args[0])

    async def test_get_tags_without_content(self):
        self.client.graphql_request.return_value = single(PAGE)
        result = await self.call("wikijs_get_page_tags", page_id=7)
        self.assertEqual(result["tags"], PAGE["tags"])
        self.assertNotIn("content", self.client.graphql_request.call_args.args[0])

    async def test_get_tags_missing_page(self):
        self.client.graphql_request.return_value = single(None)
        self.assertIn("error", await self.call("wikijs_get_page_tags", page_id=7))

    async def test_transport_routes_reads_only_to_retry_helper(self):
        client = self.ns["WikiJSClient"].__new__(self.ns["WikiJSClient"])
        client._graphql_read = AsyncMock(return_value={})
        client._graphql_request = AsyncMock(side_effect=TimeoutError())
        for query in ("mutation { pages { move } }", "# comment\nmutation { pages { update } }"):
            with self.assertRaises(TimeoutError):
                await client.graphql_request(query)
        self.assertEqual(client._graphql_request.await_count, 2)
        client._graphql_read.assert_not_awaited()
        await client.graphql_request("  query { pages { list } }")
        await client.graphql_request(" { pages { tags } }")
        self.assertEqual(client._graphql_read.await_count, 2)
        tree = ast.parse(SOURCE.read_text())
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "WikiJSClient")
        methods = {node.name: node for node in cls.body if isinstance(node, ast.AsyncFunctionDef)}
        self.assertFalse(methods["graphql_request"].decorator_list)
        self.assertFalse(methods["_graphql_request"].decorator_list)
        self.assertEqual(len(methods["_graphql_read"].decorator_list), 1)

    async def test_move_rejection_reports_error_and_verifies(self):
        denied = {"data": {"pages": {"move": {"responseResult": {
            "succeeded": False, "message": "Destination exists",
        }}}}}
        self.client.graphql_request.side_effect = [single(PAGE), denied, single(PAGE)]
        result = await self.call("wikijs_move_page", page_id=7, destination_path="new")
        self.assertEqual(result["error"], "Destination exists")
        self.assertEqual(result["actual"]["path"], PAGE["path"])
        self.assertEqual(self.client.graphql_request.await_count, 3)

    async def test_transport_posts_mutation_only_once_on_lost_response(self):
        cls = self.ns["WikiJSClient"]
        client = cls.__new__(cls)
        client.base_url = "https://offline.invalid"
        client.client = SimpleNamespace(post=AsyncMock(side_effect=TimeoutError()))
        with self.assertRaises(TimeoutError):
            await client.graphql_request("mutation { pages { move } }", {"id": 7})
        client.client.post.assert_awaited_once()

    def test_new_tools_are_registered(self):
        tree = ast.parse(SOURCE.read_text())
        tools = {node.name: node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)}
        for name in ("wikijs_move_page", "wikijs_list_pages", "wikijs_get_page_tags",
                     "wikijs_list_tags", "wikijs_add_page_tags", "wikijs_remove_page_tags",
                     "wikijs_replace_page_tags", "wikijs_update_page", "wikijs_read_page",
                     "wikijs_edit_page", "wikijs_grep_pages"):
            self.assertEqual(ast.unparse(tools[name].decorator_list[0]), "mcp.tool()")


if __name__ == "__main__":
    unittest.main()
