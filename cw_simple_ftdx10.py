"""CW Simple FTDX10（日本語名: CW最小限 FTDX10）

FTDX10 に USB（CAT）でつなぎ、TX クラリファイア（TX CLAR）で送信周波数だけをずらす。
受信周波数はそのまま（RX CLAR は OFF）。ほかに受信周波数のつまみ、キーヤー・ブレークイン・
CW スピード・RF パワー、フィルター、ログソフト用の中継（仮想 COM）がある。画面は日本語と英語を切り替えられる。

使う CAT コマンド（FTDX10 CAT Operation Reference Manual より）:
  ID;              → ID0761;   つながっている無線機が FTDX10 か確かめる
  IF;              → 状態（VFO-A 周波数・CLAR のずれ・RX/TX CLAR の ON/OFF）
  CF001+0500;      CLAR のずれを +500 Hz にする（P1=0 MAIN, P2=0, P3=1 周波数）
  CF00001000;      RX CLAR OFF・TX CLAR ON（P3=0 設定, P4=RX, P5=TX）
  CF00000000;      RX CLAR OFF・TX CLAR OFF
  RC;              CLAR のずれを 0 に戻す
  （予備）RU/RD/RT/XT … CF が効かなかったときに使う
"""

import json
import math
import os
import sys
import time
import tkinter as tk
from tkinter import ttk, messagebox

try:
    import serial
    import serial.tools.list_ports
except ImportError:  # selftest だけなら無くても動くようにする
    serial = None

VERSION = "2.1.1"
BAUDS = [38400, 19200, 9600, 4800]
MAX_OFFSET = 9990  # FTDX10 のクラリファイアは ±9.99 kHz
POLL_MS = 1000
DEFAULT_OFFSET = 50  # 初めて起動したときのずらす量（ユーザーがいちばんよく使う値）
KNOB_SIZE = 230      # つまみの大きさ（ピクセル）
DEG_PER_STEP = 10    # つまみを何度回すと 1 目盛り進むか
TUNE_SEND_MS = 60    # つまみを回したとき、この間隔でまとめて無線機に送る
TUNE_HOLD_S = 0.8    # 回し終わってから、この秒数は無線機の値を読みに行かない
RESTORE_GAP_S = 3.0  # 送信のずれが外れていたとき、戻しに行く間隔の最短
FREQ_CHANGE_WINDOW_S = 4.0  # 周波数を変えてからこの秒数の間にずれが外れたら「無線機が外した」と考えて戻す
# 周波数を変える CAT コマンド（中継でログソフトから来たら、上の「周波数を変えた」に数える）
FREQ_CHANGE_CODES = {"FA", "FB", "BS", "BU", "BD", "UP", "DN", "SV", "MA", "MB", "QR", "CH", "VM", "BA", "AB"}
FREQ_MIN, FREQ_MAX = 30_000, 75_000_000  # FTDX10 の受信範囲 30 kHz ～ 75 MHz
WPM_MIN, WPM_MAX = 4, 60     # KS コマンドの範囲
PWR_MIN, PWR_MAX = 5, 100    # PC コマンドの範囲（W）
PWR_PRESETS = (5, 10, 20, 50, 100)
RELAY_MS = 20                # 中継用の仮想 COM を見に行く間隔

# フィルター（CAT 説明書の SH・RF より）
WIDTH_SSB = (300, 400, 600, 850, 1100, 1200, 1500, 1650, 1800, 1950, 2100, 2250,
             2400, 2450, 2500, 2600, 2700, 2800, 2900, 3000, 3200, 3500, 4000)   # SH 01～23
WIDTH_CW = (50, 100, 150, 200, 250, 300, 350, 400, 450, 500, 600, 800,
            1200, 1400, 1700, 2000, 2400, 3000)                                 # SH 01～18（CW・RTTY・PSK）
ROOFING = (("1", "12 kHz"), ("2", "3 kHz"), ("4", "500 Hz"), ("5", "300 Hz"))    # 設定は RF0 + この数字
ROOF_HZ = {"1": 12000, "2": 3000, "4": 500, "5": 300}
# 読んだときの答え（RF0 + P3）。6～A が返ることがあるので、設定の数字に読みかえる
ROOFING_ANSWER = {"1": "1", "2": "2", "4": "4", "5": "5", "6": "1", "7": "2", "9": "4", "A": "5"}

# 中継（仮想 COM）で、どのコマンドが「読み出し」かを見分けるための表（CAT 説明書より）
# 読み出しのときに付く文字の数。表にないコマンドは 0（例: 'FA;'）。
READ_PARAMS = {c: 1 for c in ("AG BC CT DT GT IS KM LM MD ML NA NB NL NR OS PA PB PR "
                              "RA RF RG RI RL RM SF SH SM SQ").split()}
READ_PARAMS.update({"BP": 2, "CF": 3, "CN": 2, "CO": 2, "EM": 2, "EX": 6, "MR": 3, "MT": 3, "SS": 2})
# 設定だけで、返事が来ないコマンド
SET_ONLY = set("AB AM BA BD BM BS BU CH DN ED EN EU KY MA MB MW QI QR QS RC RD RU SV UP VM ZI".split())

MODES = {"1": "LSB", "2": "USB", "3": "CW-U", "4": "FM", "5": "AM", "6": "RTTY-L",
         "7": "CW-L", "8": "DATA-L", "9": "RTTY-U", "A": "DATA-FM", "B": "FM-N",
         "C": "DATA-U", "D": "AM-N", "E": "PSK", "F": "DATA-FM-N"}

# 画面の文字（日本語, 英語）。{ } の中は、あとで数字などが入る。
TEXTS = {
    "app_name": ("CW最小限 FTDX10", "CW Simple FTDX10"),
    "lang_btn": ("English", "日本語"),
    "f_conn": ("接続（USB）", "Connection (USB)"),
    "com_port": ("COM ポート", "COM port"),
    "refresh": ("更新", "Refresh"),
    "baud": ("速さ (bps)", "Speed (bps)"),
    "connect": ("接続", "Connect"),
    "disconnect": ("切断", "Disconnect"),
    "relay": ("中継する", "Relay"),
    "relay_hint": ("↑ ログソフト（CTESTWIN など）とつなぐ仮想 COM（com0com）の片方",
                   "↑ One side of the virtual COM pair (com0com) shared with your logger (e.g. CTESTWIN)"),
    "relaying": ("中継中", "Relaying"),
    "relay_failed": ("失敗", "Failed"),
    "relay_stopped": ("停止", "Stopped"),
    "f_offset": ("送信をずらす量（スライダーから手を離すと送ります）",
                 "TX offset (sent when you release the slider)"),
    "x10": ("×10（±1000 Hz）", "×10 (±1000 Hz)"),
    "offset_hint": ("接続すると、送信をこの量だけずらします。無線機の CLAR で変えると、ここも合わせて変わります",
                    "When connected, TX is shifted by this amount. Changing CLAR on the radio updates it here too."),
    "f_status": ("無線機の今の状態（つまみで受信周波数を変えられます）",
                 "Radio status (turn the knob to change the RX frequency)"),
    "rx": ("受信", "RX"),
    "tx": ("送信", "TX"),
    "step": ("1目盛り", "Step"),
    "txd_shift": ("受信から {d:+d} Hz", "{d:+d} Hz from RX"),
    "txd_same": ("受信と同じ", "Same as RX"),
    "clar_line": ("CLAR のずれ {o:+d} Hz ／ RX CLAR {rx} ／ TX CLAR {tx}",
                  "CLAR offset {o:+d} Hz / RX CLAR {rx} / TX CLAR {tx}"),
    "f_cw": ("CW・送信パワー", "CW / TX power"),
    "keyer": ("キーヤー", "Keyer"),
    "bkin": ("ブレークイン", "Break-in"),
    "wpm": ("CW スピード  {v} WPM", "CW speed  {v} WPM"),
    "pwr": ("RF パワー  {v} W", "RF power  {v} W"),
    "f_filter": ("フィルター", "Filter"),
    "roofing": ("ルーフィング", "Roofing"),
    "width": ("幅（WIDTH）  {v} Hz", "Width  {v} Hz"),
    "width_none": ("幅（WIDTH）  このモードでは変えられません", "Width  not adjustable in this mode"),
    "width_default": ("幅（WIDTH）  標準（フィルターに合わせた幅）", "Width  default (follows the roofing filter)"),
    # 画面の一番下のお知らせ
    "m_start": ("COM ポートを選んで「接続」を押してください。", "Choose the COM port and press \"Connect\"."),
    "m_wpm": ("CW スピードを {v} WPM にしました。", "CW speed set to {v} WPM."),
    "m_pwr": ("RF パワーを {v} W にしました。", "RF power set to {v} W."),
    "m_width": ("フィルターの幅を {v} Hz にしました。", "Filter width set to {v} Hz."),
    "m_roof": ("ルーフィングフィルターを {v} にしました。", "Roofing filter set to {v}."),
    "m_round": ("周波数を {step} Hz 単位にそろえました（{a} → {b} Hz）。",
                "Frequency rounded to {step} Hz ({a} → {b} Hz)."),
    "m_toggle": ("{name}を {state} にしました。", "{name} turned {state}."),
    "m_lost": ("通信が切れました: {e}", "Connection lost: {e}"),
    "m_relay_noport": ("中継に使う仮想 COM ポートが見つかりません。com0com を入れてから「更新」を押してください。",
                       "No virtual COM port for relaying. Install com0com, then press \"Refresh\"."),
    "m_relay_same": ("中継には、無線機とは別の COM ポート（com0com の仮想 COM）を選んでください。",
                     "For relaying, choose a COM port other than the radio's (a com0com virtual port)."),
    "m_relay_open_fail": ("中継用の {port} を開けませんでした: {e}", "Could not open {port} for relaying: {e}"),
    "m_relaying": ("{port} で中継しています。ログソフトは、{port} と組になっている方の COM につないでください。",
                   "Relaying on {port}. Connect your logger to the COM port paired with {port}."),
    "m_relay_stopped": ("中継が止まりました: {e}", "Relay stopped: {e}"),
    "m_disconnected": ("切断しました。", "Disconnected."),
    "e_noport": ("COM ポートが見つかりません。USB ケーブルと無線機の電源を確かめてください。",
                 "No COM port found. Check the USB cable and that the radio is turned on."),
    "m_connecting": ("{port} に接続しています…", "Connecting to {port}..."),
    "m_conn_fail": ("つながりませんでした。", "Could not connect."),
    "e_conn_fail": ("{port} で無線機の返事がありません。\n\n"
                    "・「Enhanced COM Port」の方を選んでいますか\n"
                    "・無線機の電源は入っていますか\n"
                    "・ほかのソフト（WSJT-X など）がこのポートを使っていませんか\n\n"
                    "くわしく: {info}",
                    "No answer from the radio on {port}.\n\n"
                    "- Did you choose the \"Enhanced COM Port\"?\n"
                    "- Is the radio turned on?\n"
                    "- Is another program (e.g. WSJT-X) using this port?\n\n"
                    "Details: {info}"),
    "m_connected": ("{port}・{baud} bps でつながりました。{note}", "Connected on {port} at {baud} bps.{note}"),
    "note_not_ftdx10": ("（FTDX10 以外の答え: {info}）", " (answer is not from an FTDX10: {info})"),
    "e_offset_range": ("ずらす量は ±{max} Hz までです。", "The offset must be within ±{max} Hz."),
    "m_restored": ("周波数を変えたら送信のずれが外れたので、{hz:+d} Hz に戻しました。",
                   "The frequency change cleared the TX offset, so it was set back to {hz:+d} Hz."),
    "m_shifted": ("送信を {hz:+d} Hz ずらしました（受信はそのまま）。", "TX shifted by {hz:+d} Hz (RX unchanged)."),
    "m_mismatch": ("設定を送りましたが、無線機の状態が思ったとおりになっていません。無線機の表示を確かめてください。",
                   "Settings were sent, but the radio is not in the expected state. Please check the radio."),
    "m_follow": ("無線機で送信のずれが {hz:+d} Hz に変わったので、合わせました。",
                 "The TX offset was changed to {hz:+d} Hz on the radio, so it was updated here."),
    "m_clar_off": ("無線機で TX CLAR が OFF（または RX CLAR が ON）になっています。スライダーを動かすと、TX CLAR ON に戻します。",
                   "TX CLAR is OFF (or RX CLAR is ON) on the radio. Move the slider to turn TX CLAR back on."),
}


def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


SETTINGS_PATH = os.path.join(app_dir(), "cw_simple_settings.json")
OLD_SETTINGS_PATH = os.path.join(app_dir(), "ftdx10_clar_settings.json")  # v1.x の名前。あれば引き継ぐ


# ---------------------------------------------------------------- CAT の文字列

def cmd_set_offset(hz):
    """CLAR のずれ（Hz）を設定するコマンド。例: +500 → 'CF001+0500;'"""
    hz = int(hz)
    if abs(hz) > MAX_OFFSET:
        raise ValueError(f"ずらす量は ±{MAX_OFFSET} Hz までです")
    sign = "-" if hz < 0 else "+"
    return f"CF001{sign}{abs(hz):04d};"


def cmd_clar_switch(rx_on, tx_on):
    return f"CF000{int(bool(rx_on))}{int(bool(tx_on))}000;"


def cmd_set_freq(hz):
    """VFO-A の周波数を設定するコマンド。例: 7041000 → 'FA007041000;'"""
    return f"FA{int(hz):09d};"


def round_to_step(hz, step):
    """step 単位で四捨五入（.5 は切り上げ）。例: 7006880, 100 → 7006900 ／ 7006880, 1000 → 7007000"""
    return int((int(hz) + step // 2) // step * step)


def step_for_freq(hz):
    """周波数の一番下の 0 でない桁に合う「1目盛り」。例: 7008715 → 1、7008710 → 10、7008700 → 100、7008000 → 1000"""
    hz = int(hz)
    for step in (1, 10, 100):
        if hz % (step * 10):
            return step
    return 1000


def width_table_for(mode):
    """モードに合ったフィルター幅の表（SH の 01～ に対応する Hz）。幅を変えられないモードは None。"""
    if mode in ("LSB", "USB", "DATA-L", "DATA-U"):
        return WIDTH_SSB
    if mode in ("CW-U", "CW-L", "RTTY-L", "RTTY-U", "PSK"):
        return WIDTH_CW
    return None


def width_top_index(table, roof_hz):
    """ルーフィングの幅を超えない、いちばん広い幅が表の何番目か（1 から数える。少なくとも 1）。"""
    return max(1, sum(1 for w in table if w <= roof_hz))


def cmd_set_width(i):
    """フィルター幅を設定するコマンド。例: 13 → 'SH0013;'（P1=0, P2=0, P3=13）"""
    return f"SH00{int(i):02d};"


def is_read_cmd(cmd):
    """中継で受けたコマンドが「読み出し」（無線機から返事が来る）かどうか。
    読み出しの形はコマンドごとに決まった数の文字だけ付く（例: 'FA;'・'MD0;'・'EX010101;'）。"""
    code, body = cmd[:2].upper(), cmd[2:-1]
    if code in SET_ONLY:
        return False
    return len(body) == READ_PARAMS.get(code, 0)


def split_cmds(buf):
    """受け取った文字をコマンドごとに分ける。→ (コマンドのリスト, 残り)"""
    parts = buf.split(";")
    cmds = [p.strip() + ";" for p in parts[:-1] if p.strip()]
    return cmds, parts[-1].lstrip()


def parse_if(ans):
    """IF の答え（28 文字）を読む。読めなければ None。"""
    if not ans or not ans.startswith("IF") or not ans.endswith(";") or len(ans) < 28:
        return None
    try:
        freq = int(ans[5:14])
        offset = int(ans[14:19])  # '+0500' のような形
    except ValueError:
        return None
    return {
        "freq": freq,
        "offset": offset,
        "rx_clar": ans[19] == "1",
        "tx_clar": ans[20] == "1",
        "mode": MODES.get(ans[21], "?"),
    }


def snap(v, step, limit):
    """v を step の倍数に丸め、±limit の中に収める。例: snap(37, 10, 100) → 40"""
    try:
        v = float(v)
    except (TypeError, ValueError):
        v = DEFAULT_OFFSET
    v = int(round(v / step)) * step
    return max(-limit, min(limit, v))


def fmt_freq(hz):
    """14250000 → '14.250.000'"""
    s = f"{hz:,}".replace(",", ".")
    return s


# ---------------------------------------------------------------- 無線機との通信

class Radio:
    def __init__(self, port_obj):
        self.ser = port_obj

    @classmethod
    def open(cls, port, baud):
        s = serial.Serial()
        s.port = port
        s.baudrate = baud
        s.bytesize = 8
        s.parity = "N"
        s.stopbits = 2  # FTDX10 の CAT は 8N2（1 でも多くの場合動く）
        s.timeout = 0.3
        s.write_timeout = 1
        s.rtscts = False
        s.dsrdtr = False
        s.rts = True   # メニュー「CAT RTS = ENABLE」でも送れるように RTS を上げておく
        s.dtr = False  # DTR は PTT に使う設定があるので下げたまま
        s.open()
        return cls(s)

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass

    def send(self, cmd):
        self.ser.write(cmd.encode("ascii"))
        self.ser.flush()

    def query(self, cmd):
        self.ser.reset_input_buffer()
        self.send(cmd)
        data = self.ser.read_until(b";")
        return data.decode("ascii", "replace").strip()

    def identify(self):
        return self.query("ID;")

    def read_value(self, code):
        """'KS;' → 'KS025;' のような答えから '025' を取り出す。読めなければ None。"""
        ans = self.query(code + ";")
        if ans.startswith(code) and ans.endswith(";") and len(ans) > len(code) + 1:
            return ans[len(code):-1]
        return None

    def read_int(self, code):
        v = self.read_value(code)
        try:
            return int(v) if v is not None else None
        except ValueError:
            return None

    def relay(self, cmd):
        """中継: ほかのソフトから来たコマンドを無線機に渡し、返事（無いときは ''）を返す。"""
        code = cmd[:2].upper()
        if code == "AI":
            # 「自動で状態を送り続ける」機能は、返事が混ざって中継が乱れるので使わせない
            return "AI0;" if cmd.upper() == "AI;" else ""
        if is_read_cmd(cmd):
            return self.query(cmd)
        self.send(cmd)
        return ""

    def status(self):
        return parse_if(self.query("IF;"))

    def apply_tx_offset(self, hz):
        """送信だけ hz ずらす。成功したら IF の結果を返す。"""
        self.send(cmd_set_offset(hz))
        self.send(cmd_clar_switch(rx_on=False, tx_on=True))
        st = self.status()
        if st and st["offset"] == hz and st["tx_clar"] and not st["rx_clar"]:
            return st
        # 予備: 古い形のコマンドでやり直す
        self.send("RC;")
        if hz > 0:
            self.send(f"RU{hz:04d};")
        elif hz < 0:
            self.send(f"RD{-hz:04d};")
        self.send("RT0;")
        self.send("XT1;")
        return self.status()

    def clear(self):
        self.send(cmd_clar_switch(rx_on=False, tx_on=False))
        self.send("RC;")
        return self.status()


def registry_serial_ports():
    """Windows の「HARDWARE\\DEVICEMAP\\SERIALCOMM」から COM ポートを読む。
    com0com の仮想 COM（use Ports class なし）は pyserial の一覧に出ないが、ここには載っている。
    → {'COM20': '\\Device\\com0com10', ...}"""
    try:
        import winreg
    except ImportError:
        return {}
    found = {}
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM") as k:
            i = 0
            while True:
                try:
                    dev, port, _ = winreg.EnumValue(k, i)
                except OSError:
                    break
                found[str(port)] = str(dev)
                i += 1
    except OSError:
        pass
    return found


def port_sort_key(name):
    digits = "".join(ch for ch in name if ch.isdigit())
    return (0 if name.upper().startswith("COM") else 1, int(digits) if digits else 0, name)


def list_serial_ports():
    """COM ポートの一覧（'COM7  説明' の形）。FTDX10 の Enhanced COM Port を先頭にする。"""
    desc = {}
    if serial:
        for p in serial.tools.list_ports.comports():
            desc[p.device] = p.description or ""
    for port, dev in registry_serial_ports().items():
        if port not in desc:
            desc[port] = "com0com (virtual COM)" if "com0com" in dev.lower() else dev
    names = sorted(desc, key=port_sort_key)
    names.sort(key=lambda n: 0 if "Enhanced" in desc[n] else 1)
    return [f"{n}  {desc[n]}" for n in names]


def find_radio(port, first_baud):
    """選んだ速さから順に試し、ID0761 が返ってきたらつながったとする。"""
    order = [first_baud] + [b for b in BAUDS if b != first_baud]
    last_err = None
    for baud in order:
        try:
            r = Radio.open(port, baud)
        except Exception as e:  # ポートが使用中など
            last_err = e
            break
        try:
            ans = r.identify()
            if ans.startswith("ID"):
                return r, baud, ans
        except Exception as e:
            last_err = e
        r.close()
    return None, None, last_err


# ---------------------------------------------------------------- 画面

class Knob(tk.Canvas):
    """マウスでつまんで回す、大きなつまみ。ホイールでも回せる。
    右回り（時計回り）で on_step(+n)、左回りで on_step(-n) を呼ぶ。"""

    RIDGES = 48

    def __init__(self, master, size, on_step, on_release=None):
        super().__init__(master, width=size, height=size, highlightthickness=0,
                         bg=master.winfo_toplevel().cget("bg"), cursor="hand2")
        self.size = size
        self.c = size / 2
        self.on_step = on_step
        self.angle = -math.pi / 2  # くぼみの位置（最初は真上）
        self.acc = 0.0
        self.prev = None
        r = size / 2 - 4
        self.r = r
        c = self.c
        # 外側のふち → 本体 → 中央の少し明るい面
        self.create_oval(c - r, c - r, c + r, c + r, fill="#1e1e1e", outline="#555", width=2)
        self.create_oval(c - r * 0.88, c - r * 0.88, c + r * 0.88, c + r * 0.88,
                         fill="#3a3a3a", outline="#222", width=2)
        self.create_oval(c - r * 0.62, c - r * 0.62, c + r * 0.62, c + r * 0.62,
                         fill="#474747", outline="#333")
        self.ridges = [self.create_line(0, 0, 0, 0, fill="#6a6a6a", width=2) for _ in range(self.RIDGES)]
        self.dimple = self.create_oval(0, 0, 0, 0, fill="#2a2a2a", outline="#888", width=2)
        self.redraw()
        self.bind("<ButtonPress-1>", self.press)
        self.bind("<B1-Motion>", self.drag)
        self.on_release = on_release
        self.bind("<ButtonRelease-1>", self.release)
        self.bind("<MouseWheel>", self.wheel)

    def redraw(self):
        c, r = self.c, self.r
        for i, line in enumerate(self.ridges):
            a = self.angle + i * 2 * math.pi / self.RIDGES
            self.coords(line, c + math.cos(a) * r * 0.89, c + math.sin(a) * r * 0.89,
                        c + math.cos(a) * r * 0.99, c + math.sin(a) * r * 0.99)
        dr, d = r * 0.11, r * 0.45
        x, y = c + math.cos(self.angle) * d, c + math.sin(self.angle) * d
        self.coords(self.dimple, x - dr, y - dr, x + dr, y + dr)

    def _angle_at(self, e):
        return math.atan2(e.y - self.c, e.x - self.c)

    def press(self, e):
        self.prev = self._angle_at(e)

    def release(self, e):
        self.prev = None
        if self.on_release:
            self.on_release()

    def drag(self, e):
        if self.prev is None:
            self.prev = self._angle_at(e)
            return
        # 中心のすぐ近くは角度がふらつくので使わない
        if math.hypot(e.x - self.c, e.y - self.c) < self.r * 0.15:
            return
        a = self._angle_at(e)
        d = (a - self.prev + math.pi) % (2 * math.pi) - math.pi  # 画面は y が下向きなので + が時計回り
        self.prev = a
        self.turn(math.degrees(d))

    def wheel(self, e):
        n = int(e.delta / 120) or (1 if e.delta > 0 else -1)
        self.turn(n * DEG_PER_STEP)

    def turn(self, deg):
        self.angle += math.radians(deg)
        self.redraw()
        self.acc += deg
        steps = int(self.acc / DEG_PER_STEP)
        if steps:
            self.acc -= steps * DEG_PER_STEP
            self.on_step(steps)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.resizable(False, False)
        # ウィンドウの左上とタスクバーのアイコン（exe なら exe に入れたアイコン、そうでなければ icon.ico）
        try:
            self.iconbitmap(default=sys.executable if getattr(sys, "frozen", False)
                            else os.path.join(app_dir(), "icon.ico"))
        except Exception:
            pass
        self.radio = None
        self.poll_job = None
        self.settings = self.load_settings()
        self.lang = "en" if self.settings.get("lang") == "en" else "ja"
        self.i18n = []              # (部品, 文字のキー)。言語を切り替えたら書きかえる
        self.last_msg = ("m_start", {})
        self.relay_state = ("", "")  # (文字のキー, 色)

        pad = {"padx": 8, "pady": 4}
        big = ("Yu Gothic UI", 14, "bold")

        # いちばん上: 名前と、言語の切り替えボタン
        top = ttk.Frame(self)
        top.grid(row=0, column=0, columnspan=2, sticky="ew", padx=8, pady=(6, 0))
        top.columnconfigure(0, weight=1)
        self.name_lbl = self.tw(ttk.Label(top, font=("Yu Gothic UI", 16, "bold")), "app_name")
        self.name_lbl.grid(row=0, column=0, sticky="w")
        self.lang_btn = self.tw(ttk.Button(top, width=10, command=self.toggle_lang), "lang_btn")
        self.lang_btn.grid(row=0, column=1, sticky="e")

        # 接続
        f1 = self.tw(ttk.LabelFrame(self), "f_conn")
        f1.grid(row=1, column=0, sticky="nsew", **pad)
        self.tw(ttk.Label(f1), "com_port").grid(row=0, column=0, **pad)
        self.port_var = tk.StringVar()
        self.port_box = ttk.Combobox(f1, textvariable=self.port_var, width=34, state="readonly")
        self.port_box.grid(row=0, column=1, sticky="w", **pad)
        self.tw(ttk.Button(f1, width=8, command=self.refresh_ports), "refresh").grid(row=0, column=2, **pad)
        self.tw(ttk.Label(f1), "baud").grid(row=1, column=0, **pad)
        self.baud_var = tk.StringVar(value=str(self.settings.get("baud", 38400)))
        ttk.Combobox(f1, textvariable=self.baud_var, values=[str(b) for b in BAUDS],
                     width=8, state="readonly").grid(row=1, column=1, sticky="w", **pad)
        self.conn_btn = ttk.Button(f1, width=10, command=self.toggle_connect)
        self.conn_btn.grid(row=1, column=2, **pad)
        # 中継（仮想 COM。CTESTWIN などのログソフトをこのソフト経由で無線機につなぐ）
        self.relay_on_var = tk.BooleanVar(value=bool(self.settings.get("relay_on", False)))
        self.tw(ttk.Checkbutton(f1, variable=self.relay_on_var, command=self.relay_changed),
                "relay").grid(row=2, column=0, sticky="w", **pad)
        self.relay_port_var = tk.StringVar()
        self.relay_box = ttk.Combobox(f1, textvariable=self.relay_port_var, width=34, state="readonly")
        self.relay_box.grid(row=2, column=1, sticky="w", **pad)
        self.relay_box.bind("<<ComboboxSelected>>", lambda e: self.relay_changed())
        self.relay_lbl = ttk.Label(f1, text="", width=9)
        self.relay_lbl.grid(row=2, column=2, **pad)
        self.tw(ttk.Label(f1, foreground="#666"), "relay_hint").grid(row=3, column=0, columnspan=3,
                                                                    sticky="w", padx=8)

        # ずらす量
        f2 = self.tw(ttk.LabelFrame(self), "f_offset")
        f2.grid(row=1, column=1, sticky="nsew", **pad)
        self.x10_var = tk.BooleanVar(value=bool(self.settings.get("x10", False)))
        self.off_var = tk.IntVar(value=0)   # スライダーの位置
        self.offset_hz = 0                  # 本当のずらす量（無線機で 10 Hz 単位に変えたときなど、スライダーの目盛りと違うことがある）
        self.off_lbl = ttk.Label(f2, text="", font=big, width=10, anchor="e")
        self.off_lbl.grid(row=0, column=0, sticky="w", **pad)
        self.tw(ttk.Checkbutton(f2, variable=self.x10_var, command=self.toggle_x10),
                "x10").grid(row=0, column=1, sticky="w")
        self.scale = tk.Scale(f2, variable=self.off_var, orient="horizontal", length=420,
                              showvalue=False, command=lambda v: self.slider_moved())
        self.scale.grid(row=1, column=0, columnspan=3, padx=8)
        self.scale.bind("<ButtonPress-1>", lambda ev: self.dragging.add("OFF"))
        self.scale.bind("<ButtonRelease-1>", lambda ev: self.slider_released())
        self.scale.bind("<KeyRelease>", lambda ev: self.slider_released())
        self.dragging = set()
        self.set_scale_range()
        # 初めて起動したときは +50 Hz、2 回目からは前に使った値
        self.set_offset_value(self.settings.get("offset", DEFAULT_OFFSET))

        # ボタンは無い。接続すると自動で TX CLAR ON になり、つないでいる間はずっとずらしたままにする。
        self.tw(ttk.Label(f2, foreground="#666", wraplength=440), "offset_hint").grid(
            row=2, column=0, columnspan=3, sticky="w", padx=8, pady=(2, 6))

        # 状態
        f3 = self.tw(ttk.LabelFrame(self), "f_status")
        f3.grid(row=2, column=1, rowspan=2, sticky="nsew", **pad)
        lf = ttk.Frame(f3)
        lf.grid(row=0, column=0, sticky="nw")
        self.tw(ttk.Label(lf), "rx").grid(row=0, column=0, sticky="w", padx=8, pady=(8, 0))
        self.rx_lbl = ttk.Label(lf, text="---", font=("Consolas", 22, "bold"))
        self.rx_lbl.grid(row=1, column=0, sticky="w", padx=8)
        self.mode_lbl = ttk.Label(lf, text="", font=("Consolas", 12))
        self.mode_lbl.grid(row=2, column=0, sticky="w", padx=8)
        self.tw(ttk.Label(lf), "tx").grid(row=3, column=0, sticky="w", padx=8, pady=(14, 0))
        self.tx_lbl = ttk.Label(lf, text="---", font=("Consolas", 22, "bold"))
        self.tx_lbl.grid(row=4, column=0, sticky="w", padx=8)
        self.txd_lbl = ttk.Label(lf, text="", font=("Consolas", 12))
        self.txd_lbl.grid(row=5, column=0, sticky="w", padx=8)

        kf = ttk.Frame(f3)
        kf.grid(row=0, column=1, padx=8, pady=4)
        self.knob = Knob(kf, size=KNOB_SIZE, on_step=self.on_knob, on_release=self.knob_released)
        self.knob.pack()
        sf = ttk.Frame(kf)
        sf.pack(pady=2)
        self.tw(ttk.Label(sf), "step").pack(side="left")
        self.tune_step_var = tk.IntVar(value=int(self.settings.get("tune_step", 10)))
        for v, t in ((1, "1 Hz"), (10, "10 Hz"), (100, "100 Hz"), (1000, "1 kHz")):
            # 押すと、今の周波数をその単位で四捨五入する（例: 100 Hz → x.xxx.880 が x.xxx.900）
            ttk.Radiobutton(sf, text=t, value=v, variable=self.tune_step_var,
                            command=self.round_freq).pack(side="left", padx=2)

        self.clar_lbl = ttk.Label(f3, text="CLAR ---")
        self.clar_lbl.grid(row=1, column=0, columnspan=2, sticky="w", **pad)

        # つまみで回している周波数（無線機へ送るまでの間の値）
        self.last_st = None
        self.last_restore = 0.0
        self.last_freq_change = 0.0  # このソフトやログソフトが周波数を変えた時刻
        self.tune_target = None
        self.tune_job = None
        self.last_tune = 0.0

        # CW・送信パワー（左下）。キーヤーの横に CW スピード、ブレークインの横に RF パワー。
        f4 = self.tw(ttk.LabelFrame(self), "f_cw")
        f4.grid(row=2, column=0, sticky="nsew", **pad)
        self.toggles = {}
        mid = ("Yu Gothic UI", 12, "bold")

        def toggle_row(row, code, name):
            self.tw(ttk.Label(f4, font=mid), name).grid(row=row, column=0, sticky="w", padx=(8, 2), pady=(8, 0))
            b = tk.Button(f4, text="---", width=5, font=mid, command=lambda: self.toggle_switch(code))
            b.grid(row=row, column=1, rowspan=2, sticky="w", padx=4, pady=(8, 0))
            self.toggles[code] = {"btn": b, "on": None}

        toggle_row(0, "KR", "keyer")
        self.wpm_var = tk.IntVar(value=WPM_MIN)
        self.wpm_known = False  # 無線機から読めたか（読めるまでは --- と出す）
        self.wpm_lbl = ttk.Label(f4, font=mid)
        self.wpm_lbl.grid(row=0, column=2, sticky="w", padx=8, pady=(8, 0))
        self.wpm_scale = self.make_scale(f4, self.wpm_var, WPM_MIN, WPM_MAX, 1, "KS", self.update_wpm_label)
        self.wpm_scale.grid(row=1, column=2, sticky="w", padx=8)

        ttk.Separator(f4).grid(row=2, column=0, columnspan=3, sticky="ew", pady=8)

        toggle_row(3, "BI", "bkin")
        self.pwr_var = tk.IntVar(value=PWR_MIN)
        self.pwr_known = False
        self.pwr_lbl = ttk.Label(f4, font=mid)
        self.pwr_lbl.grid(row=3, column=2, sticky="w", padx=8, pady=(8, 0))
        self.pwr_scale = self.make_scale(f4, self.pwr_var, PWR_MIN, PWR_MAX, 1, "PC", self.update_pwr_label)
        self.pwr_scale.grid(row=4, column=2, sticky="w", padx=8)
        pf = ttk.Frame(f4)
        pf.grid(row=5, column=2, sticky="w", padx=8, pady=(2, 8))
        self.pwr_btns = []
        for w in PWR_PRESETS:
            b = ttk.Button(pf, text=f"{w}W", width=5, command=lambda w=w: self.set_power(w))
            b.pack(side="left", padx=1)
            self.pwr_btns.append(b)

        # フィルター（左下、④ の下）
        f5 = self.tw(ttk.LabelFrame(self), "f_filter")
        f5.grid(row=3, column=0, sticky="nsew", **pad)
        self.tw(ttk.Label(f5, font=mid), "roofing").grid(row=0, column=0, sticky="w", padx=8, pady=(8, 2))
        rf = ttk.Frame(f5)
        rf.grid(row=0, column=1, sticky="w", padx=4, pady=(8, 2))
        self.roof_var = tk.StringVar(value="")
        self.roof_btns = []
        for code, text in ROOFING:
            b = ttk.Radiobutton(rf, text=text, value=code, variable=self.roof_var,
                                command=self.set_roofing)
            b.pack(side="left", padx=3)
            self.roof_btns.append(b)
        self.width_var = tk.IntVar(value=1)
        self.width_lbl = ttk.Label(f5, font=mid)
        self.width_lbl.grid(row=1, column=0, columnspan=2, sticky="w", padx=8, pady=(8, 0))
        wf = ttk.Frame(f5)
        wf.grid(row=2, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 8))
        wf.columnconfigure(1, weight=1)
        self.width_scale = self.make_scale(wf, self.width_var, 1, 23, 1, "SH", self.update_width_label)
        self.width_scale.config(length=400)
        self.width_scale.grid(row=0, column=0, columnspan=2, sticky="w")
        # スライダーの両端の値（ルーフィングに合わせて変わる）
        self.width_min_lbl = ttk.Label(wf, text="", foreground="#666")
        self.width_min_lbl.grid(row=1, column=0, sticky="w")
        self.width_max_lbl = ttk.Label(wf, text="", foreground="#666")
        self.width_max_lbl.grid(row=1, column=1, sticky="e")
        self.width_table = None  # 今のモードで使える幅の表（Hz のリスト）
        self.width_top = 0       # スライダーの右端（表の何番目まで使うか。ルーフィングで決まる）
        self.width_default = False  # 無線機が「標準の幅」（SH の答えが 0）のとき

        self.msg_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.msg_var, foreground="#555",
                  wraplength=900).grid(row=4, column=0, columnspan=2, sticky="w", **pad)

        self.relay_ser = None
        self.relay_buf = ""
        self.relay_job = None

        self.refresh_ports()
        self.set_connected(False)
        self.refresh_texts()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------------- 言語（日本語・英語）
    def t(self, key, **kw):
        """今の言語の文字を返す。kw の値が '@キー' なら、それも訳す（例: name='@keyer'）。"""
        ja, en = TEXTS[key]
        s = en if self.lang == "en" else ja
        if not kw:
            return s
        kw = {k: (self.t(v[1:]) if isinstance(v, str) and v.startswith("@") else v) for k, v in kw.items()}
        return s.format(**kw)

    def tw(self, widget, key):
        """部品に文字を付け、言語を切り替えたときに書きかえる一覧に入れる。"""
        self.i18n.append((widget, key))
        widget.config(text=self.t(key))
        return widget

    def say(self, key, **kw):
        """画面の一番下にお知らせを出す（言語を切り替えたら訳し直せるように覚えておく）。"""
        self.last_msg = (key, kw)
        self.msg_var.set(self.t(key, **kw))

    def set_relay_state(self, key, color=""):
        self.relay_state = (key, color)
        self.relay_lbl.config(text=self.t(key) if key else "", foreground=color)

    def toggle_lang(self):
        self.lang = "en" if self.lang == "ja" else "ja"
        self.refresh_texts()
        self.save_settings()

    def refresh_texts(self):
        self.title(f"{self.t('app_name')}  v{VERSION}")
        for widget, key in self.i18n:
            widget.config(text=self.t(key))
        self.conn_btn.config(text=self.t("disconnect" if self.radio else "connect"))
        self.set_relay_state(*self.relay_state)
        self.update_wpm_label()
        self.update_pwr_label()
        self.update_width_label()
        self.show_status(self.last_st, keep=True)
        key, kw = self.last_msg
        self.say(key, **kw)

    def update_wpm_label(self):
        v = self.wpm_var.get() if self.wpm_known else "---"
        self.wpm_lbl.config(text=self.t("wpm", v=v))

    def update_pwr_label(self):
        v = self.pwr_var.get() if self.pwr_known else "---"
        self.pwr_lbl.config(text=self.t("pwr", v=v))

    # 設定の保存
    def load_settings(self):
        for path in (SETTINGS_PATH, OLD_SETTINGS_PATH):  # 新しい名前が無ければ v1.x の設定を引き継ぐ
            try:
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def save_settings(self):
        data = {"port": self.port_var.get().split(" ")[0], "baud": int(self.baud_var.get()),
                "offset": int(self.offset_hz), "x10": bool(self.x10_var.get()),
                "tune_step": int(self.tune_step_var.get()),
                "relay_on": bool(self.relay_on_var.get()),
                "relay_port": self.relay_port_var.get().split(" ")[0],
                "lang": self.lang}
        try:
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception:
            pass

    def refresh_ports(self):
        items = list_serial_ports()
        self.port_box["values"] = items
        saved = self.settings.get("port")
        pick = next((i for i in items if i.split(" ")[0] == saved), None)
        if pick is None and items:
            pick = items[0]
        self.port_var.set(pick or "")
        # 中継用: com0com の仮想 COM を先頭にする
        relay_items = sorted(items, key=lambda i: 0 if "com0com" in i.lower() else 1)
        self.relay_box["values"] = relay_items
        saved = self.settings.get("relay_port")
        pick = next((i for i in relay_items if i.split(" ")[0] == saved), None)
        if pick is None:
            pick = next((i for i in relay_items if "com0com" in i.lower()), "")
        self.relay_port_var.set(pick)

    def set_connected(self, ok):
        st = "normal" if ok else "disabled"
        for b in self.roof_btns:
            b.config(state=st)
        self.width_scale.config(state=st if (ok and self.width_table) else "disabled")
        for t in self.toggles.values():
            t["btn"].config(state=st)
        self.wpm_scale.config(state=st)
        self.pwr_scale.config(state=st)
        for b in self.pwr_btns:
            b.config(state=st)
        self.conn_btn.config(text=self.t("disconnect" if ok else "connect"))

    # ---------------- CW・送信パワー
    def make_scale(self, parent, var, lo, hi, step, code, on_move):
        s = tk.Scale(parent, variable=var, orient="horizontal", length=250, from_=lo, to=hi,
                     resolution=step, showvalue=False, tickinterval=0,
                     command=lambda v: on_move())
        s.on_move = on_move
        s.bind("<ButtonPress-1>", lambda e: self.dragging.add(code))
        s.bind("<ButtonRelease-1>", lambda e: self.scale_released(code))
        s.bind("<KeyRelease>", lambda e: self.scale_released(code))
        return s

    def scale_released(self, code):
        self.dragging.discard(code)
        if code == "KS":
            self.send_setting(f"KS{self.wpm_var.get():03d};", "m_wpm", v=self.wpm_var.get())
        elif code == "PC":
            self.send_setting(f"PC{self.pwr_var.get():03d};", "m_pwr", v=self.pwr_var.get())
        elif code == "SH":
            self.width_default = False
            if self.width_table:
                i = self.width_var.get()
                self.send_setting(cmd_set_width(i), "m_width", v=self.width_table[i - 1])

    # ---------------- フィルター
    def update_width_label(self):
        if self.width_default and self.width_table:
            self.width_lbl.config(text=self.t("width_default"))
        elif self.width_table:
            i = max(1, min(self.width_top, self.width_var.get()))
            self.width_lbl.config(text=self.t("width", v=self.width_table[i - 1]))
        else:
            self.width_lbl.config(text=self.t("width_none"))

    def set_width_range(self, mode, roof):
        """モードとルーフィングに合わせて、幅スライダーの範囲を決める。
        例: CW で 300 Hz のルーフィング → 左端 50 Hz・右端 300 Hz。"""
        table = width_table_for(mode)
        top = width_top_index(table, ROOF_HZ.get(roof, 12000)) if table else 0
        if table is self.width_table and top == self.width_top:
            return
        self.width_table, self.width_top = table, top
        if table:
            self.width_scale.config(from_=1, to=top, state="normal" if self.radio else "disabled")
            if self.width_var.get() > top:
                self.width_var.set(top)
            self.width_min_lbl.config(text=f"{table[0]} Hz")
            self.width_max_lbl.config(text=f"{table[top - 1]} Hz")
        else:
            self.width_scale.config(state="disabled")
            self.width_min_lbl.config(text="")
            self.width_max_lbl.config(text="")
        self.update_width_label()

    def set_roofing(self):
        code = self.roof_var.get()
        text = dict(ROOFING).get(code, "")
        if self.send_setting(f"RF0{code};", "m_roof", v=text):
            self.set_width_range(self.last_st["mode"] if self.last_st else None, code)

    def round_freq(self):
        """「1目盛り」を押したら、今の周波数をその単位で四捨五入して無線機に送る。"""
        self.save_settings()
        if not self.radio or not self.last_st:
            return
        step = self.tune_step_var.get()
        base = self.tune_target if self.tune_target is not None else self.last_st["freq"]
        new = round_to_step(base, step)
        if new == base:
            return
        self.tune_target = new
        self.last_tune = time.monotonic()
        self.show_status(dict(self.last_st, freq=new), keep=True)
        self.flush_tune()
        self.say("m_round", step=step, a=fmt_freq(base), b=fmt_freq(new))

    def set_power(self, w):
        self.pwr_var.set(w)
        self.pwr_scale.on_move()
        self.scale_released("PC")

    def toggle_switch(self, code):
        t = self.toggles[code]
        new = not t["on"]
        name = "@keyer" if code == "KR" else "@bkin"
        if self.send_setting(f"{code}{int(new)};", "m_toggle", name=name, state="ON" if new else "OFF"):
            self.refresh_cw()

    def send_setting(self, cmd, key, **kw):
        if not self.radio:
            return False
        try:
            self.radio.send(cmd)
        except Exception as e:
            self.disconnect("m_lost", e=e)
            return False
        self.say(key, **kw)
        return True

    def show_toggle(self, code, on):
        t = self.toggles[code]
        t["on"] = on
        if on is None:
            t["btn"].config(text="---", bg="SystemButtonFace", fg="black")
        elif on:
            t["btn"].config(text="ON", bg="#2e7d32", fg="white")
        else:
            t["btn"].config(text="OFF", bg="#d0d0d0", fg="black")

    def refresh_cw(self):
        """キーヤー・ブレークイン・CW スピード・パワーを無線機から読んで表示する。"""
        r = self.radio
        if not r:
            return
        for code in ("KR", "BI"):
            v = r.read_value(code)
            self.show_toggle(code, None if v is None else v == "1")
        for code, var, scale in (("KS", self.wpm_var, self.wpm_scale), ("PC", self.pwr_var, self.pwr_scale)):
            if code in self.dragging:  # 動かしている最中は上書きしない
                continue
            v = r.read_int(code)
            if v is not None:
                var.set(v)
                if code == "KS":
                    self.wpm_known = True
                else:
                    self.pwr_known = True
                scale.on_move()
        # フィルター
        roof = r.read_value("RF0")
        if roof:
            self.roof_var.set(ROOFING_ANSWER.get(roof.upper(), ""))
        self.set_width_range(self.last_st["mode"] if self.last_st else None, self.roof_var.get())
        if self.width_table and "SH" not in self.dragging:
            v = r.read_value("SH0")  # 答え 'SH00xx;' → '0xx'
            try:
                i = int(v) if v is not None else 0
            except ValueError:
                i = 0
            if i >= 1:
                # 無線機の幅がルーフィングより広いとき（本体のつまみを速く回したときなど）は、
                # スライダーを右端（ルーフィングに合わせた一番広い幅）に置く
                self.width_default = False
                self.width_var.set(min(i, self.width_top))
                self.update_width_label()
            elif i == 0:
                self.width_default = True
                self.update_width_label()

    # ---------------- 中継（仮想 COM）
    def relay_changed(self):
        self.save_settings()
        self.stop_relay()
        if self.radio and self.relay_on_var.get():
            self.start_relay()

    def start_relay(self):
        port = self.relay_port_var.get().split(" ")[0]
        radio_port = self.port_var.get().split(" ")[0]
        if not port:
            self.say("m_relay_noport")
            return
        if port == radio_port:
            self.say("m_relay_same")
            return
        try:
            s = serial.Serial()
            # com0com の名前（CNCA0 など）は '\\.\' を付けないと開けない
            s.port = port if port.upper().startswith("COM") else "\\\\.\\" + port
            s.baudrate = int(self.baud_var.get())
            s.timeout = 0
            s.write_timeout = 1
            s.open()
        except Exception as e:
            self.set_relay_state("relay_failed", "#c62828")
            self.say("m_relay_open_fail", port=port, e=e)
            return
        self.relay_ser = s
        self.relay_buf = ""
        self.set_relay_state("relaying", "#2e7d32")
        self.say("m_relaying", port=port)
        self.relay_loop()

    def stop_relay(self):
        if self.relay_job:
            self.after_cancel(self.relay_job)
            self.relay_job = None
        if self.relay_ser:
            try:
                self.relay_ser.close()
            except Exception:
                pass
            self.relay_ser = None
        self.set_relay_state("")

    def relay_loop(self):
        self.relay_job = None
        if not self.relay_ser or not self.radio:
            return
        changed = False
        try:
            n = self.relay_ser.in_waiting
            if n:
                self.relay_buf += self.relay_ser.read(n).decode("ascii", "replace")
                cmds, self.relay_buf = split_cmds(self.relay_buf)
                self.relay_buf = self.relay_buf[-64:]  # ; が来ないごみは捨てる
                for cmd in cmds:
                    try:
                        ans = self.radio.relay(cmd)
                    except Exception as e:
                        self.disconnect("m_lost", e=e)
                        return
                    if ans:
                        self.relay_ser.write(ans.encode("ascii"))
                    elif not is_read_cmd(cmd):
                        changed = True
                        if cmd[:2].upper() in FREQ_CHANGE_CODES:  # ログソフトが周波数を変えた
                            self.last_freq_change = time.monotonic()
                            try:  # 外れた送信のずれを、すぐ送り直す
                                self.radio.send(cmd_set_offset(self.offset_hz))
                                self.radio.send(cmd_clar_switch(rx_on=False, tx_on=True))
                            except Exception as e:
                                self.disconnect("m_lost", e=e)
                                return
        except Exception as e:
            self.stop_relay()
            self.set_relay_state("relay_stopped", "#c62828")
            self.say("m_relay_stopped", e=e)
            return
        if changed:  # ログソフトが何か変えたら、早めに表示を読み直す
            self.refresh_soon()
        self.relay_job = self.after(RELAY_MS, self.relay_loop)

    def refresh_soon(self):
        if self.poll_job:
            self.after_cancel(self.poll_job)
        self.poll_job = self.after(150, self.poll)

    def toggle_connect(self):
        if self.radio:
            self.disconnect("m_disconnected")
            return
        port = self.port_var.get().split(" ")[0]
        if not port:
            messagebox.showwarning(self.t("app_name"), self.t("e_noport"))
            return
        self.say("m_connecting", port=port)
        self.update()
        r, baud, info = find_radio(port, int(self.baud_var.get()))
        if not r:
            self.say("m_conn_fail")
            messagebox.showerror(self.t("app_name"), self.t("e_conn_fail", port=port, info=info))
            return
        self.radio = r
        self.baud_var.set(str(baud))
        note = "" if info == "ID0761;" else self.t("note_not_ftdx10", info=info)
        self.say("m_connected", port=port, baud=baud, note=note)
        self.save_settings()
        self.set_connected(True)
        self.poll()
        # 今の周波数の一番下の 0 でない桁に合わせて「1目盛り」を選ぶ（例: 7.008.715 → 1 Hz、7.008.700 → 100 Hz）
        if self.last_st:
            self.tune_step_var.set(step_for_freq(self.last_st["freq"]))
        # 起動して接続したら、すぐにスライダーの量で送信をずらす（TX CLAR ON）
        self.apply()
        if self.relay_on_var.get():
            self.start_relay()

    def disconnect(self, key, **kw):
        self.stop_relay()
        for code in self.toggles:
            self.show_toggle(code, None)
        for job in (self.poll_job, self.tune_job):
            if job:
                self.after_cancel(job)
        self.poll_job = self.tune_job = None
        self.tune_target = None
        self.last_tune = 0.0  # つないだ直後に、すぐ周波数を読めるように
        if self.radio:
            self.radio.close()
            self.radio = None
        self.set_connected(False)
        self.wpm_known = self.pwr_known = False
        self.update_wpm_label()
        self.update_pwr_label()
        self.say(key, **kw)
        self.show_status(None)

    # スライダー
    def scale_max(self):
        return 1000 if self.x10_var.get() else 100

    def scale_step(self):
        return 100 if self.x10_var.get() else 10

    def set_scale_range(self):
        # resolution を 10（×10 のときは 100）にすると、その数字ごとにカチッと止まる
        m = self.scale_max()
        self.scale.config(from_=-m, to=m, resolution=self.scale_step(), tickinterval=m // 2)

    def set_offset_value(self, v):
        self.offset_hz = snap(v, self.scale_step(), self.scale_max())
        self.off_var.set(self.offset_hz)
        self.update_off_label()

    def slider_moved(self):
        # 人が動かしているときだけ、スライダーの値をずらす量にする
        # （無線機に合わせてスライダーを動かしたときは、本当の値を目盛りで丸めないように）
        if "OFF" in self.dragging:
            self.offset_hz = int(self.off_var.get())
            self.update_off_label()

    def slider_released(self):
        self.dragging.discard("OFF")
        self.offset_hz = int(self.off_var.get())
        self.update_off_label()
        self.apply()

    def toggle_x10(self):
        """×10 を切り替えたら、ずれの量も 10 倍（または 1/10）にする。"""
        v = self.offset_hz
        v = v * 10 if self.x10_var.get() else v / 10
        self.set_scale_range()
        self.set_offset_value(v)
        self.apply()

    def follow_radio_offset(self, hz):
        """無線機の CLAR で変えたずれに、スライダーと表示を合わせる（無線機には送らない）。"""
        self.offset_hz = int(hz)
        want_x10 = abs(hz) > 100  # ±100 Hz を超えたら ×10 の目盛りにする
        if want_x10 != bool(self.x10_var.get()):
            self.x10_var.set(want_x10)
            self.set_scale_range()
        m, step = self.scale_max(), self.scale_step()
        self.off_var.set(max(-m, min(m, int(round(hz / step)) * step)))
        self.update_off_label()
        self.save_settings()

    def update_off_label(self):
        self.off_lbl.config(text=f"{self.offset_hz:+d} Hz")

    def get_offset(self):
        hz = int(self.offset_hz)
        if abs(hz) > MAX_OFFSET:
            raise ValueError(self.t("e_offset_range", max=MAX_OFFSET))
        return hz

    def apply(self, restored=False):
        if not self.radio:
            return
        try:
            hz = self.get_offset()
        except ValueError as e:
            messagebox.showwarning(self.t("app_name"), str(e))
            return
        try:
            st = self.radio.apply_tx_offset(hz)
        except Exception as e:
            self.disconnect("m_lost", e=e)
            return
        self.save_settings()
        self.show_status(st)
        if st and st["tx_clar"] and st["offset"] == hz:
            if restored:
                self.say("m_restored", hz=hz)
            else:
                self.say("m_shifted", hz=hz)
        else:
            self.say("m_mismatch")

    def sync_tx_shift(self, st):
        """無線機の送信のずれと、このソフトのずらす量をそろえる。
        - このソフト（つまみ・四捨五入）や中継したログソフトで周波数を変えた直後に、ずれが外れていたら
          → 無線機が外したと考えて、元のずれに戻す。
        - それ以外でずれが違っていたら → 無線機の CLAR で人が変えたと考えて、このソフトが合わせる。"""
        if not st or not self.radio or self.tuning_now() or "OFF" in self.dragging:
            return
        hz = self.offset_hz
        if st["tx_clar"] and not st["rx_clar"] and st["offset"] == hz:
            return
        now = time.monotonic()
        if now - self.last_freq_change < FREQ_CHANGE_WINDOW_S:
            if now - self.last_restore >= RESTORE_GAP_S:  # 直らないときに送り続けない
                self.last_restore = now
                self.apply(restored=True)
            return
        if st["tx_clar"] and not st["rx_clar"]:
            self.follow_radio_offset(st["offset"])
            self.say("m_follow", hz=st["offset"])
        else:
            self.say("m_clar_off")

    # つまみ
    def tuning_now(self):
        return self.tune_job is not None or time.monotonic() - self.last_tune < TUNE_HOLD_S

    def on_knob(self, steps):
        if not self.radio or not self.last_st:
            return
        base = self.tune_target if self.tune_target is not None else self.last_st["freq"]
        self.tune_target = max(FREQ_MIN, min(FREQ_MAX, base + steps * self.tune_step_var.get()))
        self.last_tune = time.monotonic()
        self.show_status(dict(self.last_st, freq=self.tune_target), keep=True)
        if self.tune_job is None:  # 送りすぎないよう、少しまとめてから送る
            self.tune_job = self.after(TUNE_SEND_MS, self.flush_tune)

    def flush_tune(self):
        self.tune_job = None
        if not self.radio or self.tune_target is None:
            return
        try:
            self.radio.send(cmd_set_freq(self.tune_target))
            # 無線機は周波数を変えると送信のずれを外すので、すぐ後ろでずれ（TX CLAR ON）を送り直す
            self.radio.send(cmd_set_offset(self.offset_hz))
            self.radio.send(cmd_clar_switch(rx_on=False, tx_on=True))
        except Exception as e:
            self.disconnect("m_lost", e=e)
            return
        self.last_tune = self.last_freq_change = time.monotonic()

    def knob_released(self):
        """つまみから手を離したら、待たずに最後の周波数と送信のずれを送る。"""
        if self.tune_job is not None:
            self.after_cancel(self.tune_job)
            self.flush_tune()

    def poll(self):
        self.poll_job = None
        if not self.radio:
            return
        if not self.tuning_now():  # 回している間は、表示が古い値に戻らないよう読まない
            try:
                st = self.radio.status()
            except Exception as e:
                self.disconnect("m_lost", e=e)
                return
            self.tune_target = None
            self.show_status(st)
            self.sync_tx_shift(st)
            if not self.radio:
                return
            try:
                self.refresh_cw()
            except Exception as e:
                self.disconnect("m_lost", e=e)
                return
        if self.poll_job is None:  # 中継の refresh_soon で別に予約されていなければ
            self.poll_job = self.after(POLL_MS, self.poll)

    def show_status(self, st, keep=False):
        if not keep:
            self.last_st = st
        if not st:
            self.rx_lbl.config(text="---")
            self.mode_lbl.config(text="")
            self.tx_lbl.config(text="---", foreground="")
            self.txd_lbl.config(text="")
            self.clar_lbl.config(text="CLAR ---")
            return
        rx = st["freq"] + (st["offset"] if st["rx_clar"] else 0)
        tx = st["freq"] + (st["offset"] if st["tx_clar"] else 0)
        self.rx_lbl.config(text=f"{fmt_freq(rx)} Hz")
        self.mode_lbl.config(text=st["mode"])
        shifted = tx != rx
        self.tx_lbl.config(text=f"{fmt_freq(tx)} Hz", foreground="#c62828" if shifted else "")
        self.txd_lbl.config(text=self.t("txd_shift", d=tx - rx) if shifted else self.t("txd_same"))
        self.clar_lbl.config(text=self.t("clar_line", o=st["offset"], rx="ON" if st["rx_clar"] else "OFF",
                                         tx="ON" if st["tx_clar"] else "OFF"))

    def on_close(self):
        self.save_settings()
        self.stop_relay()
        if self.radio:
            self.radio.close()
        self.destroy()


# ---------------------------------------------------------------- 自己テスト

class FakeFTDX10:
    """本物の代わりに CAT コマンドに答える、テスト用のにせ無線機。"""

    def __init__(self):
        self.freq, self.offset, self.rx, self.tx = 7041000, 0, False, False
        self.vals = {"KR": "1", "BI": "0", "KS": "025", "PC": "050"}
        self.width, self.roof = 13, "2"
        self.mode = "2"
        self.fa_clears_clar = False
        self.out = b""
        self.log = []

    def reset_input_buffer(self):
        self.out = b""

    def flush(self):
        pass

    def close(self):
        pass

    def write(self, data):
        for c in data.decode().split(";"):
            if not c:
                continue
            self.log.append(c + ";")
            if c == "ID":
                self.out += b"ID0761;"
            elif c == "IF":
                self.out += (f"IF001{self.freq:09d}{'-' if self.offset < 0 else '+'}"
                             f"{abs(self.offset):04d}{int(self.rx)}{int(self.tx)}{self.mode}00000;").encode()
            elif c.startswith("CF001"):
                self.offset = int(c[5:10])
            elif c.startswith("CF000"):
                self.rx, self.tx = c[5] == "1", c[6] == "1"
            elif c == "RC":
                self.offset = 0
            elif c.startswith("FA") and len(c) > 2:
                self.freq = int(c[2:])
                if self.fa_clears_clar:  # 周波数を変えると TX CLAR が外れる無線機のまね
                    self.tx, self.offset = False, 0
            elif c == "FA":
                self.out += f"FA{self.freq:09d};".encode()
            elif c[:2] in self.vals:
                code, body = c[:2], c[2:]
                if body:
                    self.vals[code] = body.zfill(len(self.vals[code]))
                else:
                    self.out += f"{code}{self.vals[code]};".encode()
            elif c.startswith("MD0") and len(c) == 3:
                self.out += b"MD03;"
            elif c == "SH0":
                self.out += f"SH00{self.width:02d};".encode()
            elif c.startswith("SH00") and len(c) == 6:
                self.width = int(c[4:])
            elif c == "RF0":
                self.out += f"RF0{'6729A'['12345'.index(self.roof)] if self.roof in '1245' else '6'};".encode()
            elif c.startswith("RF0") and len(c) == 4:
                self.roof = c[3]

    def read_until(self, term):
        i = self.out.find(term)
        if i < 0:
            d, self.out = self.out, b""
            return d
        d, self.out = self.out[:i + 1], self.out[i + 1:]
        return d


def selftest():
    assert cmd_set_offset(500) == "CF001+0500;"
    assert cmd_set_offset(-1200) == "CF001-1200;"
    assert cmd_set_offset(0) == "CF001+0000;"
    assert cmd_clar_switch(False, True) == "CF00001000;"
    assert cmd_clar_switch(False, False) == "CF00000000;"
    for bad in (10000, -9991):
        try:
            cmd_set_offset(bad)
            raise AssertionError("範囲外が通った")
        except ValueError:
            pass
    sample = "IF001014250000-030001200000;"
    assert len(sample) == 28
    st = parse_if(sample)
    assert st == {"freq": 14250000, "offset": -300, "rx_clar": False, "tx_clar": True, "mode": "USB"}, st
    assert parse_if("?;") is None

    fake = FakeFTDX10()
    r = Radio(fake)
    assert r.identify() == "ID0761;"
    st = r.apply_tx_offset(-750)
    assert st["offset"] == -750 and st["tx_clar"] and not st["rx_clar"], st
    assert "RU" not in "".join(fake.log)  # CF で済んだので予備は使っていない
    st = r.clear()
    assert st["offset"] == 0 and not st["tx_clar"], st
    assert cmd_set_freq(7041000) == "FA007041000;" and cmd_set_freq(50313000) == "FA050313000;"
    r.send(cmd_set_freq(14074000))
    assert r.status()["freq"] == 14074000
    assert fmt_freq(7041000) == "7.041.000"

    # CW・パワー
    assert r.read_int("KS") == 25 and r.read_value("KR") == "1" and r.read_int("PC") == 50
    r.send("KS032;")
    r.send("BI1;")
    assert r.read_int("KS") == 32 and r.read_value("BI") == "1"

    # 四捨五入
    assert round_to_step(7006880, 100) == 7006900 and round_to_step(7006880, 1000) == 7007000
    assert round_to_step(7006880, 10) == 7006880 and round_to_step(7006885, 10) == 7006890
    assert round_to_step(7006449, 1000) == 7006000 and round_to_step(7006500, 1000) == 7007000

    # フィルター
    assert len(WIDTH_SSB) == 23 and len(WIDTH_CW) == 18
    assert width_table_for("USB") is WIDTH_SSB and width_table_for("CW-U") is WIDTH_CW
    assert width_table_for("FM") is None
    assert cmd_set_width(13) == "SH0013;" and cmd_set_width(5) == "SH0005;"
    # ルーフィングに合わせたスライダーの右端
    assert WIDTH_CW[width_top_index(WIDTH_CW, 300) - 1] == 300 and width_top_index(WIDTH_CW, 300) == 6
    assert WIDTH_CW[width_top_index(WIDTH_CW, 500) - 1] == 500
    assert width_top_index(WIDTH_CW, 3000) == 18 and width_top_index(WIDTH_CW, 12000) == 18
    assert WIDTH_SSB[width_top_index(WIDTH_SSB, 3000) - 1] == 3000 and width_top_index(WIDTH_SSB, 12000) == 23
    assert width_top_index(WIDTH_SSB, 300) == 1
    assert r.read_value("SH0") == "013"
    r.send(cmd_set_width(7))
    assert int(r.read_value("SH0")) == 7
    assert ROOFING_ANSWER[r.read_value("RF0")] == "2"
    r.send("RF04;")
    assert ROOFING_ANSWER[r.read_value("RF0")] == "4"

    # 中継: 読み出しかどうかの見分け
    for c in ("FA;", "IF;", "MD0;", "KS;", "EX010101;", "CF001;", "ID;"):
        assert is_read_cmd(c), c
    for c in ("FA014074000;", "MD03;", "KS025;", "BI1;", "RC;", "SV;", "EX0101011;", "PC050;"):
        assert not is_read_cmd(c), c
    assert split_cmds("FA;IF;\r\nMD0") == (["FA;", "IF;"], "MD0")
    assert r.relay("FA;") == "FA014074000;"
    assert r.relay("FA007074000;") == "" and r.status()["freq"] == 7074000
    assert r.relay("MD0;") == "MD03;"
    assert r.relay("AI;") == "AI0;" and r.relay("AI1;") == "" and "AI1;" not in fake.log
    assert snap(37, 10, 100) == 40 and snap(-12, 10, 100) == -10 and snap(250, 10, 100) == 100
    assert snap(500, 100, 1000) == 500 and snap(5.0, 10, 100) in (0, 10) and snap("x", 10, 100) == 50
    assert [step_for_freq(f) for f in (7008715, 7008710, 7008700, 7008000, 14000000)] == [1, 10, 100, 1000, 1000]
    assert round_to_step(7008715, 1) == 7008715
    # 日本語と英語で、{ } の名前がそろっているか
    import string
    for key, pair in TEXTS.items():
        assert len(pair) == 2 and all(pair), key
        names = [sorted({f[1] for f in string.Formatter().parse(s) if f[1]}) for s in pair]
        assert names[0] == names[1], key
    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        App().mainloop()
