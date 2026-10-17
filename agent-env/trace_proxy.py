"""
Logging reverse proxy for agent-harness calibration traces.

Sits between a harness (pi, opencode, codex) and the OpenAI-compatible model server. Every
request/response pair is passed through unchanged (streaming SSE included) and appended to
<log_dir>/<session>.jsonl, where <session> is the first path segment:

    harness base_url = http://127.0.0.1:1338/<session>/v1   ->   upstream http://127.0.0.1:1337/v1

Streamed responses are stored raw (the SSE lines) and reassembled offline. The proxy also
tracks the largest prompt size seen per session (prompt_tokens from the upstream usage block,
when present), exposed at GET /_status, so a driver can stop sessions that reached the target
context length.

Harnesses do not send a reasoning mode, so the chat template falls back to its default (xhigh).
A session named <name>-eff_<mode> (mode: off, low, medium, xhigh) gets the mode injected into
every chat request before it is forwarded and logged: top-level reasoning_effort, or
enable_thinking = false for off (TabbyAPI reads template variables from the top level). With
--vllm the variables go into chat_template_kwargs instead, the only place vLLM passes them to the
template. The logged request is what the server rendered, so build_rows.py can reproduce the
exact token stream.

    python trace_proxy.py --upstream http://127.0.0.1:1337 --port 1338 --log_dir traces/
    python trace_proxy.py --upstream http://127.0.0.1:18000 --vllm --api_key_env REGEN_API_KEY --log_dir traces/
"""
import argparse, asyncio, json, os, re, time
from aiohttp import web, ClientError, ClientSession, ClientTimeout

EFFORT_RE = re.compile(r"-eff_(off|low|medium|xhigh)$")
HOP = {"host", "content-length", "transfer-encoding", "connection", "keep-alive", "content-encoding"}
STATUS = {}


def log_line(log_dir, session, rec):
    with open(os.path.join(log_dir, f"{session}.jsonl"), "a") as f:
        f.write(json.dumps(rec, ensure_ascii = False) + "\n")


def prompt_tokens_from(body_text):
    """usage.prompt_tokens from a JSON body or from the last SSE chunk carrying usage."""
    try:
        return json.loads(body_text)["usage"]["prompt_tokens"]
    except Exception:
        pass
    pt = None
    for line in body_text.splitlines():
        if line.startswith("data:") and '"usage"' in line:
            try:
                u = json.loads(line[5:].strip()).get("usage") or {}
                pt = u.get("prompt_tokens", pt)
            except Exception:
                pass
    return pt


async def handle(request):
    app = request.app
    parts = request.path.lstrip("/").split("/", 1)
    if parts[0] == "_status":
        return web.json_response(STATUS)
    if len(parts) < 2:
        return web.Response(status = 404, text = "expected /<session>/v1/...")
    session, rest = parts[0], "/" + parts[1]
    url = app["upstream"] + rest + (("?" + request.query_string) if request.query_string else "")
    body = await request.read()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
    if app["api_key"]:
        # harnesses carry a dummy key; the real server key never leaves the host process
        headers = {k: v for k, v in headers.items() if k.lower() not in ("authorization", "x-api-key")}
        headers["Authorization"] = f"Bearer {app['api_key']}"
    t0 = time.time()
    st0 = STATUS.setdefault(session, {"requests": 0, "max_prompt_tokens": 0, "last": time.time(), "inflight": 0})
    st0["inflight"] = st0.get("inflight", 0) + 1
    try:
        req_json = json.loads(body) if body else None
    except Exception:
        req_json = None
    m = EFFORT_RE.search(session)
    if m and isinstance(req_json, dict) and rest.rstrip("/").endswith("chat/completions"):
        tvars = {"enable_thinking": False} if m.group(1) == "off" else \
            {"enable_thinking": True, "reasoning_effort": m.group(1)}
        if app["vllm"]:
            req_json["chat_template_kwargs"] = {**(req_json.get("chat_template_kwargs") or {}), **tvars}
            req_json.pop("enable_thinking", None)
            if "reasoning_effort" in tvars:
                req_json["reasoning_effort"] = tvars["reasoning_effort"]
            else:
                req_json.pop("reasoning_effort", None)
        else:
            req_json.update(tvars)
        body = json.dumps(req_json).encode()

    # Harnesses (pi, codex) close the connection right after "data: [DONE]", before the chunked
    # terminator; that must neither lose the log record nor cut the upstream stream short
    chunks, status, resp, client_gone = [], None, None, False
    try:
        async with app["client"].request(request.method, url, data = body, headers = headers) as up:
            status = up.status
            resp = web.StreamResponse(status = up.status,
                                      headers = {k: v for k, v in up.headers.items() if k.lower() not in HOP})
            await resp.prepare(request)
            async for chunk in up.content.iter_any():
                chunks.append(chunk)
                if not client_gone:
                    try:
                        await resp.write(chunk)
                    except (ConnectionError, ClientError):
                        client_gone = True
            if not client_gone:
                try:
                    await resp.write_eof()
                except (ConnectionError, ClientError):
                    client_gone = True
    finally:
        st0["inflight"] -= 1
        st0["last"] = time.time()
        if status is not None and request.method == "POST" and \
                rest.rstrip("/").endswith(("chat/completions", "/responses", "/completions")):
            text = b"".join(chunks).decode("utf-8", errors = "replace")
            pt = prompt_tokens_from(text)
            st0["requests"] += 1
            if pt:
                st0["max_prompt_tokens"] = max(st0["max_prompt_tokens"], pt)
            log_line(app["log_dir"], session, {
                "t": t0, "dt": round(time.time() - t0, 3), "path": rest, "status": status,
                "request": req_json, "response_raw": text, "prompt_tokens": pt,
                "client_gone": client_gone,
            })
    return resp


async def make_app(args):
    app = web.Application(client_max_size = 256 * 1024 * 1024)
    app["upstream"] = args.upstream.rstrip("/")
    app["log_dir"] = args.log_dir
    app["api_key"] = None
    app["vllm"] = args.vllm
    if args.api_key_env:
        app["api_key"] = os.environ[args.api_key_env]
    elif args.api_key_file:
        import yaml
        app["api_key"] = yaml.safe_load(open(args.api_key_file))["api_key"]
    app["client"] = ClientSession(timeout = ClientTimeout(total = None, sock_read = 3600))
    app.router.add_route("*", "/{tail:.*}", handle)
    async def close(app_):
        await app_["client"].close()
    app.on_cleanup.append(close)
    return app


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--upstream", default = "http://127.0.0.1:1337")
    ap.add_argument("--host", default = "0.0.0.0")
    ap.add_argument("--port", type = int, default = 1338)
    ap.add_argument("--log_dir", required = True)
    ap.add_argument("--api_key_file", default = None, help = "tabbyAPI api_tokens.yml; its api_key replaces the client's")
    ap.add_argument("--api_key_env", default = None, help = "environment variable holding the server key, instead of --api_key_file")
    ap.add_argument("--vllm", action = "store_true", help = "pass the reasoning mode in chat_template_kwargs")
    args = ap.parse_args()
    os.makedirs(args.log_dir, exist_ok = True)
    web.run_app(make_app(args), host = args.host, port = args.port, print = None)
