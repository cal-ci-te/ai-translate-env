#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
local-mt-gateway.py   v1.1   （母本，已合入两处补丁）

  1. 代码块隔离   : Markdown 围栏代码块 / <pre><code> 整块不进模型，还原时用原文逐字节贴回
  2. 行内保护     : --flag / API_KEY / v1.2.3 / pkg.func() / URL / 变量 / emoji 一律掩码
  3. 占位符校验   : 模型吞掉标记 -> 低温重试 -> 仍失败回退原文（绝不吐坏数据）
  4. 漏译检测     : 空输出 / 拒绝 / 过短 / 过长 / 句数骤减 -> 重试
  5. 页级术语记忆 : 会话级术语表，后台自动抽取 + 种子术语表
  6. 双入口       : /v1/chat/completions (OpenAI 兼容) 与 /translate (沉浸式翻译自定义接口)

改动记录：
  P1  httpx.AsyncClient(..., trust_env=False)  —— 绕开系统/IE 代理（否则报 502 Bad Gateway）
  P2  looks_bad 中文句末标点独立计数          —— 修复 3 句以上段落被误判为"截断"而回退原文

运行: D:\Python\Python313\python.exe local-mt-gateway.py
依赖: fastapi, uvicorn[standard], httpx
环境变量: BACKEND / MODEL_ID / TARGET / PORT / DEBUG
"""
import os, re, json, time, uuid, asyncio, threading
from typing import Dict, List, Tuple
import httpx, uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse

# ============================ 配置 ============================
BACKEND       = os.getenv("BACKEND", "http://127.0.0.1:8080/v1/chat/completions")
MODEL_ID      = os.getenv("MODEL_ID", "qwen3-8b")
TARGET        = os.getenv("TARGET", "zh-CN")
PORT          = int(os.getenv("PORT", "8000"))
DEBUG         = os.getenv("DEBUG", "0") == "1"
HERE          = os.path.dirname(os.path.abspath(__file__))
LOGDIR        = os.path.join(HERE, "logs")
os.makedirs(LOGDIR, exist_ok=True)

TEMP, TOP_P, TOP_K, MAX_TOKENS = 0.2, 0.8, 20, 2048
TIMEOUT       = 180.0
SESSION_IDLE  = 900      # 会话空闲超时(秒)：超时后术语记忆清空
HARVEST_EVERY = 50       # 每累计 N 个源片段，后台抽取一次术语
HARVEST_KEEP  = 40       # 注入 prompt 的术语上限
HARVEST_INPUT = 6000     # 抽取时使用的源文本最大字符数
SEED_FILE     = os.path.join(HERE, "glossary.seed.json")
MASK_FMT      = "[[ph{}]]"


def log(msg: str):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with open(os.path.join(LOGDIR, "gateway.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ======================= 1. 代码块隔离 =======================
FENCE_OPEN = re.compile(r'^( {0,3})(`{3,}|~{3,})(.*)$')
HTML_CODE  = re.compile(r'<pre\b[\s\S]*?</pre>|<code\b[\s\S]*?</code>', re.I)


def split_fenced(text: str):
    """按 CommonMark 规则切分围栏代码块；未闭合则保守到文末。拼接回去等于原文。"""
    lines, out, buf, i = text.split('\n'), [], [], 0
    while i < len(lines):
        line = lines[i]
        m = FENCE_OPEN.match(line)
        if not m or (m.group(2)[0] == '`' and '`' in m.group(3)):
            buf.append(line); i += 1; continue
        if buf:
            out.append(('text', '\n'.join(buf))); buf = []
        ch, n = m.group(2)[0], len(m.group(2))
        close = re.compile(r'^ {0,3}' + re.escape(ch) + '{' + str(n) + r',}\s*$')
        seg, i = [line], i + 1
        while i < len(lines):
            seg.append(lines[i])
            if close.match(lines[i]):
                i += 1; break
            i += 1
        out.append(('code', '\n'.join(seg)))
    if buf:
        out.append(('text', '\n'.join(buf)))
    return out


def mask_code(text: str, store: Dict[str, str]) -> str:
    """围栏代码块 + <pre>/<code> 元素 -> 占位符；原文进 store（保险箱）"""
    def put(s: str) -> str:
        k = MASK_FMT.format(len(store)); store[k] = s; return k
    parts = split_fenced(text)
    if any(k == 'code' for k, _ in parts) or HTML_CODE.search(text):
        text = '\n'.join(put(seg) if kind == 'code' else seg for kind, seg in parts)
    return HTML_CODE.sub(lambda m: put(m.group(0)), text)


# ==================== 2. 行内标识符保护 ====================
MASK_RE = re.compile(
    r"(?P<code>`[^`\n]+`)"
    r"|(?P<brackets>\[\[(?!ph\d+\])[^\[\]\n]{0,60}\]\])"
    r"|(?P<extph><[A-Za-z]?\d+></[A-Za-z]?\d+>)"
    r"|(?P<tag></?[A-Za-z][^<>]{0,300}?>)"
    r"|(?P<var>\{\{[^{}]{0,200}\}\}|\{[A-Za-z_][\w.\[\]]{0,60}\}|\$\{[^}]{0,200}\}|%[sdifrg]\b|\{\d+\})"
    r"|(?P<url>https?://[^\s<>\"')\]]+|www\.[^\s<>\"')\]]+)"
    r"|(?P<mail>[\w.+-]+@[\w-]+\.[A-Za-z]{2,})"
    r"|(?P<path>(?:[A-Za-z]:\\[^\s]+|(?:\.{0,2}/)[\w./\-]{2,}))"
    r"|(?P<flag>(?<![\w-])--?[a-zA-Z][\w-]{1,})"
    r"|(?P<const>\b[A-Z][A-Z0-9_]{2,}\b)"
    r"|(?P<ver>\bv\d+(?:\.\d+){1,3}\b)"
    r"|(?P<call>(?<![\w.])\w+(?:\.\w+){1,}\(\))"
    r"|(?P<num>\d+(?:[.,]\d+)?\s?(?:%|GB|MB|KB|GHz|MHz|ms|px|em|rem|kg|km|°C|°F)\b)"
    r"|(?P<emoji>[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F])"
)
MASK_SCAN = re.compile(r"\[\[\s*ph?\s*(\d+)\s*\]\]|【\s*ph?\s*(\d+)\s*】|<ph\s*(\d+)\s*/?>", re.I)


def protect(text: str, store: Dict[str, str]) -> str:
    def _sub(m):
        k = MASK_FMT.format(len(store)); store[k] = m.group(0); return k
    return MASK_RE.sub(_sub, text)


def restore(text: str, store: Dict[str, str]) -> Tuple[str, List[str]]:
    """把标记换回原文；同时报告哪些标记被模型弄丢了"""
    seen = set()

    def _r(m):
        i = next(g for g in m.groups() if g is not None)
        seen.add(i)
        return store.get(MASK_FMT.format(i), m.group(0))

    out = MASK_SCAN.sub(_r, text)
    missing = [k for k in store if k[len("[[ph"):-2] not in seen]
    return out, missing


def reinsert_missing(text: str, store: Dict[str, str]) -> str:
    """模型吞掉的代码块，按编号补回（内容绝对正确）"""
    for k, v in store.items():
        if k not in text:
            text = text.rstrip() + "\n\n" + v
    return text


class StreamUnmasker:
    """流式还原：尾部可能是被截断的标记，先扣住不发"""
    TAIL = re.compile(r"[\[【<][^\]】>]{0,14}$")

    def __init__(self, store): self.store, self.buf = store, ""

    def feed(self, chunk: str) -> str:
        self.buf += chunk
        m = self.TAIL.search(self.buf)
        cut = m.start() if m else len(self.buf)
        emit, self.buf = self.buf[:cut], self.buf[cut:]
        return restore(emit, self.store)[0]

    def finish(self) -> str:
        out = restore(self.buf, self.store)[0]
        self.buf = ""
        return out


# ================== 3. 会话级术语记忆 ==================
def load_seed() -> Dict[str, str]:
    try:
        with open(SEED_FILE, encoding="utf-8") as f:
            return {str(k): str(v) for k, v in json.load(f).items()}
    except Exception:
        return {}


class SessionStore:
    def __init__(self, idle=SESSION_IDLE):
        self.idle, self.lock, self.sessions = idle, threading.Lock(), {}
        self.seed = load_seed()

    def _gc(self, now):
        for sid in [s for s, v in self.sessions.items() if now - v["last"] > self.idle]:
            self.sessions.pop(sid, None)

    def get(self, sid: str) -> dict:
        with self.lock:
            now = time.time(); self._gc(now)
            s = self.sessions.get(sid)
            if s is None:
                s = {"terms": {}, "buf": [], "n": 0, "last": now, "busy": False}
                self.sessions[sid] = s
            s["last"] = now
            return s

    def reset(self, sid=None):
        with self.lock:
            self.sessions.pop(sid, None) if sid else self.sessions.clear()

    def glossary_for(self, sid: str, raw_text: str) -> Dict[str, str]:
        s = self.get(sid)
        merged = dict(self.seed); merged.update(s["terms"])
        pri = {k: v for k, v in merged.items()
               if re.search(r"(?i)\b" + re.escape(k) + r"\b", raw_text)}
        out = dict(pri)
        for k, v in merged.items():
            if k not in out and len(out) < HARVEST_KEEP:
                out[k] = v
        return out

    def note(self, sid: str, raw_text: str) -> bool:
        s = self.get(sid); s["buf"].append(raw_text); s["n"] += 1
        return s["n"] % HARVEST_EVERY == 0 and not s["busy"]

    def take_buf(self, sid: str) -> str:
        s = self.get(sid)
        data = "\n".join(s["buf"])[-HARVEST_INPUT:]
        s["buf"] = []; s["busy"] = bool(data)
        return data

    def merge(self, sid: str, terms: dict):
        s = self.get(sid)
        for k, v in (terms or {}).items():
            if k not in self.seed:
                s["terms"][k] = v
        s["busy"] = False


SESS = SessionStore()


# ================== 4. 提示词 ==================
SYS = (
    "你是技术文档翻译引擎，只输出译文，不要解释、不要前言、不要 markdown 包裹。 /no_think\n"
    "铁律：\n"
    "1. 绝不翻译、绝不改动：标识符、变量名、函数名、类名、API 路径、命令行参数、文件名、"
    "配置键、版本号，以及字符串里的错误信息/正则/SQL/日志格式串。\n"
    "2. 保留全部占位标记（形如 [[ph0]]）及其位置，不增不减不改写。\n"
    "3. 不增删内容，不加“注：”，不做润色改写，不截断、不省略。\n"
    "4. 目标语言为简体中文；术语必须与下方术语表一致。\n"
    # 可选：想改善中文语序（如"在 config.yaml 中设置"而非"设置...在 config.yaml 中"），
    # 取消下面这行的注释后重启网关即可。
    # "5. 译文需符合中文语序和表达习惯（可调整语序），但不得改变技术含义、不得增删信息。\n"
)


def build_messages(masked_text: str, glossary: dict, strict=False):
    sysmsg = SYS + ("5. 本次务必完整翻译全部内容，不得省略任何句子。\n" if strict else "")
    user = ""
    if glossary:
        user += "术语表（必须遵守）：\n" + "\n".join(f"{k} => {v}" for k, v in glossary.items()) + "\n\n"
    user += "待翻译内容：\n" + masked_text
    return [{"role": "system", "content": sysmsg}, {"role": "user", "content": user}]


HARVEST_SYS = (
    "你是技术文档术语抽取器。从给定英文技术文档片段中抽取需要统一译法的技术名词，"
    "给出建议的简体中文译法。只输出 JSON 对象（英文: 中文），不要解释，不要 markdown 包裹。"
    "不要收录纯缩写（如 API/HTTP/JSON），不要收录人名公司名。最多 25 条。 /no_think"
)


# ================== 5. 后端调用 ==================
def _body(messages, temperature, stream, max_tokens):
    return {
        "model": MODEL_ID, "messages": messages, "temperature": temperature,
        "top_p": TOP_P, "top_k": TOP_K, "max_tokens": max_tokens, "stream": stream,
        "cache_prompt": True, "chat_template_kwargs": {"enable_thinking": False},
    }


def _content(msg: dict) -> str:
    return (msg.get("content") or msg.get("reasoning_content") or "").strip()


async def backend_text(messages, temperature=TEMP, max_tokens=MAX_TOKENS) -> str:
    # P1: trust_env=False —— 忽略系统/IE 代理，否则会 502
    async with httpx.AsyncClient(timeout=TIMEOUT, trust_env=False) as c:
        r = await c.post(BACKEND, json=_body(messages, temperature, False, max_tokens))
        r.raise_for_status()
        return _content(r.json()["choices"][0]["message"])


async def backend_stream(messages, temperature=TEMP, max_tokens=MAX_TOKENS):
    # P1: trust_env=False
    async with httpx.AsyncClient(timeout=TIMEOUT, trust_env=False) as c:
        async with c.stream("POST", BACKEND, json=_body(messages, temperature, True, max_tokens)) as r:
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                d = line[5:].strip()
                if d == "[DONE]":
                    break
                try:
                    delta = json.loads(d)["choices"][0].get("delta", {})
                except Exception:
                    continue
                if delta.get("content"):
                    yield delta["content"]


# ================== 6. 漏译检测 ==================
CJK = re.compile(r"[\u4e00-\u9fff]")


def mostly_target(t: str) -> bool:
    return (TARGET.startswith("zh") and len(t) > 8
            and len(CJK.findall(t)) / max(1, len(t)) > 0.5)


def looks_bad(src: str, out: str):
    """src/out 都应是『含占位符』的形态，避免代码块长度干扰判断"""
    if not out or not out.strip():
        return "empty"
    if re.search(r"(抱歉|无法完成|作为一个 ?AI|As an AI|I cannot)", out):
        return "refusal"
    if len(src) >= 60 and len(out) < len(src) * 0.18:
        return "too_short"
    if len(out) > len(src) * 3.5:
        return "too_long"
    # P2: 中文句末标点后面没有空格，必须独立计数；ASCII 标点仍要求后跟空格/结尾
    s = len(re.findall(r"[.!?](?:\s|$)", src))
    o = len(re.findall(r"[。！？]", out)) + len(re.findall(r"[.!?](?:\s|$)", out))
    if s >= 3 and o <= max(1, s // 3):
        return "truncated"
    return None


# ================== 7. 翻译核心 ==================
async def translate(text: str, sid: str) -> Tuple[str, bool]:
    """返回 (译文, 是否成功)；失败时回退原文，绝不吐坏数据"""
    if not text.strip():
        return text, True
    if mostly_target(text):
        return text, True

    store: Dict[str, str] = {}
    masked = protect(mask_code(text, store), store)      # ① 代码块隔离 ② 行内保护
    gl = SESS.glossary_for(sid, text)

    for temp, strict in ((TEMP, False), (0.0, True)):
        try:
            out = await backend_text(build_messages(masked, gl, strict), temperature=temp)
        except Exception as e:
            log(f"[err] backend: {e}")
            continue
        restored, missing = restore(out, store)
        reason = ("missing:" + ",".join(missing)) if missing else looks_bad(masked, out)
        if not reason:
            return restored, True
        log(f"[retry] {reason} | {text[:50]!r}")

    log(f"[fallback] 两次均不合格，回退原文 | {text[:50]!r}")
    return text, False


async def harvest(sid: str, source_text: str):
    try:
        raw = await backend_text(
            [{"role": "system", "content": HARVEST_SYS},
             {"role": "user", "content": source_text}],
            temperature=0.1, max_tokens=800)
        m = re.search(r"\{[\s\S]*\}", raw)
        terms = json.loads(m.group(0)) if m else {}
        terms = {k: v for k, v in terms.items() if isinstance(k, str) and isinstance(v, str)}
        SESS.merge(sid, terms)
        log(f"[harvest] sid={sid} +{len(terms)}")
    except Exception as e:
        SESS.merge(sid, {})
        log(f"[harvest] failed: {e}")


def after_translate(sid: str, raw_text: str):
    if SESS.note(sid, raw_text):
        buf = SESS.take_buf(sid)
        if buf.strip():
            asyncio.create_task(harvest(sid, buf))


# ================== 8. HTTP 接口 ==================
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def extract_text(body) -> str:
    for m in reversed(body.get("messages") or []):
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, list):
                c = "".join(p.get("text", "") for p in c if isinstance(p, dict))
            return c or ""
    return ""


def _sse(obj): return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def _chunk(cid, delta=None, finish=None, role=None):
    d = {}
    if role: d["role"] = role
    if delta: d["content"] = delta
    return _sse({"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                 "model": MODEL_ID,
                 "choices": [{"index": 0, "delta": d, "finish_reason": finish}]})


def _full(cid, content):
    return {"id": cid, "object": "chat.completion", "created": int(time.time()),
            "model": MODEL_ID,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}


@app.post("/v1/chat/completions")
async def chat(req: Request):
    body = await req.json()
    if DEBUG:
        with open(os.path.join(HERE, "last_request.json"), "w", encoding="utf-8") as f:
            json.dump(body, f, ensure_ascii=False, indent=2)
    sid = req.headers.get("x-session-id") or "default"
    text = extract_text(body)
    cid = "chatcmpl-" + uuid.uuid4().hex[:12]

    if body.get("stream"):        # 流式：无法校验/重试，只做占位符还原
        store: Dict[str, str] = {}
        masked = protect(mask_code(text, store), store)
        gl = SESS.glossary_for(sid, text)

        async def gen():
            yield _chunk(cid, role="assistant")
            unm = StreamUnmasker(store)
            async for piece in backend_stream(build_messages(masked, gl)):
                d = unm.feed(piece)
                if d:
                    yield _chunk(cid, delta=d)
            tail = unm.finish()
            if tail:
                yield _chunk(cid, delta=tail)
            yield _chunk(cid, finish="stop")
            yield "data: [DONE]\n\n"
            after_translate(sid, text)

        return StreamingResponse(gen(), media_type="text/event-stream")

    out, _ok = await translate(text, sid)
    after_translate(sid, text)
    return JSONResponse(_full(cid, out))


@app.post("/translate")       # 沉浸式翻译「自定义接口」备用通道
async def immersive(req: Request):
    b = await req.json()
    sid = req.headers.get("x-session-id") or "default"
    src = b.get("source_lang", "auto")
    outs = [await translate(t, sid)[0] for t in (b.get("text_list") or [])]
    return {"translations": [{"detected_source_lang": src, "text": o} for o in outs]}


@app.get("/health")
async def health():
    return {"ok": True, "backend": BACKEND, "target": TARGET}


@app.get("/glossary")
async def glossary(sid: str = "default"):
    s = SESS.get(sid)
    return {"seed": SESS.seed, "learned": s["terms"], "segments": s["n"]}


@app.post("/reset")
async def reset(sid: str = None):
    SESS.reset(sid)
    return {"ok": True}


if __name__ == "__main__":
    log(f"gateway on http://127.0.0.1:{PORT}  ->  {BACKEND}")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
