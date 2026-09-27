# -*- coding: utf-8 -*-
"""纯 Spider_XHS SDK 的 Cookie 采集验证脚本（临时）。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
cookies = os.environ.get("COOKIES", "").strip()
print("cookie 字段数:", len(cookies.split(";")))
print("含 web_session:", "web_session" in cookies, "| 含 a1:", "a1" in cookies)

from apis.xhs_pc_apis import XHS_Apis
from xhs_utils.xhs_pc import XHSPcAuth

auth = XHSPcAuth.from_cookie(cookies)
api = XHS_Apis(auth).bootstrap()

# 蹦蹦团子（之前验证过有 30 条笔记）
target = "https://www.xiaohongshu.com/user/profile/5c89a339000000001203eb66"
ok, msg, data = api.get_user_all_notes(target)
print("\n抓取:", "SUCCESS" if ok else "FAILED", "| msg:", str(msg)[:120])
notes = data if isinstance(data, list) else []
print("笔记条数:", len(notes))
for n in notes[:8]:
    if not isinstance(n, dict):
        print("  -", n)
        continue
    user = n.get("user") or {}
    it = n.get("interact_info") or {}
    print(
        "  -", (n.get("display_title") or n.get("note_id") or n.get("title") or "")[:38],
        "|", user.get("nickname") or user.get("nick_name"),
        "| 赞", it.get("liked_count"),
    )
