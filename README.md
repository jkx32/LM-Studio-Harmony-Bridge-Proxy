# LM Studio Harmony Bridge \ Adapter Proxy

**A Python proxy server that converts GPT-OSS Harmony format to clean XML tool calls for Cline, Roo Code, and other AI coding assistants.**

## 🎯 Problem

When using GPT-OSS models (20B/120B) with LM Studio and coding assistants like Cline or Roo Code, you encounter raw Harmony channel tags in the output:

```
<|channel|>commentary to=write_to_file <|constrain|>json<|message|>{"path":"test.py",...}
```

Instead of clean, executable tool calls. This happens because:

- **LM Studio's Harmony parsing is incomplete** — Tool calls are partially parsed but raw tags leak through
- **Cline/Roo Code native support is unstable** — Works sometimes, breaks on restart or different tasks
- **Kilo Code has no Harmony support** — Shows raw model output without any parsing
- **SSE streaming issues** — Chunks arrive out of order, breaking XML/JSON structure

## ✨ Solution

This proxy sits between LM Studio and your coding assistant, performing:

1. **Strict Harmony parsing** — Extracts complete `commentary` and `final` channels
2. **XML transformation** — Converts tool calls to Cline-compatible format: `<tool_name><param>value</param></tool_name>`
3. **Stream buffering** — Accumulates complete blocks before sending to prevent broken output (!!may create the appearance of text streaming being disabled)
4. **Clean output** — Zero raw Harmony tags, 100% reliable tool execution

## 🚀 Quick Start

### Installation

```bash
pip install aiohttp
```

### Run the Proxy

```bash
# Default: XML mode for Cline/Roo Code/Kilo
python lm_studio_harmony_bridge.py

# JSON mode for OpenAI-compatible clients
python lm_studio_harmony_bridge.py --format json

# Custom ports
python lm_studio_harmony_bridge.py --port 8000 --lm-studio-url http://localhost:1234

# Debug mode
python lm_studio_harmony_bridge.py --debug
```

### Configure Your Coding Assistant

**Cline** (VS Code):
1. Open Cline settings
2. Set Base URL: `http://localhost:8000/v1` or `http://localhost:8000/api/v0`
3. Select model: `gpt-oss-20b` or `gpt-oss-120b`
4. Start coding!

**Roo Code**:
1. Settings → API Provider → Custom
2. Base URL: `http://localhost:8000/v1`
3. Model: `gpt-oss-20b`

**Kilo Code**:
1. Settings → LLM Provider → Custom OpenAI
2. Base URL: `http://localhost:8000/v1`
3. Model: `gpt-oss-20b`

## 📖 How It Works

GPT-OSS generates responses in **Harmony format** with three parallel channels:

```
<|start|>assistant
<|channel|>analysis<|message|>Let me plan the solution...<|end|>
<|channel|>commentary to=write_to_file <|constrain|>json<|message|>{"path":"test.py","content":"..."}
<|channel|>final<|message|>I created test.py with basic structure.<|end|>
```

The proxy:
1. **Parses channels** — Extracts `analysis` (CoT), `commentary` (tool calls), `final` (user message)
2. **Buffers SSE stream** — Waits for complete `<|channel|>...<|end|>` blocks
3. **Converts to XML** — Transforms `commentary to=write_to_file` into:
   ```xml
   <write_to_file>
   <path>test.py</path>
   <content>...</content>
   </write_to_file>
   ```
4. **Cleans output** — Suppresses `analysis` and incomplete blocks

## 🔧 Features

- ✅ **Dual format support**: XML (Cline) or JSON (OpenAI)
- ✅ **Multiple endpoints**: `/v1/*` and `/api/v0/*` for compatibility
- ✅ **Streaming**: Real-time token streaming
- ✅ **Buffering**: Complete tool calls sent as atomic units
- ✅ **Error handling**: Graceful fallback on malformed chunks
- ✅ **Logging**: Colored logs with request tracking
- ✅ **Lightweight**: ~300 lines, zero dependencies except aiohttp

## 📝 Example Output

**Before (raw Harmony leaking through):**
```
<|channel|>commentary to=write_to_file <|constrain|>json<|message|>{"path":"main.py","content":"def hello():..."}
I created the file.
```

**After (clean XML):**
```xml
<write_to_file>
<path>main.py</path>
<content>def hello():
    print("Hello, world!")
</content>
</write_to_file>
```

User sees: "I created the file." ✨

## 🐛 Troubleshooting

**"API Request Cancelled / Empty Response"**
- Ensure LM Studio is running on `http://localhost:1234`
- Check proxy logs for connection errors
- Try `--debug` flag for verbose output

**Tool calls still show raw tags**
- Verify you're using the proxy URL in Cline settings, not direct LM Studio URL
- Restart Cline/VS Code after changing settings
- Check proxy logs show "Parsing Harmony format..."

**Streaming is slow/choppy**
- Normal for tool calls (they're buffered until complete)
- Regular text messages stream normally
- Try reducing `max_tokens` in LM Studio if model is slow

## 🔗 Related Issues

This proxy solves problems reported in:
- [Cline #6698](https://github.com/cline/cline/pull/6698) — Partial GPT-OSS support (unstable)
- [Roo Code #6750](https://github.com/RooCodeInc/Roo-Code/issues/6750) — LM Studio Harmony rendering issues
- [LM Studio #942](https://github.com/lmstudio-ai/lmstudio-bug-tracker/issues/942) — Parsing errors for tool calls
- [Reddit: GPT-OSS + LM Studio + Cline](https://www.reddit.com/r/CLine/comments/1mtcj2v/making_gptoss_20b_and_cline_work_together/) — Community workarounds

## 💡 Why Not Use Native Support?

**Native support is improving but not production-ready:**
- Works ~80-90% of the time, may breaks randomly
- Raw tags leak through in multi-turn conversations
- No support in Kilo Code or older Cline versions

By the way, you always can use llama.cpp with grammar (like there: - [aldegr's blog post](https://alde.dev/blog/gpt-oss-20b-with-cline-and-roo-code/) — Original llama.cpp grammar workaround), but it’s may be less convenient in some use cases.

## 📚 References

- [OpenAI Harmony Format](https://cookbook.openai.com/articles/openai-harmony) — Official docs
- [GPT-OSS Repository](https://github.com/openai/gpt-oss) — Model details
- [LM Studio Harmony Guide](https://cookbook.openai.com/articles/gpt-oss/run-locally-lmstudio) — Setup instructions

## 📄 License

MIT License — Use freely for any purpose.

---

**⭐ If this project help you or something, please star the repo!**
