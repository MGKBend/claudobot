import asyncio, json, io, os, re, shutil, subprocess, logging, time, tempfile, zipfile
from pathlib import Path

import discord
from discord import app_commands
from dotenv import load_dotenv
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions, AssistantMessage, ResultMessage,
    TextBlock, ToolUseBlock, PermissionResultAllow, PermissionResultDeny,
    SystemMessage, RateLimitEvent, create_sdk_mcp_server, tool,
)

load_dotenv()
os.environ.pop("ANTHROPIC_API_KEY", None)

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
ALLOWED = {int(x) for x in os.environ["ALLOWED_USER_IDS"].replace(" ", "").split(",") if x}
ROOT = Path(os.getenv("PROJECTS_ROOT", "~/projects")).expanduser()
CLI = shutil.which("claude") or str(Path.home() / ".local/bin/claude")
STATE_FILE = Path(__file__).with_name("state.json")
CATEGORY = "Claude Projects"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,50}$")
NO_PING = discord.AllowedMentions.none()
log = logging.getLogger("openclaw")

APPEND_PROMPT = (
    "You are being driven remotely from a Discord channel. Ask the user every question with the "
    "AskUserQuestion tool (never as free text) and reply concisely, in the language the user writes in. "
    "The project's GOALS.md file, if it exists, describes the goals to pursue. "
    "Files the user attaches on Discord are saved in the project's attachments/ folder. "
    "To send the user the project as a .zip, use the mcp__discord__send_project_zip tool; for a single file, "
    "use mcp__discord__send_file. Only include .env files (include_env) if the user explicitly asks for them."
)


def authorized(user_id: int, guild_id: int | None) -> bool:
    return guild_id == GUILD_ID and user_id in ALLOWED


state: dict[str, dict] = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def save_state():
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


USAGE_FILE = Path(__file__).with_name("usage.json")
usage: dict[str, dict] = json.loads(USAGE_FILE.read_text()) if USAGE_FILE.exists() else {}
WINDOWS = {"five_hour": "5H", "seven_day": "7D"}


def update_usage(info) -> None:
    wins = (info.raw or {}).get("unifiedWindows") or {}
    for key in WINDOWS:
        w = wins.get(key)
        if w and w.get("utilization") is not None:
            usage[key] = {"utilization": w["utilization"], "resetsAt": w.get("resetsAt")}
    if info.status != "allowed" or "status" not in usage:
        usage["status"] = info.status
    else:
        usage["status"] = "allowed"
    usage["updated"] = int(time.time())
    USAGE_FILE.write_text(json.dumps(usage))


def usage_line() -> str:
    parts = [f"{label} {usage[k]['utilization']*100:.0f}%" for k, label in WINDOWS.items() if k in usage]
    return " · ".join(parts)


def usage_block() -> str | None:
    now = time.time()
    for k, label in WINDOWS.items():
        w = usage.get(k)
        if w and w["utilization"] >= 1.0 and (w.get("resetsAt") or 0) > now:
            return f"⛔ {label} limit reached — resumes <t:{int(w['resetsAt'])}:R> (<t:{int(w['resetsAt'])}:t>)."
    return None


def bar(frac: float, width: int = 16) -> str:
    n = max(0, min(width, round(frac * width)))
    return "█" * n + "░" * (width - n)


async def refresh_presence() -> None:
    if not bot.is_ready():
        return
    if usage_block():
        status = discord.Status.dnd
    elif usage.get("status") == "allowed_warning":
        status = discord.Status.idle
    else:
        status = discord.Status.online
    text = usage_line() or "Waiting for measurement"
    try:
        await bot.change_presence(status=status, activity=discord.CustomActivity(name=text))
    except Exception:
        log.exception("presence")


def chunks(text: str, size: int = 1900):
    while text:
        if len(text) <= size:
            yield text; return
        cut = text.rfind("\n", 0, size)
        cut = cut if cut > size // 2 else size
        yield text[:cut]; text = text[cut:].lstrip("\n")


async def send_long(channel, text: str):
    text = text.strip()
    if not text:
        return
    if len(text) > 8000:
        await channel.send("📎 Long reply attached.", allowed_mentions=NO_PING,
                           file=discord.File(io.BytesIO(text.encode()), "reply.md"))
        return
    for part in chunks(text):
        await channel.send(part, allowed_mentions=NO_PING)


MAX_ATTACH = 200 * 1024 * 1024
ZIP_SKIP_DIRS = {"node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache", ".cache"}


def ensure_gitignored(project_dir: Path, entry: str) -> None:
    gi = project_dir / ".gitignore"
    lines = gi.read_text().splitlines() if gi.exists() else []
    if entry not in lines:
        gi.write_text("\n".join(lines + [entry]) + "\n")


DOTFILES = {"gitignore", "gitattributes", "dockerignore", "npmrc", "editorconfig", "eslintrc", "prettierrc",
            "bashrc", "zshrc", "profile", "envrc"}


def restore_dotfile_name(name: str) -> str:
    if re.fullmatch(r"env(\.[A-Za-z0-9_-]+)?", name) or name in DOTFILES:
        return "." + name
    return name


async def save_attachments(msg, project_dir: str) -> tuple[list[str], list[str]]:
    root = Path(project_dir)
    dest = root / "attachments"
    saved, refused = [], []
    for a in getattr(msg, "attachments", []):
        if a.size > MAX_ATTACH:
            refused.append(f"{a.filename} ({a.size / 1e6:.0f} MB)")
            continue
        dest.mkdir(exist_ok=True)
        ensure_gitignored(root, "attachments/")
        name = restore_dotfile_name(Path(a.filename).name.replace("\x00", "") or "file")
        target = dest / name
        if target.exists():
            target = dest / f"{int(time.time())}-{name}"
        await a.save(target)
        target.chmod(0o600)
        saved.append(str(target.relative_to(root)))
    return saved, refused


def build_zip(project_dir: str, include_env: bool = False, include_git: bool = False) -> Path:
    root = Path(project_dir)
    fd, tmp = tempfile.mkstemp(suffix=".zip", prefix="openclaw-")
    os.close(fd)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in ZIP_SKIP_DIRS and (include_git or d != ".git")]
            for f in filenames:
                full = Path(dirpath) / f
                if full.is_symlink() or not full.is_file():
                    continue
                if not include_env and (f == ".env" or f.startswith(".env.")):
                    continue
                z.write(full, full.relative_to(root))
    return Path(tmp)


def upload_limit(channel) -> int:
    return getattr(getattr(channel, "guild", None), "filesize_limit", None) or 10 * 1024 * 1024


async def send_file_checked(channel, path: Path, filename: str) -> str:
    size = path.stat().st_size
    limit = upload_limit(channel)
    if size > limit:
        return f"File too large for Discord ({size / 1e6:.1f} MB > {limit / 1e6:.0f} MB)."
    await channel.send(file=discord.File(path, filename=filename))
    return f"Sent: {filename} ({size / 1e6:.2f} MB)."


async def send_project_zip(channel, project_dir: str, include_env: bool = False, include_git: bool = False) -> str:
    zp = await asyncio.to_thread(build_zip, project_dir, include_env, include_git)
    try:
        return await send_file_checked(channel, zp, f"{Path(project_dir).name}.zip")
    finally:
        zp.unlink(missing_ok=True)


class AuthView(discord.ui.View):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        ok = authorized(interaction.user.id, interaction.guild_id)
        if not ok:
            await interaction.response.send_message("⛔ Not authorized.", ephemeral=True)
        return ok


class TextModal(discord.ui.Modal):
    def __init__(self, title: str, on_submit, long: bool = True):
        super().__init__(title=title[:45])
        self._cb = on_submit
        self.field = discord.ui.TextInput(
            label="Your answer"[:45], style=discord.TextStyle.paragraph if long else discord.TextStyle.short,
            max_length=2000)
        self.add_item(self.field)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self._cb(self.field.value)


class QuestionView(AuthView):
    def __init__(self, q: dict):
        super().__init__(timeout=None)
        self.future: asyncio.Future = asyncio.get_running_loop().create_future()
        self.multi = bool(q.get("multiSelect"))
        opts = q.get("options", [])
        if self.multi:
            sel = discord.ui.Select(
                placeholder="Pick one or more options", min_values=1, max_values=len(opts),
                options=[discord.SelectOption(label=o["label"][:100], value=o["label"][:100],
                                              description=(o.get("description") or "")[:100] or None) for o in opts])
            sel.callback = self._on_select
            self.add_item(sel)
        else:
            for o in opts:
                b = discord.ui.Button(label=o["label"][:80], style=discord.ButtonStyle.primary)
                b.callback = self._make_choice(o["label"])
                self.add_item(b)
        other = discord.ui.Button(label="Other…", style=discord.ButtonStyle.secondary)
        other.callback = self._on_other
        self.add_item(other)

    def _done(self, value, interaction_msg=None):
        if not self.future.done():
            self.future.set_result(value)
        self.stop()

    def _make_choice(self, label):
        async def cb(interaction: discord.Interaction):
            await self._finish(interaction, label)
        return cb

    async def _on_select(self, interaction: discord.Interaction):
        await self._finish(interaction, list(interaction.data["values"]))

    async def _on_other(self, interaction: discord.Interaction):
        message = interaction.message

        async def got(text):
            for item in self.children:
                item.disabled = True
            await message.edit(view=self)
            await message.channel.send(f"✅ {text[:1500]}", allowed_mentions=NO_PING)
            self._done(text)
        await interaction.response.send_modal(TextModal("Other answer", got))

    async def _finish(self, interaction: discord.Interaction, value):
        for item in self.children:
            item.disabled = True
        shown = ", ".join(value) if isinstance(value, list) else value
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(f"✅ {shown}", allowed_mentions=NO_PING)
        self._done(value)


class PlanView(AuthView):
    def __init__(self, session: "Session"):
        super().__init__(timeout=None)
        self.session = session

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, _):
        await interaction.response.edit_message(view=None)
        self.session.submit("The plan is approved: execute it now.", "bypassPermissions")

    @discord.ui.button(label="Edit", style=discord.ButtonStyle.secondary)
    async def amend(self, interaction: discord.Interaction, _):
        async def got(text):
            self.session.submit(f"Revise the plan with these remarks: {text}", "plan")
        await interaction.response.send_modal(TextModal("Remarks on the plan", got))

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, _):
        await interaction.response.edit_message(view=None)
        await interaction.followup.send("🗑️ Plan rejected.")


class Session:
    def __init__(self, channel: discord.abc.Messageable, info: dict):
        self.channel, self.info = channel, info
        self.queue: asyncio.Queue = asyncio.Queue()
        self.client: ClaudeSDKClient | None = None
        self.busy = False
        self.pending_plan: str | None = None
        self.questions: list[QuestionView] = []
        self.goals_mtime = 0.0
        self.init: dict = {}
        self.files: list[str] = []
        self.worker = asyncio.create_task(self._run())

    @property
    def goals_path(self) -> Path:
        return Path(self.info["dir"]) / "GOALS.md"

    def submit(self, prompt: str, mode: str = "bypassPermissions"):
        self.queue.put_nowait((prompt, mode))

    async def stop(self):
        while not self.queue.empty():
            self.queue.get_nowait()
        for v in self.questions:
            if not v.future.done():
                v.future.set_result("(question cancelled by /stop)")
        if self.client and self.busy:
            await self.client.interrupt()

    async def reset(self):
        await self.stop()
        await self._disconnect()
        self.info.pop("session_id", None)
        save_state()

    async def _disconnect(self):
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                log.exception("disconnect")
            self.client = None

    def _discord_tools(self):
        @tool("send_project_zip",
              "Send the current project to the user on Discord as a .zip (excludes .env, .git, node_modules, .venv by default).",
              {"include_env": bool, "include_git": bool})
        async def t_zip(args):
            text = await send_project_zip(self.channel, self.info["dir"], bool(args.get("include_env")),
                                          bool(args.get("include_git")))
            return {"content": [{"type": "text", "text": text}]}

        @tool("send_file", "Send a project file to the user on Discord.", {"path": str})
        async def t_file(args):
            root = Path(self.info["dir"]).resolve()
            target = (root / args["path"]).resolve()
            if root not in target.parents or not target.is_file():
                return {"content": [{"type": "text", "text": "File not found in the project."}], "is_error": True}
            return {"content": [{"type": "text", "text": await send_file_checked(self.channel, target, target.name)}]}

        return create_sdk_mcp_server("discord", tools=[t_zip, t_file])

    async def _connect(self):
        opts = ClaudeAgentOptions(
            mcp_servers={"discord": self._discord_tools()},
            cwd=self.info["dir"], permission_mode="bypassPermissions", can_use_tool=self.can_use_tool,
            cli_path=CLI, resume=self.info.get("session_id"),
            system_prompt={"type": "preset", "preset": "claude_code", "append": APPEND_PROMPT},
        )
        self.client = ClaudeSDKClient(options=opts)
        await self.client.connect()

    async def can_use_tool(self, name, input_data, ctx):
        if name == "AskUserQuestion":
            answers = {}
            for q in input_data.get("questions", []):
                emb = discord.Embed(title=q.get("header", "Question")[:256], description=q["question"][:4000],
                                    color=0x5865F2)
                for o in q.get("options", []):
                    if o.get("description"):
                        emb.add_field(name=o["label"][:256], value=o["description"][:1024], inline=False)
                view = QuestionView(q)
                self.questions.append(view)
                await self.channel.send(embed=emb, view=view)
                try:
                    answers[q["question"]] = await view.future
                finally:
                    self.questions.remove(view)
            return PermissionResultAllow(updated_input={"questions": input_data["questions"], "answers": answers})
        if name == "ExitPlanMode":
            self.pending_plan = input_data.get("plan", "")
            return PermissionResultDeny(
                message="The plan has been sent to the user on Discord. End your turn without executing anything.",
                interrupt=True)
        return PermissionResultAllow()

    async def _run(self):
        while True:
            prompt, mode = await self.queue.get()
            self.busy = True
            try:
                await self._turn(prompt, mode)
            except Exception as e:
                log.exception("turn")
                await self._disconnect()
                await self.channel.send(f"❌ Error: `{type(e).__name__}: {str(e)[:500]}`")
            finally:
                self.busy = False

    async def _turn(self, prompt: str, mode: str):
        if blocked := usage_block():
            await self.channel.send(blocked)
            return
        if self.client is None:
            await self._connect()
        await self.client.set_permission_mode(mode)
        if self.goals_path.exists() and self.goals_path.stat().st_mtime != self.goals_mtime:
            self.goals_mtime = self.goals_path.stat().st_mtime
            prompt = f"[Project goals — GOALS.md]\n{self.goals_path.read_text()}\n\n[Request]\n{prompt}"
        if self.files:
            listing = "\n".join(f"- {f}" for f in self.files)
            prompt = f"[Files received from the user, paths relative to the project]\n{listing}\n\n{prompt}"
            self.files.clear()
        self.pending_plan = None
        last_text, tools = "", []

        async def flush_tools():
            if tools:
                await self.channel.send("\n".join(tools)[:1900], allowed_mentions=NO_PING)
                tools.clear()

        await self.client.query(prompt)
        async for m in self.client.receive_response():
            if isinstance(m, AssistantMessage):
                for b in m.content:
                    if isinstance(b, ToolUseBlock):
                        if b.name not in ("AskUserQuestion", "ExitPlanMode"):
                            arg = next(iter(b.input.values()), "") if b.input else ""
                            tools.append(f"🔧 `{b.name}` {str(arg)[:100]}".replace("\n", " "))
                    elif isinstance(b, TextBlock) and b.text.strip():
                        await flush_tools()
                        last_text = b.text
                        await send_long(self.channel, b.text)
            elif isinstance(m, SystemMessage) and m.subtype == "init":
                self.init = m.data
            elif isinstance(m, RateLimitEvent):
                update_usage(m.rate_limit_info)
                await refresh_presence()
                if m.rate_limit_info.status == "rejected":
                    await self.channel.send(usage_block() or "⛔ Usage limit reached.")
                elif m.rate_limit_info.status == "allowed_warning":
                    await self.channel.send(f"⚠️ Close to the usage limit ({usage_line()}).")
            elif isinstance(m, ResultMessage):
                await flush_tools()
                if m.session_id:
                    self.info["session_id"] = m.session_id
                    save_state()
                secs = (m.duration_ms or 0) / 1000
                cost = f" · ${m.total_cost_usd:.3f}" if m.total_cost_usd else ""
                await self.channel.send(f"-# ✔ done in {secs:.0f}s{cost}")
        if self.pending_plan or mode == "plan":
            plan = self.pending_plan or last_text
            if self.pending_plan:
                await send_long(self.channel, f"📋 **Proposed plan**\n{plan}")
            await self.channel.send("What should we do with this plan?", view=PlanView(self))


intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)


class AuthTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        ok = authorized(interaction.user.id, interaction.guild_id)
        if not ok:
            await interaction.response.send_message("⛔ Not authorized.", ephemeral=True)
        return ok


tree = AuthTree(bot)
sessions: dict[int, Session] = {}
GUILD = discord.Object(id=GUILD_ID)


def session_for(channel) -> Session | None:
    info = state.get(str(channel.id))
    if not info:
        return None
    if channel.id not in sessions:
        sessions[channel.id] = Session(channel, info)
    return sessions[channel.id]


def init_project_dir(path: Path, goal: str | None):
    path.mkdir(parents=True, exist_ok=True)
    if not (path / ".git").exists():
        subprocess.run(["git", "init", "-q"], cwd=path, check=False)
    if goal:
        (path / "GOALS.md").write_text(f"# Goals\n\n{goal}\n")


project = app_commands.Group(name="project", description="Manage Claude Code projects")


@project.command(name="new", description="Create a project (folder + channel)")
@app_commands.describe(name="lowercase letters, digits, - and _", goal="Initial goal (GOALS.md)")
async def project_new(interaction: discord.Interaction, name: str, goal: str | None = None):
    if not NAME_RE.match(name):
        return await interaction.response.send_message("Invalid name (a-z, 0-9, -, _).", ephemeral=True)
    path = ROOT / name
    if any(v["dir"] == str(path) for v in state.values()):
        return await interaction.response.send_message("This project already exists.", ephemeral=True)
    await interaction.response.defer()
    init_project_dir(path, goal)
    guild = interaction.guild
    cat = discord.utils.get(guild.categories, name=CATEGORY) or await guild.create_category(CATEGORY)
    ch = await guild.create_text_channel(name, category=cat, topic=f"Claude Code project: {path}")
    state[str(ch.id)] = {"dir": str(path)}
    save_state()
    await ch.send(f"🚀 Project **{name}** ready (`{path}`). Write here to talk to Claude.")
    await interaction.followup.send(f"Channel created: {ch.mention}")


@project.command(name="link", description="Link this channel to a folder under PROJECTS_ROOT")
async def project_link(interaction: discord.Interaction, name: str):
    path = (ROOT / name).resolve()
    if ROOT.resolve() not in path.parents or not path.is_dir():
        return await interaction.response.send_message("Folder not found under PROJECTS_ROOT.", ephemeral=True)
    old = sessions.pop(interaction.channel_id, None)
    if old:
        await old.stop()
        await old._disconnect()
        old.worker.cancel()
    state[str(interaction.channel_id)] = {"dir": str(path)}
    save_state()
    await interaction.response.send_message(f"🔗 Channel linked to `{path}`.")


goal = app_commands.Group(name="goal", description="Project goals (GOALS.md)")


@goal.command(name="set", description="Replace GOALS.md")
async def goal_set(interaction: discord.Interaction, text: str):
    s = session_for(interaction.channel)
    if not s:
        return await interaction.response.send_message("Channel not linked to a project.", ephemeral=True)
    s.goals_path.write_text(f"# Goals\n\n{text}\n")
    await interaction.response.send_message("🎯 Goals updated (injected with the next message).")


@goal.command(name="show", description="Show GOALS.md")
async def goal_show(interaction: discord.Interaction):
    s = session_for(interaction.channel)
    if not s or not s.goals_path.exists():
        return await interaction.response.send_message("No GOALS.md.", ephemeral=True)
    await interaction.response.send_message(s.goals_path.read_text()[:1900])


@tree.command(name="plan", description="Claude proposes a plan (plan mode) before executing")
async def plan_cmd(interaction: discord.Interaction, request: str):
    s = session_for(interaction.channel)
    if not s:
        return await interaction.response.send_message("Channel not linked to a project.", ephemeral=True)
    await interaction.response.send_message(f"📝 Planning: {request[:1500]}")
    s.submit(request, "plan")


@tree.command(name="zip", description="Send the project as a .zip in the channel")
@app_commands.describe(with_env="Include .env files", with_git="Include the .git folder")
async def zip_cmd(interaction: discord.Interaction, with_env: bool = False, with_git: bool = False):
    s = session_for(interaction.channel)
    if not s:
        return await interaction.response.send_message("Channel not linked to a project.", ephemeral=True)
    await interaction.response.defer()
    text = await send_project_zip(interaction.channel, s.info["dir"], with_env, with_git)
    await interaction.followup.send(f"📦 {text}")


@tree.command(name="stop", description="Interrupt Claude")
async def stop_cmd(interaction: discord.Interaction):
    s = session_for(interaction.channel)
    if not s:
        return await interaction.response.send_message("Channel not linked to a project.", ephemeral=True)
    await interaction.response.send_message("🛑 Interruption requested.")
    await s.stop()


@tree.command(name="reset", description="New session (forget the context)")
async def reset_cmd(interaction: discord.Interaction):
    s = session_for(interaction.channel)
    if not s:
        return await interaction.response.send_message("Channel not linked to a project.", ephemeral=True)
    await interaction.response.send_message("♻️ Session reset.")
    await s.reset()


@tree.command(name="status", description="Projects and Claude Code session (model, skills, plugins)")
async def status_cmd(interaction: discord.Interaction):
    lines = []
    for cid, info in state.items():
        s = sessions.get(int(cid))
        line = f"<#{cid}> `{info['dir']}` — {'⚙️ busy' if s and s.busy else '💤 idle'}"
        if s and s.init:
            i = s.init
            cmds = i.get("slash_commands", [])
            plugins = sorted({c.split(":")[0] for c in cmds if ":" in c})
            line += (f"\n-# Claude Code {i.get('claude_code_version')} · {i.get('model')} · {len(i.get('tools', []))} tools · "
                     f"{len(cmds)} commands/skills · plugins: {', '.join(plugins) or '—'} · "
                     f"MCP: {', '.join(m['name'] for m in i.get('mcp_servers', [])) or '—'} · session `{(info.get('session_id') or '')[:8]}`")
        lines.append(line)
    await interaction.response.send_message("\n".join(lines)[:1900] or "No projects.", allowed_mentions=NO_PING)


@tree.command(name="usage", description="Subscription usage limits (5H and 7D)")
async def usage_cmd(interaction: discord.Interaction):
    if not any(k in usage for k in WINDOWS):
        return await interaction.response.send_message("No measurement yet: send Claude a message first.", ephemeral=True)
    lines = []
    for k, label in WINDOWS.items():
        w = usage.get(k)
        if w:
            reset = f" — reset <t:{int(w['resetsAt'])}:R>" if w.get("resetsAt") else ""
            lines.append(f"**{label}** `{bar(w['utilization'])}` {w['utilization']*100:.0f}%{reset}")
    lines.append(f"-# measured <t:{usage.get('updated', 0)}:R> · status: {usage.get('status', '?')}")
    await interaction.response.send_message("\n".join(lines))


tree.add_command(project, guild=GUILD)
tree.add_command(goal, guild=GUILD)
for cmd in (plan_cmd, zip_cmd, stop_cmd, reset_cmd, status_cmd, usage_cmd):
    tree.remove_command(cmd.name)
    tree.add_command(cmd, guild=GUILD)


@bot.event
async def on_ready():
    if getattr(bot, "_started", False):
        return
    bot._started = True
    await refresh_presence()
    await tree.sync(guild=GUILD)
    for cid in list(state):
        ch = bot.get_channel(int(cid))
        if ch:
            session_for(ch)
            await ch.send("🟢 OpenClaw is online (questions pending before the restart are lost).")
    log.info("ready: %s", bot.user)


@bot.event
async def on_message(msg: discord.Message):
    if msg.author.bot or not authorized(msg.author.id, msg.guild.id if msg.guild else None):
        return
    s = session_for(msg.channel)
    if not s:
        return
    text = msg.content.strip()
    if getattr(msg, "attachments", None):
        saved, refused = await save_attachments(msg, s.info["dir"])
        s.files.extend(saved)
        note = []
        if saved:
            note.append("📥 Saved: " + ", ".join(f"`{f}`" for f in saved))
        if refused:
            note.append("⚠️ Too large, ignored: " + ", ".join(refused))
        if not text:
            note.append("They will be passed to Claude with your next message.")
        if note:
            await msg.channel.send("\n".join(note), allowed_mentions=NO_PING)
    if not text:
        return
    if s.busy or not s.queue.empty():
        await msg.add_reaction("⏳")
    s.submit(text)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ROOT.mkdir(parents=True, exist_ok=True)
    bot.run(TOKEN, log_handler=None)
