#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🌉 LM Studio Harmony Bridge Proxy v2.2
- Converts GPT‑OSS Harmony → XML tool calls (Cline) or JSON tool_calls (OpenAI).
- Strong SSE buffering: never leaks raw Harmony tags. (may disable text streaming in some cases) 
- Supports /v1 and /api/v0 endpoints.
"""

import re
import json
import copy
import asyncio
import logging
from typing import Dict, List, Any, Tuple
from aiohttp import web, ClientSession, ClientTimeout
import aiohttp
from datetime import datetime

# ---------- Colored logging ----------
class ColoredFormatter(logging.Formatter):
    COLORS = {
        'DEBUG': '\033[36m', 'INFO': '\033[32m',
        'WARNING': '\033[33m', 'ERROR': '\033[31m',
        'CRITICAL': '\033[35m', 'RESET': '\033[0m'
    }
    def format(self, record):
        log_color = self.COLORS.get(record.levelname, self.COLORS['RESET'])
        reset = self.COLORS['RESET']
        time_str = datetime.now().strftime('%H:%M:%S')
        level = f"{log_color}{record.levelname:8}{reset}"
        message = record.getMessage()
        return f"[{time_str}] {level} | {message}"

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(ColoredFormatter())
logger.addHandler(handler)

# ---------- Harmony parser ----------
class HarmonyParser:
    CHANNEL_PATTERN = re.compile(
        r'<\|channel\|>(?P<channel>\w+)'
        r'(?:\s+to=(?P<recipient>[^<\s]+))?'
        r'(?:\s*<\|constrain\|>(?P<constrain>\w+))?'
        r'<\|message\|>(?P<content>.*?)(?=(?:<\|channel\|>|\Z))',
        re.DOTALL
    )
    @staticmethod
    def has_harmony(text: str) -> bool:
        return '<|channel|>' in text or '<|message|>' in text or '<|start|>' in text
    @staticmethod
    def parse_block(block: str) -> Dict[str, Any]:
        result = {'final_message': '', 'analysis': [], 'tool_calls': [], 'commentary': []}
        for m in HarmonyParser.CHANNEL_PATTERN.finditer(block):
            channel = (m.group('channel') or '').strip()
            recipient = (m.group('recipient') or '').strip()
            constrain = (m.group('constrain') or '').strip()
            content = (m.group('content') or '').strip()
            if channel == 'final':
                result['final_message'] += content
            elif channel == 'analysis':
                result['analysis'].append(content)
            elif channel == 'commentary':
                if recipient:
                    name = recipient.replace('functions.', '')
                    if constrain == 'json':
                        try:
                            args = json.loads(content) if content else {}
                        except json.JSONDecodeError:
                            args = {'raw': content}
                    else:
                        args = {'raw': content}
                    result['tool_calls'].append({'name': name, 'arguments': args})
                else:
                    result['commentary'].append(content)
        return result

# ---------- XML formatter (Cline format) ----------
class XMLFormatter:
    @staticmethod
    def _esc(s: str) -> str:
        return (s.replace('&', '&amp;')
                 .replace('<', '&lt;')
                 .replace('>', '&gt;')
                 .replace('"', '&quot;')
                 .replace("'", '&apos;'))
    @staticmethod
    def tool_calls_to_xml(tool_calls: List[Dict[str, Any]]) -> str:
        parts: List[str] = []
        for tc in tool_calls:
            name = tc['name']
            args = tc['arguments']
            parts.append(f'<{name}>')
            if isinstance(args, dict):
                for k, v in args.items():
                    if v is None:
                        v = ''
                    if not isinstance(v, str):
                        v = json.dumps(v, ensure_ascii=False)
                    parts.append(f'<{k}>{XMLFormatter._esc(v)}</{k}>')
            else:
                parts.append(XMLFormatter._esc(str(args)))
            parts.append(f'</{name}>')
        return '\n'.join(parts)
    @staticmethod
    def tool_calls_to_openai(tool_calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out = []
        for i, tc in enumerate(tool_calls):
            out.append({
                'id': f'call_{i}_{abs(hash(tc["name"])) % 10000}',
                'type': 'function',
                'function': {
                    'name': tc['name'],
                    'arguments': json.dumps(tc['arguments'], ensure_ascii=False)
                    if isinstance(tc['arguments'], dict) else str(tc['arguments'])
                }
            })
        return out

# ---------- SSE buffer ----------
class HarmonyStreamState:
    def __init__(self):
        self.harmony_mode = False
        self.buf = ""

# ---------- Bridge ----------
class LMStudioBridge:
    def __init__(self, lm_studio_url='http://localhost:1234', port=8000, xml_mode=True):
        self.lm_studio_url = lm_studio_url.rstrip('/')
        self.port = port
        self.xml_mode = xml_mode
        self.req_id = 0

    # ---- HTTP handlers ----
    async def handle_chat_completions(self, request: web.Request) -> web.StreamResponse:
        self.req_id += 1
        rid = self.req_id
        try:
            body = await request.json()
        except Exception:
            return web.json_response({'error': {'message': 'Invalid JSON', 'type': 'bad_request'}}, status=400)
        stream = bool(body.get('stream', False))
        model = body.get('model', 'unknown')
        logger.info(f"[#{rid}] Request: model={model}, stream={stream}, mode={'XML' if self.xml_mode else 'JSON'}")
        timeout = ClientTimeout(total=600, connect=10)
        async with ClientSession(timeout=timeout) as session:
            async with session.post(f"{self.lm_studio_url}/v1/chat/completions",
                                   json=body, headers={'Content-Type': 'application/json'}) as lm_resp:
                if stream:
                    return await self._stream_transform(lm_resp, request, rid)
                else:
                    return await self._nonstream_transform(lm_resp, rid)

    async def handle_models(self, request: web.Request) -> web.Response:
        try:
            async with ClientSession() as session:
                async with session.get(f"{self.lm_studio_url}/v1/models") as r:
                    data = await r.json()
                    return web.json_response(data)
        except Exception as e:
            logger.error(f"Models fetch error: {e}")
            return web.json_response({'error': {'message': str(e), 'type': 'proxy_error'}}, status=502)

    # ---- Streaming transformer ----
    async def _stream_transform(self, lm_resp, request: web.Request, rid: int) -> web.StreamResponse:
        resp = web.StreamResponse()
        resp.headers['Content-Type'] = 'text/event-stream'
        resp.headers['Cache-Control'] = 'no-cache'
        resp.headers['Connection'] = 'keep-alive'
        await resp.prepare(request)

        st = HarmonyStreamState()

        try:
            async for raw_line in lm_resp.content:
                line = raw_line.decode('utf-8', errors='ignore').strip()
                if not line:
                    continue
                if line == 'data: [DONE]':
                    # final flush
                    for out_chunk in self._flush_harmony_buffer(st.buf):
                        await resp.write(f"data: {json.dumps(out_chunk, ensure_ascii=False)}\n\n".encode('utf-8'))
                    st.buf = ""
                    await resp.write(b"data: [DONE]\n\n")
                    break
                if not line.startswith('data: '):
                    continue

                # Robustly parse chunk; skip if malformed
                try:
                    chunk = json.loads(line[6:])
                except Exception:
                    continue

                choices = chunk.get('choices') or []
                delta = (choices[0].get('delta') if choices and isinstance(choices[0], dict) else {}) or {}
                content = delta.get('content', '')

                # Enter buffer mode on any Harmony token or when already in it
                if (content and HarmonyParser.has_harmony(content)) or st.harmony_mode:
                    st.harmony_mode = True
                    if content:
                        st.buf += content
                    out_chunks, st.buf = self._extract_ready_blocks(st.buf, base_chunk=chunk)
                    for out_chunk in out_chunks:
                        await resp.write(f"data: {json.dumps(out_chunk, ensure_ascii=False)}\n\n".encode('utf-8'))
                    continue

                # Pass-through non‑Harmony chunks unchanged
                await resp.write((line + "\n\n").encode('utf-8'))

        except Exception as e:
            logger.error(f"[#{rid}] Stream error: {e}")
        finally:
            try:
                await resp.write_eof()
            except Exception:
                pass
        return resp

    def _make_base_chunk_like(self, base_chunk: dict) -> dict:
        # Ensure OpenAI-like chunk structure
        out = {
            'id': base_chunk.get('id', 'chunk'),
            'object': base_chunk.get('object', 'chat.completion.chunk'),
            'created': base_chunk.get('created', int(datetime.now().timestamp())),
            'model': base_chunk.get('model', 'gpt-oss'),
            'choices': [{'index': 0, 'delta': {}, 'finish_reason': None}]
        }
        return out

    def _extract_ready_blocks(self, buf: str, base_chunk: dict) -> Tuple[List[dict], str]:
        outputs: List[dict] = []
        # Find all <|channel|> markers
        markers = [m.start() for m in re.finditer(r'<\|channel\|>', buf)]
        if len(markers) <= 1:
            return outputs, buf  # nothing complete yet

        for i in range(len(markers) - 1):
            start, end = markers[i], markers[i + 1]
            block = buf[start:end]
            parsed = HarmonyParser.parse_block(block)

            out_chunk = self._make_base_chunk_like(base_chunk)

            if parsed['tool_calls']:
                if self.xml_mode:
                    xml = XMLFormatter.tool_calls_to_xml(parsed['tool_calls'])
                    if xml.strip():
                        out_chunk['choices'][0]['delta'] = {'content': xml}
                        outputs.append(out_chunk)
                else:
                    tool_calls = XMLFormatter.tool_calls_to_openai(parsed['tool_calls'])
                    if tool_calls:
                        out_chunk['choices'][0]['delta'] = {'tool_calls': tool_calls}
                        outputs.append(out_chunk)
            elif parsed['final_message']:
                out_chunk['choices'][0]['delta'] = {'content': parsed['final_message']}
                outputs.append(out_chunk)
            # analysis/commentary without tool are dropped

        remaining = buf[markers[-1]:]
        return outputs, remaining

    def _flush_harmony_buffer(self, buf: str) -> List[dict]:
        outputs: List[dict] = []
        if not buf.strip():
            return outputs
        base_chunk = self._make_base_chunk_like({})
        parsed = HarmonyParser.parse_block(buf)
        if parsed['tool_calls']:
            if self.xml_mode:
                xml = XMLFormatter.tool_calls_to_xml(parsed['tool_calls'])
                if xml.strip():
                    base_chunk['choices'][0]['delta'] = {'content': xml}
                    outputs.append(copy.deepcopy(base_chunk))
            else:
                tool_calls = XMLFormatter.tool_calls_to_openai(parsed['tool_calls'])
                if tool_calls:
                    base_chunk['choices'][0]['delta'] = {'tool_calls': tool_calls}
                    outputs.append(copy.deepcopy(base_chunk))
        elif parsed['final_message']:
            base_chunk['choices'][0]['delta'] = {'content': parsed['final_message']}
            outputs.append(copy.deepcopy(base_chunk))
        return outputs

    # ---- Non‑stream transformer ----
    async def _nonstream_transform(self, lm_resp, rid: int) -> web.Response:
        try:
            data = await lm_resp.json()
        except Exception as e:
            logger.error(f"[#{rid}] Non‑stream JSON error: {e}")
            text = await lm_resp.text()
            return web.Response(text=text, content_type='application/json')

        try:
            choices = data.get('choices') or []
            choice = choices[0] if choices else {'index': 0, 'message': {}}
            message = choice.get('message') or {}
            raw = message.get('content') or ''

            if HarmonyParser.has_harmony(raw):
                parsed = HarmonyParser.parse_block(raw)
                if parsed['tool_calls']:
                    if self.xml_mode:
                        xml = XMLFormatter.tool_calls_to_xml(parsed['tool_calls'])
                        message['content'] = xml
                        message.pop('tool_calls', None)
                        choice['finish_reason'] = 'stop'
                    else:
                        message['content'] = None
                        message['tool_calls'] = XMLFormatter.tool_calls_to_openai(parsed['tool_calls'])
                        choice['finish_reason'] = 'tool_calls'
                else:
                    message['content'] = parsed['final_message']
                    choice['finish_reason'] = 'stop'
                choice['message'] = message
                if not choices:
                    data['choices'] = [choice]
                else:
                    data['choices'][0] = choice
            return web.json_response(data)
        except Exception as e:
            logger.error(f"[#{rid}] Non‑stream transform error: {e}", exc_info=True)
            return web.json_response({'error': {'message': str(e), 'type': 'proxy_error'}}, status=500)

    # ---- Server ----
    def print_banner(self):
        mode = "XML (Cline/Anthropic)" if self.xml_mode else "JSON (OpenAI tool_calls)"
        banner = f"""
╔══════════════════════════════════════════════════════════════╗
║        🌉  LM Studio Harmony Bridge Proxy v2.2  🌉          
╠══════════════════════════════════════════════════════════════╣
║  Converts Harmony → {mode:<35} 
╚══════════════════════════════════════════════════════════════╝

📡 Proxy:         http://localhost:{self.port}
🎯 LM Studio API: {self.lm_studio_url}
🔧 Endpoints:     /v1/*  and  /api/v0/*
"""
        print(banner)

    def run(self):
        self.print_banner()
        app = web.Application()
        # /v1
        app.router.add_post('/v1/chat/completions', self.handle_chat_completions)
        app.router.add_get('/v1/models', self.handle_models)
        # /api/v0 (Cline)
        app.router.add_post('/api/v0/chat/completions', self.handle_chat_completions)
        app.router.add_get('/api/v0/models', self.handle_models)

        logger.info("Server starting … Press CTRL+C to quit")
        web.run_app(app, host='0.0.0.0', port=self.port, print=None)

# ---------- CLI ----------
if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(
        description='LM Studio Harmony Bridge Proxy (Harmony → XML/JSON)'
    )
    parser.add_argument('--lm-studio-url', default='http://localhost:1234',
                        help='LM Studio API base URL (default: http://localhost:1234)')
    parser.add_argument('--port', type=int, default=8000,
                        help='Proxy HTTP port (default: 8000)')
    parser.add_argument('--format', choices=['xml', 'json'], default='xml',
                        help='Output format: xml (Cline) or json (OpenAI tool_calls)')
    parser.add_argument('--debug', action='store_true', help='Enable debug logging')
    args = parser.parse_args()
    if args.debug:
        logger.setLevel(logging.DEBUG)
    bridge = LMStudioBridge(
        lm_studio_url=args.lm_studio_url,
        port=args.port,
        xml_mode=(args.format == 'xml')
    )
    bridge.run()
