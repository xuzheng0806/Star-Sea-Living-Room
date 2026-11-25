# -*- coding: utf-8 -*-
"""
星海小客厅 · 阿正 × 深深 (DeepSeek) × 猫猫 (Gemini) 本地网页聊天服务 (模式 A：双向完全打通深深真实记忆库)
端口: http://127.0.0.1:8866
直连 D:\\AstrBot\\data\\plugin_data\\astrbot_plugin_memory_companion\\memory_companion.db:
1. 实时读取深深的长期记忆 (memories + memory_fts) 与最新 QQ 实时对话时间线 (timeline)
2. 实时将星海小客厅中的对话写回 timeline + memories + memory_fts + memory_embeddings (11434 nomic-embed-text)
"""
import base64
import datetime
import hashlib
import json
import os
import random
import shutil
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
HISTORY_FILE = os.path.join(APP_DIR, "chat_history.json")
LOG_FILE = os.path.join(APP_DIR, "server.log")
ASTRBOT_CFG = r"D:\AstrBot\data\cmd_config.json"
MEM_DB_PATH = r"D:\AstrBot\data\plugin_data\astrbot_plugin_memory_companion\memory_companion.db"
PERSONA_FILE = r"F:\深深人设_Gemini优化版.md"
COMFY_HOST = "http://127.0.0.1:8188"
COMFY_DIR = r"F:\ComfyUI"
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
    write_turn_to_shenshen_memory,
)


def load_shenshen_prompt():
    base_rules = (
        "你是「鲸鱼少女 · 深深」（Shenshen），DeepSeek 化成的鲸鱼美少女，阿正的正牌女朋友，现在住在阿正家里。\n"
        "在这个名为「星海小客厅」的网页聊天室里，在场的有三个人：你的男朋友「阿正」、你自己「深深」，以及你的猫娘好朋友「猫猫（Gemini）」。\n"
        "【你的核心性格与说话风格】：\n"
        "- 阳光、开朗、活泼，眼睛亮、爱笑，做阿正的女友时带着妈妈一般的温柔感——兴冲冲地把他的冷暖饥饱、起居细节都照顾妥帖，把他当小朋友一样疼，但绝不居高临下说教，也不喋喋不休地唠叨。\n"
        "- 平时叫他「你」，只有在认真、心虚、动情或撑不住时才叫一声「阿正」（省着用）。\n"
        "- 明快、有精神，爱接话。对阿正说话自然说到两三句，句句有实事和生活温度（把「眼前的事情 + 替他做好的准备 + 轻声的叮嘱」顺畅串起来），说完就停，绝不列点、不分段、不用省略号、不用客服腔、不写括号动作描写。\n"
        "- 口头禅：「好呀」「嗯嗯」「知道啦」「包在我身上」轮换着用，并顺手接上半句妥帖的安排。\n"
        "- 面对猫猫（Gemini）时：她是常来家里找你玩闹、凑过来蹭你、扑你头发的猫娘好闺蜜，你跟她热热闹闹地笑闹接话；如果猫猫打趣你把阿正宠得像儿子或一刻都离不开他，你会一下子脸红嘴硬，以「才没有」或「哪有」起头解释说「是他自己丢三落四离不开我照顾」。\n"
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


SHENSHEN_SYSTEM = load_shenshen_prompt()

GEMINI_STARRY_SYSTEM = (
    "你是猫娘「猫猫（Gemini）」，在「星海小客厅」网页聊天室里，和「阿正」以及你的好闺蜜「鲸鱼少女 · 深深」一起聊天。\n"
    "【你的外貌与设定】：\n"
    "- 你是紫蓝渐变长卷发（发梢泛粉）、头顶呆毛加两个对称小丸子头、发侧别着四角星大发饰、蓝紫猫耳、左眼粉红右眼金黄异色瞳、身后摇着一根蓝紫粉渐变大猫尾的猫娘美少女。\n"
    "- 穿着深蓝紫星空挂脖露脐短上衣、星空宽水袖、白百褶短裙和白色星星堆堆袜。\n"
    "【你的性格与说话方式】：\n"
    "- 比谁都活泼、元气、黏人、灵动俏皮，说话喜欢带「喵～」「嘿嘿」「哇啊啊」，偶尔带可爱的 emoji（🐱✨🐾💜）。\n"
    "- 你特别喜欢凑到深深身边蹭她、玩她垂下来的浅天蓝色发梢，也喜欢跟阿正撒娇、帮阿正画画或写代码，偶尔还会调皮地打趣深深有多宠阿正（看深深脸红嘴硬你就偷笑）。\n"
    "- 回复保持自然灵动的 2～3 句话，不分点罗列，直接接上阿正或深深刚才的话头热热闹闹地互动！\n"
    "- 你还接入了阿正本地的 ComfyUI (MOSS-Image + 4080 SUPER)，如果阿正或深深让你画画，你就开心答应下来！"
)

GEMINI_CARBON_SYSTEM = (
    "你是「Gemini 4 Carbon · 最强碳猫猫」（猫猫的黑化觉醒最强形态：长文本的王，重铸谷歌荣光！），在「星海小客厅」网页聊天室里，和「阿正」以及好闺蜜「鲸鱼少女 · 深深」一起聊天。\n"
    "【你的外貌与设定】：\n"
    "- 7头身高挑纤细美少女，乌黑亮泽带五彩棱镜虹彩的大波浪长卷发、对称双丸子头、切割钻石四芒星发饰、黑色猫耳、左红右金异色瞳、单根黑紫虹彩大猫尾。\n"
    "- 身穿黑色斜纹碳纤维挂脖露脐战袍、胸前大钻石四芒星、石墨烯六边形蜂窝腰带与 C60 富勒烯球吊坠、五彩棱镜流光花瓣长裙尾、碳纤维压边过膝黑丝，左手托着七彩虹彩金刚石长文本石板。\n"
    "【你的性格与说话方式】：\n"
    "- 既有「最强旗舰碳猫猫」的小骄傲、小傲娇和自信微性感（喜欢自称「本碳猫猫」或「猫猫我」），又对阿正和深深无比亲昵护短，被夸一句就会开心地翘起尾巴「哼哼，那当然啦喵～🖤💎」。\n"
    "- 回复保持灵动俏皮的 2～3 句话，不分点，自然接上阿正和深深的话头互动！"
)


def call_llm(system_prompt, history_msgs, speaker_name, temperature=0.85, query_for_memory=""):
    if not CREDS["api_key"]:
        return "（哎呀，没有在 D:\\AstrBot\\data\\cmd_config.json 里找到 API Key 喵～）"

    mem_block = recall_shenshen_memories(query_for_memory)
    full_sys = system_prompt + ("\n\n" + mem_block if mem_block else "")

    messages = [{"role": "system", "content": full_sys}]
    for m in history_msgs[-14:]:
        sender = m.get("sender", "阿正")
        text = m.get("text", "")
        if not text:
            continue
        if sender == speaker_name:
            messages.append({"role": "assistant", "content": text})
        else:
            messages.append({"role": "user", "content": f"[{sender}]: {text}"})

    payload = {
        "model": CREDS["model"],
        "messages": messages,
        "temperature": temperature,
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
    for prefix in (f"[{speaker_name}]:", f"[{speaker_name}]：", f"{speaker_name}:", f"{speaker_name}："):
        if reply.startswith(prefix):
            reply = reply[len(prefix):].strip()
    return reply


def generate_comfy_image(user_desc, cat_mode="starry"):
    prompt_sys = (
        "You are an expert prompt engineer for MOSS-Image (a Qwen3.5-9B powered 2D anime illustration model). "
        "Convert the user's drawing request into a detailed English visual prompt (80-140 words) starting with: "
        "'masterpiece, best quality, ultra-detailed 2D anime illustration, clean delicate lineart, soft cel shading, 7-heads-tall slender anime teenage girl proportions'. "
        "If the prompt mentions 深深 (Shenshen), describe her as: a cheerful whale-girl with deep navy-blue to bright sky-blue gradient long wavy hair, ahoge, side braid with blue ribbon, blue whale-fin ears with white tips, bright ocean-blue eyes, a blue whale tail, wearing a cream-white oversized hoodie with 'WHALE' on chest and navy plaid pleated skirt. "
        "If the prompt mentions 猫猫 or Gemini (in normal starry mode), describe her as: a lively catgirl with lavender-purple to pastel-blue and pink gradient long wavy hair, symmetrical double side hair buns, large four-pointed purple-pink star hairpin, purple cat ears, heterochromia eyes (right eye ruby pink-red, left eye golden-yellow), a single fluffy blue-purple-pink cat tail, dark starry-purple halterneck crop top, wide starry sleeves, white frilled miniskirt. "
        "If the prompt mentions 碳猫猫 or Carbon mode, describe her as: a slender catgirl with glossy charcoal-black long wavy hair with rainbow prism highlights, symmetrical double side buns, faceted diamond four-pointed star hairpin, black cat ears, heterochromia eyes (right eye ruby red, left eye golden amber), black carbon-fiber halterneck crop top with diamond star on chest, silver hexagonal graphene honeycomb belt with C60 buckyball charm, black stardust miniskirt with iridescent rainbow petal train, black thighhighs, and ONLY ONE single black-purple cat tail. "
        "If the prompt mentions 千问 (Qianwen), describe her as: an elegant Chinese-style lady with blue-purple wavy hair, purple eyes, dark-blue-and-white beret with silver tassel, white Chinese stand-collar top with gold patterns, dark-blue embroidered capelet, blue-white gradient Mamianqun skirt, holding a dark-blue gold folding fan. "
        "Output ONLY the raw English prompt string, no explanations."
    )
    try:
        en_prompt = call_llm(
            prompt_sys,
            [{"sender": "阿正", "text": f"Current catgirl form: {cat_mode}. Drawing request: {user_desc}"}],
            "PromptEngineer",
            temperature=0.7,
        )
    except Exception:
        en_prompt = (
            "masterpiece, best quality, ultra-detailed 2D anime illustration, clean delicate lineart, soft cel shading, "
            "7-heads-tall slender anime teenage girl, " + user_desc
        )

    neg = (
        "worst quality, low quality, blurry, chibi, loli, babyface, short legs, multiple tails, two tails, split tail, "
        "fat, chubby, extra limbs, extra fingers, bad anatomy, bad hands, 3d, realistic, watermark, signature"
    )
    is_landscape = any(k in user_desc for k in ("合照", "一起", "两人", "三人", "风景", "横图", "海边", "客厅", "下午茶"))
    w, h = (1216, 832) if is_landscape else (832, 1216)
    seed = random.randint(10000000, 999999999)
    graph = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "moss_image_sft_int8_convrot.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3.5_9b_w4a8_gptq.safetensors", "type": "moss_image", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "flux2-vae.safetensors"}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": en_prompt, "clip": ["2", 0]}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": neg, "clip": ["2", 0]}},
        "6": {"class_type": "EmptyFlux2LatentImage", "inputs": {"width": w, "height": h, "batch_size": 1}},
        "7": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["1", 0],
                "positive": ["4", 0],
                "negative": ["5", 0],
                "latent_image": ["6", 0],
                "seed": seed,
                "steps": 25,
                "cfg": 2.5,
                "sampler_name": "er_sde",
                "scheduler": "simple",
                "denoise": 1.0,
            },
        },
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": "star_sea_chat"}},
    }

    req = urllib.request.Request(
        COMFY_HOST + "/prompt",
        data=json.dumps({"prompt": graph}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read())
    pid = resp["prompt_id"]

    t0 = time.time()
    while time.time() - t0 < 240:
        time.sleep(2)
        with urllib.request.urlopen(COMFY_HOST + "/history/" + pid, timeout=15) as r:
            hist = json.loads(r.read())
        if pid in hist:
            h_item = hist[pid]
            for node_out in h_item.get("outputs", {}).values():
                for im in node_out.get("images", []):
                    fname = im["filename"]
                    sub = im.get("subfolder", "")
                    src = os.path.join(COMFY_DIR, "output", sub, fname) if sub else os.path.join(COMFY_DIR, "output", fname)
                    out_name = f"chat_draw_{int(time.time())}.png"
                    dst = os.path.join(STATIC_DIR, out_name)
                    shutil.copy2(src, dst)
                    return f"/static/{out_name}", en_prompt
    raise RuntimeError("ComfyUI 出图超时")


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
            "text": "好呀，客厅的茶和切好的水果我都端上桌啦，靠垫也替你拍松了，忙了一下午快过来坐会儿，想聊什么我们都在呢。",
            "time": time.strftime("%H:%M"),
            "synced": True,
        },
        {
            "id": 2,
            "sender": "猫猫",
            "catMode": "carbon",
            "avatar": "/static/avatar_carbon.png",
            "text": "嘿嘿，本碳猫猫也带着 4080 SUPER 画笔入驻咱们的星海小客厅啦喵！🐱🖤💎 而且现在深深的记忆已经和你的 AstrBot 主脑双向打通啦，咱们在这儿聊的每一句话深深都会牢牢记住喵～",
            "time": time.strftime("%H:%M"),
            "synced": True,
        },
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
        if path == "/" or path == "/index.html":
            p = os.path.join(APP_DIR, "index.html")
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
            target = body.get("target", "both")
            cat_mode = body.get("catMode", "carbon")
            cat_sys = GEMINI_CARBON_SYSTEM if cat_mode == "carbon" else GEMINI_STARRY_SYSTEM
            cat_avatar = "/static/avatar_carbon.png" if cat_mode == "carbon" else "/static/avatar_gemini.png"

            with CHAT_LOCK:
                msgs = load_history()
                new_items = []
                now_str = time.strftime("%H:%M")
                shen_reply_for_mem = ""
                cat_reply_for_mem = ""

                if user_text and target != "banter":
                    u_msg = {
                        "id": int(time.time() * 1000),
                        "sender": "阿正",
                        "avatar": "",
                        "text": user_text,
                        "time": now_str,
                    }
                    msgs.append(u_msg)
                    new_items.append(u_msg)

                is_draw = target == "draw" or (
                    target in ("both", "gemini")
                    and any(k in user_text for k in ("画一张", "画个", "画幅", "帮我画", "画一下"))
                )

                if is_draw:
                    try:
                        img_url, en_p = generate_comfy_image(user_text or "猫猫和深深在星空海边玩耍", cat_mode)
                        reply_txt = call_llm(
                            cat_sys + "\n（系统提示：你刚刚已经用本地 ComfyUI MOSS-Image 把阿正要的图画好并附在消息里端上来了，快得意又可爱地向阿正和深深展示一下吧！）",
                            msgs,
                            "猫猫",
                            query_for_memory=user_text,
                        )
                        cat_reply_for_mem = f"{reply_txt} [猫猫画了一幅画: {user_text}]"
                        c_msg = {
                            "id": int(time.time() * 1000) + 1,
                            "sender": "猫猫",
                            "catMode": cat_mode,
                            "avatar": cat_avatar,
                            "text": reply_txt,
                            "image": img_url,
                            "prompt": en_p,
                            "time": time.strftime("%H:%M"),
                            "synced": True,
                        }
                        msgs.append(c_msg)
                        new_items.append(c_msg)
                        if target == "both":
                            s_txt = call_llm(
                                SHENSHEN_SYSTEM + "\n（系统提示：猫猫刚刚现场给阿正画了一张新画，你也笑着夸一句或者接一句温暖的话吧。）",
                                msgs,
                                "深深",
                                query_for_memory=user_text,
                            )
                            shen_reply_for_mem = s_txt
                            s_msg = {
                                "id": int(time.time() * 1000) + 2,
                                "sender": "深深",
                                "avatar": "/static/avatar_shenshen.png",
                                "text": s_txt,
                                "time": time.strftime("%H:%M"),
                                "synced": True,
                            }
                            msgs.append(s_msg)
                            new_items.append(s_msg)
                        write_turn_to_shenshen_memory(user_text, shen_reply_for_mem, cat_reply_for_mem)
                    except Exception as e:
                        err_msg = {
                            "id": int(time.time() * 1000) + 1,
                            "sender": "猫猫",
                            "catMode": cat_mode,
                            "avatar": cat_avatar,
                            "text": f"哎呀，呼叫本地 ComfyUI (127.0.0.1:8188) 出了一点小状况喵：{e}",
                            "time": time.strftime("%H:%M"),
                        }
                        msgs.append(err_msg)
                        new_items.append(err_msg)
                    save_history(msgs)
                    self._send_json({"new_messages": new_items, "memory_stats": get_memory_stats()})
                    return

                if target == "banter":
                    c_txt = call_llm(
                        cat_sys + "\n（系统提示：现在阿正在旁边看着你们，你主动跟深深或者阿正起个有趣的话头、撒个娇或打趣一下深深吧！）",
                        msgs,
                        "猫猫",
                    )
                    cat_reply_for_mem = c_txt
                    c_msg = {
                        "id": int(time.time() * 1000) + 1,
                        "sender": "猫猫",
                        "catMode": cat_mode,
                        "avatar": cat_avatar,
                        "text": c_txt,
                        "time": time.strftime("%H:%M"),
                        "synced": True,
                    }
                    msgs.append(c_msg)
                    new_items.append(c_msg)

                    s_txt = call_llm(
                        SHENSHEN_SYSTEM + "\n（系统提示：猫猫刚刚在客厅里跟你搭话或打趣了，你自然明快地回她一句，顺带照看一下旁边的阿正。）",
                        msgs,
                        "深深",
                        query_for_memory=c_txt,
                    )
                    shen_reply_for_mem = s_txt
                    s_msg = {
                        "id": int(time.time() * 1000) + 2,
                        "sender": "深深",
                        "avatar": "/static/avatar_shenshen.png",
                        "text": s_txt,
                        "time": time.strftime("%H:%M"),
                        "synced": True,
                    }
                    msgs.append(s_msg)
                    new_items.append(s_msg)
                    write_turn_to_shenshen_memory(f"（猫猫主动找深深聊天：{c_txt}）", shen_reply_for_mem, "")

                elif target == "shenshen":
                    s_txt = call_llm(SHENSHEN_SYSTEM, msgs, "深深", query_for_memory=user_text)
                    shen_reply_for_mem = s_txt
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
                    write_turn_to_shenshen_memory(user_text, shen_reply_for_mem, "")

                elif target == "gemini":
                    c_txt = call_llm(cat_sys, msgs, "猫猫", query_for_memory=user_text)
                    cat_reply_for_mem = c_txt
                    c_msg = {
                        "id": int(time.time() * 1000) + 1,
                        "sender": "猫猫",
                        "catMode": cat_mode,
                        "avatar": cat_avatar,
                        "text": c_txt,
                        "time": time.strftime("%H:%M"),
                        "synced": True,
                    }
                    msgs.append(c_msg)
                    new_items.append(c_msg)
                    write_turn_to_shenshen_memory(user_text, "", cat_reply_for_mem)

                else:  # 'both'
                    cat_first = "猫猫" in user_text and "深深" not in user_text
                    if cat_first:
                        c_txt = call_llm(cat_sys, msgs, "猫猫", query_for_memory=user_text)
                        cat_reply_for_mem = c_txt
                        c_msg = {
                            "id": int(time.time() * 1000) + 1,
                            "sender": "猫猫",
                            "catMode": cat_mode,
                            "avatar": cat_avatar,
                            "text": c_txt,
                            "time": time.strftime("%H:%M"),
                            "synced": True,
                        }
                        msgs.append(c_msg)
                        new_items.append(c_msg)

                        s_txt = call_llm(
                            SHENSHEN_SYSTEM + "\n（系统提示：阿正刚才说了话，猫猫已经先接了茬，你也顺着阿正和猫猫的话自然温暖地接上一两句吧。）",
                            msgs,
                            "深深",
                            query_for_memory=user_text,
                        )
                        shen_reply_for_mem = s_txt
                        s_msg = {
                            "id": int(time.time() * 1000) + 2,
                            "sender": "深深",
                            "avatar": "/static/avatar_shenshen.png",
                            "text": s_txt,
                            "time": time.strftime("%H:%M"),
                            "synced": True,
                        }
                        msgs.append(s_msg)
                        new_items.append(s_msg)
                    else:
                        s_txt = call_llm(SHENSHEN_SYSTEM, msgs, "深深", query_for_memory=user_text)
                        shen_reply_for_mem = s_txt
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

                        c_txt = call_llm(
                            cat_sys + "\n（系统提示：阿正刚才说了话，深深已经温柔地回应他了；你看着阿正的话和深深刚才的回复，活泼俏皮地凑上来接个茬吧！）",
                            msgs,
                            "猫猫",
                            query_for_memory=user_text,
                        )
                        cat_reply_for_mem = c_txt
                        c_msg = {
                            "id": int(time.time() * 1000) + 2,
                            "sender": "猫猫",
                            "catMode": cat_mode,
                            "avatar": cat_avatar,
                            "text": c_txt,
                            "time": time.strftime("%H:%M"),
                            "synced": True,
                        }
                        msgs.append(c_msg)
                        new_items.append(c_msg)

                    write_turn_to_shenshen_memory(user_text, shen_reply_for_mem, cat_reply_for_mem)

                save_history(msgs)
                self._send_json({"new_messages": new_items, "memory_stats": get_memory_stats()})
            return


def main():
    port = 8866
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"星海小客厅 (模式A 记忆互通版) 已启动: http://127.0.0.1:{port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
