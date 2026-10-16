# Minimal OpenAI-style upstream for proxy tests: streams two content deltas, a tool call and usage
import json, os
from aiohttp import web
async def chat(request):
    body = await request.json()
    if not body.get("stream"):
        return web.json_response({"choices": [{"message": {"role": "assistant", "content": "ok"}}],
                                  "usage": {"prompt_tokens": 123, "completion_tokens": 1}})
    resp = web.StreamResponse(headers = {"Content-Type": "text/event-stream"})
    await resp.prepare(request)
    deltas = [{"reasoning_content": "think"}, {"content": "Hel"}, {"content": "lo"},
              {"tool_calls": [{"index": 0, "id": "c1", "type": "function", "function": {"name": "bash", "arguments": "{\"cmd\": \"ls\"}"}}]}]
    if os.environ.get("MOCK_TOOLS") != "1":
        deltas = deltas[:3]
    for d in deltas:
        await resp.write(f"data: {json.dumps({'choices': [{'index': 0, 'delta': d}]})}\n\n".encode())
    fr = "tool_calls" if os.environ.get("MOCK_TOOLS") == "1" else "stop"
    await resp.write(f"data: {json.dumps({'choices': [{'index': 0, 'delta': {}, 'finish_reason': fr}]})}\n\n".encode())
    await resp.write(f"data: {json.dumps({'choices': [], 'usage': {'prompt_tokens': 456, 'completion_tokens': 4}})}\n\n".encode())
    await resp.write(b"data: [DONE]\n\n")
    return resp
app = web.Application(); app.router.add_post("/v1/chat/completions", chat)
web.run_app(app, host = "127.0.0.1", port = 1399, print = None)
