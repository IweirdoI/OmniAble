"""Manual UI fixture: real API/LangGraph, synthetic model, isolated memory/hash.

Run from repository root; pair Vite with VITE_API_TARGET=http://127.0.0.1:18088.
Messages containing 失败 read a missing file; 慢速 emit cancellable slow tokens;
读取 reads a synthetic guide. This fixture never calls external model services.
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ["OS_AGENT_MEMORY_BACKEND"] = "memory"
os.environ["OS_AGENT_EMBEDDING_BACKEND"] = "hash"
(ROOT / "tmp").mkdir(exist_ok=True)
out = Path(tempfile.mkdtemp(prefix="workflow-ui-", dir=ROOT / "tmp"))
os.environ["AGENT_EXEC_CWD"] = str(out)
from src.common.config import load_default_config

cfg = load_default_config()
cfg["storage"]["staging_sqlite"] = str(out / "staging.db")
cfg["storage"]["staging_auto_purge_interval_seconds"] = 0
cfg["preference"]["storage_dir"] = str(out / "preferences")
cfg["preference"].update(use_llm=False, allow_rules=True)
cfg["semantic_detection"] = {"enabled": False}
cfg["index_path"] = str(out / "security.json")
(out / "guide.txt").write_text("虚构课程资料：先阅读，再整理，最后核验。", encoding="utf-8")
import api.server as server


class SyntheticModel:
    tools_enabled = True
    timeout = 5

    def complete_stream(self, messages, tools=None, timeout=None):
        text = next(m["content"] for m in reversed(messages) if m["role"] == "user")
        if "慢速" in text:
            for _ in range(40):
                time.sleep(.15)
                yield {"kind": "token", "text": "模拟生成中。"}
            answer = "慢速替身回答完成。"
        elif messages[-1]["role"] != "tool" and ("读取" in text or "失败" in text):
            path = "missing.txt" if "失败" in text else "guide.txt"
            yield {"kind": "message", "message": {"tool_calls": [{"id": "read-guide", "type": "function", "function": {
                "name": "read_file", "arguments": json.dumps({"path": path})}}]}}
            return
        else:
            answer = "已完成虚构资料读取；这是替身模型的接线验证。"
        yield {"kind": "token", "text": "<think>中间文本不应进入最终答案</think>"}
        yield {"kind": "message", "message": {"content": answer}}


server.ChatModel = SyntheticModel
# MemoryAgent constructs ChatModel in its own module.
import src.agent.service as service
service.ChatModel = SyntheticModel
if __name__ == "__main__":
    import uvicorn
    print(f"FIXTURE_DIR={out}", flush=True)
    uvicorn.run(server.app, host="127.0.0.1", port=18088, log_level="warning")
