#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vfix — 批量视频修复工具
=======================

针对「删除后恢复 / 录制中断 / 索引损坏 / 封装头丢失」的 MP4/MOV 视频做抢救性修复。

处理流水线::

    阶段1  结构探伤    box 签名扫描 + moov 解析，判断容器健康度与编码格式
    阶段2  快速通道    容器完整时直接转封装（保留音轨，几秒搞定）
    阶段3  NAL 雕刻    H.264/H.265 AVCC 长度前缀链扫描 + 区间合并去伪
    阶段4  逐链体检    并行 ffmpeg 解码验证，垃圾链不进成品（防花屏）
    阶段5  编码输出    x264/x265 重编码 + ffprobe 验证（可 --copy 流复制）

阶段 4 全链失败 ≠ 没救，自动走降级阶梯（穷尽一切手段才判定失败）::

    ①  换编码假设（H.264 ↔ H.265）重新雕刻 + 体检
    ②  改用流内雕刻参数集（moov 里的可能是恢复时混入的别人的）
    ③  放宽体检阈值（容忍 <2% 解码错误）
    ④  借用参数集：SPS/PPS 按来源（moov / 流内 / 供体）分列候选，
        两两组合快筛——供体的 SPS 配原文件完好的 PPS 这类混搭是正解。
        供体优先级：--sps-from 指定 > 同目录/上级目录自动搜索。
        注意：重编码过的视频不配当供体（熵编码模式等参数不同），
        同设备录制的兄弟文件才是最佳供体。
    ⑤  强力抢救：-fflags discardcorrupt 容错解码硬啃全部雕刻数据
    ⑤b 换流内参数集再次强拼

用法::

    python vfix.py <视频文件或文件夹...> [-o 输出目录] [选项]
    支持把文件/文件夹直接拖到 修复视频.bat 上。

速度建议::

    --copy        流复制封装（最快、无损，兼容性略低，坏链已被体检剔除）
    --no-triage   跳过体检（最快，但输出可能含花屏段，不推荐）

环境变量::

    VFIX_FFMPEG / VFIX_FFPROBE   显式指定 ffmpeg / ffprobe 路径（优先级最高）
    VFIX_KEEP_TMP=1              保留体检临时文件（调试用）
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import mmap
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

try:  # Windows 控制台默认 GBK，强制 UTF-8 避免 emoji/中文乱码
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

VERSION = "1.4.0"

MAX_NAL = 256 * 1024 * 1024          # 单个 NAL 长度上限 256MB
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".3gp"}
OUT_DIR_NAME = "修复输出"
MIN_FILE_SIZE = 64 * 1024            # 小于 64KB 的文件不值得修
SIGNAL_CHUNK = 64 * 1024 * 1024      # 签名扫描分块大小（8 字节重叠防跨界漏检）

# 候选 NAL 起点预筛：长度前缀首字节必为 0x00，次字节 <0x10（即 NAL < 268MB）
CAND_RE = re.compile(rb"\x00(?=[\x00-\x0f])")

# 常见 ffmpeg 安装位置兜底（按顺序探测，之后还会查 PATH）
FF_SEARCH = [
    r"C:\ffmpeg\bin\ffmpeg.exe",
    r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
    r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
]


# ══════════════════════════════════════════════════════════ 通用小工具

def human(n: float) -> str:
    """字节数 → 人类可读大小（自适应 B/KB/MB/GB）"""
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def hms(sec: float) -> str:
    """秒 → m:ss / h:mm:ss"""
    sec = int(sec)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def safe_remove(path: str) -> None:
    """尽力删除文件，任何失败都静默（临时文件不值得为之中断主流程）"""
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


# ══════════════════════════════════════════════════════════ 引擎定位

def _portable_dirs() -> list[str]:
    """便携模式搜索目录：exe/脚本 同目录及其 bin/ 子目录"""
    dirs = []
    if getattr(sys, "frozen", False):          # PyInstaller：内嵌二进制在 _MEIPASS
        dirs.append(getattr(sys, "_MEIPASS", ""))
        dirs.append(os.path.dirname(os.path.abspath(sys.executable)))
    here = os.path.dirname(os.path.abspath(__file__))
    dirs += [here, os.path.join(here, "bin")]
    return [d for d in dirs if d and os.path.isdir(d)]


def _find_binary(name: str, env_key: str) -> str | None:
    """按优先级查找二进制：环境变量 → 便携目录 → imageio → 常见路径 → PATH"""
    env = os.environ.get(env_key)
    if env and os.path.exists(env):
        return env
    for d in _portable_dirs():
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    if name == "ffmpeg.exe":                   # imageio-ffmpeg 自带完整版 ffmpeg
        try:
            import imageio_ffmpeg
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            pass
    for base in FF_SEARCH:
        p = os.path.join(os.path.dirname(base), name)
        if os.path.exists(p):
            return p
    return shutil.which(name)


def find_tools() -> tuple[str | None, str | None]:
    """返回 (ffmpeg, ffprobe) 路径，找不到的为 None"""
    ff = _find_binary("ffmpeg.exe", "VFIX_FFMPEG")
    fp = _find_binary("ffprobe.exe", "VFIX_FFPROBE")
    if ff and not fp:                          # ffmpeg 旁边常常就有 ffprobe
        p = os.path.join(os.path.dirname(ff), "ffprobe.exe")
        if os.path.exists(p):
            fp = p
    return ff, fp


# ══════════════════════════════════════════════════════════ MP4 box 解析

def iter_boxes(buf, s: int, e: int):
    """在 buf[s:e] 内迭代 (typ, abs_pos, size, header_len)，坏结构立即停止"""
    p = s
    while p + 8 <= e:
        sz = int.from_bytes(buf[p:p + 4], "big")
        typ = buf[p + 4:p + 8]
        h = 8
        if sz == 1:                            # 64 位 largesize
            if p + 16 > e:
                return
            sz = int.from_bytes(buf[p + 8:p + 16], "big")
            h = 16
        elif sz == 0:                          # 到文件末尾的 mdat
            sz = e - p
        if sz < h or p + sz > e:
            return
        yield typ, p, sz, h
        p += sz


def parse_avcc(content: bytes) -> tuple[list, list]:
    """avcC 载荷 → ([SPS...], [PPS...])"""
    sps, pps = [], []
    try:
        num_sps = content[5] & 0x1F
        off = 6
        for _ in range(num_sps):
            sl = int.from_bytes(content[off:off + 2], "big")
            off += 2
            sps.append(bytes(content[off:off + sl]))
            off += sl
        num_pps = content[off]
        off += 1
        for _ in range(num_pps):
            pl = int.from_bytes(content[off:off + 2], "big")
            off += 2
            pps.append(bytes(content[off:off + pl]))
            off += pl
    except Exception:
        pass
    return sps, pps


def parse_hvcc(content: bytes) -> tuple[list, list, list]:
    """hvcC 载荷 → ([VPS...], [SPS...], [PPS...])"""
    vps, sps, pps = [], [], []
    try:
        n_arrays = content[22]
        off = 23
        for _ in range(n_arrays):
            t = content[off + 1]
            n = int.from_bytes(content[off + 2:off + 4], "big")
            off += 4
            for _ in range(n):
                l = int.from_bytes(content[off:off + 2], "big")
                off += 2
                nal = bytes(content[off:off + l])
                off += l
                if t == 32:
                    vps.append(nal)
                elif t == 33:
                    sps.append(nal)
                elif t == 34:
                    pps.append(nal)
    except Exception:
        pass
    return vps, sps, pps


def _codec_payload(seg: bytes, tag: bytes):
    """在 stsd 段内找 tag(如 avcC) box，按 box 长度返回完整载荷（防长参数集被截断）"""
    i = seg.find(tag)
    if i < 4:
        return None
    size = int.from_bytes(seg[i - 4:i], "big")
    end = i - 4 + size if 8 <= size <= len(seg) - (i - 4) else i + 4 + 256
    return seg[i + 4:end]


def parse_trak(mm, s: int, e: int, res: dict) -> None:
    """解析一个 trak：视频编码、参数集、帧率、最大采样尺寸"""
    ts = None
    stts_entries = None
    stsz_count = None
    stsd_range = None
    for typ, p, sz, _h in iter_boxes(mm, s, e):
        if typ == b"mdia":
            for t2, p2, s2, _h2 in iter_boxes(mm, p + 8, p + sz):
                if t2 == b"mdhd":
                    c = mm[p2 + 8:p2 + s2]
                    try:
                        ts = (int.from_bytes(c[20:24], "big") if c[0] == 1
                              else int.from_bytes(c[12:16], "big"))
                    except Exception:
                        pass
                elif t2 == b"minf":
                    for t3, p3, s3, _h3 in iter_boxes(mm, p2 + 8, p2 + s2):
                        if t3 == b"stbl":
                            for t4, p4, s4, _h4 in iter_boxes(mm, p3 + 8, p3 + s3):
                                if t4 == b"stts":
                                    c = mm[p4 + 8:p4 + s4]
                                    try:
                                        n = int.from_bytes(c[4:8], "big")
                                        stts_entries = [
                                            struct.unpack(">II", c[8 + 8 * i:16 + 8 * i])
                                            for i in range(min(n, 500000))
                                        ]
                                    except Exception:
                                        pass
                                elif t4 == b"stsz":
                                    c = mm[p4 + 8:p4 + s4]
                                    try:
                                        stsz_count = int.from_bytes(c[8:12], "big")
                                        ss = int.from_bytes(c[4:8], "big")
                                        if ss == 0 and len(c) >= 16:
                                            n_ent = min(stsz_count, (len(c) - 12) // 4)
                                            if n_ent > 0:
                                                sizes = struct.unpack(
                                                    f">{n_ent}I", c[12:12 + 4 * n_ent])
                                                res["max_sample"] = max(
                                                    res.get("max_sample", 0), max(sizes))
                                    except Exception:
                                        pass
                                elif t4 == b"stsd":
                                    stsd_range = (p4 + 8, p4 + s4)

    if stsd_range:
        seg = mm[stsd_range[0]:stsd_range[1]]
        if b"avc1" in seg or b"avc3" in seg:
            res["codec"] = "h264"
            payload = _codec_payload(seg, b"avcC")
            if payload:
                res["sps"], res["pps"] = parse_avcc(payload)
        elif b"hvc1" in seg or b"hev1" in seg:
            res["codec"] = "hevc"
            payload = _codec_payload(seg, b"hvcC")
            if payload:
                res["vps"], res["sps"], res["pps"] = parse_hvcc(payload)
        if stts_entries and ts:
            frames = sum(c for c, _d in stts_entries)
            dur = sum(c * d for c, d in stts_entries)
            if dur > 0 and frames > 0:
                res["fps"] = round(frames * ts / dur, 3)
            res["frames"] = frames
    if res.get("frames") is None and stsz_count:
        res["frames"] = stsz_count


def parse_moov(mm, moov_pos: int, fsize: int) -> dict | None:
    """moov_pos 指向 'moov' 四字符码；返回结构信息 dict，无效返回 None"""
    try:
        size = int.from_bytes(mm[moov_pos - 4:moov_pos], "big")
    except Exception:
        return None
    if size < 8 or moov_pos - 4 + size > fsize:
        return None
    res = {"codec": None, "fps": None, "duration": None, "timescale": None,
           "sps": [], "pps": [], "vps": [], "frames": None, "size": size,
           "max_sample": 0}
    found_trak = False
    for typ, p, sz, _h in iter_boxes(mm, moov_pos + 4, moov_pos - 4 + size):
        if typ == b"mvhd":
            c = mm[p + 8:p + sz]
            try:
                if c[0] == 1:                  # version 1: 64 位时间
                    res["timescale"] = int.from_bytes(c[20:24], "big")
                    res["duration"] = int.from_bytes(c[24:32], "big")
                else:
                    res["timescale"] = int.from_bytes(c[12:16], "big")
                    res["duration"] = int.from_bytes(c[16:20], "big")
            except Exception:
                pass
        elif typ == b"trak":
            parse_trak(mm, p + 8, p + sz, res)
            found_trak = True
    return res if found_trak else None


def scan_sigs(mm, fsize: int) -> dict:
    """扫描关键 box 签名位置（每类最多记 4 处；分块重叠 8 字节防跨界漏检）"""
    sigs = [b"ftyp", b"moov", b"mdat", b"avcC", b"hvcC"]
    pos = {s: set() for s in sigs}
    overlap = 8
    for off in range(0, fsize, SIGNAL_CHUNK - overlap):
        buf = mm[off:off + SIGNAL_CHUNK]
        for s in sigs:
            st = 0
            while len(pos[s]) < 4:
                i = buf.find(s, st)
                if i < 0:
                    break
                pos[s].add(off + i)
                st = i + 1
    return {s: sorted(v) for s, v in pos.items()}


def find_valid_moov(mm, fsize: int, cands: list) -> dict | None:
    """依次尝试验选 moov 位置，返回第一个解析成功的"""
    for p in cands:
        r = parse_moov(mm, p, fsize)
        if r:
            return r
    return None


# ══════════════════════════════════════════════════════════ NAL 链雕刻

def nal_check(b0: int, codec: str) -> tuple[bool, int, bool, bool]:
    """检查 NAL 头首字节合法性，返回 (ok, type, is_idr, is_param)"""
    if b0 & 0x80:                              # forbidden_zero_bit
        return False, 0, False, False
    if codec == "h264":
        t = b0 & 0x1F
        return 1 <= t <= 23, t, t == 5, t in (7, 8)
    t = (b0 >> 1) & 0x3F
    return t <= 40, t, t in (19, 20), t in (32, 33, 34)


def walk_chain(mm, p: int, fsize: int, codec: str, cap: int) -> tuple:
    """从长度前缀位置 p 起走链，返回 (nals, end_pos, total_bytes, idrs, params)"""
    nals = total = idrs = 0
    params = []
    q = p
    while q + 4 <= fsize and nals < 500000:
        ln = int.from_bytes(mm[q:q + 4], "big")
        if ln < 2 or ln > cap or q + 4 + ln > fsize:
            break
        ok, _t, is_idr, is_param = nal_check(mm[q + 4], codec)
        if not ok:
            break
        if is_idr:
            idrs += 1
        if is_param and 4 <= ln <= 1024:
            params.append(bytes(mm[q + 4:q + 4 + ln]))
        q += 4 + ln
        nals += 1
        total += ln
    return nals, q, total, idrs, params


def carve(mm, fsize: int, codec: str, cap: int, log) -> tuple[list, list]:
    """全文件雕刻。返回 (chains, carved_params)

    chains: [(start, end, nals, total_bytes, idrs), ...] 已做区间合并去伪；
    carved_params: 流内雕刻到的 SPS/PPS/VPS，moov 参数集可疑时的备用。
    """
    chains = []
    param_pool = {}
    t0 = time.time()
    last_pct = -5
    for m in CAND_RE.finditer(mm):
        p = m.start()
        pct = int(p * 20 / max(fsize, 1)) * 5
        if pct > last_pct:
            last_pct = pct
            log(f"\r    🔍 雕刻扫描 {pct}%  (已发现 {len(chains)} 条链)", end="", flush=True)
        if p + 8 > fsize:
            break
        ln = int.from_bytes(mm[p:p + 4], "big")
        if ln < 2 or ln > cap or p + 4 + ln > fsize:
            continue
        ok, _t, _idr, _param = nal_check(mm[p + 4], codec)
        if not ok:
            continue
        nals, end, total, idrs, params = walk_chain(mm, p, fsize, codec, cap)
        if (nals >= 2 and total >= 16384) or (nals >= 4 and total >= 4096):
            chains.append((p, end, nals, total, idrs))
            for x in params:
                param_pool[x] = None
    log(f"\r    🔍 雕刻扫描 100%  (共 {len(chains)} 条链, 流内参数集 {len(param_pool)} 个, "
        f"用时 {time.time() - t0:.1f}s)          ")

    # 区间合并：按 (终点, 起点) 贪心——短的真链优先被选中，吞并区的假巨链被挤出
    chains.sort(key=lambda c: (c[1], c[0]))
    merged = []
    last_end = 0
    for c in chains:
        if c[0] < last_end:
            continue
        merged.append(c)
        last_end = c[1]
    return merged, list(param_pool.keys())


# ══════════════════════════════════════════════════════════ Annex B 重建

def build_annexb(mm, chains, codec, cap, param_sets, out_path, log,
                 force_all: bool = False) -> int:
    """雕刻链 → Annex B 裸流，返回写入的 NAL 数。

    - 链不连续（中间有被剔除的坏链）时，从本链首个 IDR 重新同步：
      断参考后的 P/B 帧必然花屏，宁可不发。
    - force_all=True 强力抢救模式：不做重同步筛选，全部硬拼交给容错解码。
    - IDR 前自动补参数集；参数集 NAL 全程去重不重复写。
    """
    pending = b"".join(b"\x00\x00\x00\x01" + x for x in param_sets)
    written_params = set(param_sets)
    n_written = n_resync = n_drop = 0
    with open(out_path, "wb") as f:
        f.write(pending)                       # 文件开头先放一份参数集
        params_since_idr = True                # 开头已放，视为就绪
        prev_end = None
        for (s, e, _nals, _total, _idrs) in chains:
            q = s
            contiguous = prev_end is not None and 0 <= s - prev_end <= 64
            if not contiguous and not force_all:
                # 找本链首个 IDR 作重同步点
                found = False
                qq = s
                while qq + 4 <= e:
                    ln = int.from_bytes(mm[qq:qq + 4], "big")
                    if ln < 2 or ln > cap or qq + 4 + ln > e:
                        break
                    _ok, _t, is_idr, _p = nal_check(mm[qq + 4], codec)
                    if is_idr:
                        q = qq
                        found = True
                        break
                    qq += 4 + ln
                if not found:
                    n_drop += 1
                    prev_end = None
                    continue
            if not contiguous and prev_end is not None:
                n_resync += 1
            while q + 4 <= e:
                ln = int.from_bytes(mm[q:q + 4], "big")
                if ln < 2 or ln > cap or q + 4 + ln > e:
                    break
                nal = bytes(mm[q + 4:q + 4 + ln])
                _ok, _t, is_idr, is_param = nal_check(nal[0] if nal else 0, codec)
                if is_param:
                    if nal not in written_params:
                        f.write(b"\x00\x00\x00\x01" + nal)
                        written_params.add(nal)
                    params_since_idr = True
                else:
                    if is_idr and not params_since_idr and pending:
                        f.write(pending)
                    f.write(b"\x00\x00\x00\x01" + nal)
                    if is_idr:
                        params_since_idr = False
                n_written += 1
                q += 4 + ln
            prev_end = e
    log(f"    🧩 裸流已生成: {n_written} 个 NAL → {human(os.path.getsize(out_path))}"
        f"  (重同步 {n_resync}, 整链剔除 {n_drop})")
    return n_written


# ══════════════════════════════════════════════════════════ 逐链解码体检

def deep_triage(mm, chains, codec, cap, param_sets, tmp_dir, ff, log,
                workers: int | None = None, tolerant: bool = False) -> tuple:
    """并行逐链 ffmpeg 解码体检。返回 (干净链列表, 诊断信息)。

    - 从链内首个 IDR 起测——链中段起步的 P/B 帧必然报缺参考，会误杀好内容。
    - 通过标准：解码错误数在容忍内 **且** 真实解出 ≥1 帧
      （悬空参考的 P 帧可能零报错但解不出图像，必须看 frame= 统计）。
    - tolerant=True 时允许少量解码错误（坏链抢救模式）。
    """
    if workers is None:
        workers = min(8, os.cpu_count() or 4)
    fmt = "h264" if codec == "h264" else "hevc"
    t0 = time.time()
    results = [False] * len(chains)
    done = [0]
    lock = threading.Lock()
    diag = {"decoder_missing": False, "sample": ""}

    def test_chain(i: int, chain: tuple):
        s, e, nals, _total, idrs = chain
        start = s
        if idrs > 0:                           # 定位链内首个 IDR
            qq = s
            while qq + 4 <= e:
                ln = int.from_bytes(mm[qq:qq + 4], "big")
                if ln < 2 or ln > cap or qq + 4 + ln > e:
                    break
                _ok, _t, is_idr, _p = nal_check(mm[qq + 4], codec)
                if is_idr:
                    start = qq
                    break
                qq += 4 + ln
        # 每个工作线程复用同一临时文件（截断重写），避免海量删除
        mini = os.path.join(tmp_dir, f"_triage_w{threading.get_ident() % 100000}.h264")
        try:
            with open(mini, "wb") as f:
                for x in param_sets:
                    f.write(b"\x00\x00\x00\x01" + x)
                q = start
                while q + 4 <= e:
                    ln = int.from_bytes(mm[q:q + 4], "big")
                    if ln < 2 or ln > cap or q + 4 + ln > e:
                        break
                    f.write(b"\x00\x00\x00\x01")
                    f.write(mm[q + 4:q + 4 + ln])
                    q += 4 + ln
            r = subprocess.run(
                [ff, "-v", "error", "-stats", "-threads", "1", "-f", fmt,
                 "-i", mini, "-f", "null", "-"],
                capture_output=True, timeout=180)
            err = r.stderr.lower()
            bad = (err.count(b"error") + err.count(b"non-existing")
                   + err.count(b"invalid nal"))
            m_frames = re.findall(rb"frame=\s*(\d+)", r.stderr)
            n_decoded = int(m_frames[-1]) if m_frames else 0
            # 体检命令本身没跑起来 ≠ 链干净：格式/解码器缺失、非零返回码都算失败
            if r.returncode != 0 or b"unknown input format" in err \
                    or b"unknown decoder" in err \
                    or (b"decoder" in err and b"not found" in err):
                bad = max(bad, 1)
                with lock:
                    if b"unknown input format" in err:
                        diag["sample"] = diag["sample"] or "ffmpeg 缺少该裸流输入格式"
                    elif b"unknown decoder" in err or b"not found" in err:
                        diag["sample"] = diag["sample"] or "ffmpeg 缺少该编码的解码器"
                        diag["decoder_missing"] = True
                    else:
                        diag["sample"] = diag["sample"] or \
                            err.decode(errors="replace").strip()[:160]
            elif bad:
                with lock:
                    if not diag["sample"]:
                        diag["sample"] = err.decode(errors="replace").strip()[:160]
        except Exception:
            bad = 1
            with lock:
                if not diag["sample"]:
                    diag["sample"] = "ffmpeg 运行异常/超时"
        limit = max(2, nals // 50) if tolerant else 0   # 容差：<2% NAL 出错
        ok = (bad <= limit) and n_decoded > 0
        if not ok and not diag["sample"] and n_decoded == 0 and bad == 0:
            with lock:
                diag["sample"] = "链内NAL解不出任何图像(疑似纯P帧无IDR)"
        with lock:
            done[0] += 1
            if done[0] % 20 == 0 or done[0] == len(chains):
                log(f"\r    🩺 逐链体检 {done[0]}/{len(chains)}  (x{workers} 并行)",
                    end="", flush=True)
        return i, ok

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(test_chain, i, c) for i, c in enumerate(chains)]
        for fut in as_completed(futs):
            try:
                i, ok = fut.result()
                results[i] = ok
            except Exception:
                pass
    clean = [c for i, c in enumerate(chains) if results[i]]
    log(f"\r    🩺 逐链体检完成: {len(clean)}/{len(chains)} 条干净, "
        f"x{workers} 并行, 用时 {time.time() - t0:.1f}s          ")

    if os.environ.get("VFIX_KEEP_TMP") != "1":  # 调试开关：保留体检临时文件
        for fn in os.listdir(tmp_dir):
            if fn.startswith("_triage_w"):
                safe_remove(os.path.join(tmp_dir, fn))
    return clean, diag


# ══════════════════════════════════════════════════════════ ffmpeg 封装/验证

def parse_out_time(line: bytes) -> float | None:
    """解析 -progress 输出的一行，返回媒体秒数"""
    try:
        s = line.decode(errors="replace").strip()
        if s.startswith("out_time_us="):
            return float(s.split("=", 1)[1]) / 1e6
        if s.startswith("out_time_ms="):
            return float(s.split("=", 1)[1]) / 1e6  # 该字段历史上实为微秒
        if s.startswith("out_time="):
            v = s.split("=", 1)[1]
            if ":" in v:
                hh, mm_, ss = v.split(":")
                return int(hh) * 3600 + int(mm_) * 60 + float(ss)
    except Exception:
        pass
    return None


def run_encode(ff: str, args: list, log) -> tuple[int, bytes]:
    """带进度地跑 ffmpeg，返回 (returncode, stderr_bytes)。

    stderr 必须落临时文件——用 PIPE 且不同时读取会因缓冲区写满而死锁。
    """
    errf = os.path.join(os.environ.get("TEMP", os.environ.get("TMPDIR", ".")),
                        f"_vfix_err_{os.getpid()}.log")
    cmd = [ff, "-y", "-hide_banner", "-loglevel", "error", "-progress", "pipe:1"] + args
    with open(errf, "wb") as ef:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=ef)
        last = None
        try:
            for line in proc.stdout:
                t = parse_out_time(line)
                if t is not None:
                    msg = f"    ⏳ 重编码中... 已处理媒体时间 {hms(t)}"
                    if msg != last:
                        log(msg, end="", flush=True)
                        last = msg
        finally:
            proc.wait()
    log("")
    with open(errf, "rb") as f:
        err = f.read()
    safe_remove(errf)
    return proc.returncode, err


def encode_output(ff: str, h264_path: str, out_path: str, fps: float, codec: str,
                  log, preset: str = "superfast", copy_mode: bool = False,
                  tolerant: bool = False) -> tuple[int, bytes, float]:
    """裸流 → MP4。返回 (returncode, stderr, 编码耗时)。

    内置两级兜底：libx265 失败退流复制；奇数分辨率自动取偶重编。
    """
    fmt = "h264" if codec == "h264" else "hevc"
    # 容错模式：跳过损坏包、加大探测窗口帮助识别残缺裸流
    pre = (["-err_detect", "ignore_err", "-fflags", "+discardcorrupt+igndts",
            "-probesize", "32M", "-analyzeduration", "32M"] if tolerant else [])
    if copy_mode:
        # 流复制：无损、最快（几秒），坏 NAL 原样保留，个别播放器兼容性差
        args = (pre + ["-framerate", str(fps), "-f", fmt, "-i", h264_path,
                "-an", "-sn", "-dn", "-c", "copy",
                "-movflags", "+faststart", out_path])
    else:
        # 注意：编码器参数必须放在 -i 之后，放前面会被当成输入解码器
        encoders = (["-c:v", "libx264", "-preset", preset, "-crf", "17"]
                    if codec == "h264"
                    else ["-c:v", "libx265", "-preset", preset, "-crf", "20"])
        args = (pre + ["-framerate", str(fps), "-f", fmt, "-i", h264_path]
                + encoders
                + ["-an", "-sn", "-dn", "-pix_fmt", "yuv420p",
                   "-movflags", "+faststart", out_path])
    t0 = time.time()
    rc, err = run_encode(ff, args, log)
    if rc != 0 and codec == "hevc" and not copy_mode:
        log("    ⚠️ libx265 编码失败，退回流复制封装")
        rc, err = run_encode(ff, ["-framerate", str(fps), "-f", fmt, "-i", h264_path,
                                  "-an", "-sn", "-dn", "-c", "copy",
                                  "-movflags", "+faststart", out_path], log)
    if rc != 0 and not copy_mode:
        # 编码器硬错误兜底：奇数宽高（如 1081x1921）会被 yuv420p/libx264 拒编
        es = err.decode(errors="replace")
        if "divisible by 2" in es or "width not" in es or "height not" in es:
            log("    🔄 检测到奇数分辨率，自动取偶重编...")
            i = args.index("-i")
            args2 = (args[:i + 2]
                     + ["-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2"] + args[i + 2:])
            rc, err = run_encode(ff, args2, log)
    return rc, err, time.time() - t0


def _probe_by_ffmpeg(ff: str, path: str) -> tuple[int, float, int | None, int | None]:
    """ffprobe 缺失/失效时的兜底探测：用 ffmpeg 解码全片统计帧数（慢但可靠）"""
    try:
        r = subprocess.run(
            [ff, "-y", "-hide_banner", "-v", "info", "-progress", "pipe:1",
             "-i", path, "-map", "0:v:0", "-an", "-sn", "-dn", "-f", "null", "-"],
            capture_output=True, timeout=1800)
        m_frames = re.findall(rb"frame=\s*(\d+)", r.stdout)
        frames = int(m_frames[-1]) if m_frames else 0
        m_time = re.findall(rb"out_time_us=(\d+)", r.stdout)
        dur = int(m_time[-1]) / 1e6 if m_time else 0.0
        w = h = None
        m_res = re.search(rb"Video:.*?, (\d{2,5})x(\d{2,5})",
                          r.stderr, re.S)          # Video: ... 1440x3200, ...
        if m_res:
            w, h = int(m_res.group(1)), int(m_res.group(2))
        return frames, dur, w, h
    except Exception:
        return 0, 0.0, None, None


def probe_out(fp: str | None, path: str, ff: str | None = None) -> tuple:
    """探测输出文件，返回 (帧数, 时长, 宽, 高)。ffprobe 不可用时退回 ffmpeg 解码统计"""
    if fp:
        try:
            r = subprocess.run(
                [fp, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=nb_frames,duration,width,height",
                 "-of", "json", path],
                capture_output=True, timeout=120)
            j = json.loads(r.stdout or b"{}")
            st = (j.get("streams") or [{}])[0]
            frames = int(st.get("nb_frames") or 0)
            dur = float(st.get("duration") or 0)
            if frames > 0 or dur > 0:
                return frames, dur, st.get("width"), st.get("height")
        except Exception:
            pass
    return _probe_by_ffmpeg(ff, path) if ff else (0, 0.0, None, None)


def try_remux(ff: str, src: str, out_path: str) -> bool:
    """快速通道：容器健康时转封装修复（保留音轨）"""
    r = subprocess.run(
        [ff, "-y", "-hide_banner", "-loglevel", "error", "-err_detect", "ignore_err",
         "-i", src, "-map", "0:v:0", "-map", "0:a?", "-map", "0:s?", "-c", "copy",
         "-sn", "-dn", "-movflags", "+faststart", out_path],
        capture_output=True, timeout=3600)
    return r.returncode == 0 and os.path.exists(out_path)


# ══════════════════════════════════════════════════════════ 参数集借用

def find_sps_donors(path: str, max_n: int = 3) -> list[str]:
    """自动搜索参数集供体：与坏件同目录及上级目录的视频文件。

    按修改时间降序取前 max_n 个。同目录优先——批量修复时同批次的
    兄弟文件（同设备录制）正是最佳供体。
    """
    d = os.path.dirname(os.path.abspath(path)) or "."
    dirs, prev = [d, os.path.dirname(d)], None  # 上级目录兜底（常按文件夹分批次）
    cands = []
    for dd in dirs:
        if not dd or dd == prev or not os.path.isdir(dd):
            continue
        prev = dd
        try:
            for fn in os.listdir(dd):
                if not fn.lower().endswith((".mp4", ".mov", ".m4v")):
                    continue
                if fn == os.path.basename(path) or fn.endswith("_RECOVERED.mp4"):
                    continue
                fp_full = os.path.join(dd, fn)
                try:
                    if os.path.getsize(fp_full) >= 16384:
                        cands.append(fp_full)
                except OSError:
                    pass
        except OSError:
            pass
    cands = sorted(set(cands), key=os.path.getmtime, reverse=True)
    return cands[:max_n]


def extract_params_from_file(path: str) -> tuple[str | None, list]:
    """从健康 MP4/MOV 提取参数集，返回 (codec, [SPS/PPS/VPS...])"""
    with open(path, "rb") as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            sigs = scan_sigs(mm, len(mm))
            info = find_valid_moov(mm, len(mm), sigs[b"moov"]) or {}
            if info.get("codec") == "hevc":
                ps = [x for x in (info.get("vps") or []) + (info.get("sps") or [])
                      + (info.get("pps") or []) if x]
            else:
                ps = [x for x in (info.get("sps") or []) + (info.get("pps") or []) if x]
            return info.get("codec"), ps
        finally:
            try:
                mm.close()
            except Exception:
                pass


def _param_type(b: bytes, codec: str) -> str | None:
    """参数集 NAL 类型判定：sps / pps / vps / None"""
    if not b:
        return None
    if codec == "h264":
        return {7: "sps", 8: "pps"}.get(b[0] & 0x1F)
    return {32: "vps", 33: "sps", 34: "pps"}.get((b[0] >> 1) & 0x3F)


def borrow_param_combo(mm, chains, codec, cap, param_sets, carved_params,
                       path, opts, outdir, ff, log) -> tuple[list | None, str]:
    """降级④：SPS×PPS 组合快筛。返回 (胜出参数集组合, 来源标签) 或 (None, "")。

    SPS/PPS 分别按来源收集候选（moov 的 / 流内雕刻的 / 健康供体的），
    按优先级两两组合，用文件尾部的大链快筛——覆盖损坏常见于文件前段，
    尾部链最可能是完好数据。典型场景：moov 只坏了 SPS，PPS 完好但
    pps_id 与供体不同，只有「供体SPS + 原PPS」这类混搭才能匹配。
    """
    cand = {"sps": [], "pps": [], "vps": []}   # {类型: [(NAL字节, 来源标签), ...]}

    def add(blist, tag):
        for x in blist:
            k = _param_type(x, codec)
            if k and not any(b == x for b, _t in cand[k]):
                cand[k].append((x, tag))

    add(param_sets, "moov")                    # moov 提取的（可能部分损坏）
    add(carved_params, "流内")                 # 流内雕刻的
    donors = []
    if getattr(opts, "sps_from", None) and os.path.exists(opts.sps_from):
        donors = [opts.sps_from]
    else:
        donors = find_sps_donors(path)
        if donors:
            log(f"    🔄 降级4: 借用参数集 (供体{len(donors)}个, SPS×PPS 组合快筛)...")
    for donor in donors:
        try:
            add(extract_params_from_file(donor)[1], f"供体{os.path.basename(donor)}")
        except Exception as e:
            log(f"        {os.path.basename(donor)} 解析失败: {e}")
    if not (cand["sps"] and cand["pps"]):
        log("        无可用 SPS/PPS 候选")
        return None, ""

    # 供体(健康)参数最优先 → 流内雕刻 → moov 的(最可能坏)排最后，
    # 快筛名额有限，别让已知坏的 moov 参数集浪费组合名额
    def rank(tag):
        return 0 if tag.startswith("供体") else (1 if tag == "流内" else 2)

    cand["sps"].sort(key=lambda t: rank(t[1]))
    cand["pps"].sort(key=lambda t: rank(t[1]))
    pairs = [(s, p) for s in cand["sps"] for p in cand["pps"]][:10]

    # 快筛样本：优先取文件尾部的大链，再补全文件最大链；
    # 只要组合能让任意一条样本链解出图像即视为配对成功
    tail_pool = sorted(chains, key=lambda c: -c[0])[:20]
    sample = sorted(tail_pool, key=lambda c: -c[3])[:6]
    for c in sorted(chains, key=lambda c: -c[3])[:6]:
        if c not in sample:
            sample.append(c)

    for (s, st), (p, pt) in pairs:
        combo = ([v[0] for v in cand["vps"][:1]]) + [s, p]
        try:
            fc, diag = deep_triage(mm, sample, codec, cap, combo, outdir, ff, log)
        except Exception as e:
            diag = {"sample": str(e)[:100]}
            fc = []
        if fc:
            return combo, f"{st}SPS+{pt}PPS"
        log(f"        组合[{st}SPS{len(s)}B+{pt}PPS{len(p)}B]失败 "
            f"({(diag.get('sample') or '')[:60]})")
    return None, ""


# ══════════════════════════════════════════════════════════ 单文件流水线

def unique_out(outdir: str, name: str) -> str:
    """生成不冲突的输出路径：<名字>_RECOVERED.mp4, _2, _3 ..."""
    stem, _ = os.path.splitext(name)
    outp = os.path.join(outdir, f"{stem}_RECOVERED.mp4")
    n = 2
    while os.path.exists(outp):
        outp = os.path.join(outdir, f"{stem}_RECOVERED_{n}.mp4")
        n += 1
    return outp


def collect_files(inputs: list) -> list[str]:
    """收集输入中的视频文件（目录递归、跳过输出目录、去重保序）"""
    files = []
    for p in inputs:
        p = p.strip().strip('"')
        if not p:
            continue
        if os.path.isdir(p):
            for root, dirs, fns in os.walk(p):
                dirs[:] = [d for d in dirs if d != OUT_DIR_NAME]
                for fn in fns:
                    if os.path.splitext(fn)[1].lower() in VIDEO_EXTS:
                        files.append(os.path.join(root, fn))
        elif os.path.isfile(p):
            files.append(p)
    seen = set()
    out = []
    for f in files:
        rp = os.path.normcase(os.path.abspath(f))
        if rp not in seen:
            seen.add(rp)
            out.append(f)
    return out


def process_file(path: str, outdir: str, ff: str, fp: str, opts, idx: int,
                 total: int) -> dict:
    """处理单个文件，返回报告行 dict"""
    name = os.path.basename(path)
    size = os.path.getsize(path)
    row = {"文件": name, "大小": human(size), "状态": "", "输出": "",
           "时长": "", "帧数": "", "分辨率": "", "备注": ""}

    def log(*a, **k):
        k.setdefault("flush", True)
        print(*a, **k)

    log(f"\n▶ [{idx}/{total}] {name}  ({human(size)})")
    if name.endswith("_RECOVERED.mp4"):
        row["状态"], row["备注"] = "⏭ 跳过", "已是修复产物"
        log("    已是修复产物，跳过")
        return row
    if size < MIN_FILE_SIZE:
        row["状态"], row["备注"] = "⏭ 跳过", f"文件过小(<{human(MIN_FILE_SIZE)})"
        log("    文件过小，跳过")
        return row

    t0 = time.time()
    with open(path, "rb") as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    try:
        # ---- 阶段 1: 结构探伤
        log("    🔍 阶段1 结构探伤...")
        sigs = scan_sigs(mm, size)
        head = bytes(mm[:64])
        moov = find_valid_moov(mm, size, sigs[b"moov"])
        has_ftyp = b"ftyp" in head[:40]
        has_mdat = bool(sigs[b"mdat"])
        info = moov or {}
        codec = info.get("codec") or ("hevc" if sigs[b"hvcC"] else "h264")
        fps = info.get("fps")
        fps_src = "moov"
        if not fps or not (1 <= fps <= 240):
            fps = opts.fps or 30.0
            fps_src = "估计值"
        log(f"    📋 容器: ftyp={'✅' if has_ftyp else '❌'} "
            f"moov={'✅' if moov else '❌'} mdat={'✅' if has_mdat else '❌'} "
            f"| 编码={codec.upper()} 帧率={fps}({fps_src})")

        # ---- 阶段 2: 快速通道（容器健康 → 转封装）
        if has_ftyp and moov and has_mdat:
            log("    🔧 阶段2 容器结构完整，尝试转封装快速修复（保留音轨）...")
            outp = unique_out(outdir, name)
            if try_remux(ff, path, outp):
                frames, dur, w, h = probe_out(fp, outp, ff)
                if frames > 0 and dur > 1:
                    row.update({"状态": "✅ 容器修复", "输出": os.path.basename(outp),
                                "时长": hms(dur), "帧数": frames,
                                "分辨率": f"{w}x{h}" if w else "",
                                "备注": f"索引/封装修复, 音轨保留, "
                                        f"用时{time.time() - t0:.0f}s"})
                    log(f"    ✅ 转封装成功: {frames}帧 {hms(dur)}")
                    return row
                safe_remove(outp)              # 探测不过就撤掉，不能中断主流程
            log("    ⚠️ 快速通道失败，转雕刻修复")

        # ---- 阶段 3: NAL 链雕刻
        max_sample = info.get("max_sample") or 0
        cap = (min(MAX_NAL, int(max_sample * 1.1) + 4096)
               if max_sample else 32 * 1024 * 1024)
        log(f"    🔧 阶段3 {codec.upper()} NAL 链雕刻 (NAL长度上限 {human(cap)})...")
        chains, carved_params = carve(mm, size, codec, cap, log)
        if len(chains) < 2 or sum(c[3] for c in chains) < 65536:
            if codec == "h264" and not sigs[b"avcC"]:
                log("    ⚠️ H.264 雕刻产出过低，改试 H.265...")
                codec = "hevc"
                chains, carved_params = carve(mm, size, codec, cap, log)
        carved_total = sum(c[3] for c in chains)
        cov = carved_total * 100 / size
        if len(chains) < 1 or carved_total < 32768:
            row["状态"], row["备注"] = "❌ 失败", "未找到有效视频数据(可能被覆盖)"
            log("    ❌ 未找到有效视频数据")
            return row
        log(f"    📊 雕刻结果: {len(chains)} 条链, {human(carved_total)}, "
            f"覆盖率 {cov:.1f}%")

        if codec == "h264":
            param_sets = [x for x in (info.get("sps") or [])
                          + (info.get("pps") or []) if x]
        else:
            param_sets = [x for x in (info.get("vps") or [])
                          + (info.get("sps") or []) + (info.get("pps") or []) if x]
        if not param_sets:
            log("    ⚠️ moov 缺失或无参数集，将使用雕刻到的 SPS/PPS")

        tmp_raw = os.path.join(outdir, "_tmp_carve.raw")

        def build_and_encode(chain_list, codec_v=None, params_v=None,
                             tolerant=False, force_all=False, copy_mode=None):
            """拼裸流 → 编码 → 探测验证，返回 (路径, 帧, 时长, 宽, 高, 编码s) 或 None"""
            cv = codec_v or codec
            pv = param_sets if params_v is None else params_v
            cm = opts.copy if copy_mode is None else copy_mode
            nw = build_annexb(mm, chain_list, cv, cap, pv, tmp_raw, log,
                              force_all=force_all)
            if nw == 0:
                log("    ⚠️ 裸流中没有任何有效视频NAL(链内缺IDR关键帧)，"
                    "编码必然0帧，跳过")
                return None
            outp = unique_out(outdir, name)
            rc, err, enc_t = encode_output(ff, tmp_raw, outp, fps, cv, log,
                                           preset=opts.preset, copy_mode=cm,
                                           tolerant=tolerant)
            if rc != 0:
                log("    ❌ ffmpeg 编码失败:\n      " +
                    err.decode(errors="replace").strip()
                    .replace("\n", "\n      ")[-900:])
                safe_remove(outp)
                return None
            frames, dur, w, h = probe_out(fp, outp, ff)
            if frames == 0 and dur and dur > 1:
                # 流复制等场景 nb_frames 可能缺失，按时长×帧率估算
                frames = max(1, int(dur * fps))
            if frames == 0:
                log("    ⚠️ 编码完成但解出 0 帧:\n      " +
                    err.decode(errors="replace").strip()
                    .replace("\n", "\n      ")[-500:])
                safe_remove(outp)
                return None
            return (outp, frames, dur, w, h, enc_t)

        def finish(status, outp, frames, dur, w, h, enc_t, note) -> dict:
            row.update({"状态": status, "输出": os.path.basename(outp),
                        "时长": hms(dur) if dur else "?", "帧数": frames,
                        "分辨率": f"{w}x{h}" if w else "",
                        "备注": (f"{note}, "
                                 f"{'流复制' if opts.copy else opts.preset}"
                                 f"编码{enc_t:.0f}s, 数据覆盖{cov:.0f}%, "
                                 f"帧率{fps}({fps_src}), 无音轨, "
                                 f"用时{time.time() - t0:.0f}s")})
            return row

        # ---- 阶段 4: 逐链体检 + 降级阶梯
        tri_note = ""
        if opts.no_triage:
            final_chains = chains
            tri_note = "未体检(--no-triage)"
            log("    ⚠️ 已跳过逐链体检(--no-triage)：输出可能含花屏段")
        else:
            log("    🔧 阶段4 逐链解码体检 (剔除垃圾链)...")
            final_chains, tri_diag = deep_triage(mm, chains, codec, cap, param_sets,
                                                 outdir, ff, log)
            if final_chains:
                tri_note = f"体检链{len(final_chains)}/{len(chains)}"
            else:
                # ======== 降级阶梯：全链失败 ≠ 没救，逐级换假设重试 ========
                log(f"    ⚠️ 全部链体检失败 (原因: {tri_diag['sample'] or '无错误输出'})")
                ld = _downgrade_ladder(mm, size, chains, carved_params, codec, cap,
                                       param_sets, cov, outdir, ff, log, opts, path,
                                       build_and_encode)
                final_chains = ld["final_chains"]
                param_sets = ld["param_sets"]
                codec = ld["codec"]
                carved_params = ld["carved_params"]
                chains = ld["chains"]
                cov = ld["cov"]
                tri_note = ld["tri_note"]
                if ld["rescued"]:
                    outp, frames, dur, w, h, enc_t = ld["rescued"]
                    log(f"    🚑 抢救成功: {frames} 帧, {hms(dur) if dur else '?'} "
                        f"(可能有零星花屏段)")
                    return finish("🚑 强力抢救", outp, frames, dur, w, h, enc_t,
                                  "容错解码")
                if final_chains:
                    log(f"    ✅ 降级成功: {tri_note}")

        if not final_chains:
            row["状态"], row["备注"] = "❌ 失败", \
                ("6级手段全部穷尽(换编码假设/流内参数集/放宽阈值/"
                 "组合借用参数集/强制硬拼)；数据可能已被覆盖或非标准流")
            log("    ❌ 所有降级方案均失败")
            log("    💡 若这是同型号手机录的视频，把同机型其它视频放进同目录，"
                "或用 --sps-from <健康视频路径> 重试可借用其参数集")
            return row

        # ---- 阶段 5: 封装/编码 + 验证（只编一次，失败逐级兜底）
        mode_txt = ("流复制封装(--copy, 无损)" if opts.copy
                    else f"x264 CRF17 preset={opts.preset}")
        log(f"    🔧 阶段5 输出编码 ({mode_txt}, 帧率 {fps})...")
        res = build_and_encode(final_chains)
        if not res:
            log("    🔄 标准编码无有效输出，改用容错模式重试...")
            res = build_and_encode(final_chains, tolerant=True)
        if not res:
            log("    🔄 容错仍失败，强力抢救(不做重同步筛选, 全链硬拼)...")
            res = build_and_encode(final_chains, tolerant=True, force_all=True)
        if not res:
            log("    🔄 编码器仍拒绝输出，最后尝试流复制封装(不重编码)...")
            res = build_and_encode(final_chains, tolerant=True, copy_mode=True)
        if not res:
            row["状态"], row["备注"] = "❌ 失败", \
                "编码后无法解出有效帧(容错/强拼/流复制均失败)"
            log("    ❌ 修复失败")
            return row

        outp, frames, dur, w, h, enc_t = res
        log(f"    ✅ 修复完成: {frames} 帧, {hms(dur) if dur else '?'}, "
            f"{human(os.path.getsize(outp))}")
        return finish("🛠 雕刻修复", outp, frames, dur, w, h, enc_t, tri_note)
    finally:
        try:
            mm.close()
        except Exception:
            pass
        if not getattr(opts, "keep", False):
            safe_remove(os.path.join(outdir, "_tmp_carve.raw"))


def _downgrade_ladder(mm, size, chains, carved_params, codec, cap, param_sets,
                      cov, outdir, ff, log, opts, path, build_and_encode) -> dict:
    """降级阶梯（阶段4全链失败时调用）。

    build_and_encode: process_file 内的编码闭包（降级5 强力抢救复用）。
    返回 dict，final_chains 与 rescued 同时为空才表示全部手段穷尽：
      final_chains  胜出的干净链（①~④ 级成功时非空）
      param_sets    胜出的参数集组合
      codec         胜出的编码假设
      carved_params 同步更新后的流内参数集（防换假设后错配）
      chains        当前雕刻链
      cov           当前覆盖率
      tri_note      体检备注
      rescued       ⑤级强力抢救产出 (outp, frames, dur, w, h, enc_t) 或 None
    """
    res = {"final_chains": [], "param_sets": param_sets, "codec": codec,
           "carved_params": carved_params, "chains": chains, "cov": cov,
           "tri_note": "", "rescued": None}

    # 降级①: 换编码假设（H.264↔H.265 判断可能反了）重新雕刻 + 体检
    other = "hevc" if codec == "h264" else "h264"
    log(f"    🔄 降级1: 改用 {other.upper()} 假设重新雕刻+体检...")
    chains2, params2 = carve(mm, size, other, cap, log)
    if chains2 and sum(c[3] for c in chains2) >= 32768:
        fc, _ = deep_triage(mm, chains2, other, cap, params2 or param_sets,
                            outdir, ff, log)
        if fc and sum(c[3] for c in fc) >= 32768:
            # 验收门槛：干净链的数据量必须可观，防止幻链假成功
            res.update({
                "final_chains": fc, "param_sets": params2 or param_sets,
                "codec": other, "carved_params": params2, "chains": chains2,
                "cov": sum(c[3] for c in chains2) * 100 / size,
                "tri_note": f"体检链{len(fc)}/{len(chains2)}({other.upper()}假设)"})
            return res

    # 降级②: 改用流内雕刻参数集（moov 可能是恢复时混入的别人的）
    if carved_params and carved_params != param_sets:
        log(f"    🔄 降级2: 改用流内雕刻参数集 ({len(carved_params)} 个)...")
        fc, d2 = deep_triage(mm, chains, codec, cap, carved_params, outdir, ff, log)
        if fc:
            res.update({"final_chains": fc, "param_sets": carved_params,
                        "tri_note": f"体检链{len(fc)}/{len(chains)}(流内参数集)"})
            return res
        log(f"        仍失败 ({d2['sample'][:100]})")

    # 降级③: 放宽体检阈值（容忍 <2% 解码错误，小错不整链枪毙）
    log("    🔄 降级3: 放宽体检阈值...")
    fc, d3 = deep_triage(mm, chains, codec, cap, param_sets, outdir, ff, log,
                         tolerant=True)
    if fc:
        res.update({"final_chains": fc,
                    "tri_note": f"体检链{len(fc)}/{len(chains)}(放宽阈值)"})
        return res
    log(f"        仍失败 ({d3['sample'][:100]})")

    # 降级④: 借用参数集（avcC 损坏/被污染时的正解，SPS×PPS 组合快筛）
    combo, combo_tag = borrow_param_combo(mm, chains, codec, cap, param_sets,
                                          carved_params, path, opts, outdir, ff, log)
    if combo:
        fc, d4 = deep_triage(mm, chains, codec, cap, combo, outdir, ff, log)
        if fc:
            res.update({"final_chains": fc, "param_sets": combo,
                        "tri_note": f"体检链{len(fc)}/{len(chains)}"
                                    f"(组合参数集:{combo_tag})"})
            return res
        log(f"        快筛通过但全量体检失败 ({(d4.get('sample') or '')[:80]})")

    # 降级⑤/⑤b: 强力抢救（容错解码硬啃全部雕刻数据，能救多少是多少）
    log("    🔄 降级5: 强力抢救模式 (容错解码全部雕刻数据)...")
    fb = build_and_encode(chains, tolerant=True, force_all=True)
    if not fb and carved_params and carved_params != param_sets:
        log("    🔄 降级5b: 换流内参数集再次强拼...")
        fb = build_and_encode(chains, params_v=carved_params,
                              tolerant=True, force_all=True)
    if fb:
        res["rescued"] = fb
    return res


# ══════════════════════════════════════════════════════════ CLI 入口

def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="vfix",
        description="批量视频修复工具（MP4/MOV 雕刻修复，H.264/H.265）",
        formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("inputs", nargs="*", help="视频文件或文件夹（可多个）")
    ap.add_argument("-o", "--out", help="输出目录（默认: 输入目录\\修复输出）")
    ap.add_argument("--no-triage", action="store_true",
                    help="跳过逐链体检（更快，但输出可能含花屏段）")
    ap.add_argument("--copy", action="store_true",
                    help="流复制封装替代重编码（最快且无损，兼容性略低）")
    ap.add_argument("--preset", default="superfast",
                    help="x264 预设: ultrafast/superfast/veryfast/medium (默认 superfast)")
    ap.add_argument("--sps-from", metavar="健康视频",
                    help="借用参数集的健康视频路径（同机型效果最好，"
                         "用于 moov 内 avcC/hvcC 损坏的场景）")
    ap.add_argument("--fps", type=float,
                    help="雕刻修复时的帧率（默认从 moov 读取或 30）")
    ap.add_argument("--keep", action="store_true", help="保留中间裸流文件")
    return ap


def write_report(outdir: str, rows: list) -> str:
    """写 CSV 报告（utf-8-sig 保证 Excel 直接打开不乱码），返回报告路径"""
    report = os.path.join(outdir, f"修复报告_{datetime.now():%Y%m%d_%H%M%S}.csv")
    with open(report, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return report


def main(argv: list | None = None) -> None:
    args = build_argparser().parse_args(argv)

    print("=" * 62)
    print(f"  🔧 批量视频修复工具 v{VERSION}  (H.264/H.265 MP4/MOV)")
    print("=" * 62)

    if not args.inputs:
        print("请把视频文件/文件夹拖到 修复视频.bat 上运行，")
        print("或用命令行: python vfix.py <路径...>")
        try:
            line = input("\n或现在输入路径后回车: ")
        except (EOFError, KeyboardInterrupt):
            sys.exit(1)
        args.inputs = [line]

    files = collect_files(args.inputs)
    if not files:
        print("❌ 没有找到可处理的视频文件 (支持 mp4/mov/m4v/3gp)")
        sys.exit(1)

    first_dir = os.path.dirname(os.path.abspath(files[0]))
    outdir = args.out or os.path.join(first_dir, OUT_DIR_NAME)
    os.makedirs(outdir, exist_ok=True)

    ff, fp = find_tools()
    if not ff:
        print("❌ 未找到 ffmpeg，请安装或将其加入 PATH（也可用 VFIX_FFMPEG 环境变量指定）")
        sys.exit(1)
    print(f"📁 待处理: {len(files)} 个文件")
    print(f"📤 输出目录: {outdir}")
    print(f"🛠 引擎: {ff}")
    print(f"🛠 校验: {fp or '未找到 ffprobe!'}")

    rows = []
    t_start = time.time()
    n_ok = n_rescued = 0
    try:
        for i, f in enumerate(files, 1):
            try:
                row = process_file(f, outdir, ff, fp, args, i, len(files))
            except KeyboardInterrupt:
                raise
            except Exception:
                row = {"文件": os.path.basename(f), "大小": "?", "状态": "❌ 异常",
                       "输出": "", "时长": "", "帧数": "", "分辨率": "",
                       "备注": traceback.format_exc(limit=2).replace("\n", " ")}
                print(f"    ❌ 处理异常: {row['备注']}")
            rows.append(row)
            if row["状态"].startswith(("✅", "🛠")):
                n_ok += 1
            elif row["状态"].startswith("🚑"):
                n_rescued += 1
    except KeyboardInterrupt:
        print("\n\n⏹ 用户中断，已处理的文件结果如下：")

    report = write_report(outdir, rows) if rows else "(无)"
    elapsed = time.time() - t_start
    print("\n" + "=" * 62)
    print("  📊 修复汇总")
    print("=" * 62)
    for r in rows:
        print(f"  {r['状态']:<12} {r['文件'][:40]:<42} {r['时长']:>9}  "
              f"{r['备注'][:50]}")
    parts = [f"✅ 修复成功 {n_ok}", f"🚑 抢救 {n_rescued}",
             f"❌ 失败 {len(rows) - n_ok - n_rescued}"]
    print(f"\n  {' / '.join(parts)}  ⏱ 总用时 {elapsed:.0f}s")
    if report != "(无)":
        print(f"  📄 报告: {report}")
    print(f"  📤 输出: {outdir}")
    print("\a", end="", flush=True)


if __name__ == "__main__":
    main()
