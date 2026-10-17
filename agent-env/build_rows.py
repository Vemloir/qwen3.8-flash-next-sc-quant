"""
Turn proxy logs of agent sessions into calibration rows (exact model-visible token streams).

For each session log (<session>.jsonl from trace_proxy.py) the LAST successful chat request is
used: its messages and tools are what the model saw at the final step, and its response is what
the model produced. The assistant response (reasoning, content, tool calls) is reassembled from
the stream, appended to the messages, and the whole conversation is rendered with the model's
chat template using the same template variables as the server (preserve_thinking, plus the
reasoning mode the proxy injected into the request: reasoning_effort / enable_thinking).

Output: JSON list of {"session", "ids", "n_prompt", "n_response", "requests"} rows.

    python build_rows.py --tokenizer <model_dir> --logs traces/ -o agent_rows.json
"""
import argparse, glob, json, os


def reassemble(raw):
    """Final assistant message from a JSON body or an SSE stream (chat.completions format)."""
    raw = raw.strip()
    if raw.startswith("{"):
        msg = json.loads(raw)["choices"][0]["message"]
        return {"role": "assistant", "content": msg.get("content") or "",
                "reasoning_content": msg.get("reasoning_content") or msg.get("reasoning") or "",
                "tool_calls": msg.get("tool_calls") or []}
    content, reasoning, calls = [], [], {}
    finish = None
    for line in raw.splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            ev = json.loads(data)
        except Exception:
            continue
        for ch in ev.get("choices") or []:
            d = ch.get("delta") or {}
            finish = ch.get("finish_reason") or finish
            if d.get("content"):
                content.append(d["content"])
            r = d.get("reasoning_content") or d.get("reasoning")
            if r:
                reasoning.append(r)
            for tc in d.get("tool_calls") or []:
                c = calls.setdefault(tc.get("index", 0), {"id": None, "type": "function",
                                                          "function": {"name": "", "arguments": ""}})
                if tc.get("id"):
                    c["id"] = tc["id"]
                fn = tc.get("function") or {}
                c["function"]["name"] += fn.get("name") or ""
                c["function"]["arguments"] += fn.get("arguments") or ""
    return {"role": "assistant", "content": "".join(content), "reasoning_content": "".join(reasoning),
            "tool_calls": [calls[i] for i in sorted(calls)], "_finish": finish}


def normalize(messages):
    """Template expects tool-call arguments as mappings; harnesses send JSON strings."""
    out = []
    for m in messages:
        m = dict(m)
        if isinstance(m.get("content"), list):
            # OpenAI content parts: keep text parts only (no images in these runs)
            m["content"] = "".join(p.get("text", "") for p in m["content"] if isinstance(p, dict))
        if m.get("tool_calls"):
            tcs = []
            for tc in m["tool_calls"]:
                tc = json.loads(json.dumps(tc))
                fn = tc.get("function", tc)
                if isinstance(fn.get("arguments"), str):
                    try:
                        fn["arguments"] = json.loads(fn["arguments"]) if fn["arguments"].strip() else {}
                    except Exception:
                        fn["arguments"] = {"raw": fn["arguments"]}
                tcs.append(tc)
            m["tool_calls"] = tcs
        m.pop("_finish", None)
        out.append(m)
    return out


def main(args):
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(args.tokenizer)
    rows = []
    for path in sorted(glob.glob(os.path.join(args.logs, "*.jsonl"))):
        session = os.path.splitext(os.path.basename(path))[0]
        recs = [json.loads(l) for l in open(path) if l.strip()]
        recs = [r for r in recs if r.get("status") == 200 and r.get("request")
                and r["path"].rstrip("/").endswith("chat/completions")]
        if not recs:
            print(f" !! {session}: no successful chat requests")
            continue
        # the last request with a non-empty response: a session stopped at the context cap, or one
        # whose harness ended on an empty turn, still has its full history in the previous request
        for back, last in enumerate(reversed(recs)):
            reply = reassemble(last["response_raw"])
            if reply["content"] or reply["reasoning_content"] or reply["tool_calls"]:
                break
        else:
            print(f" !! {session}: no non-empty response, skipped")
            continue
        if back:
            print(f"    {session}: last {back} response(s) empty, using the one before")
        req = last["request"]
        msgs = normalize(req["messages"])
        tvars = {"preserve_thinking": True}
        tvars.update(req.get("chat_template_kwargs") or req.get("template_vars") or {})
        for k in ("reasoning_effort", "enable_thinking"):
            if req.get(k) is not None:
                tvars[k] = req[k]
        prompt_ids = tok.apply_chat_template(msgs, tools = req.get("tools"), add_generation_prompt = True,
                                             tokenize = True, **tvars)
        full_ids = tok.apply_chat_template(msgs + normalize([reply]), tools = req.get("tools"),
                                           add_generation_prompt = False, tokenize = True, **tvars)
        if hasattr(prompt_ids, "input_ids"):
            prompt_ids, full_ids = prompt_ids["input_ids"], full_ids["input_ids"]
        rows.append({"session": session, "ids": list(full_ids), "n_prompt": len(prompt_ids),
                     "n_response": len(full_ids) - len(prompt_ids), "requests": len(recs),
                     "template_vars": tvars,
                     "upstream_prompt_tokens": last.get("prompt_tokens")})
        r = rows[-1]
        check = "" if not r["upstream_prompt_tokens"] else \
            f", server counted {r['upstream_prompt_tokens']} prompt tokens"
        print(f" -- {session}: {r['requests']} requests, {r['n_prompt']} prompt + {r['n_response']} response tokens{check}")
    json.dump(rows, open(args.out, "w"))
    print(f" -- {len(rows)} rows -> {args.out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tokenizer", required = True)
    ap.add_argument("--logs", required = True)
    ap.add_argument("-o", "--out", required = True)
    main(ap.parse_args())
