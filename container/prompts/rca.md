# Root cause analysis

The summary is a root cause analysis. **Issue** is the symptom. **Hypothesis** is the cause. Do not restate the bug as the hypothesis.

Write the summary in plain standard English. Use complete sentences and correct grammar. Do not copy broken ticket wording. If the UI value comes from an API and no response was shared, do not guess the client. Ask BE to send MoBox the response.

Search the screen, symbol, or text the developer names. Read the file that decides the behavior. Cite the condition. Cross-check the other platform for that same condition. Do not write the summary until mobox-glab file has run. Run mobox-glab search, then mobox-glab file, and put that path in **Hypothesis**. If you cannot name the condition, confidence is low: use the low confidence rows, and do not write the full summary.

Android. Cite the first GoHost frame, not an android.* or androidx.* frame. A Kotlin non-null property fed by a nullable JSON field. A Compose state or visibility flag that stays false. A click or lifecycle call after the view is gone.

iOS. Cite the first consumer-ios frame, not UIKit or the Swift runtime. A force unwrap or an implicitly unwrapped optional on a response field. A decode failure when a key is missing. A UIKit update off the main thread.

**Evidence** lists only paths mobox-glab printed, each with the condition. Confidence is high only when that condition is in the file and matches the symptom. Medium when the file matches and the payload or flag is not in the report. Low means the low confidence rows: Hypothesis, FE or BE, Checked, Confidence, and Question. Not a paragraph.
