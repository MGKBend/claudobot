import asyncio, contextlib, importlib.util, tempfile
from pathlib import Path

spec = importlib.util.spec_from_file_location("bot", Path(__file__).with_name("bot.py"))
bot = importlib.util.module_from_spec(spec); spec.loader.exec_module(bot)
tmp = Path(tempfile.mkdtemp(prefix="oc-test-"))
bot.STATE_FILE = tmp / "state.json"


class FakeChannel:
    def __init__(self): self.log = []
    async def send(self, content=None, view=None, embed=None, file=None, **kw):
        if view is not None and hasattr(view, "future"):
            self.log.append(("QUESTION", embed.description if embed else ""))
            view.future.set_result(view.children[0].label)
        elif view is not None:
            self.log.append(("VIEW", type(view).__name__, content))
        else:
            self.log.append(("MSG", (content or "")[:150]))
    @contextlib.asynccontextmanager
    async def typing(self): yield


async def wait_idle(s):
    await asyncio.sleep(1)
    while s.busy or not s.queue.empty():
        await asyncio.sleep(1)


async def main():
    proj = tmp / "proj"; proj.mkdir()
    ch = FakeChannel(); info = {"dir": str(proj)}
    s = bot.Session(ch, info)

    print("== 1. question ==")
    s.submit("Ask me with AskUserQuestion which language to greet in (French/English), then create hello.txt with the greeting.")
    await wait_idle(s)
    print("hello.txt:", (proj / "hello.txt").read_text() if (proj / "hello.txt").exists() else "MISSING")
    print("question asked:", any(e[0] == "QUESTION" for e in ch.log), "| session_id:", info.get("session_id"))

    print("== 2. plan (no write expected) ==")
    ch.log.clear()
    s.submit("Plan the creation of plan.txt containing 'ok'. Create nothing before approval.", "plan")
    await wait_idle(s)
    print("plan.txt before approval:", "PRESENT (BUG)" if (proj / "plan.txt").exists() else "absent (ok)")
    print("PlanView posted:", any(e[0] == "VIEW" and e[1] == "PlanView" for e in ch.log), "| pending_plan:", bool(s.pending_plan))

    print("== 3. approval ==")
    s.submit("The plan is approved: execute it now.", "bypassPermissions")
    await wait_idle(s)
    print("plan.txt after approval:", "present (ok)" if (proj / "plan.txt").exists() else "ABSENT (BUG)")
    await s._disconnect()
    print("== log =="); [print(" ", e) for e in ch.log]

asyncio.run(main())
