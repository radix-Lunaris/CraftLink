"""host 释放后名字要立刻刷新：HOST_STATE 必须带上"当前主机是谁"。"""

import io

# ------------------------------------------------------- server: room_members.py
PATH = "mc-link-v1/server/room_members.py"
text = io.open(PATH, encoding="utf-8").read()

old = '''    line = (
        f"HOST_STATE host={1 if room.host_claimed else 0} "
        f"members={room.member_count()} "
        f"world={room.world_player_count()}\\n"
    )'''

new = '''    # host_name= 是必须的，不是锦上添花：
    #
    # host=0/1 只说"有没有人当主机"，说不出"是谁"。于是某个人释放主机之后，
    # 别人客户端上那一行还挂着他上一次收到的名字 —— 看起来像"释放没生效"，
    # 也看不出自己现在能不能认领。名字跟着状态一起推，界面才是实时的。
    current_host = room.host_member
    host_name = ""

    if current_host is not None:
        host_name = current_host.username or current_host.label or ""

    host_name = "".join(
        c for c in host_name if ord(c) >= 33 and ord(c) != 127
    ) or "-"

    line = (
        f"HOST_STATE host={1 if room.host_claimed else 0} "
        f"host_name={host_name} "
        f"members={room.member_count()} "
        f"world={room.world_player_count()}\\n"
    )'''

assert text.count(old) == 1, text.count(old)
text = text.replace(old, new)

io.open(PATH, "w", encoding="utf-8", newline="\n").write(text)
print("room_members.py: HOST_STATE 现在带 host_name")

# -------------------------------------------------------- client: room.py
PATH2 = "mc-link-v1/client/mclink/core/room.py"
text2 = io.open(PATH2, encoding="utf-8").read()

old_parse = '''                if key == "host":
                    self.state.host_claimed = value == "1"

                    # If we no longer hold it, our own flag must follow.
                    if value != "1":
                        self.state.is_host = False'''

new_parse = '''                if key == "host":
                    self.state.host_claimed = value == "1"

                    # If we no longer hold it, our own flag must follow.
                    if value != "1":
                        self.state.is_host = False
                        # 名字也要跟着清掉，否则界面上会一直挂着上一个人的名字。
                        self.state.host_name = ""
                elif key == "host_name":
                    self.state.host_name = "" if value in ("", "-") else value'''

assert text2.count(old_parse) == 1, text2.count(old_parse)
text2 = text2.replace(old_parse, new_parse)

# RoomState 增加 host_name 字段 + 文案
old_state = '''    host_claimed: bool = False'''
new_state = '''    host_claimed: bool = False
    #: 当前持有主机身份的人的名字（中继广播，可能为空 = 没人认领）。
    host_name: str = ""'''
assert text2.count(old_state) == 1
text2 = text2.replace(old_state, new_state)

old_summary = '''            "有房主" if self.host_claimed else "无房主"
        )'''
new_summary = '''            (
                f"主机：{self.host_name}"
                if self.host_claimed and self.host_name
                else ("有房主" if self.host_claimed else "无房主")
            )
        )'''
assert text2.count(old_summary) == 1
text2 = text2.replace(old_summary, new_summary)

# 认领/释放成功后，本地名字立即更新（不用等广播）
old_claim = '''                self.state.is_host = True
                self.state.host_claimed = True
                future.set_result((True, "已获得房主身份"))'''
new_claim = '''                self.state.is_host = True
                self.state.host_claimed = True
                self.state.host_name = self.username or ""
                future.set_result((True, "已获得房主身份"))'''
assert text2.count(old_claim) == 1
text2 = text2.replace(old_claim, new_claim)

old_release = '''                self.state.is_host = False
                # The relay decides whether the slot is now free; it will
                # broadcast. Assume free so the UI is not stuck on "有房主".
                self.state.host_claimed = False
                future.set_result((True, "已释放房主身份"))'''
new_release = '''                self.state.is_host = False
                # The relay decides whether the slot is now free; it will
                # broadcast. Assume free so the UI is not stuck on "有房主",
                # and drop the name at the same time -- a stale name next to
                # "无房主" is exactly the confusion being fixed here.
                self.state.host_claimed = False
                self.state.host_name = ""
                future.set_result((True, "已释放房主身份"))'''
assert text2.count(old_release) == 1
text2 = text2.replace(old_release, new_release)

io.open(PATH2, "w", encoding="utf-8", newline="\n").write(text2)
print("room.py: 客户端解析并展示 host_name")
