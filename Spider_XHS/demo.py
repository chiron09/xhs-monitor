# encoding: utf-8
"""一次扫码登录后的三个最小示例：发布、直播监听、私信发送与监听。

只修改本文件顶部的业务参数即可；Cookie、localStorage、sessionStorage、
b1、DSL、MNS 与 webSsk 都由统一 Auth 登录流程自动创建和维护。Creator
发布和 PC 直播/私信共用一次主站登录，内部按站点保留独立签名上下文。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from apis.xhs_creator_apis import XHS_Creator_Apis
from apis.xhs_live import XHSLiveAPI
from xhs_utils.xhs_auth import XHSUnifiedAuth


DEMO_TYPE = "publish"  # publish / live / im

# publish：至少填写一张本地图片。
PUBLISH_TITLE = "扫码发布测试"
PUBLISH_DESC = "由 Spider_XHS demo.py 发布"
PUBLISH_IMAGE_PATHS = [r"C:\path\to\image.jpg"]

# live：填写直播间 room_id。
LIVE_ROOM_ID = "570443028306756154"

# im：填写接收方 user_id 与消息正文。
IM_RECEIVER_ID = "641562b90000000011020aef"
IM_MESSAGE = "你好，这是一条扫码登录后的测试私信。"


def _print_result(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


_ROOM_EVENT_NAMES = {
    "text": "弹幕", "praise": "点赞", "light": "点亮", "gift": "礼物",
    "come": "进场", "enter": "进场", "enter_room": "进场", "follow": "关注",
    "share": "分享", "viewer_heart": "心跳", "audience_num": "人气",
    "room_notify": "通知",
}

# 信封/环境字段不进单行摘要，剩余字段才是动作本身的内容。
_ROOM_ENVELOPE_KEYS = {
    "type", "profile", "current_time", "room_id", "host_id",
    "source", "pre_source", "view_session_id",
}


def _shorten(value: Any, limit: int = 120) -> str:
    text = value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), default=str)
    return text if len(text) <= limit else text[:limit] + "…"


def _fmt_ts(ms: Any = None) -> str:
    try:
        seconds = int(ms) / 1000
    except (TypeError, ValueError):
        seconds = time.time()
    return time.strftime("%H:%M:%S", time.localtime(seconds))


def _room_event_line(event: dict) -> str:
    payload = event.get("payload") or {}
    custom = payload.get("customData")
    custom = custom if isinstance(custom, dict) else {}
    event_type = str(event.get("event_type") or payload.get("command") or "?")
    action = _ROOM_EVENT_NAMES.get(event_type, event_type)
    profile = custom.get("profile") or {}
    who = profile.get("nickname") or profile.get("user_id") or "?"
    if event_type == "text":
        content = str(custom.get("desc", ""))
    elif event_type == "praise":
        content = f"x{(custom.get('praise_info') or {}).get('count', '?')}"
    else:
        rest = {key: value for key, value in custom.items()
                if key not in _ROOM_ENVELOPE_KEYS}
        content = _shorten(rest) if rest else ""
    when = _fmt_ts(payload.get("ts") or custom.get("current_time"))
    if content:
        return f"[{when}] {action} | {content} | {who}"
    return f"[{when}] {action} | {who}"


def _im_text(body: Any) -> tuple[str, str]:
    """从私信 payload 里剥出最内层文本和发送者（正文常嵌套多层 JSON）。"""
    sender = str(body.get("sender") or "") if isinstance(body, dict) else ""
    content = body
    for _ in range(6):
        if isinstance(content, dict) and "content" in content:
            content = content["content"]
            continue
        if isinstance(content, str):
            try:
                parsed = json.loads(content)
            except (TypeError, json.JSONDecodeError):
                break
            if isinstance(parsed, (dict, list)):
                content = parsed
                continue
        break
    text = content if isinstance(content, str) else _shorten(content)
    return text, sender


def _print_im_entry(entry: Any) -> None:
    if not isinstance(entry, dict):
        print(f"[{_fmt_ts()}] 私信 | {_shorten(entry)}")
        return
    if "chatMessage" in entry:
        message = entry["chatMessage"]
        body = message.get("payload_json", message.get("payload"))
        text, sender = _im_text(body)
        line = f"[{_fmt_ts(message.get('ts'))}] 私信 | {_shorten(text)}"
        if sender:
            line += f" | {sender}"
        print(line)
    if "chatACK" in entry:
        ack = entry["chatACK"]
        note = ("成功" if ack.get("code") == 0
                else f"code={ack.get('code')} {ack.get('msg') or ''}".rstrip())
        print(f"[{_fmt_ts(ack.get('ts'))}] 私信回执 | {note} | mid={ack.get('mid')}")
    if "decode_error" in entry:
        print(f"[{_fmt_ts()}] 私信 | 解码失败: {entry['decode_error']} | {_shorten(entry.get('envelope'))}")


def _print_event(frame: Any) -> None:
    """把一条 RWP 帧压成单行摘要：[时间] 动作 | 内容 | 人。"""
    decoded = frame.get("decoded") if isinstance(frame, dict) else None
    decoded = decoded or {}
    printed = False
    for event in decoded.get("room") or []:
        print(_room_event_line(event))
        printed = True
    im = decoded.get("im")
    for entry in (im if isinstance(im, list) else [im] if isinstance(im, dict) else []):
        _print_im_entry(entry)
        printed = True
    if printed:
        return
    if isinstance(frame, dict) and frame.get("t") == 2:
        ack = (frame.get("b") or {}).get("a") or {}
        note = ("成功" if ack.get("c") == 0
                else f"c={ack.get('c')} {ack.get('m') or ''}".rstrip())
        print(f"[{_fmt_ts()}] 系统应答 | {note}")
        return
    print(f"[{_fmt_ts()}] 其他 | {_shorten(frame, 200)}")


def creator_qrcode_publish() -> None:
    """Creator 扫码登录并发布一篇图文作品。"""
    image_paths = [str(Path(path).expanduser().resolve()) for path in PUBLISH_IMAGE_PATHS if path]
    if not image_paths:
        raise ValueError("请先填写 PUBLISH_IMAGE_PATHS")
    missing = [path for path in image_paths if not Path(path).is_file()]
    if missing:
        raise FileNotFoundError(f"图片不存在: {missing}")

    auth = XHSUnifiedAuth.from_qrcode_login(show_in_terminal=True)
    try:
        api = XHS_Creator_Apis(auth.creator).bootstrap()
        success, message, result = api.post_note({
            "title": PUBLISH_TITLE,
            "desc": PUBLISH_DESC,
            "media_type": "image",
            "images": image_paths,
            "type": 1,
        })
        _print_result({"success": success, "message": message, "result": result})
        if not success:
            raise RuntimeError(message)
    finally:
        auth.close()


async def pc_qrcode_listen_live() -> None:
    """PC 扫码登录并持续监听一个直播间；Ctrl+C 结束。"""
    if not LIVE_ROOM_ID:
        raise ValueError("请先填写 LIVE_ROOM_ID")
    auth = XHSUnifiedAuth.from_qrcode_login(show_in_terminal=True)
    websocket = None
    try:
        live = XHSLiveAPI(auth.pc)
        websocket = await live.connect_push_from_storage(room_id=LIVE_ROOM_ID)
        async for event in websocket.events():
            _print_event(event)
    finally:
        if websocket is not None:
            await websocket.close()
        auth.close()


async def pc_qrcode_send_and_listen_im() -> None:
    """PC 扫码登录，发送一条私信，然后持续监听私信事件。"""
    if not IM_RECEIVER_ID:
        raise ValueError("请先填写 IM_RECEIVER_ID")
    auth = XHSUnifiedAuth.from_qrcode_login(show_in_terminal=True)
    websocket = None
    try:
        live = XHSLiveAPI(auth.pc)
        websocket = await live.connect_push_from_storage()
        sent = await websocket.send_private_message(IM_RECEIVER_ID, IM_MESSAGE)
        print(f"[{_fmt_ts()}] 私信已发送 | {IM_MESSAGE} | mid={sent['mid']}")
        async for event in websocket.events():
            _print_event(event)
    finally:
        if websocket is not None:
            await websocket.close()
        auth.close()


def main() -> None:
    if DEMO_TYPE == "publish":
        creator_qrcode_publish()
    elif DEMO_TYPE == "live":
        asyncio.run(pc_qrcode_listen_live())
    elif DEMO_TYPE == "im":
        asyncio.run(pc_qrcode_send_and_listen_im())
    else:
        raise ValueError("DEMO_TYPE 只支持 publish、live 或 im")


if __name__ == "__main__":
    main()
