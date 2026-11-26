# MoBox

MoBox is a mobile support buddy. A Lark @mention asks about the code, reports a bug, asks for a small change, or asks for a review of an MR this team must approve. The agent reads `mobile/GoHost` (Android) and `mobile/consumer-ios` (iOS). A confirmed draft MR is the only clone.

Run this from the repo root. Use `container`. `container_test` is a local copy and is gitignored. Keep each command on one line. A wrapped paste is executed as several commands.

## One-time setup

1. Start Podman and build the image. This uses Podman, not Docker.

```bash
podman machine start
podman build -t mobox-poc -f container/Containerfile container
```

If `podman machine start` prints success and the machine is then `stopped`, delete the stale `gvproxy.pid` and `*.sock` files in the Podman temp dir, start the machine again, and leave `sleep 86400` running in that same terminal. The VM stays up while that shell stays up.

2. Copy `container/.env.example` to `container/.env` and fill it in. Git ignores `.env`.

- `GITLAB_TOKEN`: `read_api` and `read_repository` for search. A draft MR also needs push and MR create on the same token.
- `MOBOX_GIT_EMAIL` and `MOBOX_GIT_NAME`: the draft MR commit author. GoHost's push rule rejects an email outside the company domains, so use the token owner's email.
- `CURSOR_API_KEY`: key for Cursor Agent. Create one under Cursor Dashboard → Integrations.
- `CURSOR_MODEL`: model id passed to the agent. Default is `grok-4.7`.
- `CURSOR_IMAGE_INPUT`: `1` sends Lark images to the agent. Set `0` for a text-only model. The bridge then asks for a text equivalent instead of a screenshot.
- `LARK_APP_ID` and `LARK_APP_SECRET`: the MoBox Lark app. Leave them empty if you only use the web page.
- `LARK_BOT_OPEN_ID`: the bot open_id. Group messages count only when this id is mentioned.
- `LARK_TAG_OPEN_IDS`: comma-separated on-call tag open_ids. A mention of one of these in a watched channel starts the same triage.
- `LARK_CHANNEL_HACK_OF_THRONES`, `LARK_CHANNEL_FCT`, `LARK_CHANNEL_CRASH`, `LARK_CHANNEL_FEEDBACK`: group chat ids. The bridge also answers a personal chat with the bot.

3. Install the Lark bridge dependency. Skip this if you only use the web page.

```bash
python3 -m venv container/.venv
container/.venv/bin/pip install 'lark-oapi==1.8.0'
```

In the Lark console, set **Events and Callbacks** to **Receive events through persistent connection**, add **Receive message** (`im.message.receive_v1`) and the card callback (`card.action.trigger`), and save while the bridge in the next section is already running. Publish the version if the console still shows a draft. The bot must be a member of Hack of Thrones. A personal chat with the bot is in scope, so the app also needs the private-message permission.

## Run

Two terminals.

Terminal A, the agent:

```bash
container/up.sh
```

`up.sh` mounts the GitLab wrapper and the assembled `AGENTS.md`, and serves Cursor Agent on `127.0.0.1:4096`. Leave it running. Ctrl-C stops the container. `--rm` deletes that container, including its chat sessions.

Terminal B, the Lark bridge:

```bash
container/.venv/bin/python container/lark_bridge.py container/.env
```

Leave it running. One status message moves from reading the thread, to searching, to writing the triage, then the card arrives.

Open `http://127.0.0.1:4096` for the Cursor Agent page. The port is bound to localhost. The Lark bridge uses the same port.

## What to send

Write it the way you would ask a teammate. Details can come across several messages in the thread.

- A question about the code gets a plain answer with the paths read. No platform named means both are checked and compared.
- A small change, such as a telemetry event on both platforms, gets a change plan ending in `Raise MR?`.
- An MR from another team that this team must approve gets an MR review: what changed, the risk, the one check before approving, and a verdict of approve or hold. MoBox does not approve or merge it.
- A bug gets a root cause analysis. Only what is wrong and what was expected are required. Works on one platform and broken on the other is compared on both. A medium or high confidence result is one list: Issue, Platform, Where, FE or BE, Impact, Checked, Next, Hypothesis, Evidence, Confidence, Disproof, and Question, ending in `Raise MR fix?`. Low confidence gets up to three hypotheses and the check that decides them.
- No branch, no MR link, and no app version: the search uses `develop`. An app version such as `5.76` searches the matching `rc/` branch. A later message can name a release branch such as `rc/5.81.0_bm_develop`. An MR link is enough. The branch is the MR `source_branch`. When a named branch is missing, MoBox suggests matching names from GitLab for that project and asks before using one.

Confirm on the card within 5 minutes. Only the requester can confirm. The host then shallow-clones, applies one patch, and opens a draft MR on `mobox/change-<message-id>` for a change plan, or `mobox/fix-<message-id>` for a bug, one per platform the card names. The bot does not merge, close, or retry a pipeline. The requester reviews, approves, and merges.

In Lark, `@` **MoBox** inside the group or thread.

## Logs

An idle agent log is only:

```text
cursor-agent listening on http://0.0.0.0:4096
```

Follow the container anyway:

```bash
podman logs -f "$(podman ps -q --filter ancestor=localhost/mobox-poc:latest)"
```

The bridge terminal is the log that shows a mention. A delivered event prints `receive message`, then `mention message_id=...`. Heartbeats alone mean Lark has not delivered a message.
