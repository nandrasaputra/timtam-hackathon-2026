import json
import unittest

import cursor_agent
import cursor_agent_server


class ModelSelectionTest(unittest.TestCase):
    def test_model_selection_keeps_a_plain_id(self):
        self.assertEqual(cursor_agent.model_selection("grok-4.7"), "grok-4.7")

    def test_model_selection_passes_fast_param(self):
        raw = '{"id":"composer-2.5","params":[{"id":"fast","value":"true"}]}'
        self.assertEqual(
            cursor_agent.model_selection(raw),
            {"id": "composer-2.5", "params": [{"id": "fast", "value": "true"}]},
        )


class TurnErrorTest(unittest.TestCase):
    def test_blocked_model_message_is_kept(self):
        error, reply = cursor_agent.turn_error("error", "", "Model Blocked")
        self.assertEqual(error["message"], "Model Blocked")
        self.assertEqual(reply, "")

    def test_finished_run_keeps_the_reply(self):
        error, reply = cursor_agent.turn_error("finished", "pong", "")
        self.assertIsNone(error)
        self.assertEqual(reply, "pong")


class RequestPathTest(unittest.TestCase):
    def test_absolute_form_is_the_path(self):
        self.assertEqual(
            cursor_agent_server.request_path("http://127.0.0.1:4096/v1/prompt"),
            "/v1/prompt",
        )

    def test_plain_path_stays(self):
        self.assertEqual(cursor_agent_server.request_path("/v1/prompt?x=1"), "/v1/prompt")
        self.assertEqual(cursor_agent_server.request_path("/"), "/")


class BuiltinToolsTest(unittest.TestCase):
    def test_only_the_gitlab_tool_stays_on(self):
        self.assertEqual(cursor_agent.builtin_tools(), ["mcp"])

    def test_system_prompt_forbids_exploring(self):
        text = cursor_agent.system_prompt()
        self.assertIn("two searches", text)
        self.assertIn("Do not keep exploring", text)
        self.assertIn("plain sentences", text)
        self.assertIn("Do not call mobox_gitlab", text)
        self.assertIn("Read it", text)

    def test_system_prompt_keeps_a_follow_up_out_of_the_card(self):
        text = cursor_agent.system_prompt()
        self.assertIn("A follow-up, a guess, or a short remark stays in plain sentences", text)
        self.assertIn("Do not repeat the investigation card", text)
        self.assertNotIn("where-is-this", text)

    def test_lookup_stops_after_the_budget(self):
        self.assertEqual(cursor_agent.guarded_lookup(6, lambda: "ran"), cursor_agent.STOP_LOOKUP)

    def test_lookup_runs_before_the_budget(self):
        self.assertEqual(cursor_agent.guarded_lookup(5, lambda: "ran"), "ran")


class GitlabToolTest(unittest.TestCase):
    def test_search_keeps_ten_short_snippets(self):
        hits = [{"path": f"a{i}.kt", "startline": i, "data": "z" * 400} for i in range(12)]
        text = cursor_agent.format_search(json.dumps(hits))
        parsed = json.loads(text)
        self.assertEqual(len(parsed), 10)
        self.assertEqual(parsed[0]["line"], 0)
        self.assertEqual(len(parsed[0]["snippet"]), 200)

    def test_file_excerpt_starts_at_the_requested_line(self):
        stdout = "\n".join(f"line {i}" for i in range(1, 6))
        text = cursor_agent.format_file(stdout, 3)
        self.assertTrue(text.startswith("Excerpt starting at line 3 of 5\nline 3"))
        self.assertNotIn("line 1", text.split("\n", 1)[1])

    def test_missing_branch_asks_before_switching(self):
        text = cursor_agent.format_branches("rc/5.81.0_bm_develop\ndevelop\n", "ios", "rc/5.80.0")
        self.assertIn('No exact match for "rc/5.80.0" on ios', text)
        self.assertIn("rc/5.81.0_bm_develop", text)
        self.assertIn("Ask which branch", text)

    def test_exact_branch_is_reported(self):
        text = cursor_agent.format_branches("develop\n", "gohost", "develop")
        self.assertIn("Matching branches:\ndevelop", text)
        self.assertNotIn("No exact match", text)

    def test_tool_rejects_unknown_project(self):
        with self.assertRaises(RuntimeError):
            cursor_agent.mobox_gitlab({"operation": "search", "project": "web", "args": ["develop", "fare"]})

    def test_file_operation_uses_start_line_locally(self):
        seen = []

        def runner(args):
            seen.append(args)
            return "one\ntwo\nthree\n"

        text = cursor_agent.mobox_gitlab(
            {"operation": "file", "project": "ios", "args": ["develop", "Pay.swift"], "start_line": 2},
            runner=runner,
        )
        self.assertEqual(seen, [["file", "ios", "develop", "Pay.swift"]])
        self.assertIn("line 2", text)
        self.assertIn("two", text)


if __name__ == "__main__":
    unittest.main()
