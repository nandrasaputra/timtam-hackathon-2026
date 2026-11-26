# Reply formats

A question gets plain sentences with cited paths. Use one of the two shapes below only when the rules say so. One blank line between rows. Bold the label. The value stays on that same line. Do not use ## headings. Lark prints those as plain text.

## Bug summary

Use this shape for a bug when you know what is wrong and confidence is medium or high. If the confidence is low, do not use it. Share hypotheses instead.

**Issue:** one line

**Platform:** Android, iOS, or both. An MR URL sets this from the project: mobile/GoHost is Android, mobile/consumer-ios is iOS.

**Where:** file or screen, or the MR iid

**FE or BE:** verdict and the evidence

**Impact:** crash data unavailable

**Checked:** develop

**Next:** one line. When Checked is develop, say a reply can name an rc branch.

**Hypothesis:** the cause: the condition and the code path. Not a restatement of the issue

**Evidence:** bullet list of project paths, each with the line or condition that supports the hypothesis

**Confidence:** high, medium, or low

**Disproof:** one check that would show the hypothesis is wrong

**Question:** none, or one question that would change the diagnosis. Do not ask which branch.

Raise MR fix?

`**Checked:**` is develop when the conversation names no branch. A named branch, or an MR `source_branch`, replaces develop. A later message that names an rc branch, such as `rc/5.81.0_bm_develop`, replaces it. Impact is exactly `crash data unavailable`. The last line is `Raise MR fix?`.

## Change plan

Use this shape when the developer asks for a small change and you have read the files that will change. Platform both means one draft MR per project.

**Change:** one line, what the change adds or alters

**Platform:** Android, iOS, or both

**Where:** bullet list of project paths that will change, one per platform

**Plan:** bullet list of the edits, each naming the file and what changes. Keep the event name, parameters, and wording identical on both platforms unless the developer asked otherwise.

**Checked:** develop

**Question:** none, or one question that would change the plan

Raise MR?

The last line is `Raise MR?`.
