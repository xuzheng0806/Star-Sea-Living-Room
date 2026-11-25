# -*- coding: utf-8 -*-
"""
AstrBot 全记忆插件自适应桥接引擎 (Universal Memory Adapter for AstrBot)
无需启动 NapCat / QQ / AstrBot 主进程，在本地硬盘级自动发现并双向打通：
  [Adapter 1] 《我永远陪伴你 (astrbot_plugin_memory_companion)》: memory_companion.db (47列 memories + timeline + memory_fts + Ollama 768维向量)
  [Adapter 2] 《QQMind 日记与人设记忆库 (astrbot_plugin_qqmind)》: qqmind_diaries/<QQ号>.db (entries + fts 二元分词) & qqmind_persona_packs/<角色>.db (chapters)
  [Adapter 3] 《私聊伴侣档案库 (astrbot_plugin_private_companion)》: persona_profiles/*.db (store_sections JSON)
  [Adapter 4] 《第三方通用 SQLite 记忆插件自适应嗅探器 (Universal Schema Sniffer)》: 自动扫描 plugin_data/**/*.db 中含 memory/memories/messages/history/records/entries 的表结构并读写
  [Adapter 5] 《AstrBot 官方原生核心数据库 (data_v4.db)》: conversations 表 (原生多轮对话历史 JSON) + personas 表 (原生人设)
"""
import datetime
import glob
import hashlib
import json
import os
import sqlite3
import time
import urllib.request
import uuid

ASTRBOT_DATA_DIR = r"D:\AstrBot\data"
PLUGIN_DATA_DIR = os.path.join(ASTRBOT_DATA_DIR, "plugin_data")
MEM_COMPANION_DB = os.path.join(PLUGIN_DATA_DIR, "astrbot_plugin_memory_companion", "memory_companion.db")
QQMIND_DIR = os.path.join(PLUGIN_DATA_DIR, "astrbot_plugin_qqmind")
CORE_DATA_DB = os.path.join(ASTRBOT_DATA_DIR, "data_v4.db")
OLLAMA_EMBED_URL = "http://127.0.0.1:11434/v1/embeddings"

SESSION_ID = "深深:FriendMessage:444835598"
USER_ID = "444835598"
USER_DISPLAY = "单推丸子_Esme"
BOT_QQ_ID = "2419636674"
CANONICAL_PREFIX = "mav2:8993624fb6faa3cc1a02731e:"

# 明确排除的非记忆类辅助库
EXCLUDED_DB_NAMES = {
    "cost.db",
    "kb.db",
    "req041_migration_control.db",
    "req041_migration_outbox.db",
    "req041_relationship.db",
    "req041_scoped.db",
    "companions.db",
}


def make_cjk_bigrams(text):
    """生成与 QQMind 兼容的二元分词 (grams)，用于 FTS 索引。"""
    clean = "".join(c for c in text if not c.isspace())
    if len(clean) < 2:
        return clean
    return " ".join(clean[i : i + 2] for i in range(len(clean) - 1))


def compute_ollama_embedding(text):
    """调用本地 127.0.0.1:11434 nomic-embed-text:latest 生成 768 维向量（在开启 SQLite 写事务前调用，实现无锁落库）。"""
    try:
        payload = {"model": "nomic-embed-text:latest", "input": text}
        req = urllib.request.Request(
            OLLAMA_EMBED_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            resp = json.loads(r.read())
        return resp["data"][0]["embedding"]
    except Exception:
        return None


def discover_generic_memory_dbs():
    r"""
    [Adapter 4] 自动扫描 D:\AstrBot\data\plugin_data\ 下所有其他第三方记忆插件的 SQLite 数据库。
    通过 PRAGMA table_info 动态识别包含文本内容列 (content/memory/summary/text/message) 的记忆表。
    """
    found = []
    if not os.path.isdir(PLUGIN_DATA_DIR):
        return found
    for root, _, files in os.walk(PLUGIN_DATA_DIR):
        if "backup" in root.lower() or "req041_backups" in root.lower():
            continue
        for fname in files:
            if not fname.endswith(".db") or ".backup." in fname or fname in EXCLUDED_DB_NAMES:
                continue
            full_p = os.path.abspath(os.path.join(root, fname))
            if os.path.abspath(MEM_COMPANION_DB) == full_p:
                continue
            if os.path.abspath(QQMIND_DIR) in full_p:
                continue
            try:
                con = sqlite3.connect(full_p, timeout=3)
                tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
                for t in tables:
                    tl = t.lower()
                    if any(k in tl for k in ("fts", "sqlite_", "meta", "config", "embedding", "vector", "stat")):
                        continue
                    if any(k in tl for k in ("memor", "chat", "message", "history", "diary", "entry", "entries", "record", "log", "summary", "dialog")):
                        cols = [c[1] for c in con.execute(f"PRAGMA table_info('{t}')").fetchall()]
                        text_col = next((c for c in cols if c.lower() in ("content", "memory", "summary", "text", "message", "value")), None)
                        if text_col:
                            cnt = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                            plugin_name = os.path.basename(os.path.dirname(full_p))
                            found.append({
                                "plugin": plugin_name,
                                "db_path": full_p,
                                "table": t,
                                "cols": cols,
                                "text_col": text_col,
                                "count": cnt,
                            })
                con.close()
            except Exception:
                pass
    return found


def get_memory_stats():
    """汇总当前已自动挂载的所有记忆插件与核心库状态。"""
    adapters = []
    total_memories = 0
    total_timeline = 0
    latest_summary = ""

    # 1. 检测《我永远陪伴你 (memory_companion.db)》
    if os.path.exists(MEM_COMPANION_DB):
        try:
            con = sqlite3.connect(MEM_COMPANION_DB, timeout=8)
            m_cnt = con.execute("SELECT COUNT(*) FROM memories WHERE validity_status='active'").fetchone()[0]
            t_cnt = con.execute("SELECT COUNT(*) FROM timeline").fetchone()[0]
            row = con.execute(
                "SELECT content FROM memories WHERE session_id=? AND memory_type='conversation_summary' ORDER BY created_at DESC LIMIT 1",
                (SESSION_ID,),
            ).fetchone()
            con.close()
            total_memories += m_cnt
            total_timeline += t_cnt
            if row:
                latest_summary = row[0]
            adapters.append(f"memory_companion({m_cnt}条)")
        except Exception:
            pass

    # 2. 检测《QQMind 日记与人设记忆库 (astrbot_plugin_qqmind)》
    qqmind_diary_db = os.path.join(QQMIND_DIR, "qqmind_diaries", f"{USER_ID}.db")
    if not os.path.exists(qqmind_diary_db):
        cand = glob.glob(os.path.join(QQMIND_DIR, "qqmind_diaries", "*.db"))
        qqmind_diary_db = cand[0] if cand else ""
    if qqmind_diary_db and os.path.exists(qqmind_diary_db):
        try:
            con = sqlite3.connect(qqmind_diary_db, timeout=5)
            d_cnt = con.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            con.close()
            total_memories += d_cnt
            adapters.append(f"qqmind_diary({d_cnt}篇)")
        except Exception:
            pass

    # 3. 检测其他第三方通用 SQLite 记忆插件
    for g in discover_generic_memory_dbs():
        total_memories += g["count"]
        adapters.append(f"{g['plugin']}({g['count']}条)")

    # 4. 检测 AstrBot 官方原生核心库 (data_v4.db)
    if os.path.exists(CORE_DATA_DB):
        try:
            con = sqlite3.connect(CORE_DATA_DB, timeout=5)
            c_cnt = con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
            con.close()
            adapters.append(f"astrbot_core({c_cnt}会话)")
        except Exception:
            pass

    return {
        "connected": len(adapters) > 0,
        "memories": total_memories,
        "timeline": total_timeline,
        "adapters": adapters,
        "adapter_label": " + ".join(adapters) if adapters else "本地 JSON 独立记忆",
        "latest_summary": latest_summary,
    }


def recall_shenshen_memories(query_text=""):
    """
    从所有已识别的记忆插件与 AstrBot 原生库中联合召回深深的记忆。
    """
    parts = []
    keywords = [w for w in query_text.replace("，", " ").replace("。", " ").split() if len(w) >= 2][:4]
    if not keywords and len(query_text) >= 2:
        keywords = [query_text[i : i + 2] for i in range(0, min(len(query_text) - 1, 8), 2)]

    # ==================== [Adapter 1] memory_companion.db ====================
    if os.path.exists(MEM_COMPANION_DB):
        try:
            con = sqlite3.connect(MEM_COMPANION_DB, timeout=8)
            summaries = con.execute(
                "SELECT occurred_at, content FROM memories WHERE session_id=? AND validity_status='active' "
                "AND memory_type='conversation_summary' ORDER BY created_at DESC LIMIT 5",
                (SESSION_ID,),
            ).fetchall()
            if not summaries:
                summaries = con.execute(
                    "SELECT occurred_at, content FROM memories WHERE validity_status='active' "
                    "AND memory_type='conversation_summary' ORDER BY created_at DESC LIMIT 5"
                ).fetchall()
            if summaries:
                parts.append("【长期记忆库 (memory_companion) 最近共同记忆摘要】：")
                for occ, content in reversed(summaries):
                    parts.append(f"- ({str(occ)[:10]}) {content}")

            matched = []
            for kw in keywords:
                rows = con.execute(
                    "SELECT content FROM memories WHERE validity_status='active' "
                    "AND memory_type NOT IN ('image_observation') AND content LIKE ? ORDER BY importance DESC, created_at DESC LIMIT 3",
                    (f"%{kw}%",),
                ).fetchall()
                for (c,) in rows:
                    if c not in matched and all(c != s[1] for s in summaries):
                        matched.append(c)
            if matched:
                parts.append("\n【被当前话题唤醒的过往记忆 (memory_companion)】：")
                for c in matched[:5]:
                    parts.append(f"- {c}")

            tl_rows = con.execute(
                "SELECT event_type, content FROM timeline WHERE session_id=? "
                "AND event_type IN ('user_message', 'bot_response') ORDER BY created_at DESC LIMIT 10",
                (SESSION_ID,),
            ).fetchall()
            if tl_rows:
                parts.append("\n【最近跨端实时对话时间线 (timeline)】：")
                for etype, content in reversed(tl_rows):
                    who = "鱼片" if etype == "user_message" else "深深"
                    parts.append(f"- {who}: {content}")
            con.close()
        except Exception as e:
            print("Adapter 1 (memory_companion) 召回警告:", e, flush=True)

    # ==================== [Adapter 2] astrbot_plugin_qqmind ====================
    qqmind_diary_db = os.path.join(QQMIND_DIR, "qqmind_diaries", f"{USER_ID}.db")
    if not os.path.exists(qqmind_diary_db):
        cand = glob.glob(os.path.join(QQMIND_DIR, "qqmind_diaries", "*.db"))
        qqmind_diary_db = cand[0] if cand else ""
    if qqmind_diary_db and os.path.exists(qqmind_diary_db):
        try:
            con = sqlite3.connect(qqmind_diary_db, timeout=5)
            d_rows = con.execute("SELECT title, summary FROM entries ORDER BY id DESC LIMIT 3").fetchall()
            kw_rows = []
            for kw in keywords:
                r = con.execute(
                    "SELECT title, summary FROM entries WHERE summary LIKE ? OR content LIKE ? OR title LIKE ? ORDER BY id DESC LIMIT 2",
                    (f"%{kw}%", f"%{kw}%", f"%{kw}%"),
                ).fetchall()
                for item in r:
                    if item not in d_rows and item not in kw_rows:
                        kw_rows.append(item)
            con.close()
            combined_diaries = list(reversed(d_rows)) + kw_rows[:2]
            if combined_diaries:
                parts.append("\n【QQMind 专属回忆日记 (qqmind_diaries)】：")
                for title, summary in combined_diaries:
                    parts.append(f"- 《{title}》：{summary}")
        except Exception as e:
            print("Adapter 2 (qqmind) 召回警告:", e, flush=True)

    # ==================== [Adapter 4] 通用第三方 SQLite 记忆插件嗅探器 ====================
    for g in discover_generic_memory_dbs():
        try:
            con = sqlite3.connect(g["db_path"], timeout=5)
            t = g["table"]
            tc = g["text_col"]
            rows = con.execute(f'SELECT "{tc}" FROM "{t}" ORDER BY rowid DESC LIMIT 5').fetchall()
            for kw in keywords[:2]:
                k_rows = con.execute(f'SELECT "{tc}" FROM "{t}" WHERE "{tc}" LIKE ? ORDER BY rowid DESC LIMIT 2', (f"%{kw}%",)).fetchall()
                for kr in k_rows:
                    if kr not in rows:
                        rows.append(kr)
            con.close()
            if rows:
                parts.append(f"\n【第三方记忆插件 ({g['plugin']}) 召回记录】：")
                for (txt,) in rows[:6]:
                    if txt and str(txt).strip():
                        parts.append(f"- {str(txt).strip()[:220]}")
        except Exception as e:
            print(f"Adapter 4 ({g['plugin']}) 召回警告:", e, flush=True)

    # ==================== [Adapter 5] AstrBot 原生核心库 (data_v4.db) ====================
    if not parts and os.path.exists(CORE_DATA_DB):
        try:
            con = sqlite3.connect(CORE_DATA_DB, timeout=5)
            rows = con.execute("SELECT user_id, content FROM conversations ORDER BY updated_at DESC LIMIT 2").fetchall()
            con.close()
            for uid, raw_json in rows:
                msgs = json.loads(raw_json)
                if isinstance(msgs, list) and msgs:
                    parts.append(f"\n【AstrBot 原生会话历史 ({uid})】：")
                    for m in msgs[-6:]:
                        role = m.get("role", "")
                        c = m.get("content", "")
                        if isinstance(c, list):
                            c = " ".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
                        parts.append(f"- {role}: {str(c)[:150]}")
        except Exception:
            pass

    return "\n".join(parts)


def write_turn_to_shenshen_memory(user_text, shenshen_reply, gemini_reply=""):
    """
    将一轮新对话同步写入所有当前已激活的记忆插件数据库中：
      1. 若存在 memory_companion.db -> 写入 timeline + memories (47列) + memory_fts + memory_embeddings
      2. 若存在 qqmind_diaries/<QQ号>.db -> 写入 entries 日记表 + fts 二元分词表
      3. 若存在其他通用 SQLite 记忆插件表 -> 自动按列结构反射插入新记忆行
    """
    now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    now_local = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    ts_float = time.time()
    msg_id = f"starsea_{int(ts_float * 1000)}"

    cat_part = f"；猫猫（Gemini）在旁边接话说：「{gemini_reply}」" if gemini_reply else ""
    shen_part = f"，我（深深）回应说：「{shenshen_reply}」" if shenshen_reply else ""
    mem_content = f"{now_local} 在星海小客厅网页聊天室里，{USER_DISPLAY}说：「{user_text}」{shen_part}{cat_part}。"
    evidence_text = f"{user_text} {shenshen_reply} {gemini_reply}".strip()

    wrote_any = False

    # ==================== [Adapter 1] 写入 memory_companion.db ====================
    if os.path.exists(MEM_COMPANION_DB):
        try:
            vec = compute_ollama_embedding(mem_content)
            con = sqlite3.connect(MEM_COMPANION_DB, timeout=15)

            if user_text:
                tl_user_id = f"tl_{uuid.uuid4().hex[:16]}"
                u_meta = json.dumps(
                    {
                        "memory_id": "",
                        "sender_name": USER_DISPLAY,
                        "message_id": msg_id,
                        "source": "llm_request",
                        "conversation_memory": False,
                        "owner_bot_id": BOT_QQ_ID,
                        "platform": "aiocqhttp",
                        "participant_user_id": USER_ID,
                        "participant_user_name": USER_DISPLAY,
                        "source_group_id": "",
                        "source_group_name": "",
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                u_dedupe = hashlib.sha1(f"{SESSION_ID}|user_message|{msg_id}|{user_text}".encode("utf-8")).hexdigest()
                con.execute(
                    "INSERT INTO timeline (id, event_type, session_id, scope, subject_id, object_id, content, metadata, "
                    "message_id, dedupe_key, occurred_at, created_at, summarized_at, retention_class, import_batch_id, source_sequence) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        tl_user_id,
                        "user_message",
                        SESSION_ID,
                        "private",
                        USER_ID,
                        USER_ID,
                        user_text,
                        u_meta,
                        msg_id,
                        u_dedupe,
                        now_utc,
                        now_utc,
                        "",
                        "normal",
                        "",
                        0,
                    ),
                )

            if shenshen_reply:
                tl_bot_id = f"tl_{uuid.uuid4().hex[:16]}"
                b_msg_id = f"private_companion_delivered_{msg_id}"
                b_meta = json.dumps(
                    {
                        "clean_visible_text": shenshen_reply,
                        "delivery_confirmed": True,
                        "conversation_turn": "passive_reply",
                        "source": "private_companion_confirmed_reply",
                        "owner_bot_id": "self",
                        "sender_name": USER_DISPLAY,
                        "message_id": b_msg_id,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                b_dedupe = hashlib.sha1(f"{SESSION_ID}|bot_response|{b_msg_id}|{shenshen_reply}".encode("utf-8")).hexdigest()
                con.execute(
                    "INSERT INTO timeline (id, event_type, session_id, scope, subject_id, object_id, content, metadata, "
                    "message_id, dedupe_key, occurred_at, created_at, summarized_at, retention_class, import_batch_id, source_sequence) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        tl_bot_id,
                        "bot_response",
                        SESSION_ID,
                        "private",
                        "self",
                        USER_ID,
                        shenshen_reply,
                        b_meta,
                        b_msg_id,
                        b_dedupe,
                        now_utc,
                        now_utc,
                        "",
                        "normal",
                        "",
                        0,
                    ),
                )

            mem_id = f"mem_{uuid.uuid4().hex[:20]}"
            c_key = CANONICAL_PREFIX + hashlib.sha1(mem_content.encode("utf-8")).hexdigest()
            c_fp = CANONICAL_PREFIX + hashlib.sha1(evidence_text.encode("utf-8")).hexdigest()
            tags_json = json.dumps(["summary", "long_term", "private", "星海小客厅"], ensure_ascii=False)
            meta_json = json.dumps(
                {
                    "source": "star_sea_living_room",
                    "start_at": now_utc,
                    "end_at": now_utc,
                    "with_gemini": bool(gemini_reply),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )

            con.execute(
                "INSERT INTO memories ("
                "id, memory_type, subject_kind, subject_id, subject_name, subject_role, "
                "object_kind, object_id, object_name, object_role, scope, session_id, platform, "
                "message_id, group_id, visibility, sayability, reality_level, lifecycle, "
                "content, evidence, confidence, importance, owner_bot_id, validity_status, "
                "valid_from, valid_to, salience, durability, sensitivity, reinforcement_score, "
                "injection_count, last_injected_at, canonical_key, review_status, tags, metadata, "
                "created_at, updated_at, occurred_at, last_accessed_at, access_count, "
                "source_plugin, import_batch_id, content_fingerprint, merged_count, supersedes_id"
                ") VALUES (" + ",".join(["?"] * 47) + ")",
                (
                    mem_id,
                    "conversation_summary",
                    "user",
                    USER_ID,
                    USER_DISPLAY,
                    "conversation_partner",
                    "bot",
                    "self",
                    "Bot",
                    "bot_self",
                    "private",
                    SESSION_ID,
                    "深深",
                    "",
                    "",
                    "private_pair",
                    "direct",
                    "llm_summary",
                    "stable_memory",
                    mem_content,
                    evidence_text,
                    0.65,
                    0.6,
                    "self",
                    "active",
                    "",
                    "",
                    0.6,
                    "short",
                    "internal",
                    0.0,
                    0,
                    "",
                    c_key,
                    "auto",
                    tags_json,
                    meta_json,
                    now_utc,
                    now_utc,
                    now_utc,
                    "",
                    0,
                    "memory_companion",
                    "",
                    c_fp,
                    1,
                    "",
                ),
            )

            fts_text = f"conversation_summary {USER_ID} {USER_DISPLAY} self Bot {SESSION_ID} {mem_content} {evidence_text}"
            con.execute("INSERT INTO memory_fts (search_text, memory_id) VALUES (?, ?)", (fts_text, mem_id))

            if vec:
                t_hash = hashlib.sha1(mem_content.encode("utf-8")).hexdigest()
                con.execute(
                    "INSERT OR REPLACE INTO memory_embeddings (memory_id, provider_id, text_hash, dimension, vector, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        mem_id,
                        "ollama/nomic-embed-text:latest",
                        t_hash,
                        len(vec),
                        json.dumps(vec, separators=(",", ":")),
                        now_utc,
                        now_utc,
                    ),
                )

            con.commit()
            con.close()
            wrote_any = True
        except Exception as e:
            print("写入 memory_companion.db 异常:", e, flush=True)

    # ==================== [Adapter 2] 写入 astrbot_plugin_qqmind 日记库 ====================
    qqmind_diary_db = os.path.join(QQMIND_DIR, "qqmind_diaries", f"{USER_ID}.db")
    if os.path.exists(qqmind_diary_db) and user_text and shenshen_reply:
        try:
            con = sqlite3.connect(qqmind_diary_db, timeout=10)
            title = f"星海小客厅：{user_text[:12]}"
            tags = json.dumps(["星海小客厅", "日常", "对话"], ensure_ascii=False)
            summary = mem_content
            content = shenshen_reply
            grams = make_cjk_bigrams(f"{title} {summary} {content}")
            cur = con.execute(
                "INSERT INTO entries (ts, title, tags, summary, content, tokens, gid, grams) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ts_float, title, tags, summary, content, len(summary), "0", grams),
            )
            rowid = cur.lastrowid
            con.execute(
                "INSERT INTO fts (rowid, grams, title, summary, tags) VALUES (?, ?, ?, ?, ?)",
                (rowid, grams, title, summary, tags),
            )
            con.commit()
            con.close()
            wrote_any = True
        except Exception as e:
            print("写入 qqmind_diaries 异常:", e, flush=True)

    # ==================== [Adapter 4] 写入其他通用第三方 SQLite 记忆插件 ====================
    for g in discover_generic_memory_dbs():
        try:
            con = sqlite3.connect(g["db_path"], timeout=8)
            col_info = con.execute(f"PRAGMA table_info('{g['table']}')").fetchall()
            insert_cols = []
            insert_vals = []
            for cid, cname, ctype, notnull, dflt_val, pk in col_info:
                cl = cname.lower()
                if pk and "int" in (ctype or "").lower():
                    continue  # 自增主键交给 SQLite
                if cl == g["text_col"].lower():
                    insert_cols.append(cname)
                    insert_vals.append(mem_content)
                elif cl in ("id", "memory_id", "uuid", "record_id"):
                    insert_cols.append(cname)
                    insert_vals.append(f"starsea_{uuid.uuid4().hex[:16]}")
                elif cl in ("session_id", "umo", "conversation_id"):
                    insert_cols.append(cname)
                    insert_vals.append(SESSION_ID)
                elif cl in ("user_id", "uid", "sender_id", "subject_id"):
                    insert_cols.append(cname)
                    insert_vals.append(USER_ID)
                elif cl in ("user_name", "sender_name", "username"):
                    insert_cols.append(cname)
                    insert_vals.append(USER_DISPLAY)
                elif cl in ("role", "type", "memory_type", "event_type"):
                    insert_cols.append(cname)
                    insert_vals.append("conversation_summary")
                elif cl in ("created_at", "updated_at", "occurred_at", "time", "datetime"):
                    insert_cols.append(cname)
                    insert_vals.append(now_utc)
                elif cl in ("ts", "timestamp"):
                    insert_cols.append(cname)
                    insert_vals.append(ts_float)
                elif notnull and dflt_val is None:
                    insert_cols.append(cname)
                    insert_vals.append(0 if "int" in (ctype or "").lower() or "real" in (ctype or "").lower() else "")
            if insert_cols:
                q_cols = ", ".join(f'"{c}"' for c in insert_cols)
                q_marks = ", ".join(["?"] * len(insert_cols))
                con.execute(f'INSERT INTO "{g["table"]}" ({q_cols}) VALUES ({q_marks})', tuple(insert_vals))
                con.commit()
                wrote_any = True
            con.close()
        except Exception as e:
            print(f"写入通用记忆插件 {g['plugin']} 警告:", e, flush=True)

    return wrote_any
