#!/usr/bin/env python3
"""
TimelapsePro
============
Timelapse post-processing pipeline for PiSlider + Sony camera sequences.

Reads Sony ARW RAW files with PiSlider XMP sidecars and applies:
  • Exposure correction   (crs:Exposure2012)
  • White balance         (camera-commanded WB embedded in RAW via gphoto2)
  • Tilt correction       (ps:Rig_Tilt_Deg)
  • S-Log3 tone encoding
  • Exports Apple ProRes 4444 via ffmpeg

Requirements:
  pip3 install rawpy numpy opencv-python
  brew install ffmpeg exiftool
"""

import sys
import os
import json
import queue
import subprocess
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import numpy as np
import rawpy
import cv2


# ── Camera constants ──────────────────────────────────────────────────────────

SENSOR_WIDTH_MM  = 35.6   # Sony A7 III full-frame
SENSOR_HEIGHT_MM = 23.8
DEFAULT_FOCAL_MM = 14.0


# ── Per-frame metadata ────────────────────────────────────────────────────────

@dataclass
class FrameMeta:
    arw_path: Path
    xmp_path: Optional[Path] = None
    tilt_deg: float = 0.0    # ps:Rig_Tilt_Deg
    ec_stops: float = 0.0    # crs:Exposure2012 (stops, positive = boost)
    focal_mm: float = DEFAULT_FOCAL_MM


# ── XMP parsing ───────────────────────────────────────────────────────────────

_NS = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "crs": "http://ns.adobe.com/camera-raw-settings/1.0/",
    "ps":  "http://ns.pislider.io/1.0/",
}

def _xmp_attr(desc, tag: str) -> Optional[str]:
    """Read a tag from rdf:Description — tries child element then attribute."""
    elem = desc.find(tag, _NS)
    if elem is not None:
        return elem.text
    # Try as a Clark-notation attribute
    ns, local = tag.split(":")
    clark = f"{{{_NS[ns]}}}{local}"
    return desc.get(clark)

def parse_xmp(xmp_path: Path) -> dict:
    """Return dict with tilt_deg and ec_stops parsed from XMP; missing = 0."""
    result = {}
    try:
        root = ET.parse(xmp_path).getroot()
        for desc in root.findall(".//rdf:Description", _NS):
            for tag, key in [("ps:Rig_Tilt_Deg",  "tilt_deg"),
                              ("crs:Exposure2012", "ec_stops")]:
                val = _xmp_attr(desc, tag)
                if val is not None:
                    result[key] = float(val)
    except Exception:
        pass
    return result


# ── Tone curve: Sony S-Log3 ───────────────────────────────────────────────────

def slog3(linear: np.ndarray) -> np.ndarray:
    """
    Sony S-Log3 OETF (scene-referred to display code values).

    Input:  linear float, where 0.18 = 18% gray at nominal exposure.
    Output: code value in [0, 1]  (multiply by 65535 for 16-bit).

    Reference: Sony S-Log3 specification.
    Black = 95/1023 ≈ 0.093 · 18% gray = 420/1023 ≈ 0.410 · white clip > 1.0
    """
    cut = 0.01125
    x = np.asarray(linear, dtype=np.float32)
    log_out = (420.0 + np.log10(np.maximum(x + 0.01, 1e-10) / 0.19) * 261.5) / 1023.0
    lin_out = (x / cut * (171.2102946929 - 95.0) + 95.0) / 1023.0
    return np.where(x >= cut, log_out, lin_out)


# ── Perspective warp helpers ──────────────────────────────────────────────────

def warp_matrix(tilt_deg: float, focal_mm: float, w: int, h: int):
    """Return (H, H_inv) for vertical tilt correction."""
    fx = focal_mm * (w / SENSOR_WIDTH_MM)
    fy = focal_mm * (h / SENSOR_HEIGHT_MM)
    K  = np.array([[fx, 0, w / 2.0],
                   [0, fy, h / 2.0],
                   [0,  0,     1.0]], dtype=np.float64)
    r  = np.radians(-tilt_deg)
    R  = np.array([[1,           0,            0],
                   [0,  np.cos(r), -np.sin(r)],
                   [0,  np.sin(r),  np.cos(r)]], dtype=np.float64)
    H = K @ R @ np.linalg.inv(K)
    return H, np.linalg.inv(H)

def max_crop_scale(H_inv, w: int, h: int) -> float:
    """
    Binary search for the largest centred crop (same aspect ratio) that
    maps entirely within [0,w]×[0,h] after the perspective transform.
    """
    cx, cy = w / 2.0, h / 2.0
    lo, hi, best = 0.1, 1.0, 0.1
    for _ in range(30):
        s   = (lo + hi) / 2.0
        hw, hh = s * w / 2.0, s * h / 2.0
        corners = np.array([[cx - hw, cy - hh],
                             [cx + hw, cy - hh],
                             [cx + hw, cy + hh],
                             [cx - hw, cy + hh]])
        ch_h = np.hstack([corners, np.ones((4, 1))])
        mc   = (H_inv @ ch_h.T).T
        mc   = mc[:, :2] / mc[:, 2:]
        if mc[:, 0].min() >= 0 and mc[:, 0].max() <= w - 1 \
                and mc[:, 1].min() >= 0 and mc[:, 1].max() <= h - 1:
            best = s
            lo   = s
        else:
            hi = s
    return best


# ── Frame processing ──────────────────────────────────────────────────────────

def decode_raw(path: Path) -> np.ndarray:
    """
    Decode ARW to 16-bit linear RGB using the camera's own commanded WB.
    The A7III stores our gphoto2-commanded Kelvin as embedded WB in the RAW,
    so use_camera_wb=True gives us exactly the right colour rendering without
    any extra math.
    """
    with rawpy.imread(str(path)) as raw:
        return raw.postprocess(
            output_bps=16,
            use_camera_wb=True,
            no_auto_bright=True,
            gamma=(1, 1),          # linear — log curve applied later
        )

def process_frame(rgb16: np.ndarray,
                  meta:  FrameMeta,
                  H,                   # warp matrix (or None)
                  crop:  tuple,        # (x, y, w, h) output crop
                  do_tilt: bool) -> np.ndarray:
    """
    Pipeline for one frame:
      1. Normalise rawpy 16-bit linear to float [0,1]
      2. Apply sidecar exposure correction (linear multiply)
      3. Perspective warp + crop
      4. S-Log3 encode
      5. Return uint16 [0,65535]
    """
    frame = rgb16.astype(np.float32) / 65535.0

    # 1. Exposure correction
    if meta.ec_stops:
        frame = frame * (2.0 ** meta.ec_stops)

    h_px, w_px = frame.shape[:2]

    # 2. Tilt warp
    if do_tilt and H is not None:
        frame = cv2.warpPerspective(
            frame, H, (w_px, h_px),
            flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
            borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0),
        )

    # 3. Crop (always, for stable animation dimensions)
    x0, y0, cw, ch = crop
    frame = frame[y0:y0 + ch, x0:x0 + cw]

    # 4. S-Log3
    frame = np.clip(frame, 0.0, None)
    frame = np.clip(slog3(frame), 0.0, 1.0)

    return (frame * 65535.0).astype(np.uint16)


# ── Pipeline orchestration ────────────────────────────────────────────────────

def get_focal_mm(arw_path: Path) -> float:
    try:
        r = subprocess.run(["exiftool", "-FocalLength", "-j", str(arw_path)],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            data = json.loads(r.stdout)
            if data and "FocalLength" in data[0]:
                return float(str(data[0]["FocalLength"]).split()[0])
    except Exception:
        pass
    return DEFAULT_FOCAL_MM


class Pipeline:
    def __init__(self, input_dir: Path, output_path: Path, fps: int,
                 do_tilt: bool, do_ec: bool,
                 on_progress=None, on_log=None, on_done=None):
        self.input_dir   = input_dir
        self.output_path = output_path
        self.fps         = fps
        self.do_tilt     = do_tilt
        self.do_ec       = do_ec
        self._progress   = on_progress or (lambda n, t: None)
        self._log        = on_log      or print
        self._done       = on_done     or (lambda ok, m: None)
        self._cancelled  = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            self._run()
        except Exception as exc:
            self._done(False, str(exc))

    def _run(self):
        # ── Scan ─────────────────────────────────────────────────────────────
        arw_files = sorted(self.input_dir.glob("*.[Aa][Rr][Ww]"))
        if not arw_files:
            self._done(False, "No ARW files found in folder."); return

        self._log(f"Found {len(arw_files)} ARW files")

        frames: list[FrameMeta] = []
        for arw in arw_files:
            xmp = next((arw.with_suffix(s) for s in (".xmp", ".XMP")
                        if arw.with_suffix(s).exists()), None)
            m = FrameMeta(arw_path=arw, xmp_path=xmp)
            if xmp:
                p = parse_xmp(xmp)
                m.tilt_deg = p.get("tilt_deg", 0.0)
                m.ec_stops  = p.get("ec_stops",  0.0) if self.do_ec else 0.0
            frames.append(m)

        n_xmp = sum(1 for f in frames if f.xmp_path)
        self._log(f"Matched {n_xmp}/{len(frames)} XMP sidecars")

        # ── Focal length ─────────────────────────────────────────────────────
        self._log("Reading focal length from EXIF…")
        focal = get_focal_mm(frames[0].arw_path)
        self._log(f"Focal length: {focal:.1f} mm")
        for f in frames:
            f.focal_mm = focal

        # ── Frame dimensions ─────────────────────────────────────────────────
        with rawpy.imread(str(frames[0].arw_path)) as raw:
            fw, fh = raw.sizes.width, raw.sizes.height
        self._log(f"Frame size: {fw} × {fh}")

        # ── Pass 1 — global crop ─────────────────────────────────────────────
        global_scale = 1.0
        if self.do_tilt:
            self._log("Pass 1: computing global crop across all tilt angles…")
            for m in frames:
                if self._cancelled:
                    self._done(False, "Cancelled."); return
                if m.tilt_deg == 0.0:
                    continue
                _, H_inv = warp_matrix(m.tilt_deg, m.focal_mm, fw, fh)
                s = max_crop_scale(H_inv, fw, fh)
                if s < global_scale:
                    global_scale = s
            self._log(f"Global crop scale: {global_scale:.4f}")

        cw = int(global_scale * fw);  cw -= cw % 2   # ensure even for yuv
        ch = int(global_scale * fh);  ch -= ch % 2
        x0 = (fw - cw) // 2
        y0 = (fh - ch) // 2
        crop = (x0, y0, cw, ch)
        self._log(f"Output dimensions: {cw} × {ch}")

        # ── Start ffmpeg ─────────────────────────────────────────────────────
        self._log("Starting ffmpeg ProRes 4444 encoder…")
        cmd = [
            "ffmpeg", "-y",
            "-f",        "rawvideo",
            "-vcodec",   "rawvideo",
            "-s",        f"{cw}x{ch}",
            "-pix_fmt",  "rgb48le",
            "-r",        str(self.fps),
            "-i",        "pipe:0",
            "-vcodec",   "prores_ks",
            "-profile:v", "4444",
            "-pix_fmt",  "yuv444p10le",
            "-r",        str(self.fps),
            "-vendor",   "apl0",         # Apple vendor tag for QuickTime compat
            str(self.output_path),
        ]
        try:
            ff = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.PIPE)
        except FileNotFoundError:
            self._done(False, "ffmpeg not found.\nInstall with:  brew install ffmpeg")
            return

        # ── Pass 2 — process + encode ─────────────────────────────────────────
        self._log("Pass 2: decoding RAW → EC → tilt → S-Log3 → ProRes 4444…")
        total = len(frames)

        try:
            for i, m in enumerate(frames):
                if self._cancelled:
                    ff.stdin.close(); ff.terminate()
                    self._done(False, "Cancelled."); return

                rgb16 = decode_raw(m.arw_path)

                H = None
                if self.do_tilt and m.tilt_deg != 0.0:
                    H, _ = warp_matrix(m.tilt_deg, m.focal_mm, fw, fh)

                out16 = process_frame(rgb16, m, H, crop, do_tilt=self.do_tilt)

                # rgb48le = little-endian uint16 R G B — matches numpy uint16
                ff.stdin.write(out16.tobytes())

                self._progress(i + 1, total)

                if i < 3 or (i + 1) % 50 == 0 or i == total - 1:
                    self._log(f"  [{i+1:4d}/{total}]  {m.arw_path.name}"
                              f"  tilt={m.tilt_deg:+.2f}°  EC={m.ec_stops:+.2f}EV")

        finally:
            ff.stdin.close()

        _, ff_err = ff.communicate()
        if ff.returncode != 0:
            self._done(False, f"ffmpeg error:\n{ff_err.decode()[-800:]}")
            return

        size_mb = self.output_path.stat().st_size / 1e6
        self._done(True,
            f"Done — {total} frames → {self.output_path.name}  "
            f"({size_mb:.0f} MB)")


# ── GUI ───────────────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("TimelapsePro")
        self.resizable(False, False)
        self._pipeline: Optional[Pipeline] = None
        self._log_q: queue.Queue = queue.Queue()
        self._build_ui()
        self._poll_log()

    # ── Layout ────────────────────────────────────────────────────────────────

    def _build_ui(self):
        P = 12

        # Header
        hdr = tk.Frame(self, bg="#1c1c1e")
        hdr.pack(fill="x")
        tk.Label(hdr, text="TimelapsePro",
                 font=("Helvetica Neue", 18, "bold"),
                 fg="white", bg="#1c1c1e", pady=14).pack(side="left", padx=P)
        tk.Label(hdr, text="PiSlider · ProRes 4444 pipeline",
                 font=("Helvetica Neue", 11),
                 fg="#888", bg="#1c1c1e").pack(side="left")

        body = tk.Frame(self, padx=P, pady=P)
        body.pack(fill="both")

        # I/O rows
        self._in_var  = tk.StringVar()
        self._out_var = tk.StringVar()
        self._io_row(body, "Input folder:", self._in_var,  self._browse_input)
        self._io_row(body, "Output file:",  self._out_var, self._browse_output)

        # Options
        opts = tk.LabelFrame(body, text="Options", padx=8, pady=8)
        opts.pack(fill="x", pady=8)

        self._tilt_var = tk.BooleanVar(value=True)
        self._ec_var   = tk.BooleanVar(value=True)
        self._fps_var  = tk.StringVar(value="24")

        tk.Checkbutton(opts,
            text="Apply tilt correction  (ps:Rig_Tilt_Deg from sidecar)",
            variable=self._tilt_var
        ).grid(row=0, column=0, sticky="w", padx=4, pady=2)

        tk.Checkbutton(opts,
            text="Apply exposure correction  (crs:Exposure2012 from sidecar)",
            variable=self._ec_var
        ).grid(row=1, column=0, sticky="w", padx=4, pady=2)

        rf = tk.Frame(opts)
        rf.grid(row=0, column=1, sticky="e", padx=16)
        tk.Label(rf, text="Frame rate:").pack(side="left")
        ttk.Combobox(rf, textvariable=self._fps_var, width=5,
                     values=["24", "25", "30"],
                     state="readonly").pack(side="left", padx=4)
        tk.Label(rf, text="fps").pack(side="left")

        tf = tk.Frame(opts)
        tf.grid(row=1, column=1, sticky="e", padx=16)
        tk.Label(tf, text="Tone curve:").pack(side="left")
        tk.Label(tf, text="S-Log3", font=("Helvetica Neue", 11, "bold"),
                 fg="#0070f3").pack(side="left", padx=4)

        # Progress
        pf = tk.Frame(body)
        pf.pack(fill="x", pady=4)
        self._status = tk.StringVar(value="Ready.")
        tk.Label(pf, textvariable=self._status, anchor="w").pack(fill="x")
        self._bar = ttk.Progressbar(pf, length=580, mode="determinate")
        self._bar.pack(fill="x", pady=4)

        # Log
        lf = tk.Frame(body)
        lf.pack(fill="x")
        self._log_txt = tk.Text(lf, height=10, font=("Menlo", 10),
                                bg="#f5f5f5", relief="flat",
                                state="disabled", wrap="none")
        sb = ttk.Scrollbar(lf, command=self._log_txt.yview)
        self._log_txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self._log_txt.pack(fill="x")

        # Buttons
        bf = tk.Frame(body)
        bf.pack(fill="x", pady=8)
        self._run_btn = tk.Button(bf, text="Process & Export",
                                  command=self._start, width=20,
                                  bg="#0070f3", fg="white", relief="flat",
                                  font=("Helvetica Neue", 11, "bold"),
                                  padx=8, pady=6)
        self._run_btn.pack(side="left", padx=4)
        self._cancel_btn = tk.Button(bf, text="Cancel",
                                     command=self._cancel,
                                     width=10, state="disabled",
                                     padx=8, pady=6)
        self._cancel_btn.pack(side="left")

        tk.Label(bf,
            text="Output: Apple ProRes 4444 · S-Log3/S-Gamut3.Cine",
            fg="#888", font=("Helvetica Neue", 10)
        ).pack(side="right", padx=4)

    def _io_row(self, parent, label, var, cmd):
        f = tk.Frame(parent)
        f.pack(fill="x", pady=3)
        tk.Label(f, text=label, width=13, anchor="w").pack(side="left")
        tk.Entry(f, textvariable=var, width=50).pack(side="left", padx=4)
        tk.Button(f, text="Browse…", command=cmd).pack(side="left")

    # ── File dialogs ──────────────────────────────────────────────────────────

    def _browse_input(self):
        d = filedialog.askdirectory(title="Select folder containing ARW files")
        if d:
            self._in_var.set(d)
            if not self._out_var.get():
                name = Path(d).name
                self._out_var.set(str(Path(d).parent / f"{name}_prores4444.mov"))

    def _browse_output(self):
        f = filedialog.asksaveasfilename(
            title="Save ProRes 4444 video as…",
            defaultextension=".mov",
            filetypes=[("QuickTime Movie", "*.mov"), ("All files", "*.*")]
        )
        if f:
            self._out_var.set(f)

    # ── Log polling ───────────────────────────────────────────────────────────

    def _enqueue_log(self, msg: str):
        self._log_q.put(msg)

    def _poll_log(self):
        try:
            while True:
                msg = self._log_q.get_nowait()
                self._log_txt.config(state="normal")
                self._log_txt.insert("end", msg + "\n")
                self._log_txt.see("end")
                self._log_txt.config(state="disabled")
        except queue.Empty:
            pass
        self.after(100, self._poll_log)

    # ── Pipeline control ──────────────────────────────────────────────────────

    def _start(self):
        inp = self._in_var.get().strip()
        out = self._out_var.get().strip()
        if not inp or not Path(inp).is_dir():
            messagebox.showerror("TimelapsePro", "Please select a valid input folder.")
            return
        if not out:
            messagebox.showerror("TimelapsePro", "Please choose an output file path.")
            return

        self._log_txt.config(state="normal")
        self._log_txt.delete("1.0", "end")
        self._log_txt.config(state="disabled")

        self._bar["value"] = 0
        self._status.set("Starting…")
        self._run_btn.config(state="disabled")
        self._cancel_btn.config(state="normal")

        p = Pipeline(
            input_dir   = Path(inp),
            output_path = Path(out),
            fps         = int(self._fps_var.get()),
            do_tilt     = self._tilt_var.get(),
            do_ec       = self._ec_var.get(),
            on_progress = self._on_progress,
            on_log      = self._enqueue_log,
            on_done     = self._on_done,
        )
        self._pipeline = p
        threading.Thread(target=p.run, daemon=True).start()

    def _cancel(self):
        if self._pipeline:
            self._pipeline.cancel()
            self._status.set("Cancelling…")

    def _on_progress(self, n: int, total: int):
        def _update():
            pct = n / total * 100
            self._bar["value"] = pct
            self._status.set(f"Frame {n} of {total}  ({pct:.0f}%)")
        self.after(0, _update)

    def _on_done(self, ok: bool, msg: str):
        def _update():
            self._run_btn.config(state="normal")
            self._cancel_btn.config(state="disabled")
            self._status.set(msg)
            if ok:
                messagebox.showinfo("TimelapsePro", msg)
            elif "Cancelled" not in msg:
                messagebox.showerror("TimelapsePro", msg)
        self.after(0, _update)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    App().mainloop()
