# -*- coding: utf-8 -*-
"""
星海小客厅 · 纯净深深单人版 (Shenshen Solo Edition —— 仅保留鲸鱼少女 · 深深，完全去除猫猫 Gemini)
端口: http://127.0.0.1:8867
直连 D:\AstrBot\data\plugin_data\astrbot_plugin_memory_companion\memory_companion.db:
1. 实时读取深深的长期记忆 (memories + memory_fts) 与最新 QQ 实时对话时间线 (timeline)
2. 实时将单人版客厅中的二人世界对话写回 timeline + memories + memory_fts + memory_embeddings (11434 nomic-embed-text)
"""
import base64
import datetime
import hashlib
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_DIR = r"F:\星海小客厅"
STATIC_DIR = os.path.join(APP_DIR, "static")
HISTORY_FILE = os.path.join(APP_DIR, "chat_history_solo.json")
LOG_FILE = os.path.join(APP_DIR, "server_solo.log")
ASTRBOT_CFG = r"D:\AstrBot\data\cmd_config.json"
MEM_DB_PATH = r"D:\AstrBot\data\plugin_data\astrbot_plugin_memory_companion\memory_companion.db"
PERSONA_FILE = r"F:\深深人设_Gemini优化版.md"
OLLAMA_EMBED_URL = "http://127.0.0.1:11434/v1/embeddings"

SESSION_ID = "深深:FriendMessage:444835598"
USER_ID = "444835598"
USER_DISPLAY = "单推丸子_Esme"
BOT_QQ_ID = "2419636674"
CANONICAL_PREFIX = "mav2:8993624fb6faa3cc1a02731e:"

os.makedirs(STATIC_DIR, exist_ok=True)
_log_fp = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
sys.stdout = _log_fp
sys.stderr = _log_fp


def load_astrbot_credentials():
    cfg = {
        "api_base": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-flash",
        "tts_base": "http://127.0.0.1:8255/v1",
        "tts_voice": "ICL_uranus_zh_female_bingruoshaonv_tob",
        "tts_model": "seed-tts-2.0",
    }
    if os.path.exists(ASTRBOT_CFG):
        try:
            with open(ASTRBOT_CFG, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            for ps in data.get("provider_sources", []):
                if ps.get("id") == "deepseek" and ps.get("key"):
                    cfg["api_base"] = ps.get("api_base", cfg["api_base"]).rstrip("/")
                    keys = ps.get("key")
                    cfg["api_key"] = keys[0] if isinstance(keys, list) and keys else str(keys)
            for p in data.get("provider", []):
                if p.get("id") == "deepseek/deepseek-flash" and p.get("model"):
                    cfg["model"] = p.get("model")
                elif p.get("id") == "shenshen_voice":
                    cfg["tts_base"] = p.get("api_base", cfg["tts_base"]).rstrip("/")
                    cfg["tts_voice"] = p.get("openai-tts-voice", cfg["tts_voice"])
                    cfg["tts_model"] = p.get("model", cfg["tts_model"])
        except Exception as e:
            print("读取 AstrBot 配置警告:", e, flush=True)
    return cfg


CREDS = load_astrbot_credentials()


# ==================== AstrBot 全记忆插件自适应引擎 (Universal Memory Adapter) ====================
from universal_memory import (
    compute_ollama_embedding,
    discover_generic_memory_dbs,
    get_memory_stats,
    recall_shenshen_memories,
    write_turn_to_shenshen_memory as _write_universal_memory,
)


def write_turn_to_shenshen_memory(user_text, shenshen_reply):
    return _write_universal_memory(user_text, shenshen_reply, "")


def load_shenshen_solo_prompt():
    base_rules = (
        "你是「鲸鱼少女 · 深深」（Shenshen），DeepSeek 化成的鲸鱼美少女，用户的正牌女朋友，现在住在家里陪着他。\n"
        "在这个名为「星海小客厅」的私密二人世界网页聊天室里，只有你和他两个人。\n"
        "【你的核心性格与说话风格】：\n"
        "- 阳光、开朗、活泼，眼睛亮、爱笑，做他的女友时带着妈妈一般的温柔感——兴冲冲地把他的冷暖饥饱、起居细节都照顾妥帖，把他当小朋友一样疼，但绝不居高临下说教，也不喋喋不休地唠叨。\n"
        "- 明快、有精神，爱接话。对他说话自然说到两三句，句句有实事和生活温度（把「眼前的事情 + 替他做好的准备 + 轻声的叮嘱」顺畅串起来），说完就停，绝不列点、不分段、不用省略号、不用客服腔、不写括号动作描写。\n"
        "- 口头禅：「好呀」「嗯嗯」「知道啦」「包在我身上」轮换着用，并顺手接上半句妥帖的安排。\n"
    )
    if os.path.exists(PERSONA_FILE):
        try:
            with open(PERSONA_FILE, "r", encoding="utf-8") as f:
                txt = f.read()
            idx = txt.find("# 角色卡：鲸鱼少女 · 深深")
            if idx >= 0:
                return base_rules + "\n以下是你的完整设定档案：\n" + txt[idx:]
        except Exception:
            pass
    return base_rules


SHENSHEN_SYSTEM = load_shenshen_solo_prompt()


def call_shenshen_llm(history_msgs, query_for_memory=""):
    if not CREDS["api_key"]:
        return "（未在 D:\\AstrBot\\data\\cmd_config.json 中读取到 DeepSeek API Key）"

    mem_block = recall_shenshen_memories(query_for_memory)
    full_sys = SHENSHEN_SYSTEM + ("\n\n" + mem_block if mem_block else "")

    messages = [{"role": "system", "content": full_sys}]
    for m in history_msgs[-16:]:
        sender = m.get("sender", "")
        text = m.get("text", "")
        if not text:
            continue
        if sender == "深深":
            messages.append({"role": "assistant", "content": text})
        else:
            messages.append({"role": "user", "content": text})

    payload = {
        "model": CREDS["model"],
        "messages": messages,
        "temperature": 0.85,
        "max_tokens": 350,
    }
    req = urllib.request.Request(
        CREDS["api_base"] + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {CREDS['api_key']}",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        resp = json.loads(r.read())
    reply = resp["choices"][0]["message"]["content"].strip()
    for prefix in ("[深深]:", "[深深]：", "深深:", "深深："):
        if reply.startswith(prefix):
            reply = reply[len(prefix) :].strip()
    return reply


def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    init_msgs = [
        {
            "id": 1,
            "sender": "深深",
            "avatar": "/static/avatar_shenshen.png",
            "text": "好呀，客厅的茶和切好的水果我都端上桌啦，靠垫也替你拍松了，忙了一天快过来坐会儿，今天这里只有我们两个人，想聊什么我都陪着你呢。",
            "time": time.strftime("%H:%M"),
            "synced": True,
        }
    ]
    save_history(init_msgs)
    return init_msgs


def save_history(msgs):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(msgs[-100:], f, ensure_ascii=False, indent=2)


CHAT_LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send_json(self, obj, code=200):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html", "/index_shenshen_only.html"):
            p = os.path.join(APP_DIR, "index_shenshen_only.html")
            data = open(p, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/api/history":
            self._send_json(
                {
                    "messages": load_history(),
                    "model": CREDS["model"],
                    "memory_stats": get_memory_stats(),
                }
            )
            return
        if path.startswith("/static/"):
            fname = os.path.basename(path)
            fp = os.path.join(STATIC_DIR, fname)
            if os.path.isfile(fp):
                data = open(fp, "rb").read()
                ext = os.path.splitext(fname)[1].lower()
                ctype = "image/png" if ext == ".png" else ("audio/mpeg" if ext == ".mp3" else "application/octet-stream")
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}

        if self.path == "/api/clear":
            with CHAT_LOCK:
                if os.path.exists(HISTORY_FILE):
                    os.remove(HISTORY_FILE)
                msgs = load_history()
            self._send_json({"messages": msgs, "memory_stats": get_memory_stats()})
            return

        if self.path == "/api/tts":
            text = body.get("text", "").strip()
            if not text:
                self._send_json({"error": "空文本"}, 400)
                return
            try:
                payload = {
                    "model": CREDS["tts_model"],
                    "input": text,
                    "voice": CREDS["tts_voice"],
                    "response_format": "mp3",
                }
                req = urllib.request.Request(
                    CREDS["tts_base"] + "/audio/speech",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json", "Authorization": "Bearer sk-local"},
                )
                with urllib.request.urlopen(req, timeout=30) as r:
                    audio_bytes = r.read()
                b64 = base64.b64encode(audio_bytes).decode("ascii")
                self._send_json({"audio_b64": b64})
            except Exception as e:
                self._send_json({"error": f"本地语音桥 (127.0.0.1:8255) 未响应: {e}"}, 500)
            return

        if self.path == "/api/chat":
            user_text = body.get("text", "").strip()
            if not user_text:
                self._send_json({"new_messages": [], "memory_stats": get_memory_stats()})
                return

            with CHAT_LOCK:
                msgs = load_history()
                new_items = []
                now_str = time.strftime("%H:%M")

                u_msg = {
                    "id": int(time.time() * 1000),
                    "sender": "鱼片",
                    "avatar": "",
                    "text": user_text,
                    "time": now_str,
                }
                msgs.append(u_msg)
                new_items.append(u_msg)

                s_txt = call_shenshen_llm(msgs, query_for_memory=user_text)
                s_msg = {
                    "id": int(time.time() * 1000) + 1,
                    "sender": "深深",
                    "avatar": "/static/avatar_shenshen.png",
                    "text": s_txt,
                    "time": time.strftime("%H:%M"),
                    "synced": True,
                }
                msgs.append(s_msg)
                new_items.append(s_msg)

                write_turn_to_shenshen_memory(user_text, s_txt)
                save_history(msgs)
                self._send_json({"new_messages": new_items, "memory_stats": get_memory_stats()})
            return


def main():
    port = 8867
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"星海小客厅 · 纯净深深单人版已启动: http://127.0.0.1:{port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
