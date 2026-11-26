import unittest

import mr_flow


class MrFlowTests(unittest.TestCase):
    def test_only_requester_can_confirm_before_expiry(self):
        pending = mr_flow.Pending()
        pending.put("a1", "ou_me", {"project": "gohost"}, now=0)
        self.assertIsNone(pending.take("a1", "ou_other", now=10))
        self.assertEqual(pending.take("a1", "ou_me", now=299)["project"], "gohost")
        self.assertIsNone(pending.take("a1", "ou_me", now=300))

    def test_confirm_expires_after_five_minutes(self):
        pending = mr_flow.Pending()
        pending.put("a1", "ou_me", {"project": "gohost"}, now=0)
        self.assertIsNone(pending.take("a1", "ou_me", now=300))

    def test_confirm_card_names_the_action(self):
        card = mr_flow.confirm_card("a1")
        actions = card["elements"][-1]["actions"]
        self.assertEqual([item["text"]["content"] for item in actions], ["Confirm", "Cancel"])
        self.assertEqual(actions[0]["value"], {"action": "confirm", "action_id": "a1"})
        self.assertEqual(actions[1]["value"]["action"], "cancel")

    def test_triage_buttons_only_for_a_summary(self):
        summary = "**Issue:** green banner missing\n**Platform:** iOS\n**Confidence:** medium\nRaise MR fix?"
        self.assertTrue(mr_flow.is_triage(summary))
        self.assertFalse(mr_flow.is_triage("Hello. How can I help you today?"))
        self.assertFalse(mr_flow.is_triage("transport only"))
        empty = "**Issue:** No bug details provided\n**Platform:** not specified\nRaise MR fix?"
        self.assertFalse(mr_flow.is_triage(empty))
        low = "**Issue:** green banner missing\n**Platform:** iOS\n**Confidence:** low\nRaise MR fix?"
        self.assertFalse(mr_flow.is_triage(low))
        forgotten = (
            "**Issue:** payment method details empty\n"
            "**Platform:** iOS\n"
            "**Confidence:** Medium\n"
            "**Question:** Can you confirm the API?"
        )
        self.assertTrue(mr_flow.is_triage(forgotten))

    def test_every_reply_is_a_card_and_buttons_only_when_clear(self):
        plain = mr_flow.answer_card("The fare screen reads the flag from FareViewModel.")
        self.assertEqual(plain["elements"][0]["tag"], "div")
        self.assertNotIn("header", plain)
        self.assertNotIn("action", [item["tag"] for item in plain["elements"]])
        loading = mr_flow.status_card("Thinking....")
        self.assertNotIn("header", loading)
        self.assertTrue(loading["config"]["update_multi"])
        self.assertEqual(loading["elements"][0]["text"]["content"], "Thinking....")
        low = mr_flow.answer_card(
            "**Issue:** banner missing\n**Platform:** iOS\n**Confidence:** low",
            "a1",
        )
        self.assertNotIn("action", [item["tag"] for item in low["elements"]])
        clear = mr_flow.answer_card(
            "**Issue:** payment method details are empty\n**Platform:** iOS\n**Confidence:** medium",
            "a1",
        )
        self.assertEqual(clear["elements"][-1]["tag"], "action")
        self.assertTrue(clear["config"]["update_multi"])
        asked = mr_flow.answer_card(
            "**Issue:** payment method details are empty\n"
            "**Platform:** iOS\n"
            "**Confidence:** medium\n"
            "**Question:** Can you confirm if the issue is also present on the rc branch?",
            "a1",
        )
        asked_labels = [item["text"]["content"] for item in asked["elements"][-1]["actions"]]
        self.assertNotIn("Raise MR fix", asked_labels)
        self.assertIn("Cross-check", asked_labels)
        settled = mr_flow.answer_card(
            "**Issue:** payment method details are empty\n"
            "**Platform:** iOS\n"
            "**Confidence:** medium\n"
            "**Question:** none",
            "a1",
        )
        settled_labels = [item["text"]["content"] for item in settled["elements"][-1]["actions"]]
        self.assertIn("Raise MR fix", settled_labels)

    def test_card_fields_reads_a_string_value(self):
        from types import SimpleNamespace

        data = SimpleNamespace(
            event=SimpleNamespace(
                action=SimpleNamespace(value='{"action": "raise_mr", "action_id": "a1"}'),
                operator=SimpleNamespace(open_id="ou_me"),
                context=SimpleNamespace(open_message_id="om_1"),
            )
        )
        self.assertEqual(mr_flow.card_fields(data), ("a1", "raise_mr", "ou_me", "om_1"))

    def test_triage_card_has_the_three_actions(self):
        card = mr_flow.triage_card("Platform: Android\nRaise MR fix?", "a1")
        actions = card["elements"][-1]["actions"]
        self.assertEqual(
            [item["text"]["content"] for item in actions],
            ["Raise MR fix", "Cross-check", "Not useful"],
        )
        self.assertEqual(actions[0]["value"]["action"], "raise_mr")
        self.assertEqual(actions[1]["value"]["action"], "cross_check")
        self.assertEqual(actions[2]["value"]["action"], "not_useful")
        self.assertIn("Platform: Android", card["elements"][0]["text"]["content"])

    def test_triage_card_keeps_only_the_action_fields(self):
        summary = "\n".join([
            "**Issue:** payment details are empty",
            "**Platform:** iOS",
            "**Where:** mobile/consumer-ios",
            "**FE or BE:** FE",
            "**Impact:** crash data unavailable",
            "**Checked:** develop",
            "**Next:** confirm the rc branch",
            "**Hypothesis:** the participant screen never reads the payment field",
            "**Evidence:** not in the report",
            "**Confidence:** medium",
            "**Disproof:** compare the host screen",
            "**Question:** Can you confirm the rc branch?",
        ])
        body = mr_flow.triage_card(summary, "a1")["elements"][0]["text"]["content"]
        self.assertIn("the participant screen never reads the payment field", body)
        self.assertIn("**FE or BE:** FE", body)
        self.assertIn("**Checked:** develop", body)
        self.assertIn("**Confidence:** <text_tag color='yellow'>medium</text_tag>", body)
        self.assertIn("**Action Item:** Can you confirm the rc branch?", body)
        self.assertNotIn("color='red'>Can you confirm", body)
        self.assertNotIn("**Issue:**", body)
        self.assertNotIn("**Platform:**", body)
        self.assertNotIn("**Evidence:**", body)
        self.assertNotIn("**Question:**", body)
        plain = mr_flow.answer_card(summary.replace("**Confidence:** medium", "**Confidence:** low"))
        shown = plain["elements"][0]["text"]["content"]
        self.assertNotIn("action", [item["tag"] for item in plain["elements"]])
        self.assertNotIn("**Issue:**", shown)
        self.assertIn("**Confidence:** <text_tag color='red'>low</text_tag>", shown)
        self.assertIn("**Action Item:** Can you confirm the rc branch?", shown)
        self.assertNotIn("color='red'>Can you confirm", shown)
        high = mr_flow.triage_card(summary.replace("**Confidence:** medium", "**Confidence:** high"), "a1")
        self.assertIn("color='green'>high", high["elements"][0]["text"]["content"])
        no_question = summary.replace("**Question:** Can you confirm the rc branch?", "**Question:** none")
        fallback = mr_flow.triage_card(no_question, "a1")["elements"][0]["text"]["content"]
        self.assertIn("**Action Item:** confirm the rc branch", fallback)

    def test_draft_description_names_root_cause_and_stays_unverified(self):
        summary = "Platform: Android\n\n## Hypothesis\nnull became required\n## Evidence\n- x"
        text = mr_flow.draft_description(summary, "ou_me")
        self.assertIn("null became required", text)
        self.assertIn("unverified: no lint or compile", text)
        self.assertIn("generated by bot, needs review", text)
        self.assertIn("Requester: ou_me", text)
        self.assertEqual(mr_flow.gitlab_assignee("ou_me"), "")
        self.assertEqual(mr_flow.gitlab_assignee("alice"), "alice")

    def test_loading_card_replaces_the_buttons(self):
        card = mr_flow.loading_card()
        self.assertEqual(card["header"]["title"]["content"], "Opening draft MR")
        self.assertNotIn("action", [item["tag"] for item in card["elements"]])
        self.assertIn("still working", card["elements"][0]["text"]["content"])

    def test_change_plan_offers_a_raise_mr_card(self):
        plan = "**Change:** add estimate_banner_shown event\n**Platform:** both\n**Where:** EstimateViewModel.kt\nRaise MR?"
        self.assertTrue(mr_flow.is_triage(plan))
        self.assertFalse(mr_flow.is_triage("**Change:** add event\n**Platform:** not specified\nRaise MR?"))
        card = mr_flow.triage_card(plan, "a1")
        self.assertEqual(card["header"]["title"]["content"], "Change")
        self.assertEqual(card["elements"][-1]["actions"][0]["text"]["content"], "Raise MR")
        self.assertNotIn("root cause not stated", mr_flow.draft_description(plan, "ou_me"))

    def test_prepare_draft_needs_a_platform_and_a_branch(self):
        item, err = mr_flow.prepare_draft("Platform: unclear", "bug", "om_1")
        self.assertIn("Android or iOS", err)
        self.assertIsNone(item)
        item, err = mr_flow.prepare_draft("Platform: both", "bug on rc/5.81.0_bm_develop", "om_1")
        self.assertIsNone(err)
        self.assertEqual(item["projects"], ["gohost", "ios"])
        item, err = mr_flow.prepare_draft("Platform: iOS", "branch rc/5.81.0_bm_develop", "om_1")
        self.assertIsNone(err)
        self.assertEqual(item["projects"], ["ios"])
        self.assertEqual(item["branch"], "rc/5.81.0_bm_develop")
        self.assertEqual(item["issue"], "om_1")
        self.assertEqual(item["git_branch"], "mobox/fix-om_1")
        change, err = mr_flow.prepare_draft(
            "**Change:** add the estimate banner event\n**Platform:** Android\nRaise MR?",
            "develop",
            "om_3",
        )
        self.assertIsNone(err)
        self.assertEqual(change["git_branch"], "mobox/change-om_3")
        item, err = mr_flow.prepare_draft("Platform: iOS", "green banner missing", "om_2")
        self.assertIsNone(err)
        self.assertEqual(item["branch"], "develop")

    def test_confirm_action_calls_draft_once(self):
        pending = mr_flow.Pending()
        pending.put("a1", "ou_me", {"project": "gohost", "git_branch": "mobox/fix-om_1"}, now=0)
        calls = []
        message = mr_flow.confirm_action(
            pending,
            "a1",
            "confirm",
            "ou_me",
            now=1,
            draft=lambda item: calls.append(item["git_branch"]) or "https://mr/1",
        )
        self.assertEqual(message, "https://mr/1")
        self.assertEqual(calls, ["mobox/fix-om_1"])
        again = mr_flow.confirm_action(
            pending, "a1", "confirm", "ou_me", now=2, draft=lambda item: "nope"
        )
        self.assertEqual(again, "expired or not your request")

    def test_cancel_drops_the_action(self):
        pending = mr_flow.Pending()
        pending.put("a1", "ou_me", {"project": "gohost"}, now=0)
        self.assertEqual(
            mr_flow.confirm_action(pending, "a1", "cancel", "ou_me", now=1, draft=lambda _item: "nope"),
            "cancelled",
        )
        self.assertIsNone(pending.take("a1", "ou_me", now=2))

    def test_extract_diff(self):
        text = "note\n```diff\n--- a/x.kt\n+++ b/x.kt\n@@ -1 +1 @@\n-old\n+new\n```\n"
        self.assertIn("--- a/x.kt", mr_flow.extract_diff(text))
        self.assertEqual(mr_flow.extract_diff("no patch here"), "")


if __name__ == "__main__":
    unittest.main()
