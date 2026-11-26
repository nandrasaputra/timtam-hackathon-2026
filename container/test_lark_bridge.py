import json
import os
import unittest
from types import SimpleNamespace

import lark_bridge as bridge


def event(text, *, chat_id="oc_hack", sender="user", mentions=("ou_bot",), chat_type="group"):
    return SimpleNamespace(
        event=SimpleNamespace(
            sender=SimpleNamespace(sender_type=sender),
            message=SimpleNamespace(
                message_id="om_1",
                chat_id=chat_id,
                chat_type=chat_type,
                content=json.dumps({"text": text}),
                mentions=[SimpleNamespace(id=SimpleNamespace(open_id=item)) for item in mentions],
            ),
        )
    )


class BridgeTests(unittest.TestCase):
    def setUp(self):
        for key in ("LARK_CHANNEL_FCT", "LARK_CHANNEL_CRASH", "LARK_CHANNEL_FEEDBACK"):
            os.environ.pop(key, None)
        os.environ["LARK_CHANNEL_HACK_OF_THRONES"] = "oc_hack"
        os.environ["LARK_BOT_OPEN_ID"] = "ou_bot"
        os.environ.pop("LARK_TAG_OPEN_IDS", None)
        bridge.sent_lines = bridge.SentLines()

    def test_redact_phone_email_token(self):
        raw = "call +62 812-3456-7890 a@b.co glpat-abc_123 at -6.2001, 106.8166 driver_id=D-9"
        out = bridge.redact(raw)
        for secret in ("+62", "a@b.co", "glpat-abc_123", "-6.2001", "D-9"):
            self.assertNotIn(secret, out)

    def test_redact_name_address_and_device(self):
        raw = "name: Sari address: JlMawar device_id=abc123 idfa: XYZ"
        out = bridge.redact(raw)
        for secret in ("Sari", "JlMawar", "abc123", "XYZ"):
            self.assertNotIn(secret, out)
        self.assertIn("[NAME]", out)
        self.assertIn("[ADDRESS]", out)
        self.assertIn("[DEVICE]", out)

    def test_allow_hack_of_thrones_mention_and_personal_chat(self):
        self.assertTrue(bridge.allowed(event("fare missing")))
        self.assertTrue(bridge.allowed(event("fare missing", chat_type="p2p", chat_id="oc_dm", mentions=())))

    def test_reject_other_channel_app_and_wrong_mention(self):
        self.assertFalse(bridge.allowed(event("x", chat_id="oc_other")))
        self.assertFalse(bridge.allowed(event("x", sender="app")))
        self.assertFalse(bridge.allowed(event("x", mentions=("ou_other",))))
        self.assertFalse(bridge.allowed(event("x", mentions=())))
        self.assertFalse(bridge.allowed(event("x", chat_type="p2p", sender="app", mentions=())))

    def test_thread_session_survives_bridge_restart(self):
        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "sessions.json")
            bridge.Sessions(path).open(["om_hello", "omt_1"], lambda: "ses_1")
            after = bridge.Sessions(path)
            self.assertEqual(after.open(["om_hello", "om_follow"], lambda: "ses_new"), "ses_1")
            self.assertEqual(bridge.Sessions(path).lookup("om_follow"), "ses_1")

    def test_same_thread_reuses_one_agent_session(self):
        store = bridge.Sessions()
        created = []

        def create():
            created.append("new")
            return f"ses_{len(created)}"

        self.assertEqual(store.open("omt_1", create), "ses_1")
        self.assertEqual(store.open("omt_1", create), "ses_1")
        self.assertEqual(store.open("om_9", create), "ses_2")
        self.assertEqual(created, ["new", "new"])
        message = SimpleNamespace(thread_id="omt_1", message_id="om_9")
        self.assertEqual(bridge.session_key(message), "omt_1")
        bare = SimpleNamespace(thread_id="", message_id="om_9")
        self.assertEqual(bridge.session_key(bare), "om_9")
        follow = SimpleNamespace(root_id="om_9", thread_id="omt_1", message_id="om_bug")
        self.assertEqual(bridge.session_key(follow), "om_9")
        fresh = bridge.Sessions()
        made = []

        def create_fresh():
            made.append("new")
            return f"ses_{len(made)}"

        hello = SimpleNamespace(message_id="om_hello", thread_id="", root_id="")
        inside = SimpleNamespace(message_id="om_bug", thread_id="omt_new", root_id="")
        self.assertEqual(fresh.open(bridge.session_keys(hello) + ["omt_new"], create_fresh), "ses_1")
        self.assertEqual(fresh.open(bridge.session_keys(inside), create_fresh), "ses_1")
        self.assertEqual(made, ["new"])
        rooted = bridge.Sessions()
        opened = []

        def create_rooted():
            opened.append("new")
            return f"ses_{len(opened)}"

        root = SimpleNamespace(message_id="om_hi", thread_id="", root_id="")
        reply = SimpleNamespace(message_id="om_bot", thread_id="omt_1", root_id="om_hi", parent_id="om_hi")
        check = SimpleNamespace(message_id="om_check", thread_id="", root_id="om_bot", parent_id="om_android")
        later = SimpleNamespace(message_id="om_lala", thread_id="omt_1", root_id="om_hi", parent_id="om_check")
        self.assertEqual(rooted.open(bridge.session_keys(root, reply), create_rooted), "ses_1")
        self.assertEqual(rooted.open(bridge.session_keys(check), create_rooted), "ses_1")
        self.assertEqual(rooted.open(bridge.session_keys(later), create_rooted), "ses_1")
        self.assertEqual(opened, ["new"])

    def test_mention_after_untagged_line_reuses_the_thread_session(self):
        bridge.thread_log = bridge.ThreadLog()
        bridge.thread_log.remember(SimpleNamespace(
            message_id="om_test",
            thread_id="",
            root_id="",
            parent_id="",
            message_type="text",
            content=json.dumps({"text": "test"}),
            sender=SimpleNamespace(sender_type="user"),
        ))
        bridge.thread_log.remember(SimpleNamespace(
            message_id="om_cek",
            thread_id="",
            root_id="om_test",
            parent_id="om_test",
            message_type="text",
            content=json.dumps({"text": "cek cek"}),
            sender=SimpleNamespace(sender_type="user"),
        ))
        follow = SimpleNamespace(message_id="om_gojek", thread_id="", root_id="om_cek", parent_id="om_cek")
        store = bridge.Sessions()
        made = []

        def create():
            made.append("new")
            return f"ses_{len(made)}"

        self.assertEqual(store.open(["om_test"], create), "ses_1")
        self.assertEqual(store.open(bridge.thread_session_keys(follow), create), "ses_1")
        self.assertEqual(made, ["new"])

    def test_only_a_missing_session_is_dropped(self):
        self.assertTrue(bridge.session_missing(RuntimeError("cursor-agent POST /v1/prompt -> 404 missing")))
        self.assertFalse(bridge.session_missing(TimeoutError("timed out")))
        self.assertEqual(bridge.reply_after(
            [
                {"id": "msg_old", "type": "assistant", "content": [{"type": "text", "text": "Hello"}]},
                {"id": "msg_user", "type": "user", "content": [{"type": "text", "text": "check"}]},
                {"id": "msg_new", "type": "assistant", "content": [{"type": "text", "text": "still looking"}]},
            ],
            "msg_user",
        ), "still looking")
        self.assertEqual(bridge.reply_after(
            [
                {"id": "msg_new", "type": "assistant", "time": {"created": 3}, "content": [{"type": "text", "text": "still looking"}]},
                {"id": "msg_user", "type": "user", "time": {"created": 2}, "content": [{"type": "text", "text": "check"}]},
                {"id": "msg_old", "type": "assistant", "time": {"created": 1}, "content": [{"type": "text", "text": "Hello"}]},
            ],
            "msg_user",
        ), "still looking")

    def test_follow_up_waits_for_a_new_reply(self):
        self.assertFalse(bridge.reply_ready("old", "old", "done"))
        self.assertFalse(bridge.reply_ready("old", "", "done"))
        self.assertFalse(bridge.reply_ready("", "new", ""))
        self.assertTrue(bridge.reply_ready("old", "new diff", "done"))
        self.assertTrue(bridge.reply_ready("", "first", "done"))

    def test_oncall_tag_starts_triage_without_the_bot_mention(self):
        os.environ["LARK_TAG_OPEN_IDS"] = "ou_oncall"
        self.assertTrue(bridge.allowed(event("fare missing", mentions=("ou_oncall",))))
        self.assertFalse(bridge.allowed(event("fare missing", mentions=("ou_other",))))
        os.environ.pop("LARK_TAG_OPEN_IDS", None)

    def test_dispatch_redacts_prompt_and_reply(self):
        seen = {}

        def agent(text):
            seen["prompt"] = text
            return "email a@b.co"

        replies = []
        tracker = bridge.Tracker()
        bridge.dispatch(
            "evt_1",
            "om_1",
            "call +62 812-3456-7890 @_user_1",
            lambda _mid, text: replies.append(text),
            agent,
            tracker,
        )
        self.assertNotIn("+62", seen["prompt"])
        self.assertNotIn("@_user_1", seen["prompt"])
        self.assertEqual(replies[-1], "email [EMAIL]")
        self.assertIn("evt_1", tracker.done)

    def test_group_reply_stays_in_the_thread(self):
        seen = {}

        class Messages:
            def reply(self, request):
                seen["flag"] = request.request_body.reply_in_thread
                return SimpleNamespace(success=lambda: True, data=SimpleNamespace(message_id="om_reply"))

        client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=Messages())))
        bridge._send(client, "om_1", "text", "{}", in_thread=True)
        self.assertTrue(seen["flag"])
        bridge._send(client, "om_1", "text", "{}", in_thread=False)
        self.assertFalse(seen["flag"])

    def test_status_names_the_real_step(self):
        self.assertEqual(bridge.status_line("think"), "Thinking....")
        self.assertEqual(bridge.status_line("thread"), "[1/3] Reading the thread")
        self.assertEqual(
            bridge.status_line("search", "gohost · rc/5.81.0_bm_develop"),
            "[2/3] Searching the repos · gohost · rc/5.81.0_bm_develop",
        )
        self.assertEqual(bridge.status_line("write"), "[3/3] Writing the triage")
        self.assertEqual(bridge.status_line("ready"), "Triage ready")
        phase, lines = bridge.investigation_updates("thinking", "write")
        self.assertEqual(phase, "thinking")
        self.assertEqual(lines, [])
        phase, lines = bridge.investigation_updates("thinking", "tool", "mobox-glab search")
        self.assertEqual(
            lines,
            ["[1/3] Reading the thread", "[2/3] Searching the repos · mobox-glab search"],
        )
        phase, lines = bridge.investigation_updates(phase, "write")
        self.assertEqual(lines, ["[3/3] Writing the triage"])
        self.assertEqual(
            bridge.clue_from("Android crash on rc/5.81.0_bm_develop"),
            "gohost · rc/5.81.0_bm_develop",
        )
        messages = [{"parts": [{"type": "tool", "state": {"title": "mobox-glab search gohost"}}]}]
        self.assertEqual(bridge.tool_beat(messages), "mobox-glab search gohost")

    def test_gitlab_tool_metadata_counts_as_a_lookup_only_when_completed(self):
        messages = [{"content": [{
            "type": "tool",
            "tool": "mobox_gitlab",
            "state": {"status": "completed", "input": {"operation": "file", "project": "ios"}},
        }]}]
        self.assertTrue(bridge.repo_was_read(messages))
        messages[0]["content"][0]["state"]["status"] = "error"
        self.assertFalse(bridge.repo_was_read(messages))

    def test_root_cause_must_read_the_repo_before_the_summary(self):
        summary = "**Issue:** payment details are empty\n**Hypothesis:** the screen skips the field"
        self.assertTrue(bridge.needs_code_check(summary, []))
        read = [{"parts": [{"type": "tool", "state": {"input": {"command": "mobox-glab search ios develop payment"}}}]}]
        self.assertFalse(bridge.needs_code_check(summary, read))
        filed = [{"parts": [{"type": "tool", "state": {"input": {"command": "mobox-glab file ios develop PaymentView.swift"}}}]}]
        self.assertFalse(bridge.needs_code_check(summary, filed))
        self.assertFalse(bridge.needs_code_check("Hi. Ask me about the code.", []))
        diary = "I'll start by searching the iOS project for the payment method."
        self.assertTrue(bridge.needs_summary_shape(diary))
        self.assertFalse(bridge.needs_summary_shape(summary))
        self.assertFalse(bridge.needs_summary_shape("Hi. Ask me about the code."))
        prose = (
            "The record reports an iOS-only issue on develop. Confidence is low.\n"
            "- iOS hypothesis: the booking helper skips the payment field.\n"
            "Ask BE to send MoBox the response."
        )
        self.assertTrue(bridge.needs_summary_shape(prose))

    def test_failed_reply_is_not_remembered(self):
        tracker = bridge.Tracker()

        def reply_fn(_mid, text):
            if text != bridge.status_line("search"):
                raise RuntimeError("down")

        with self.assertRaises(RuntimeError):
            bridge.dispatch("evt_1", "om_1", "hello", reply_fn, lambda _text: "ok", tracker)
        self.assertNotIn("evt_1", tracker.done)
        self.assertFalse(tracker.busy("evt_1"))
        calls = []
        bridge.dispatch(
            "evt_1",
            "om_1",
            "hello",
            lambda *_: calls.append("reply"),
            lambda _text: calls.append("agent") or "ok",
            tracker,
        )
        self.assertIn("agent", calls)

    def test_fct_crash_and_feedback_mentions(self):
        os.environ["LARK_CHANNEL_FCT"] = "oc_fct"
        os.environ["LARK_CHANNEL_CRASH"] = "oc_crash"
        os.environ["LARK_CHANNEL_FEEDBACK"] = "oc_feedback"
        self.assertTrue(bridge.allowed(event("x", chat_id="oc_fct")))
        self.assertTrue(bridge.allowed(event("x", chat_id="oc_crash")))
        self.assertTrue(bridge.allowed(event("x", chat_id="oc_feedback")))

    def test_event_id_is_the_dedupe_key(self):
        data = event("hi")
        data.header = SimpleNamespace(event_id="evt_9")
        self.assertEqual(bridge.event_key(data), "evt_9")
        tracker = bridge.Tracker()
        calls = []
        bridge.dispatch("evt_9", "om_9", "hello", lambda *_: None, lambda _t: calls.append("agent") or "ok", tracker)
        bridge.dispatch("evt_9", "om_9", "hello", lambda *_: calls.append("again"), lambda _t: calls.append("agent2") or "ok", tracker)
        self.assertEqual(calls, ["agent"])
        self.assertIn("evt_9", tracker.done)

    def test_thread_downloads_image_and_redacts_file_text(self):
        calls = []

        def fetch(message_id, key, kind):
            calls.append((message_id, key, kind))
            if kind == "image":
                return b"\xff\xd8secret-bytes"
            return b"log a@b.co"

        out = bridge.thread_prompt(
            [
                {"sender_type": "app", "message_type": "text", "text": "bot said a@b.co"},
                {"sender_type": "user", "message_type": "text", "text": "see +62 812-3456-7890"},
                {"sender_type": "user", "message_type": "image", "message_id": "om_i", "image_key": "img_1"},
                {
                    "sender_type": "user",
                    "message_type": "file",
                    "message_id": "om_f",
                    "file_key": "file_1",
                    "file_name": "log.txt",
                },
            ],
            fetch,
        )
        self.assertNotIn("+62", out)
        self.assertNotIn("secret-bytes", out)
        self.assertNotIn("a@b.co", out)
        self.assertIn("[PHONE]", out)
        self.assertIn("[image]", out)
        self.assertIn("[EMAIL]", out)
        self.assertEqual(calls, [("om_i", "img_1", "image"), ("om_f", "file_1", "file")])

    def test_greeting_stays_on_thinking(self):
        lines = []
        replies = []
        bridge.dispatch(
            "evt_hi",
            "om_hi",
            "hi",
            lambda _mid, text: replies.append(text),
            lambda _t: "Hello. How can I help you today?",
            bridge.Tracker(),
            on_status=lambda line: lines.append(line),
        )
        self.assertEqual(lines, [])
        self.assertEqual(replies, ["Hello. How can I help you today?"])

    def test_greeting_reply_has_no_buttons(self):
        offers = []
        replies = []
        bridge.dispatch(
            "evt_hi",
            "om_hi",
            "hi",
            lambda _mid, text: replies.append(text),
            lambda _t: "Hello. How can I help you today?",
            bridge.Tracker(),
            on_offer=lambda mid, answer: offers.append(mid),
        )
        self.assertEqual(offers, [])
        self.assertEqual(replies[-1], "Hello. How can I help you today?")

    def test_offer_callback_when_answer_asks_for_mr(self):
        offers = []
        bridge.dispatch(
            "evt_offer",
            "om_offer",
            "android crash",
            lambda *_: None,
            lambda _t: "**Issue:** crash\n**Platform:** Android\n**Confidence:** high\nRaise MR fix?",
            bridge.Tracker(),
            on_offer=lambda mid, answer: offers.append((mid, "Raise MR fix?" in answer)),
        )
        self.assertEqual(offers, [("om_offer", True)])

    def test_missing_thread_id_still_loads_the_middle_messages(self):
        message = SimpleNamespace(
            message_id="om_tag",
            thread_id="",
            root_id="om_root",
            parent_id="om_root",
            content=json.dumps({"text": "@bot check this"}),
            message_type="text",
            sender=SimpleNamespace(sender_type="user"),
        )
        listed = []

        class Messages:
            def list(self, request):
                listed.append(request.container_id)
                middle = SimpleNamespace(
                    message_id="om_mid",
                    thread_id="omt_1",
                    msg_type="text",
                    sender=SimpleNamespace(sender_type="user"),
                    body=SimpleNamespace(content=json.dumps({"text": "green banner missing"})),
                )
                return SimpleNamespace(
                    success=lambda: True,
                    code=0,
                    data=SimpleNamespace(items=[middle], has_more=False, page_token=""),
                )

            def get(self, _request):
                root = SimpleNamespace(
                    message_id="om_root",
                    thread_id="omt_1",
                    msg_type="text",
                    sender=SimpleNamespace(sender_type="user"),
                    body=SimpleNamespace(content=json.dumps({"text": "first report"})),
                )
                return SimpleNamespace(success=lambda: True, code=0, data=SimpleNamespace(items=[root]))

        client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=Messages())))
        text, _images = bridge.build_context(client, message)
        self.assertEqual(listed, ["omt_1"])
        self.assertLess(text.index("first report"), text.index("green banner missing"))
        self.assertLess(text.index("green banner missing"), text.index("@bot check this"))

    def test_thread_list_failure_keeps_root_and_current(self):
        message = SimpleNamespace(
            message_id="om_bug",
            thread_id="omt_1",
            root_id="om_hello",
            parent_id="",
            content=json.dumps({"text": "green banner missing on iOS"}),
            message_type="text",
            sender=SimpleNamespace(sender_type="user"),
        )

        class Messages:
            def list(self, _request):
                return SimpleNamespace(success=lambda: False, code=99991672, data=None)

            def get(self, _request):
                item = SimpleNamespace(
                    message_id="om_hello",
                    thread_id="",
                    content=json.dumps({"text": "Hello"}),
                    message_type="text",
                    sender=SimpleNamespace(sender_type="user"),
                )
                return SimpleNamespace(success=lambda: True, code=0, data=SimpleNamespace(items=[item]))

        client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=Messages())))
        text, _images = bridge.build_context(client, message)
        self.assertIn("Hello", text)
        self.assertIn("green banner missing on iOS", text)

    def test_untagged_line_between_mentions_reaches_the_agent(self):
        bridge.thread_log = bridge.ThreadLog()
        os.environ["LARK_CHANNEL_HACK_OF_THRONES"] = "oc_hack"
        quiet = event("cek cek", mentions=())
        quiet.event.message.message_id = "om_cek"
        quiet.event.message.thread_id = "omt_hafiz"
        quiet.event.message.root_id = "om_test"
        quiet.event.message.create_time = "3000"
        bridge.handle(SimpleNamespace(), quiet)
        message = SimpleNamespace(
            message_id="om_gojek",
            thread_id="omt_hafiz",
            root_id="om_test",
            parent_id="om_test",
            create_time="4000",
            content=json.dumps({"text": "@MoBox cek gojek"}),
            message_type="text",
            sender=SimpleNamespace(sender_type="user", sender_name="Hafiz", id="ou_h"),
        )

        class Messages:
            def list(self, _request):
                hafiz = SimpleNamespace(
                    message_id="om_hafiz",
                    thread_id="omt_hafiz",
                    create_time="2000",
                    msg_type="text",
                    sender=SimpleNamespace(sender_type="user", sender_name="Hafiz", id="ou_h"),
                    body=SimpleNamespace(content=json.dumps({"text": "this is hafiz"})),
                )
                last = SimpleNamespace(
                    message_id="om_gojek",
                    thread_id="omt_hafiz",
                    create_time="4000",
                    msg_type="text",
                    sender=SimpleNamespace(sender_type="user", sender_name="Hafiz", id="ou_h"),
                    body=SimpleNamespace(content=json.dumps({"text": "@MoBox cek gojek"})),
                )
                return SimpleNamespace(
                    success=lambda: True,
                    code=0,
                    data=SimpleNamespace(items=[hafiz, last], has_more=False, page_token=""),
                )

            def get(self, _request):
                root = SimpleNamespace(
                    message_id="om_test",
                    thread_id="omt_hafiz",
                    create_time="1000",
                    msg_type="text",
                    sender=SimpleNamespace(sender_type="user", sender_name="Hafiz", id="ou_h"),
                    body=SimpleNamespace(content=json.dumps({"text": "test"})),
                )
                return SimpleNamespace(success=lambda: True, code=0, data=SimpleNamespace(items=[root]))

        client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=Messages())))
        text, _images = bridge.build_context(client, message)
        self.assertLess(text.index("test"), text.index("cek cek"))
        self.assertLess(text.index("cek cek"), text.index("cek gojek"))

    def test_draft_for_both_platforms_opens_one_mr_per_project(self):
        import tempfile
        from unittest import mock

        asked, ran = [], []

        def agent(prompt, session_key=""):
            asked.append(prompt)
            return "```diff\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n```"

        def run(command, **_kwargs):
            ran.append(command[2])
            return SimpleNamespace(returncode=0, stdout=f"https://mr/{command[2]}\n", stderr="")

        item = {"projects": ["gohost", "ios"], "branch": "develop", "issue": "om_1", "summary": "**Change:** x"}
        with tempfile.TemporaryDirectory() as work, mock.patch.dict(os.environ, {"MOBOX_WORK": work}), \
                mock.patch.object(bridge, "run_agent", agent), mock.patch.object(bridge.subprocess, "run", run):
            result = bridge.run_draft(item)
        self.assertEqual(ran, ["gohost", "ios"])
        self.assertIn("gohost", asked[0])
        self.assertIn("ios", asked[1])
        self.assertIn("https://mr/gohost", result)
        self.assertIn("https://mr/ios", result)

    def test_failed_draft_keeps_the_card_for_the_failed_project(self):
        from unittest import mock

        class Now:
            def __init__(self, target, daemon=None):
                self.target = target

            def start(self):
                self.target()

        said = []
        updated = []
        bridge.pending = bridge.mr_flow.Pending()
        bridge.pending.put("a1", "ou_me", {"projects": ["gohost", "ios"], "branch": "develop", "issue": "om_1"}, 0)
        data = SimpleNamespace(event=SimpleNamespace(
            action=SimpleNamespace(value={"action": "raise_mr", "action_id": "a1"}),
            operator=SimpleNamespace(open_id="ou_me"),
            context=SimpleNamespace(open_message_id="om_card"),
        ))
        result = "gohost: https://mr/1\nios: Draft MR failed: corrupt patch"
        with mock.patch.object(bridge.time, "time", return_value=1), \
                mock.patch.object(bridge.threading, "Thread", Now), \
                mock.patch.object(bridge, "run_draft", return_value=result), \
                mock.patch.object(bridge, "reply", lambda _c, _m, text: said.append(text)), \
                mock.patch.object(bridge, "update_card", lambda _c, _m, card: updated.append(card)):
            response = bridge.on_card(SimpleNamespace(), data)
            self.assertEqual(bridge.pending.peek("a1", 2)["projects"], ["ios"])
        self.assertEqual(response.card.type, "raw")
        self.assertIn("still working", response.card.data["elements"][0]["text"]["content"])
        self.assertNotIn("action", [item["tag"] for item in response.card.data["elements"]])
        self.assertIn("https://mr/1", said[-1])
        self.assertIn("Raise MR", said[-1])
        self.assertTrue(updated)
        self.assertNotIn("still working", updated[-1]["elements"][0]["text"]["content"])

    def test_follow_up_mention_sends_only_unsent_lines(self):
        bridge.thread_log = bridge.ThreadLog()

        def line(mid, text, at):
            return SimpleNamespace(
                message_id=mid,
                thread_id="omt_hola",
                create_time=at,
                msg_type="text",
                sender=SimpleNamespace(sender_type="user", id="ou_h"),
                body=SimpleNamespace(content=json.dumps({"text": text})),
            )

        listed = [line("om_hola", "@MoBox hola", "1000")]

        class Messages:
            def list(self, _request):
                return SimpleNamespace(
                    success=lambda: True,
                    code=0,
                    data=SimpleNamespace(items=list(listed), has_more=False, page_token=""),
                )

        client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=Messages())))
        first = SimpleNamespace(message_id="om_hola", thread_id="omt_hola", root_id="", parent_id="",
                                content=json.dumps({"text": "@MoBox hola"}), message_type="text",
                                sender=SimpleNamespace(sender_type="user", id="ou_h"))
        self.assertIn("hola", bridge.build_context(client, first)[0])
        listed += [
            line("om_bot", "Hello. Send the platform", "1500"),
            line("om_banner", "got android estimate green banner issue", "2000"),
            line("om_ios", "but working fine on iOS", "3000"),
            line("om_check", "can you check? @MoBox", "4000"),
        ]
        listed[1].sender.sender_type = "app"
        follow = SimpleNamespace(message_id="om_check", thread_id="omt_hola", root_id="om_hola", parent_id="om_hola",
                                 content=json.dumps({"text": "can you check? @MoBox"}), message_type="text",
                                 sender=SimpleNamespace(sender_type="user", id="ou_h"))
        text = bridge.build_context(client, follow)[0]
        self.assertNotIn("hola", text)
        self.assertLess(text.index("green banner"), text.index("working fine on iOS"))
        self.assertLess(text.index("working fine on iOS"), text.index("can you check"))

    def test_follow_up_sends_only_new_lines(self):
        cursor = bridge.ThreadCursor()
        first = [
            {"message_id": "om_1", "text": "bug"},
            {"message_id": "om_2", "text": "which build?"},
            {"message_id": "om_3", "text": "@bot cause?"},
        ]
        second = first + [
            {"message_id": "om_4", "sender_type": "app", "text": "the flag is false"},
            {"message_id": "om_5", "text": "5.81 rc"},
            {"message_id": "om_6", "text": "not satisfied"},
        ]
        self.assertEqual([item["message_id"] for item in cursor.slice("om_1", first)], ["om_1", "om_2", "om_3"])
        self.assertEqual([item["message_id"] for item in cursor.slice("om_1", second)], ["om_4", "om_5", "om_6"])
        self.assertEqual(
            [item["message_id"] for item in cursor.slice("om_other", second)],
            ["om_1", "om_2", "om_3", "om_4", "om_5", "om_6"],
        )

    def test_at_only_message_keeps_the_name(self):
        message = SimpleNamespace(
            message_id="om_tag",
            sender_type="user",
            sender=SimpleNamespace(sender_type="user", sender_name="QA", id="ou_qa"),
            message_type="text",
            content=json.dumps({"text": "@_user_1"}),
            mentions=[SimpleNamespace(key="@_user_1", name="Dev")],
        )
        out = bridge.thread_prompt([bridge.item_from(message)], lambda *_: b"")
        self.assertIn("QA: @Dev", out)
        self.assertNotIn("@_user_1", out)

    def test_same_sender_keeps_one_person_label(self):
        items = [
            {"message_id": "om_1", "sender_type": "user", "sender_id": "ou_qa", "message_type": "text", "text": "banner missing"},
            {"message_id": "om_2", "sender_type": "user", "sender_id": "ou_dev", "message_type": "text", "text": "which build?"},
            {"message_id": "om_3", "sender_type": "user", "sender_id": "ou_qa", "message_type": "text", "text": "5.81 rc"},
        ]
        out = bridge.thread_prompt(items, lambda *_: b"")
        self.assertIn("Person 1: banner missing", out)
        self.assertIn("Person 2: which build?", out)
        self.assertIn("Person 1: 5.81 rc", out)

    def test_thread_keeps_each_speaker(self):
        items = [
            {"message_id": "om_1", "sender_type": "user", "sender_name": "QA", "message_type": "text", "text": "banner missing on iOS"},
            {"message_id": "om_2", "sender_type": "user", "sender_name": "Dev", "message_type": "text", "text": "which screen?"},
            {"message_id": "om_3", "sender_type": "user", "sender_name": "QA", "message_type": "text", "text": "estimate screen"},
        ]
        out = bridge.thread_prompt(items, lambda *_: b"")
        self.assertIn("QA: banner missing on iOS", out)
        self.assertIn("Dev: which screen?", out)
        self.assertIn("QA: estimate screen", out)
        self.assertLess(out.index("which screen"), out.index("estimate screen"))

    def test_thread_context_keeps_qa_notes_and_redacts(self):
        current = {
            "message_id": "om_at",
            "sender_type": "user",
            "message_type": "text",
            "text": "please check @_user_1",
        }
        pages = [
            [{"message_id": "om_root", "sender_type": "user", "message_type": "text", "text": "fare blank a@b.co"}],
            [{"message_id": "om_qa", "sender_type": "user", "message_type": "text", "text": "QA: reproduced on staging"}],
        ]
        out = bridge.prompt_from_pages(current, pages, lambda *_: b"")
        self.assertIn("fare blank [EMAIL]", out)
        self.assertIn("QA: reproduced on staging", out)
        self.assertIn("please check", out)
        self.assertLess(out.index("fare blank"), out.index("QA:"))
        self.assertLess(out.index("QA:"), out.index("please check"))

    def test_qa_card_image_is_sent_as_a_file_part(self):
        qa = SimpleNamespace(
            message_id="om_qa",
            thread_id="omt_1",
            sender_type="user",
            message_type="post",
            content=json.dumps(
                {
                    "content": [
                        [
                            {"tag": "text", "text": "button overlaps"},
                            {"tag": "img", "image_key": "img_qa"},
                        ]
                    ]
                }
            ),
        )

        def fetch(_message_id, key, kind):
            self.assertEqual((key, kind), ("img_qa", "image"))
            return b"\xff\xd8secret-bytes"

        text, images = bridge.bundle([bridge.item_from(qa)], fetch)
        self.assertIn("button overlaps", text)
        self.assertIn("[image]", text)
        self.assertNotIn("secret-bytes", text)
        self.assertEqual(images[0]["mime"], "image/jpeg")
        self.assertTrue(images[0]["url"].startswith("data:image/jpeg;base64,"))
        body = bridge.prompt_body(text, images, supports_images=True)
        self.assertEqual(body["text"], text)
        self.assertEqual(body["files"], [{"uri": images[0]["url"], "name": "qa.jpg"}])
        self.assertNotIn("parts", body)

    def test_linked_record_prompt_leads_with_investigation_even_after_hi(self):
        text = "Someone: hi @MoBox can you check this?\n[linked record]\nBugs Title: Payment details missing"
        body = bridge.prompt_body(text, [], supports_images=False)

        self.assertTrue(body["text"].startswith("Investigate the request below using the linked Lark record"))
        self.assertIn(text.replace("Someone: hi ", "Someone: "), body["text"])
        self.assertNotIn("hi @MoBox", body["text"])
        self.assertIn(bridge._TEXT_ONLY_EVIDENCE, body["text"])

    def test_text_only_model_skips_lark_image_download(self):
        from unittest import mock

        data = event("check the screenshot")
        message = data.event.message
        message.message_type = "post"
        message.content = json.dumps({"content": [[
            {"tag": "text", "text": "check the screenshot"},
            {"tag": "img", "image_key": "img_qa"},
        ]]})
        with mock.patch.object(bridge, "supports_image_input", return_value=False), \
                mock.patch.object(bridge, "thread_id_of", return_value=""), \
                mock.patch.object(bridge, "download_resource", return_value=b"\xff\xd8image") as download:
            text, images = bridge.build_context(None, message)

        self.assertIn("check the screenshot", text)
        self.assertEqual(images, [])
        download.assert_not_called()

    def test_text_only_agent_gets_evidence_rules_even_without_attachments(self):
        from unittest import mock

        with mock.patch.object(bridge, "supports_image_input", return_value=False), \
                mock.patch.object(bridge, "sessions", bridge.Sessions()), \
                mock.patch.object(bridge, "ask", return_value={
                    "session_id": "test", "user_id": "u1", "outcome": "finished", "reply": "Noted.", "messages": [],
                }) as api:
            bridge.run_agent("The booking screen is blank.")

        body = api.call_args.args[1]
        self.assertIn("booking screen is blank.", body["text"].lower())
        self.assertIn("Do not ask for screenshots", body["text"])
        self.assertIn("error text", body["text"])
        self.assertNotIn("files", body)

    def test_internal_code_check_does_not_repeat_image_guidance(self):
        from unittest import mock

        first_answer = "**Issue:** Payment is blank"
        final_answer = "I could not find the cause."
        replies = [
            {"session_id": "test", "user_id": "u1", "outcome": "finished", "reply": first_answer, "messages": []},
            {"session_id": "test", "user_id": "u2", "outcome": "finished", "reply": final_answer, "messages": []},
        ]
        with mock.patch.object(bridge, "sessions", bridge.Sessions()), \
                mock.patch.object(bridge, "supports_image_input", return_value=False), \
                mock.patch.object(bridge, "ask", side_effect=replies) as api:
            answer = bridge.run_agent("The booking screen is blank.")

        self.assertEqual(answer, final_answer)
        first_prompt, follow_up = [call.args[1]["text"] for call in api.call_args_list]
        self.assertIn(bridge._TEXT_ONLY_EVIDENCE, first_prompt)
        self.assertEqual(follow_up, bridge._CODE_CHECK)

    def test_provider_422_retries_with_context_without_replaying_tool_outputs(self):
        from unittest import mock

        store = bridge.Sessions()
        store.open(["thread", "alias"], lambda: "old")
        first_prompt = bridge.prompt_body(
            "The iOS payment method is blank.\n[linked record]", [], supports_images=False,
        )
        history = [
            {"type": "user", "id": "report", "text": first_prompt["text"]},
            {"type": "user", "id": "nudge", "text": bridge._CODE_CHECK},
            {"type": "assistant", "content": [
                {"type": "tool", "tool": "mobox_gitlab", "state": {
                    "status": "completed", "input": {"operation": "file"}, "output": "Raw source dump",
                }},
                {"type": "text", "text": "PaymentView.swift reads paymentMethod."},
            ]},
        ]
        rejected = history + [
            {"type": "user", "id": "evidence"},
            {"type": "assistant", "error": {"type": "provider.invalid-request", "status": 422}},
        ]
        answer = "The response includes paymentMethod."
        overflow = {"type": "provider.invalid-request", "status": 422}
        with mock.patch.object(bridge, "sessions", store), \
                mock.patch.object(bridge, "supports_image_input", return_value=False), \
                mock.patch.object(bridge, "ask", side_effect=[
                    {"session_id": "old", "user_id": "evidence", "outcome": "failed", "reply": "", "messages": rejected, "error": overflow},
                    {"session_id": "new", "user_id": "retry", "outcome": "finished", "reply": answer, "messages": []},
                ]) as api:
            result = bridge.run_agent('API response: {"paymentMethod":"cash"}', session_key="thread")

        self.assertEqual(result, answer)
        retry = api.call_args.args
        self.assertEqual(retry[0], "")
        self.assertIn("The iOS payment method is blank.", retry[1]["text"])
        self.assertIn("PaymentView.swift", retry[1]["text"])
        self.assertIn('{"paymentMethod":"cash"}', retry[1]["text"])
        self.assertEqual(retry[1]["text"].count(bridge._LINKED_RECORD_GUIDANCE), 1)
        self.assertEqual(retry[1]["text"].count(bridge._TEXT_ONLY_EVIDENCE), 1)
        self.assertNotIn("Raw source dump", retry[1]["text"])
        self.assertNotIn(bridge._CODE_CHECK, retry[1]["text"])
        self.assertEqual(store.lookup("alias"), "new")

    def test_provider_422_after_first_turn_code_check_reuses_completed_file_results(self):
        from unittest import mock

        report = "Why is the iOS payment method blank?"
        first_answer = "**Issue:** Payment is blank. **Confidence:** Medium"
        original = {"type": "user", "id": "report", "text": report}
        nudge = {"type": "user", "id": "nudge", "text": bridge._CODE_CHECK}
        tool = {"type": "tool", "name": "mobox_gitlab", "state": {
            "input": {"operation": "file", "project": "ios", "args": ["develop", "PaymentView.swift"]},
            "status": "completed",
            "content": [{"type": "text", "text": "paymentLabel.isHidden = paymentMethod == nil\n" + "x" * 2000}],
        }}
        rejected = [original, nudge, {"type": "assistant", "content": [tool]},
                    {"type": "assistant", "error": {"type": "provider.invalid-request", "status": 422}}]
        answer = "**Issue:** The label is hidden when paymentMethod is nil."
        overflow = {"type": "provider.invalid-request", "status": 422}
        with mock.patch.object(bridge, "sessions", bridge.Sessions()), \
                mock.patch.object(bridge, "supports_image_input", return_value=False), \
                mock.patch.object(bridge, "ask", side_effect=[
                    {"session_id": "first", "user_id": "report", "outcome": "finished", "reply": first_answer, "messages": [original]},
                    {"session_id": "first", "user_id": "nudge", "outcome": "failed", "reply": "", "messages": rejected, "error": overflow},
                    {"session_id": "new", "user_id": "retry", "outcome": "finished", "reply": answer, "messages": []},
                ]) as api:
            result = bridge.run_agent(report, session_key="thread")

        self.assertEqual(result, answer)
        retry = api.call_args.args
        self.assertEqual(retry[0], "")
        self.assertIn(report, retry[1]["text"])
        self.assertIn("PaymentView.swift", retry[1]["text"])
        self.assertIn("paymentMethod == nil", retry[1]["text"])
        self.assertNotIn("x" * 2000, retry[1]["text"])
        self.assertNotIn(bridge._CODE_CHECK, retry[1]["text"])

    def test_qa_mention_post_stays_when_dev_tags_the_bot(self):
        qa = SimpleNamespace(
            message_id="om_qa",
            thread_id="omt_1",
            sender_type="user",
            message_type="post",
            content=json.dumps(
                {
                    "content": [
                        [
                            {"tag": "at", "user_name": "Dev"},
                            {"tag": "text", "text": "reproduced on staging a@b.co"},
                        ]
                    ]
                }
            ),
        )
        dev = {"message_id": "om_dev", "sender_type": "user", "message_type": "text", "text": "check this"}
        out = bridge.prompt_from_pages(dev, [[bridge.item_from(qa)]], lambda *_: b"")
        self.assertIn("reproduced on staging [EMAIL]", out)
        self.assertIn("check this", out)
        self.assertLess(out.index("reproduced"), out.index("check this"))

    def test_interactive_card_form_is_in_the_prompt(self):
        card = {
            "message_id": "om_card",
            "sender_type": "user",
            "message_type": "interactive",
            "text": bridge.text_from_body(
                {
                    "title": "Release check",
                    "elements": [
                        {
                            "tag": "form",
                            "elements": [
                                {
                                    "tag": "input",
                                    "placeholder": "5.81",
                                    "label": {"content": "Build"},
                                }
                            ],
                        }
                    ],
                }
            ),
        }
        out = bridge.bundle([card], lambda *_: b"")[0]
        self.assertIn("Release check", out)
        self.assertIn("Build", out)
        self.assertIn("5.81", out)

    def test_message_link_expansion_inserts_the_card(self):
        url = "https://applink.larksuite.com/client/message/link/open?token=abc"

        def resolve(found):
            self.assertEqual(found, url)
            return {
                "data": {
                    "messages": [
                        {
                            "message": {
                                "msg_type": "interactive",
                                "body": {
                                    "content": json.dumps(
                                        {"elements": [{"tag": "markdown", "content": "wiki row: fare blank"}]}
                                    )
                                },
                            }
                        }
                    ]
                }
            }

        out = bridge.expand_message_links(f"please read {url}", resolve)
        self.assertIn("[linked message]", out)
        self.assertIn("wiki row: fare blank", out)
        self.assertLess(out.index(url), out.index("wiki row"))

    def test_record_link_expansion_inserts_the_form_row(self):
        url = "https://gotocompany.sg.larksuite.com/record/Si7zTok?ccm_open_type=im_card_workflow_button"

        def resolve(token):
            self.assertEqual(token, "Si7zTok")
            return {
                "Bugs Title": "fare blank",
                "Description": [{"text": "banner missing"}],
                "Reporter.Work email": "a@b.co",
            }

        out = bridge.expand_record_links(f"check {url}", resolve)
        self.assertIn("[linked record]", out)
        self.assertIn("Bugs Title: fare blank", out)
        self.assertIn("Description: banner missing", out)
        self.assertIn("[EMAIL]", out)
        self.assertNotIn("a@b.co", out)

    def test_wrapped_record_link_still_loads_the_row(self):
        wrapped = (
            "https://applink.feishu.cn/client/web_url/open?mode=sidebar-semi&url="
            "https%3A%2F%2Fgotocompany.sg.larksuite.com%2Frecord%2FSi7zTok%3Fccm_open_type%3Drecord_message"
        )

        def resolve(token):
            self.assertEqual(token, "Si7zTok")
            return {"Bugs Title": "fare blank"}

        out = bridge.expand_record_links(f'[Record from "FCT Bugs Report"]({wrapped})', resolve)
        self.assertIn("[linked record]", out)
        self.assertIn("Bugs Title: fare blank", out)

    def test_unread_record_link_keeps_the_reason(self):
        url = "https://example.larksuite.com/record/AbC123"

        def resolve(_token):
            raise RuntimeError("permission denied")

        out = bridge.expand_record_links(url, resolve)
        self.assertIn("[record link unread: permission denied]", out)

    def test_unread_message_link_keeps_the_reason(self):
        url = "https://applink.larksuite.com/client/message/link/open?token=abc"

        def resolve(_url):
            raise RuntimeError("Access denied. One of the following scopes is required: [im:message.message_link:read]")

        out = bridge.expand_message_links(url, resolve)
        self.assertIn("[message link unread:", out)
        self.assertIn("im:message.message_link:read", out)

    def test_reply_keeps_root_and_parent_outside_the_page(self):
        message = SimpleNamespace(root_id="om_root", parent_id="om_qa", message_id="om_at")
        listed = [{"message_id": "om_at"}]
        self.assertEqual(bridge.ancestor_ids(message, listed), ["om_root", "om_qa"])


if __name__ == "__main__":
    unittest.main()
