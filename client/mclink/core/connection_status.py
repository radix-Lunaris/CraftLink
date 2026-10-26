"""Read-only route presentation, shared by GUI snapshots and tests."""


def connection_rows(state, streams=(), direct_rows=(), carriers=None):
    if state is None:
        return []
    peers = list(getattr(state, "peer_ids", None) or [])
    if not getattr(state, "is_host", False):
        host = getattr(state, "host_id", "")
        peers = [host] if host and host != getattr(state, "client_id", "") else []
    names = getattr(state, "member_names", {}) or {}
    ips = getattr(state, "member_ips", {}) or {}
    conns = getattr(state, "member_conns", {}) or {}
    started = set(getattr(state, "started_ids", None) or [])
    mode = getattr(state, "conn_mode", "relay")
    direct = {row["peer"]: row for row in direct_rows if row.get("peer")}
    result = []
    for peer in peers:
        name = names.get(peer) or peer
        running = peer in started or conns.get(peer) in ("waiting", "pairing", "connected")
        live = [s for s in streams if s.get("peer") == peer and s.get("switch_phase") != "ended"]
        networks = [s for s in live if s.get("channel") == "network"]
        # Business streams describe actual carrying when present. The separate
        # peer tunnel provides verified readiness even without applications.
        data = [s for s in live if s.get("channel") != "network"]
        live = data or live
        row = dict(direct.get(peer, {}), peer=peer, name=name,
                   public_ip=ips.get(peer, ""), room_mode=mode)
        carrier = (carriers or {}).get(peer)
        addr = getattr(carrier, "addr", None)
        if addr and row.get("ready"):
            from .auto_stream import format_endpoint
            row["addr"] = format_endpoint(addr)
        if live:
            row.update(live[0])
            row["public_ip"] = ips.get(peer) or live[0].get("public_ip", "")
            paths = {s["active_path"] for s in live}
            row["active_path"] = next(iter(paths)) if len(paths) == 1 else "mixed"
            row["relay_ready"] = all(s.get("relay_ready") for s in live)
            row["direct_ready"] = all(s.get("direct_ready") for s in live)
            if networks:
                row["network_active_path"] = networks[0].get("active_path", "unknown")
        elif mode == "relay":
            row["active_path"] = "relay" if conns.get(peer) == "connected" else "none"
        elif mode == "stun":
            carrying = bool(carrier and getattr(carrier, "active_streams", 0))
            row["active_path"] = "direct" if carrying else "none"
            row["direct_endpoint"] = row.get("addr", "") if row.get("ready") else ""
            row["direct_rtt_ms"] = row.get("rtt_ms")
        else:
            row["active_path"] = "none"
        path = row["active_path"]
        label = {"relay": "中继", "direct": "P2P 直连", "mixed": "切换中／部分直连",
                 "reconnecting": "路径恢复中"}.get(path, "未开始")
        if path == "none" and running:
            if mode == "stun":
                carrier = (carriers or {}).get(peer)
                if row.get("ready"):
                    label = "直连隧道已就绪" if getattr(carrier, "started", False) else "打洞成功，建立直连隧道中"
                else:
                    label = row.get("row_text") or row.get("stage_text") or "等待直连协调"
            elif mode == "relay":
                label = "中继服务已启动，等待端口接入"
            else:
                label = "建立中继网络隧道中"
        if live and live[0].get("channel") == "network":
            label += "（网络隧道）"
        details = [name, label, "公网 IP（服务端观察）：" + (row.get("public_ip") or "未知")]
        endpoint = row.get("direct_endpoint")
        if endpoint:
            details.append("直连端点：" + endpoint)
        elif mode != "relay" and row.get("addr"):
            details.append("候选：" + row["addr"])
        key = "direct_rtt_ms" if path == "direct" else "relay_rtt_ms"
        rtt = row.get(key)
        details.append(("直连 RTT：" if path == "direct" else "中继 RTT：")
                       + (f"{rtt:.0f} ms" if isinstance(rtt, (int, float)) else "—"))
        if mode == "auto" and live:
            if data and row.get("network_active_path") == "direct" and path != "direct":
                details.append("P2P 网络隧道仍就绪；当前应用连接由中继承载" if path == "relay"
                               else "P2P 网络隧道仍就绪")
            if path == "direct":
                details.append("中继备用就绪" if row.get("relay_ready") else "中继备用恢复中")
            elif row.get("switch_phase") in ("preparing", "ready"):
                details.append("正在验证并切换直连")
            elif row.get("switch_phase") == "retry_wait":
                details.append("直连暂不可用，稍后重试")
            else:
                details.append("正在尝试直连")
        if mode in ("stun", "auto") and running and not row.get("direct_ready"):
            stage = row.get("row_text") or row.get("stage_text")
            if stage and stage != label:
                details.append(stage)
        row["display_text"] = "｜".join(details)
        row["display_hint"] = "ok" if path == "direct" else ("warn" if running else "muted")
        result.append(row)
    return result


def auto_summary(rows):
    active = [r for r in rows if r.get("active_path") != "none"]
    direct = sum(r.get("active_path") == "direct" for r in active)
    relay = sum(r.get("active_path") == "relay" for r in active)
    recovering = sum(r.get("active_path") == "reconnecting" for r in active)
    mixed = len(active) - direct - relay - recovering
    return (f"AUTO · 直连 {direct} 人／中继 {relay} 人"
            + (f"／切换中 {mixed} 人" if mixed else "")
            + (f"／恢复中 {recovering} 人" if recovering else ""))
