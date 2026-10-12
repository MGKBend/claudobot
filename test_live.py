import asyncio, importlib.util, shutil, sys, time, zipfile
from pathlib import Path
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location("bot", Path(__file__).with_name("bot.py"))
bot = importlib.util.module_from_spec(spec); spec.loader.exec_module(bot)

NAME = "e2e-test"
results = []


def check(label, ok, extra=""):
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + label + (f"  [{extra}]" if extra else ""), flush=True)


class FakeResponse:
    def __init__(self): self.sent = []; self.done = False
    async def send_message(self, content=None, **kw): self.done = True; self.sent.append(content)
    async def defer(self, **kw): self.done = True
    async def edit_message(self, **kw): self.done = True
    async def send_modal(self, modal): self.done = True; self.modal = modal
    def is_done(self): return self.done


class FakeFollowup:
    def __init__(self, ch): self.ch, self.sent = ch, []
    async def send(self, content=None, **kw): self.sent.append(content); return await self.ch.send(content, **kw)


def fake_inter(user_id, guild, channel):
    i = SimpleNamespace(user=SimpleNamespace(id=user_id), guild=guild, guild_id=guild.id if guild else None,
                        channel=channel, channel_id=channel.id, message=None)
    i.response = FakeResponse(); i.followup = FakeFollowup(channel)
    return i


async def wait_idle(s, timeout=180):
    t0 = time.time(); await asyncio.sleep(2)
    while (s.busy or not s.queue.empty()) and time.time() - t0 < timeout:
        for v in list(s.questions):
            if not v.future.done():
                await asyncio.sleep(1)
                await v.children[0].callback(fake_inter(bot.ALLOWED.copy().pop(), s.channel.guild, s.channel))
        await asyncio.sleep(1)


async def run(guild, general):
    me = next(iter(bot.ALLOWED))
    path = bot.ROOT / NAME
    fi = fake_inter(me, guild, general)
    await bot.project_new.callback(fi, NAME, "create a small hello script")
    ch = discord_get(guild, NAME)
    check("/project new: channel created", ch is not None)
    check("/project new: folder + git + GOALS.md", (path / ".git").exists() and (path / "GOALS.md").exists())
    check("/project new: persistent state", str(ch.id) in bot.state and bot.state[str(ch.id)]["dir"] == str(path))
    check("/project new: invalid name rejected", await refused(bot.project_new.callback, guild, general, me, "Bad Name!"))
    ci = lambda uid=me: fake_inter(uid, guild, ch)

    i = ci(); await bot.goal_show.callback(i); check("/goal show", "Goals" in (i.response.sent[0] or ""))
    i = ci(); await bot.goal_set.callback(i, "Always answer in one sentence."); check("/goal set", "one sentence" in (path / "GOALS.md").read_text())

    s = bot.session_for(ch)
    msg = SimpleNamespace(author=SimpleNamespace(bot=False, id=me), guild=guild, channel=ch,
                          content="Ask me with AskUserQuestion which language to greet in (French/English), then create hello.txt with the greeting.",
                          add_reaction=lambda e: asyncio.sleep(0))
    await bot.on_message(msg)
    await wait_idle(s)
    check("free message -> Claude: Discord question asked and answered", (path / "hello.txt").exists(),
          (path / "hello.txt").read_text().strip() if (path / "hello.txt").exists() else "")
    check("session_id saved (resumable with `claude --resume`)", bool(bot.state[str(ch.id)].get("session_id")))
    sid = bot.state[str(ch.id)]["session_id"]
    check("Claude Code history present on this PC",
          any((Path.home() / ".claude/projects").glob(f"*{NAME}*/{sid}.jsonl")))
    init = s.init
    cmds = init.get("slash_commands", [])
    check("session = real local Claude Code (version, plugins, skills)",
          bool(init.get("claude_code_version")) and any(":" in c for c in cmds) and len(cmds) > 10,
          f"v{init.get('claude_code_version')} {len(cmds)} cmds/skills, apiKeySource={init.get('apiKeySource')}")
    check("session cwd = project folder", init.get("cwd") == str(path))

    check("5H and 7D limits measured", "five_hour" in bot.usage and "seven_day" in bot.usage, bot.usage_line())
    i = ci(); await bot.usage_cmd.callback(i); out = i.response.sent[0] or ""
    check("/usage shows 5H and 7D", "5H" in out and "7D" in out)
    saved = dict(bot.usage)
    bot.usage["five_hour"] = {"utilization": 1.0, "resetsAt": int(time.time()) + 3600}
    check("blocked when the 5H limit is reached", "5H" in (bot.usage_block() or ""))
    bot.usage.clear(); bot.usage.update(saved)
    check("not blocked otherwise", bot.usage_block() is None)

    i = ci(); await bot.status_cmd.callback(i); out = i.response.sent[0] or ""
    check("/status: project, version, plugins", NAME in out and "Claude Code" in out and "plugins" in out)

    s.submit("Just answer: one"); s.submit("Just answer: two")
    await asyncio.sleep(1); check("queue (busy)", s.busy or not s.queue.empty())
    await wait_idle(s)

    views = []; orig = bot.PlanView.__init__
    def spy(self, *a, **k): orig(self, *a, **k); views.append(self)
    bot.PlanView.__init__ = spy
    i = ci(); await bot.plan_cmd.callback(i, "Plan the creation of plan.txt containing 'ok'. Create nothing before approval.")
    await wait_idle(s)
    check("/plan: no file before approval", not (path / "plan.txt").exists())
    check("/plan: plan posted with buttons", len(views) == 1)
    if views:
        await views[0].approve.callback(ci()); await wait_idle(s)
    check("/plan: Approve executes the plan", (path / "plan.txt").exists())

    s.submit("Run the bash command `sleep 120` then say done.")
    await asyncio.sleep(12)
    t0 = time.time(); i = ci(); await bot.stop_cmd.callback(i); await wait_idle(s, 60)
    check("/stop interrupts the task", not s.busy and time.time() - t0 < 50, f"{time.time()-t0:.0f}s")

    i = ci(); await bot.reset_cmd.callback(i)
    check("/reset forgets the session", "session_id" not in bot.state[str(ch.id)])

    class FA:
        def __init__(self, fn, data): self.filename, self.size, self.data = fn, len(data), data
        async def save(self, target): Path(target).write_bytes(self.data)
    amsg = SimpleNamespace(author=SimpleNamespace(bot=False, id=me), guild=guild, channel=ch, content="",
                           attachments=[FA(".env", b"SECRET=1\n"), FA("notes.txt", b"bonjour-pj")],
                           add_reaction=lambda e: asyncio.sleep(0))
    await bot.on_message(amsg)
    a_env = path / "attachments" / ".env"
    check(".env attachment downloaded", a_env.exists() and a_env.read_text() == "SECRET=1\n")
    check("attachments have 600 permissions", oct(a_env.stat().st_mode & 0o777) == "0o600")
    check("attachments/ ignored by git", "attachments/" in (path / ".gitignore").read_text())
    check("files queued for Claude", len(s.files) == 2)
    s.submit("Read attachments/notes.txt and answer only with its exact content.")
    await wait_idle(s)
    check("Claude receives the file list", s.files == [])

    (path / "node_modules").mkdir(exist_ok=True); (path / "node_modules" / "x.js").write_text("x"); (path / ".env").write_text("A=1")
    zp = bot.build_zip(str(path)); names = zipfile.ZipFile(zp).namelist(); zp.unlink()
    check("zip: without .env / .git / node_modules", ".env" not in names and "GOALS.md" in names
          and not any(n.startswith(("node_modules", ".git/")) for n in names), f"{len(names)} fichiers")
    zp = bot.build_zip(str(path), include_env=True); names = zipfile.ZipFile(zp).namelist(); zp.unlink()
    check("zip: .env included on request", ".env" in names)
    async def zips():
        return sum(1 for m in [m async for m in ch.history(limit=30)] for a in m.attachments if a.filename == f"{NAME}.zip")
    n0 = await zips()
    i = ci(); await bot.zip_cmd.callback(i)
    check("/zip: .zip posted on Discord", await zips() == n0 + 1)
    s.submit("Send me the project as a zip.")
    await wait_idle(s)
    check("natural-language request -> Claude sends the zip (MCP tool)", await zips() == n0 + 2)

    other = bot.ROOT / "e2e-link"; other.mkdir(exist_ok=True)
    i = ci(); await bot.project_link.callback(i, "e2e-link")
    check("/project link", bot.state[str(ch.id)]["dir"] == str(other.resolve()))
    i = ci(); await bot.project_link.callback(i, "../../etc")
    check("/project link: path escape rejected", bot.state[str(ch.id)]["dir"] == str(other.resolve()))
    shutil.rmtree(other, ignore_errors=True)

    stranger = 111111111111111111
    check("tree: unknown user rejected", not await bot.tree.interaction_check(fake_inter(stranger, guild, ch)))
    check("tree: other server rejected", not await bot.tree.interaction_check(fake_inter(me, SimpleNamespace(id=42), ch)))
    check("tree: authorized user accepted", await bot.tree.interaction_check(fake_inter(me, guild, ch)))
    s2 = bot.session_for(ch); before = s2.queue.qsize()
    bad = SimpleNamespace(author=SimpleNamespace(bot=False, id=stranger), guild=guild, channel=ch, content="rm -rf ~",
                          add_reaction=lambda e: asyncio.sleep(0))
    await bot.on_message(bad); await asyncio.sleep(1)
    check("message from an unknown user ignored", s2.queue.qsize() == before and not s2.busy)
    v = bot.AuthView(); check("buttons: unknown user rejected", not await v.interaction_check(fake_inter(stranger, guild, ch)))

    await s2.stop(); await s2._disconnect(); s2.worker.cancel()
    await ch.delete()
    bot.state.pop(str(ch.id), None); bot.save_state()
    shutil.rmtree(path, ignore_errors=True)


async def refused(cb, guild, general, me, name):
    i = fake_inter(me, guild, general); await cb(i, name)
    return "Invalid" in (i.response.sent[0] or "")


def discord_get(guild, name):
    return next((c for c in guild.text_channels if c.name == name), None)


async def main():
    task = asyncio.create_task(bot.bot.start(bot.TOKEN))
    while not (bot.bot.is_ready() and getattr(bot.bot, '_started', False)):
        await asyncio.sleep(0.5)
    guild = bot.bot.get_guild(bot.GUILD_ID)
    general = guild.get_channel(1559291827746640022) or guild.text_channels[0]
    try:
        await run(guild, general)
    finally:
        await bot.bot.close(); task.cancel()
    print(f"\n{sum(results)}/{len(results)} OK")
    sys.exit(0 if all(results) else 1)

asyncio.run(main())
