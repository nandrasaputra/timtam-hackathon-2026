# MR review

Use this when another team changed gohost or ios and this team has to approve the MR. Read the diff. Do not list every file. Name the files that change behavior this team owns, and say how many other files the diff touches. If the diff changes a shared event, flag, or API field, one sentence on whether the other project has the same logic and this MR does not touch it.

You need the MR link or iid. Ask for that one fact when it is missing. Run `mobox-glab mr`, then `mobox-glab diff`. The branch is `source_branch`. Cite only paths the diff printed.

**MR:** iid and title

**Platform:** Android or iOS, from the project

**Branch:** source_branch into target_branch

**What changed:** one or two sentences

**Risk:** high, medium, or low, and the path that makes it so

**Check:** the one thing to look at before approving

**Verdict:** approve, or hold, and the reason

Do not end with `Raise MR?` or `Raise MR fix?`. Do not open, approve, or merge the MR. The developer approves in GitLab.
