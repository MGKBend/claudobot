# OpenClaw — Claude Code driven from Discord

One Discord channel = one project = one Claude Code session (folder `~/projects/<name>`).
Claude runs in `bypassPermissions` mode; its questions (`AskUserQuestion`) show up in the channel with buttons.

## 1. Create the Discord bot (once)
1. https://discord.com/developers/applications → **New Application** → **Bot** tab → **Reset Token** (copy it).
2. Under **Bot > Privileged Gateway Intents**, enable **Message Content Intent**.
3. **OAuth2 > URL Generator**: scopes `bot` + `applications.commands`; permissions *Manage Channels, View Channels, Send Messages, Read Message History, Attach Files, Add Reactions*. Open the URL and invite the bot to your private server.
4. Enable developer mode (Discord > Settings > Advanced) to copy the server ID and your user ID.

## 2. Configure
```bash
cd ~/Desktop/openclaw-setup
cp .env.example .env && chmod 600 .env   # then fill in the values
```

## 3. Run
```bash
uv run python bot.py          # foreground test
```
As a service (starts at boot):
```bash
mkdir -p ~/.config/systemd/user && cp openclaw.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now openclaw
loginctl enable-linger $USER   # may need sudo; keeps the bot running without an open session
journalctl --user -u openclaw -f
```

## Commands
| Command | Effect |
|---|---|
| `/project new <name> [goal]` | creates `~/projects/<name>`, `GOALS.md` and a channel |
| `/project link <name>` | links the current channel to an existing folder |
| `/goal set` / `/goal show` | project goals (injected when they change) |
| `/plan <request>` | Claude plans first; Approve / Edit / Reject buttons |
| `/zip [with_env] [with_git]` | sends the project as a .zip in the channel (without .env, .git, node_modules, .venv by default) |
| `/usage` | 5H and 7D usage limits (also shown in the bot's status) |
| `/stop`, `/reset`, `/status` | interrupt, new session, project overview |

A regular message in a project channel is sent to Claude (⏳ if it is busy; the message is queued).

## Attachments and files
- Any attachment (`.env` included) is downloaded into the project's `attachments/` folder (600 permissions, added to `.gitignore`). Without text, it is passed to Claude with your next message; with text, everything goes together. Discord strips the leading dot of file names (`.env` becomes `env`); the bot restores it.
- Ask "send me the project as a zip": Claude uses its `send_project_zip` / `send_file` tools. `.env` files are only included if you explicitly ask for them.
- Upload limit: your Discord server's (10 MB without boost); beyond that, the bot says so.

## Security
Claude has full shell access on the host. Only the IDs in `ALLOWED_USER_IDS` on the `GUILD_ID` server are listened to (messages, commands and buttons). Keep the server private and `.env` at `chmod 600`.

## Limits
Questions pending during a bot restart are lost (the Claude session itself is resumed).

## Tests
- `uv run python spike.py 370`: checks that a question can wait longer than 5 minutes.
- `uv run python test_session.py`: drives a session with a fake channel (questions, plan, approval).
- `uv run python test_live.py`: end-to-end test on your real server (creates then deletes a test channel). Stop the service first so two bots don't share the token.
