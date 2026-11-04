#!/usr/bin/env python3
"""A stand-in for `claude -p` and `codex exec` (tests): records its arguments, answers like the real one."""
import json
import os
import sys
import time

log = os.environ.get("FAKE_AI_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps(sys.argv[1:]) + "\n")
args = sys.argv[1:]
prompt = args[args.index("-p") + 1] if "-p" in args else args[-1]
if prompt == "slow":
    time.sleep(30)
if os.path.basename(sys.argv[0]).startswith("fake_codex") or (args and args[0] == "exec"):
    tid = "th-1"
    if prompt == "fail":
        print(json.dumps({"type": "thread.started", "thread_id": tid}))
        print(json.dumps({"type": "turn.failed", "error": {"message": "model is busy"}}))
        sys.exit(1)
    print(json.dumps({"type": "thread.started", "thread_id": tid}))
    print(json.dumps({"type": "turn.started"}))
    print(json.dumps({"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": "codex: " + prompt}}))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 5}}))
    sys.exit(0)
session = args[args.index("--resume") + 1] if "--resume" in args else "sess-1"
if prompt == "fail":
    print(json.dumps({"type": "result", "subtype": "success", "is_error": True, "result": "API Error: overloaded"}))
    sys.exit(1)
text = "echo: " + prompt
if prompt == "long":
    text = "intro **bold** and `code`\n\n```python\n" + "x = 1 < 2\n" * 900 + "```\n\nthe end"
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": text, "session_id": session,
                  "total_cost_usd": 0.0123, "duration_ms": 2500,
                  "usage": {"input_tokens": 10, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 5, "output_tokens": 7}}))
