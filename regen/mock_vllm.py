"""
Stand-in for the vLLM server, to test the regeneration clients without a GPU: checks the bearer
key, streams reasoning and content deltas the way vLLM's qwen3 reasoning parser does, echoes the
template variables it received, and simulates a runaway loop (prompt contains LOOP) or the context
limit (prompt contains LENGTH).

    REGEN_API_KEY=test python regen/mock_vllm.py --port 18001
"""
import argparse, asyncio, json, os
from aiohttp import web


def last_text(messages):
    c = messages[-1]["content"]
    return c if isinstance(c, str) else " ".join(p.get("text", "") for p in c if p.get("type") == "text")


async def chat(request):
    if request.headers.get("Authorization") != f"Bearer {os.environ['REGEN_API_KEY']}":
        return web.Response(status = 401, text = "bad key")
    body = await request.json()
    prompt = last_text(body["messages"])
    kw = body.get("chat_template_kwargs") or {}
    has_image = any(isinstance(m["content"], list) and any(p.get("type") == "image_url" for p in m["content"])
                    for m in body["messages"])
    resp = web.StreamResponse(headers = {"Content-Type": "text/event-stream"})
    await resp.prepare(request)
    send = lambda d, fr = None: resp.write(
        f"data: {json.dumps({'choices': [{'index': 0, 'delta': d, 'finish_reason': fr}]})}\n\n".encode())
    finish = "stop"
    if kw.get("enable_thinking", True):
        await send({"reasoning": f"thinking at {kw.get('reasoning_effort', 'xhigh')} about: {prompt[:40]}"})
    if "LOOP" in prompt:
        for _ in range(5000):
            await send({"content": "the same sentence repeats again and again in this stream. " * 4})
            await asyncio.sleep(0)
    elif "LENGTH" in prompt:
        await send({"content": "cut short"})
        finish = "length"
    else:
        await send({"content": f"answer to: {prompt[:60]} | turns={len(body['messages'])} image={has_image} kw={kw}"})
    await send({}, finish)
    await resp.write(f"data: {json.dumps({'choices': [], 'usage': {'prompt_tokens': 10, 'completion_tokens': 5}})}\n\n".encode())
    await resp.write(b"data: [DONE]\n\n")
    return resp


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type = int, default = 18001)
    args = ap.parse_args()
    app = web.Application(client_max_size = 64 * 1024 * 1024)
    app.router.add_post("/v1/chat/completions", chat)
    web.run_app(app, host = "127.0.0.1", port = args.port, print = None)
