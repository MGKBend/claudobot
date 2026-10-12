import asyncio, os, sys, tempfile, time
from pathlib import Path
from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions, AssistantMessage, ResultMessage,
    TextBlock, ToolUseBlock, PermissionResultAllow,
)

WAIT = int(sys.argv[1]) if len(sys.argv) > 1 else 370
os.environ.pop("ANTHROPIC_API_KEY", None)
workdir = Path(tempfile.mkdtemp(prefix="spike-"))


async def can_use_tool(name, input_data, ctx):
    if name == "AskUserQuestion":
        print(f"[spike] question received, waiting {WAIT}s...", flush=True)
        await asyncio.sleep(WAIT)
        answers = {q["question"]: q["options"][0]["label"] for q in input_data["questions"]}
        print(f"[spike] answer sent: {answers}", flush=True)
        return PermissionResultAllow(updated_input={"questions": input_data["questions"], "answers": answers})
    return PermissionResultAllow()


async def main():
    opts = ClaudeAgentOptions(
        cwd=str(workdir),
        permission_mode="bypassPermissions",
        can_use_tool=can_use_tool,
        cli_path=str(Path.home() / ".local/bin/claude"),
        system_prompt={"type": "preset", "preset": "claude_code",
                       "append": "Always ask your questions with the AskUserQuestion tool."},
    )
    t0 = time.time()
    async with ClaudeSDKClient(options=opts) as c:
        await c.query("Ask me with AskUserQuestion which language to greet in (French/English), "
                      "then create hello.txt containing the greeting in that language.")
        async for m in c.receive_response():
            if isinstance(m, AssistantMessage):
                for b in m.content:
                    if isinstance(b, ToolUseBlock):
                        print("[tool]", b.name, flush=True)
                    elif isinstance(b, TextBlock):
                        print("[text]", b.text[:200], flush=True)
            elif isinstance(m, ResultMessage):
                print(f"[result] session={m.session_id} is_error={m.is_error} cost={m.total_cost_usd}", flush=True)
    f = workdir / "hello.txt"
    print(f"[spike] {time.time()-t0:.0f}s ; hello.txt:", f.read_text() if f.exists() else "MISSING")

asyncio.run(main())
