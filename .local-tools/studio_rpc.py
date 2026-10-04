#!/usr/bin/env python3
"""ZMK Studio RPC を USB シリアルで直接送る最小クライアント（DYA Studio と同じ命令）。

使い方:
  studio_rpc.py behaviors            ビヘイビアID と表示名の一覧
  studio_rpc.py keymap [layer...]    キーマップを表示（レイヤーID指定で絞り込み）
  studio_rpc.py check plan.json      plan と本体の差分を表示するだけ（書き込まない）
  studio_rpc.py apply plan.json      plan の差分だけ書き込み → 読み直して照合 → 保存
  plan.json は make_plan.py が config/mona2.keymap から作る（BASE の親指と SYM/NUM/NAV）か、手で書く
    plan.json: [{"layer": 1, "pos": 0, "behavior": "Key Press", "p1": 123, "p2": 0}, ...]
    p1 の Key Press は (修飾 << 24) | (0x07 << 16) | HID usage。修飾は LC=1 LS=2 LA=4 LG=8 RC=0x10 RS=0x20 RA=0x40 RG=0x80

  コンボ (zmk-feature-runtime-combo)
  studio_rpc.py combos                                  コンボ一覧と全体設定
  studio_rpc.py combo-set <slot> <pos,pos> "<behavior>" [p1] [p2]   書き込み → 読み直して照合 → 保存
  studio_rpc.py combo-delete <slot>                     削除して保存（ファームの初期値も無効になる）
  studio_rpc.py combo-reset <slot>                      保存済みの上書きを消してファームの初期値に戻す（即保存）
  studio_rpc.py combo-save                              未保存のコンボ変更を保存

  トラックボール (zmk-module-runtime-input-processor)
  studio_rpc.py trackball                               入力処理 (id=0 mouse / id=1 scroll) の設定一覧
  studio_rpc.py trackball-set <id> <設定名> <値>         書き込み（即保存）→ 読み直して照合。設定名は RIP_SET

  マウスジェスチャー
  studio_rpc.py gestures / gesture-delete <id>

DYA Studio / Mouse Gesture Studio がポートを開いている間は使えない。
キー配置とコンボを変えたら、config/mona2.keymap も本体に合わせておく。
"""
import glob
import json
import os
import select
import sys
import termios
import time
import tty
import fcntl
import struct

SOF, ESC, EOF = 0xAB, 0xAC, 0xAD


# ---------------- protobuf (最小限) ----------------
def enc_varint(v):
    out = bytearray()
    v &= (1 << 64) - 1
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def zigzag(n):
    return (n << 1) ^ (n >> 63)


def f_varint(field, v):
    return enc_varint(field << 3 | 0) + enc_varint(v)


def f_bytes(field, b):
    return enc_varint(field << 3 | 2) + enc_varint(len(b)) + b


def dec(buf):
    """generic decode -> {field: [values]} (varint は int、length-delimited は bytes)"""
    out, i = {}, 0
    while i < len(buf):
        key, i = _rv(buf, i)
        field, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _rv(buf, i)
        elif wt == 2:
            ln, i = _rv(buf, i)
            v = buf[i:i + ln]
            i += ln
        elif wt == 5:
            v = buf[i:i + 4]
            i += 4
        elif wt == 1:
            v = buf[i:i + 8]
            i += 8
        else:
            raise ValueError(f"wire type {wt}")
        out.setdefault(field, []).append(v)
    return out


def _rv(buf, i):
    shift = v = 0
    while True:
        b = buf[i]
        i += 1
        v |= (b & 0x7F) << shift
        if not b & 0x80:
            return v, i
        shift += 7


def unzigzag(n):
    return (n >> 1) ^ -(n & 1)


def packed_varints(b):
    out, i = [], 0
    while i < len(b):
        v, i = _rv(b, i)
        out.append(v)
    return out


# ---------------- transport ----------------
class Port:
    def __init__(self, path):
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        tty.setraw(self.fd)
        fcntl.ioctl(self.fd, getattr(termios, "TIOCMBIS", 0x8004746C), struct.pack("I", 0x6))
        time.sleep(0.2)
        self.rx = bytearray()
        self.req_id = 0

    def close(self):
        os.close(self.fd)

    def _frame(self, payload):
        out = bytearray([SOF])
        for b in payload:
            if b in (SOF, ESC, EOF):
                out.append(ESC)
            out.append(b)
        out.append(EOF)
        return bytes(out)

    def _read_frame(self, timeout):
        end = time.time() + timeout
        while time.time() < end:
            f = self._pop_frame()
            if f is not None:
                return f
            r, _, _ = select.select([self.fd], [], [], 0.05)
            if r:
                try:
                    self.rx += os.read(self.fd, 4096)
                except BlockingIOError:
                    pass
        return None

    def _pop_frame(self):
        try:
            s = self.rx.index(SOF)
        except ValueError:
            self.rx.clear()
            return None
        out, i, esc = bytearray(), s + 1, False
        while i < len(self.rx):
            b = self.rx[i]
            if esc:
                out.append(b)
                esc = False
            elif b == ESC:
                esc = True
            elif b == EOF:
                del self.rx[:i + 1]
                return bytes(out)
            elif b == SOF:
                del self.rx[:i]
                return self._pop_frame()
            else:
                out.append(b)
            i += 1
        return None

    def call(self, subsystem_field, sub_payload, timeout=3.0):
        self.req_id += 1
        req = f_varint(1, self.req_id) + f_bytes(subsystem_field, sub_payload)
        os.write(self.fd, self._frame(req))
        end = time.time() + timeout
        while time.time() < end:
            fr = self._read_frame(end - time.time())
            if fr is None:
                break
            resp = dec(fr)
            if 1 not in resp:  # notification
                continue
            rr = dec(resp[1][0])
            if rr.get(1, [None])[0] != self.req_id:
                continue
            if 2 in rr:
                raise RuntimeError(f"meta error: {dec(rr[2][0])}")
            if subsystem_field not in rr:
                raise RuntimeError(f"unexpected response: {rr}")
            return dec(rr[subsystem_field][0])
        raise TimeoutError("no response")


def open_studio_port():
    for path in sorted(glob.glob("/dev/cu.usbmodem*")):
        p = Port(path)
        try:
            p.call(3, f_varint(1, 1), timeout=1.5)  # core.get_device_info
            return p, path
        except Exception:
            p.close()
    raise SystemExit("Studio RPC に応答するポートが見つからない（DYA Studio 等が開いていないか確認）")


# ---------------- commands ----------------
def behaviors(p):
    r = p.call(4, f_varint(1, 1))
    ids = []
    for v in dec(r[1][0]).get(1, []):
        ids += packed_varints(v) if isinstance(v, bytes) else [v]
    names = {}
    for bid in ids:
        d = dec(p.call(4, f_bytes(2, f_varint(1, bid)))[2][0])
        names[bid] = d.get(2, [b""])[0].decode()
    return names


def keymap(p):
    r = p.call(5, f_varint(1, 1), timeout=6.0)
    km = dec(r[1][0])
    layers = []
    for lb in km.get(1, []):
        l = dec(lb)
        binds = []
        for bb in l.get(3, []):
            b = dec(bb)
            binds.append((unzigzag(b.get(1, [0])[0]), b.get(2, [0])[0], b.get(3, [0])[0]))
        layers.append({"id": l.get(1, [0])[0], "name": l.get(2, [b""])[0].decode(), "bindings": binds})
    return layers


def set_binding(p, layer_id, pos, bid, p1, p2):
    binding = f_varint(1, zigzag(bid)) + f_varint(2, p1) + f_varint(3, p2)
    req = f_varint(1, layer_id) + f_varint(2, pos) + f_bytes(3, binding)
    r = p.call(5, f_bytes(2, req))
    return r.get(2, [0])[0]


def save(p):
    r = p.call(5, f_varint(4, 1), timeout=30.0)  # 変更が多いとフラッシュへの書き込みに時間がかかる
    return dec(r[4][0])


MG_DIR = {0: "UP", 1: "RIGHT", 2: "DOWN", 3: "LEFT"}


def mg_call(p, mg_index, mg_req):
    """カスタムサブシステム (マウスジェスチャー) を呼ぶ。mg_req は zmk.mouse_gesture.Request"""
    call = f_varint(1, mg_index) + f_bytes(2, mg_req)
    r = p.call(100, f_bytes(2, call))
    return dec(dec(r[2][0]).get(2, [b""])[0])


def subsystem_index(p, identifier):
    """カスタムサブシステムの番号を識別名 (例: cormoran__runtime_combo) から引く"""
    r = p.call(100, f_bytes(1, b""))
    for sb in dec(r[1][0]).get(1, []):
        s = dec(sb)
        if s.get(2, [b""])[0].decode() == identifier:
            return s.get(1, [0])[0]
    raise SystemExit(f"{identifier} サブシステムが見つからない")


def mg_index_of(p):
    return subsystem_index(p, "cormoran__mouse_gesture")


# ---- コンボ (zmk-feature-runtime-combo / proto/cormoran/runtime_combo/runtime_combo.proto) ----
COMBO_SOURCE = {0: "empty", 1: "default", 2: "overridden", 3: "runtime"}


def combo_call(p, idx, req_field, req_body=b"", timeout=3.0):
    """runtime_combo.Request の oneof (req_field) を送り、Response を返す。エラー応答は例外にする"""
    call = f_varint(1, idx) + f_bytes(2, f_bytes(req_field, req_body))
    r = p.call(100, f_bytes(2, call), timeout=timeout)
    resp = dec(dec(r[2][0]).get(2, [b""])[0])
    if 1 in resp:
        raise SystemExit(f"combo error: {dec(resp[1][0]).get(1, [b''])[0].decode()}")
    return resp


def combo_decode(cb):
    c = dec(cb)
    pos = []
    for v in c.get(3, []):
        pos += packed_varints(v) if isinstance(v, bytes) else [v]
    b = dec(c.get(4, [b""])[0])
    return {
        "index": c.get(1, [0])[0], "name": c.get(2, [b""])[0].decode(), "positions": pos,
        "bid": b.get(1, [0])[0], "p1": b.get(2, [0])[0], "p2": b.get(3, [0])[0],
        "layer_mask": c.get(6, [0])[0], "enabled": bool(c.get(8, [0])[0]),
        "timeout_ms": c.get(9, [0])[0], "require_prior_idle_ms": c.get(10, [0])[0],
        "slow_release_override": c.get(11, [0])[0], "source": COMBO_SOURCE.get(c.get(12, [0])[0]),
    }


def combo_list(p, idx):
    resp = combo_call(p, idx, 1)
    return [combo_decode(cb) for cb in dec(resp.get(2, [b""])[0]).get(1, [])]


def combo_globals(p, idx):
    resp = combo_call(p, idx, 6)
    g = dec(dec(resp.get(5, [b""])[0]).get(1, [b""])[0])
    return {"timeout_ms": g.get(1, [0])[0], "slow_release": bool(g.get(2, [0])[0]),
            "max_combo": g.get(3, [0])[0], "require_prior_idle_ms": g.get(4, [0])[0]}


def combo_set(p, idx, index, positions, bid, p1, p2, layer_mask=0, timeout_ms=0, prior_idle_ms=0):
    """未保存 (persist=false) で書き込む。保存は combo_save"""
    behavior = f_varint(1, bid) + f_varint(2, p1) + f_varint(3, p2)
    body = (f_varint(1, index) + f_bytes(2, b"".join(enc_varint(x) for x in positions))
            + f_bytes(3, behavior) + f_varint(5, layer_mask) + f_varint(7, 1) + f_varint(8, 0)
            + f_varint(9, timeout_ms) + f_varint(10, prior_idle_ms))
    combo_call(p, idx, 3, body)


# ---- トラックボール (zmk-module-runtime-input-processor / proto/cormoran/rip/custom.proto) ----
# InputProcessorInfo のフィールド番号 → 名前
RIP_INFO = {1: "id", 2: "name", 3: "scale_multiplier", 4: "scale_divisor", 5: "rotation_degrees",
            6: "temp_layer_enabled", 7: "temp_layer_layer", 8: "temp_layer_activation_delay_ms",
            9: "temp_layer_deactivation_delay_ms", 10: "active_layers", 11: "axis_snap_mode",
            12: "axis_snap_threshold", 13: "axis_snap_timeout_ms", 14: "xy_to_scroll_enabled",
            15: "xy_swap_enabled", 16: "x_invert", 17: "y_invert", 18: "inertia_interval_ms",
            19: "inertia_threshold", 20: "inertia_window_ms", 21: "inertia_decay_percent",
            24: "inertia_notifications_enabled", 25: "inertia_active", 26: "inertia_fast_threshold",
            27: "inertia_fast_output_percent", 28: "inertia_enabled", 29: "inertia_normal_max_output"}
# 設定名 → Request の oneof 番号。どれも {id=1, 値=2, write_mode=3} の形
RIP_SET = {"scale_multiplier": 3, "scale_divisor": 4, "rotation_degrees": 5, "temp_layer_enabled": 7,
           "temp_layer_layer": 8, "temp_layer_activation_delay_ms": 9, "temp_layer_deactivation_delay_ms": 10,
           "active_layers": 11, "axis_snap_mode": 13, "axis_snap_threshold": 14, "axis_snap_timeout_ms": 15,
           "xy_to_scroll_enabled": 16, "xy_swap_enabled": 17, "x_invert": 18, "y_invert": 19,
           "inertia_interval_ms": 23, "inertia_threshold": 24, "inertia_window_ms": 25,
           "inertia_decay_percent": 26, "inertia_notifications_enabled": 29, "inertia_fast_threshold": 30,
           "inertia_fast_output_percent": 31, "inertia_enabled": 32, "inertia_normal_max_output": 33}


def rip_call(p, idx, req_field, req_body=b"", timeout=3.0):
    call = f_varint(1, idx) + f_bytes(2, f_bytes(req_field, req_body))
    r = p.call(100, f_bytes(2, call), timeout=timeout)
    outer = dec(r[2][0])
    if 2 not in outer:
        raise RuntimeError(f"rip: 応答なし {outer}")
    resp = dec(outer[2][0])
    if 1 in resp:
        raise RuntimeError(f"rip error: {dec(resp[1][0]).get(1, [b''])[0].decode()}")
    return resp


def rip_get(p, idx, pid):
    resp = rip_call(p, idx, 2, f_varint(1, pid))
    info = dec(dec(resp.get(3, [b""])[0]).get(1, [b""])[0])
    out = {}
    for f, name in RIP_INFO.items():
        v = info.get(f, [b"" if f == 2 else 0])[0]
        if f == 2:
            v = v.decode()
        elif f == 5 and v >= 1 << 63:  # int32 の負数
            v -= 1 << 64
        out[name] = v
    return out


def rip_list(p, idx, limit=16):
    procs = []
    for pid in range(limit):
        try:
            procs.append(rip_get(p, idx, pid))
        except (RuntimeError, SystemExit):
            break
    return procs


def combo_save(p, idx):
    resp = combo_call(p, idx, 9, timeout=15.0)  # フラッシュへの書き込みを待つ
    return dec(resp.get(4, [b""])[0]).get(2, [b""])[0].decode()


def mg_list(p, idx):
    resp = mg_call(p, idx, f_bytes(1, b""))
    out = []
    for gb in dec(resp.get(2, [b""])[0]).get(1, []):
        g = dec(gb)
        pat = g.get(3, [b""])[0]
        dirs = []
        for v in dec(pat).get(1, []):
            dirs += packed_varints(v) if isinstance(v, bytes) else [v]
        b = dec(g.get(4, [b""])[0])
        out.append({
            "id": g.get(1, [0])[0], "name": g.get(2, [b""])[0].decode(),
            "pattern": [MG_DIR.get(d, d) for d in dirs],
            "behavior": b.get(1, [b""])[0].decode(), "param1": b.get(2, [0])[0],
            "enabled": bool(g.get(5, [0])[0]), "set": g.get(6, [0])[0],
        })
    return out


def main():
    cmd = sys.argv[1]
    if cmd in ("gestures", "gesture-delete"):
        p, path = open_studio_port()
        print(f"# port {path}", file=sys.stderr)
        try:
            idx = mg_index_of(p)
            if cmd == "gesture-delete":
                gid = int(sys.argv[2])
                resp = mg_call(p, idx, f_bytes(5, f_varint(1, gid)))
                if 1 in resp:
                    raise SystemExit(f"error: {dec(resp[1][0])}")
            for g in mg_list(p, idx):
                print(f"id={g['id']} set={g['set']} {'→'.join(g['pattern'])} {g['behavior']} 0x{g['param1']:08X} {g['name']} enabled={g['enabled']}")
        finally:
            p.close()
        return
    p, path = open_studio_port()
    print(f"# port {path}", file=sys.stderr)
    try:
        names = behaviors(p)
        by_name = {v: k for k, v in names.items()}
        if cmd in ("trackball", "trackball-set"):
            idx = subsystem_index(p, "cormoran_rip")
            if cmd == "trackball-set":
                # trackball-set <id> <設定名> <値>  (例: trackball-set 0 temp_layer_deactivation_delay_ms 10000)
                pid, key, val = int(sys.argv[2]), sys.argv[3], int(sys.argv[4], 0)
                if key not in RIP_SET:
                    raise SystemExit(f"未対応の設定: {key}\n対応: {', '.join(RIP_SET)}")
                # write_mode=0 (PERSIST): 値を更新してフラッシュにも保存する
                rip_call(p, idx, RIP_SET[key], f_varint(1, pid) + f_varint(2, val) + f_varint(3, 0), timeout=15.0)
                got = rip_get(p, idx, pid)[key]
                if int(got) != val:
                    raise SystemExit(f"verify failed: {key}={got} (want {val})")
                print(f"set {key}={got} (id={pid})")
            for pr in rip_list(p, idx):
                print(f"== id={pr['id']} {pr['name']}")
                for k, v in pr.items():
                    if k not in ("id", "name"):
                        print(f"   {k} = {v}")
            return
        if cmd in ("combos", "combo-set", "combo-delete", "combo-reset", "combo-save"):
            idx = subsystem_index(p, "cormoran__runtime_combo")
            if cmd == "combo-set":
                # combo-set <slot> <pos,pos,...> "<behavior名>" [p1] [p2]
                slot, positions = int(sys.argv[2]), [int(x) for x in sys.argv[3].split(",")]
                bid = by_name[sys.argv[4]]
                p1 = int(sys.argv[5], 0) if len(sys.argv) > 5 else 0
                p2 = int(sys.argv[6], 0) if len(sys.argv) > 6 else 0
                combo_set(p, idx, slot, positions, bid, p1, p2)
                got = next(c for c in combo_list(p, idx) if c["index"] == slot)
                if (got["positions"], got["bid"], got["p1"], got["p2"], got["enabled"]) != (positions, bid, p1, p2, True):
                    raise SystemExit(f"verify failed (未保存のまま): {got}")
                print("save:", combo_save(p, idx))
            elif cmd == "combo-save":
                print("save:", combo_save(p, idx))
            elif cmd in ("combo-delete", "combo-reset"):
                slot = int(sys.argv[2])
                # delete: persist=false で削除 → save / reset: 保存済みの上書きを消してファームの初期値へ戻す
                if cmd == "combo-delete":
                    combo_call(p, idx, 5, f_varint(1, slot) + f_varint(2, 0))
                    print("save:", combo_save(p, idx))
                else:
                    combo_call(p, idx, 12, f_varint(1, slot))
            g = combo_globals(p, idx)
            print(f"# timeout={g['timeout_ms']}ms prior_idle={g['require_prior_idle_ms']}ms "
                  f"slow_release={g['slow_release']} max={g['max_combo']}")
            for c in combo_list(p, idx):
                print(f"slot={c['index']:2d} {c['source']:10s} enabled={c['enabled']} pos={c['positions']} "
                      f"{names.get(c['bid'], c['bid'])} 0x{c['p1']:08X} 0x{c['p2']:08X} "
                      f"layers=0x{c['layer_mask']:X} timeout={c['timeout_ms']} name={c['name']!r}")
            return
        if cmd == "behaviors":
            for k, v in sorted(names.items(), key=lambda x: x[1]):
                print(f"{k}\t{v}")
        elif cmd == "keymap":
            want = {int(x) for x in sys.argv[2:]}
            for l in keymap(p):
                if want and l["id"] not in want:
                    continue
                print(f"== layer id={l['id']} {l['name']}")
                for i, (bid, p1, p2) in enumerate(l["bindings"]):
                    print(f"  {i:2d} {names.get(bid, bid)} 0x{p1:08X} 0x{p2:08X}")
        elif cmd == "check":
            plan = json.load(open(sys.argv[2]))
            current = {l["id"]: l["bindings"] for l in keymap(p)}
            diff = [e for e in plan if current[e["layer"]][e["pos"]] != (by_name[e["behavior"]], e.get("p1", 0), e.get("p2", 0))]
            print(f"checked={len(plan)} diff={len(diff)}")
            for e in diff:
                print("  DIFF", e.get("src"), e["layer"], e["pos"], current[e["layer"]][e["pos"]])
        elif cmd == "apply":
            plan = json.load(open(sys.argv[2]))
            current = {l["id"]: l["bindings"] for l in keymap(p)}
            changed = 0
            for e in plan:
                bid = by_name[e["behavior"]]
                want = (bid, e.get("p1", 0), e.get("p2", 0))
                if current[e["layer"]][e["pos"]] == want:
                    continue
                rc = set_binding(p, e["layer"], e["pos"], *want)
                if rc != 0:
                    raise SystemExit(f"set failed rc={rc} at {e}")
                changed += 1
            after = {l["id"]: l["bindings"] for l in keymap(p)}
            bad = [e for e in plan if after[e["layer"]][e["pos"]] != (by_name[e["behavior"]], e.get("p1", 0), e.get("p2", 0))]
            if bad:
                raise SystemExit(f"verify failed (未保存のまま): {bad[:5]}")
            print(f"changed={changed} verified={len(plan)}")
            if changed:
                print("save:", save(p))
        else:
            raise SystemExit(__doc__)
    finally:
        p.close()


if __name__ == "__main__":
    main()
