#!/usr/bin/env python3
"""校验 Telegram 推送文本不会被 Telegram 拒收。

背景：2026-10-09 的 run 里推送报
    Bad Request: can't parse entities: Can't find end of the entity starting at byte offset 111
根因是报告里插进去的动态值没转义——Telegram 的 legacy Markdown 把 `_ * ` [` 当实体
开始符，OptikLink 抓回来的用户名（如 david_chen）里那个 `_` 没有配对，整条消息被 400
拒收。偏移 111 = 模板里用户名槽位起点 106 + 5。

这个脚本不联网：它把 optiklink_login._tg_session 换成一个假的 session，截下 tg_send
真正会 POST 出去的 payload，再按 Telegram 的解析规则校验文本。

    python3 test_report_escaping.py
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import optiklink_login as ol

# ─────────────────────────────────────────────────────────────
# 校验器 1：legacy Markdown（tdlib MessageEntity.cpp: parse_markdown 的移植）
# ─────────────────────────────────────────────────────────────
# 规则：遇到 `_` `*` `` ` `` `[` 就从它开始往后找**最近的同类字符**，找不到就
# 返回 "Can't find end of the entity starting at byte offset <起点>"。
# 只移植了报错判定，省略 pre / URL 这些我们模板里不会出现的结构。
SPECIAL = b"_*`["


def legacy_markdown_error_offset(text: str):
    raw = text.encode("utf-8")
    size = len(raw)
    i = 0
    while i < size:
        c = raw[i]
        if c == 0x5C and i + 1 < size and raw[i + 1] in SPECIAL:   # 反斜杠转义
            i += 2
            continue
        if c not in SPECIAL:
            i += 1
            continue
        begin_pos = i
        end_character = 0x5D if c == 0x5B else c                    # '[' 找 ']'
        i += 1
        while i < size and raw[i] != end_character:
            i += 1
        if i == size:
            return begin_pos
        i += 1
    return None


# ─────────────────────────────────────────────────────────────
# 校验器 2：HTML（Telegram 只认 &lt; &gt; &amp; &quot;，标签必须配平）
# ─────────────────────────────────────────────────────────────
TAG_RE = re.compile(r"</?b>")
ENTITY_RE = re.compile(r"&(?:amp|lt|gt|quot|#\d+|#x[0-9a-fA-F]+);")


def html_error(text: str):
    plain = TAG_RE.sub("", text)
    if "<" in plain or ">" in plain:
        return "有未转义的 < 或 >"
    if "&" in ENTITY_RE.sub("", plain):
        return "有未转义的 & 或非法实体"
    if text.count("<b>") != text.count("</b>"):
        return "<b> 与 </b> 不配对"
    return None


def check_text(text: str, parse_mode):
    """按声明的 parse_mode 校验，返回错误描述；能通过返回 None。"""
    if parse_mode == "HTML":
        return html_error(text)
    if parse_mode in ("Markdown", "MarkdownV2"):
        offset = legacy_markdown_error_offset(text)
        if offset is not None:
            return f"Can't find end of the entity starting at byte offset {offset}"
        return None
    return None      # 没有 parse_mode，纯文本，怎么都行


# ─────────────────────────────────────────────────────────────
# 假的 session：截下 tg_send 实际会发出去的东西
# ─────────────────────────────────────────────────────────────
class _FakeResp:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class FakeSession:
    """reject_parse_error=True 时，带 parse_mode 的请求一律回 400 解析失败。
    rate_limit_times=N 时，前 N 次请求回 429（retry_after=0，测试不真等）。"""

    def __init__(self, reject_parse_error=False, rate_limit_times=0):
        self.headers = {}
        self.sent = []
        self.reject_parse_error = reject_parse_error
        self.rate_limit_times = rate_limit_times

    def post(self, url, json=None, timeout=None):
        self.sent.append(json)
        if self.rate_limit_times > 0:
            self.rate_limit_times -= 1
            return _FakeResp({"ok": False, "error_code": 429,
                              "description": "Too Many Requests: retry later",
                              "parameters": {"retry_after": 0}})
        if self.reject_parse_error and json.get("parse_mode"):
            return _FakeResp({"ok": False,
                              "description": "Bad Request: can't parse entities: ..."})
        return _FakeResp({"ok": True, "result": {"message_id": 1}})


def capture(title, content, **kwargs):
    """跑一次 tg_send，返回它真正 POST 出去的 payload（可能多个，失败会重试/退化）。"""
    fake = FakeSession(**kwargs)
    ol._tg_session = fake
    try:
        ol.tg_send(title, content)
    finally:
        ol._tg_session = None
    return fake.sent


# ─────────────────────────────────────────────────────────────
# 用例
# ─────────────────────────────────────────────────────────────
def make_report(username, error=None):
    info = {"logged_in": True, "username": username,
            "expire_date": "17.10.2026", "running_servers": "1"}
    server = {"skipped": True}
    if error:
        server = {"skipped": False, "error": error}
    return ol.build_report(info, server)


# 真实触发这个 bug 的那个用户名：第 6 个字节是 `_`
ADVERSARIAL = [
    ("david_chen", "第 6 字节是下划线（事故现场）"),
    ("a_b", "短用户名里的下划线"),
    ("user_name_x", "两个下划线"),
    ("back`tick", "反引号"),
    ("bracket[1]", "方括号"),
    ("star*name", "星号"),
    ("a<b>&c", "HTML 保留字符"),
    ("N/A", "正常情况"),
]

FAILURES = []


def expect(cond, label):
    print(f"  {'✅' if cond else '❌'} {label}")
    if not cond:
        FAILURES.append(label)


def main():
    print("=" * 55)
    print("Telegram 推送文本校验")
    print("=" * 55)

    print("\n[1] 复现：旧写法（parse_mode=Markdown + 未转义用户名）会被 Telegram 拒收")
    # 修复前 tg_send/build_report 拼出来的原文，照抄自 2026-10-09 那次 run
    old_style = ("*✅ OptikLink 签到成功*\n\n"
                 "## OptikLink 自动登录报告\n"
                 "**状态**: ✅ 登录成功\n"
                 "**用户名**: david_chen\n"
                 "**服务到期**: 17.10.2026\n"
                 "**剩余天数**: 8 天\n"
                 "**执行时间**: 2026-10-09 01:02:35 UTC")
    offset = legacy_markdown_error_offset(old_style)
    expect(offset == 111, f"david_chen 触发报错，且偏移正是事故里的 111（实际 {offset}）")

    print("\n[2] 现在发出的文本，按它自己声明的 parse_mode 校验")
    for username, why in ADVERSARIAL:
        sent = capture("✅ OptikLink 签到成功", make_report(username))
        expect(bool(sent), f"用户名 {username!r} 有发出请求（{why}）")
        payload = sent[0]
        err = check_text(payload["text"], payload.get("parse_mode"))
        expect(err is None, f"用户名 {username!r} → {payload.get('parse_mode')} 校验（{err or '通过'}）")

    print("\n[3] 保活异常串带 [Errno 111] 这类括号也不该炸")
    err_msg = ("HTTPSConnectionPool(host='control.optiklink.net', port=443): "
               "NewConnectionError('[Errno 111] Connection refused')")
    payload = capture("✅ OptikLink 签到成功", make_report("N/A", error=err_msg))[0]
    err = check_text(payload["text"], payload.get("parse_mode"))
    expect(err is None, f"保活错误串 → 校验（{err or '通过'}）")

    print("\n[4] 万一带 parse_mode 的请求被 Telegram 拒了，要退化成纯文本再发一次")
    sent = capture("✅ OptikLink 签到成功", make_report("david_chen"), reject_parse_error=True)
    expect(len(sent) >= 2, f"被拒后换了发法（共 {len(sent)} 次请求）")
    expect(sent[-1].get("parse_mode") is None, "最后一次不带 parse_mode（纯文本）")
    expect(check_text(sent[-1]["text"], sent[-1].get("parse_mode")) is None,
           "退化后的文本依然合法")
    expect("<b>" not in sent[-1]["text"], "退化后的文本不带标签")

    print("\n[5] 被限流（429）时要原地重试，而不是急着换发法")
    sent = capture("✅ OptikLink 签到成功", make_report("N/A"), rate_limit_times=2)
    expect(len(sent) == 3 and sent[-1].get("parse_mode") == "HTML",
           f"429 后重试同一 payload 并成功（共 {len(sent)} 次请求，末次仍是 HTML）")

    print("\n" + "=" * 55)
    if FAILURES:
        print(f"❌ {len(FAILURES)} 项失败")
        for f in FAILURES:
            print(f"   - {f}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
