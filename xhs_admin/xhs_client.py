# -*- coding: utf-8 -*-
"""封装 Spider_XHS 纯 SDK 的调用。

只保留读取类能力：Cookie 校验、主页笔记抓取、关注列表、笔记归一化。
登录能力已收敛为「Cookie 导入」与「浏览器登录」两条路径
（后者由 browser_login.py 直接驱动无头浏览器完成），
故不再保留服务端直连的扫码 / 手机验证码流程。
"""
import os
import sys
import urllib.parse

from config import SDK_DIR

if SDK_DIR not in sys.path:
    sys.path.insert(0, SDK_DIR)

from apis.xhs_pc_apis import XHS_Apis, splice_str  # noqa: E402
from xhs_utils.xhs_pc import XHSPcAuth  # noqa: E402


def _build_api(cookie: str):
    return XHS_Apis(XHSPcAuth.from_cookie(cookie)).bootstrap()


def check_cookie(cookie: str):
    """验证 cookie 有效性。返回 (ok, nickname, user_id, error)。

    注意：未登录时 get_user_me 仍会返回 success=true，但 data.guest 为 true
    （一个匿名 user_id）。必须额外判断 guest，否则会把游客态当成已登录。
    """
    if not cookie or not cookie.strip():
        return False, "", "", "cookie 为空"
    try:
        api = _build_api(cookie)
        success, msg, data = api.get_user_me()
        if success and isinstance(data, dict):
            d = data.get("data") or {}
            if d.get("guest"):
                return False, "", "", "未登录（游客状态）"
            return True, d.get("nickname", ""), d.get("user_id", ""), ""
        return False, "", "", str(msg or "登录态无效")
    except Exception as e:  # noqa: BLE001
        return False, "", "", str(e)


def extract_user_id(url: str) -> str:
    """从博主主页链接提取 24 位 user_id。"""
    try:
        path = urllib.parse.urlparse(url.strip()).path
        uid = path.rstrip("/").split("/")[-1]
        if len(uid) == 24 and all(c in "0123456789abcdef" for c in uid):
            return uid
        return uid if len(uid) == 24 else ""
    except Exception:  # noqa: BLE001
        return ""


def fetch_notes_page(cookie: str, url: str):
    """抓博主最新一页笔记。返回 (ok, notes_list, nickname, error)。"""
    uid = extract_user_id(url)
    if not uid:
        return False, [], "", "无法从链接解析 user_id（应为 24 位十六进制）"
    try:
        api = _build_api(cookie)
        parsed = urllib.parse.urlparse(url.strip())
        q = urllib.parse.parse_qs(parsed.query)
        xsec_token = q.get("xsec_token", [""])[0]
        xsec_source = q.get("xsec_source", ["pc_search"])[0]
        success, msg, data = api.get_user_note_info(uid, "", xsec_token, xsec_source)
        if not success:
            err = str(msg or "")
            # SDK 在响应缺 'success' 字段时（风控/账号异常，如 code 300011）会抛 KeyError，
            # msg 变成无用的 "'success'"；真实错误藏在 data 里（{'code': 300011, 'msg': '账号异常...'}）。
            if isinstance(data, dict) and (data.get("msg") or data.get("code")):
                parts = [str(data.get("msg") or "").strip()]
                if data.get("code"):
                    parts.append(f"code {data['code']}")
                err = "，".join(p for p in parts if p)
            return False, [], "", err
        notes = []
        if isinstance(data, dict):
            notes = (data.get("data") or {}).get("notes") or []
        nickname = ""
        if notes and isinstance(notes[0], dict):
            user = notes[0].get("user") or {}
            nickname = user.get("nickname") or user.get("nick_name") or ""
        return True, notes, nickname, ""
    except Exception as e:  # noqa: BLE001
        return False, [], "", str(e)


def fetch_followings(cookie: str, user_id: str, cursor: str = ""):
    """获取账号关注列表一页。返回 (ok, users, cursor, has_more, error)。"""
    if not user_id:
        return False, [], "", False, "缺少 user_id"
    try:
        api = _build_api(cookie)
        a = "/api/sns/web/v1/user/followings"
        params = {"num": "30", "cursor": cursor, "user_id": user_id}
        sa = splice_str(a, params)
        headers, cks, _ = api._request_params(sa, "", "GET")
        resp = api.http.get(
            api.base_url + sa,
            headers=headers,
            cookies=cks,
            proxies=api._proxies(None),
            timeout=15,
        )
        body = resp.json()
        if not body.get("success"):
            return False, [], "", False, body.get("msg") or "获取关注列表失败"
        d = body.get("data") or {}
        users = d.get("users") or []
        return True, users, str(d.get("cursor") or ""), bool(d.get("has_more")), ""
    except Exception as e:  # noqa: BLE001
        return False, [], "", False, str(e)


def normalize_note(n: dict) -> dict:
    """把一条原始笔记转成标准字段。"""
    note_id = n.get("note_id", "")
    title = n.get("display_title") or n.get("title") or ""
    cover = n.get("cover") or {}
    cover_url = cover.get("url_default") or cover.get("url_pre") or ""
    xsec = n.get("xsec_token", "")
    note_url = f"https://www.xiaohongshu.com/explore/{note_id}"
    if xsec:
        note_url += f"?xsec_token={xsec}&xsec_source=pc_user"
    liked = (n.get("interact_info") or {}).get("liked_count", 0)
    try:
        liked = int(liked)
    except (ValueError, TypeError):
        liked = 0
    return {
        "note_id": note_id,
        "title": title,
        "cover_url": cover_url,
        "note_url": note_url,
        "publish_time": int(n.get("time") or 0),
        "liked_count": liked,
    }
