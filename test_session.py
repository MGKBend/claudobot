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
    s.submit("Demande-moi avec AskUserQuestion (francais/anglais) la langue, puis cree hello.txt avec la salutation.")
    await wait_idle(s)
    print("hello.txt:", (proj / "hello.txt").read_text() if (proj / "hello.txt").exists() else "ABSENT")
    print("question posee:", any(e[0] == "QUESTION" for e in ch.log), "| session_id:", info.get("session_id"))

    print("== 2. plan (aucune ecriture attendue) ==")
    ch.log.clear()
    s.submit("Planifie la creation de plan.txt contenant 'ok'. Ne cree rien avant approbation.", "plan")
    await wait_idle(s)
    print("plan.txt avant approbation:", "PRESENT (BUG)" if (proj / "plan.txt").exists() else "absent (ok)")
    print("PlanView poste:", any(e[0] == "VIEW" and e[1] == "PlanView" for e in ch.log), "| pending_plan:", bool(s.pending_plan))

    print("== 3. approbation ==")
    s.submit("Le plan est approuve : execute-le maintenant.", "bypassPermissions")
    await wait_idle(s)
    print("plan.txt apres approbation:", "present (ok)" if (proj / "plan.txt").exists() else "ABSENT (BUG)")
    await s._disconnect()
    print("== journal =="); [print(" ", e) for e in ch.log]

asyncio.run(main())
