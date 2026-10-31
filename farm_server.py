#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
phone-render-farm — 任务服务端
====================================
在容器内起一个 HTTP 服务，把渲染任务派发给手机上的默认浏览器执行，
浏览器把每一帧 PNG 回传落盘。容器负责调度、断点续传、进度与合成。

设计要点
  · 工作队列模型：浏览器每次向 /task 领取 N 个帧号，渲完 POST 回 /batch。
    页面被系统回收/用户切走后重新打开，只会领"还缺的帧"，天然断点续传。
  · 在途租约：领出的帧带时间戳，超时(默认 90s)未交回则自动回收，避免卡死。
  · 心跳：客户端每 5s POST /hb，服务端据此判断是否停滞(stalled)。
  · 控制：/control 返回 abort 标志，farm.sh stop 可优雅终止客户端循环。

仅用 Python 标准库，无第三方依赖。
"""
import argparse
import base64
import hashlib
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ----------------------------------------------------------------- 任务状态
class Job:
    def __init__(self, jobdir, outdir, fps, total, width, height, fmt, quality, context='2d'):
        self.lock = threading.Lock()
        self.jobdir = jobdir
        self.outdir = outdir
        self.fps = fps
        self.total = total
        self.width = width
        self.height = height
        self.fmt = fmt                      # png | jpeg | webp
        self.quality = quality              # 0..100，仅 jpeg/webp 有效
        self.ext = ".png" if fmt == "png" else (".jpg" if fmt == "jpeg" else ".webp")
        self.context = context          # '2d' | 'webgl2'
        self.abort = False
        self.started = time.time()
        self.render_js = open(os.path.join(jobdir, "render.js"), encoding="utf-8").read()
        # 任务身份令牌：render.js/尺寸/总帧数/启动时刻 任一变化即不同。
        # 旧浏览器标签连上新服务器时据此识别并强制刷新，杜绝 stale 页面空转或串写帧。
        self.token = hashlib.sha256(("%s|%sx%s|%s|%s" % (
            self.render_js, width, height, total, self.started)).encode()).hexdigest()[:12]
        self.last_batch = 0.0
        self.first_batch = 0.0
        self.span = 0.0          # 帧文件 mtime 跨度（重启恢复时的真实耗时依据）
        self.last_hb = 0.0
        self.inflight = {}                  # idx -> claimed_at
        self.done = set()
        self.errors = []
        os.makedirs(outdir, exist_ok=True)
        self._scan_disk()

    def _path(self, i):
        return os.path.join(self.outdir, "f%05d%s" % (i, self.ext))

    def _scan_disk(self):
        """以磁盘为准重建进度：已存在且非空的帧视为完成（支持断点续传）"""
        self.done = set()
        mt = []
        for i in range(self.total):
            p = self._path(i)
            try:
                if os.path.getsize(p) > 0:
                    self.done.add(i)
                    mt.append(os.path.getmtime(p))
            except OSError:
                pass
        # 帧文件时间戳跨度 = 真实渲染耗时（跨会话累计），比"启动至今"可靠
        self.span = (max(mt) - min(mt)) if len(mt) >= 2 else 0.0
        self.inflight = {}

    def pending(self, now):
        # 回收超时租约
        stale = [i for i, t in self.inflight.items() if now - t > 90]
        for i in stale:
            del self.inflight[i]
        return [i for i in range(self.total) if i not in self.done and i not in self.inflight]

    def claim(self, want):
        with self.lock:
            now = time.time()
            pend = self.pending(now)
            take = pend[:max(1, min(int(want), 60))]
            for i in take:
                self.inflight[i] = now
            return take

    def submit(self, pairs):
        with self.lock:
            ok = 0
            for i, data in pairs:
                try:
                    tmp = self._path(i) + ".part"
                    with open(tmp, "wb") as f:
                        f.write(data)
                    os.replace(tmp, self._path(i))
                    self.done.add(i)
                    self.inflight.pop(i, None)
                    ok += 1
                except Exception as e:
                    self.errors.append("write %d: %r" % (i, e))
            if not self.first_batch:
                self.first_batch = time.time()
            self.last_batch = time.time()
            return ok

    def _stall(self, now):
        """距上次有效产出的秒数。从未提交过则回溯心跳/启动时刻。"""
        if self.last_batch:
            return now - self.last_batch
        if self.last_hb:
            return now - self.last_hb
        return now - self.started

    def status(self):
        with self.lock:
            now = time.time()
            done = len(self.done)
            # 速率分母只统计「实际在渲染」的时间，任务完成后保持冻结：
            #   本次会话有产出 → last_batch - first_batch
            #   否则（重启恢复）→ 帧文件 mtime 跨度
            #   都没有 → 不报速率
            if self.first_batch and self.last_batch > self.first_batch:
                el = self.last_batch - self.first_batch
                rendered = done
            elif self.span > 0.5:
                el = self.span
                rendered = done
            else:
                el = 0
                rendered = 0
            rate = round(rendered / el, 2) if el > 0.5 else None
            return {
                "total": self.total, "done": done,
                "pending": self.total - done,
                "inflight": len(self.inflight),
                "fps": rate,
                "eta_sec": round((self.total - done) / rate) if rate else None,
                "render_seconds": round(el, 1),
                "wall_seconds": round(now - self.started, 1),
                "last_batch_age": round(now - self.last_batch, 1) if self.last_batch else None,
                "last_hb_age": round(now - self.last_hb, 1) if self.last_hb else None,
                "abort": self.abort,
                "complete": done >= self.total,
                "token": self.token,
                # 停滞判定：从未提交过→从启动算；有心跳→用批提交间隔；无心跳→取较近动静
                "stall_sec": int(self._stall(now)),
                "errors": self.errors[-5:],
                "out": self.outdir, "ext": self.ext,
                "width": self.width, "height": self.height, "job_fps": self.fps,
                "context": self.context,
            }


JOB = None

# ----------------------------------------------------------------- 页面模板
PAGE = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<title>render farm worker</title>
<style>
 html,body{margin:0;background:#0b0b0d;color:#e8e8ea;font:13px/1.5 ui-monospace,Menlo,monospace}
 #st{padding:10px 12px;white-space:pre-wrap;word-break:break-all}
 canvas{display:block;width:100vw;height:auto;background:#222}
 b{color:#7fd1ff}
</style></head><body>
<canvas id="c"></canvas>
<div id="st">booting…</div>
<script>
/* ================= 任务渲染代码（来自 job/render.js） ================= */
__RENDER_JS__
/* ================= 农场客户端运行时 ================= */
(async function(){
  const c = document.getElementById('c'), st = document.getElementById('st');
  const W = __W__, H = __H__, FPS = __FPS__, TOKEN = '__TOKEN__';
  const FMT = '__FMT__', QUAL = __QUAL__;
  c.width = W; c.height = H;
  const CTX_TYPE = '__CTX__';
  // WebGL 必须开 preserveDrawingBuffer：否则合成后绘制缓冲被清空，
  // 随后 toDataURL 只拿到背景色（已在 Adreno 830 上实测复现）。
  const ctx = CTX_TYPE === 'webgl2'
    ? (c.getContext('webgl2', { preserveDrawingBuffer: true, antialias: __AA__ })
       || c.getContext('webgl', { preserveDrawingBuffer: true, antialias: __AA__ }))
    : c.getContext('2d', { alpha: FMT === 'png' });
  if (!ctx) { st.textContent = 'FATAL: 无法取得 2d 上下文'; return; }
  if (typeof window.__render !== 'function') {
    st.textContent = 'FATAL: render.js 未定义 window.__render(ctx, frameIndex, t)'; return;
  }
  const mime = FMT === 'jpeg' ? 'image/jpeg' : (FMT === 'webp' ? 'image/webp' : 'image/png');
  const url = (p) => location.origin + p;
  let wakeLock = null;
  try { wakeLock = await navigator.wakeLock.request('screen'); } catch (e) {}
  const say = (m) => { st.textContent = m; };

  async function hb(){ try { await fetch(url('/hb'), {method:'POST', body:String(Date.now())}); } catch(e){} }
  setInterval(hb, 5000); hb();

  let done0 = 0, idle = 0, tStart = performance.now(), batch = __BATCH__, fails = 0;
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  while (true) {
    let task;
    try {
      const r = await fetch(url('/task?want=' + batch + '&token=' + TOKEN), {cache:'no-store'});
      task = await r.json();
    } catch (e) {
      fails++; say('领取任务失败(' + fails + ')，2s 后重试：' + e);
      await sleep(2000); continue;
    }
    // 服务器换了任务（render.js/参数/重启）→ 本页是旧副本，强制刷新载入新代码
    if (task.token && task.token !== TOKEN) {
      say('检测到服务器已更换任务，正在重新加载…');
      setTimeout(function(){ location.reload(); }, 300); return;
    }
    if (task.abort) { say('已暂停（服务器 abort），等待恢复…'); await sleep(3000); continue; }
    if (!task.frames || !task.frames.length) {
      if (task.complete) {
        say('✅ 全部 ' + task.total + ' 帧完成。可以回终端执行 assemble。'
            + '（本页保持在线；服务器换任务会自动刷新）');
        idle = 0; await sleep(4000); continue;
      }
      // 帧都在途（可能有其他页面占用）：退避等待，绝不静默退出
      idle = Math.min(idle + 1, 99);
      say('等待中… 暂无可领帧（在途 ' + (task.inflight ?? '?') + '），第 ' + idle + ' 次');
      await sleep(2000 + Math.min(idle, 10) * 500); continue;
    }
    idle = 0;
    const payload = [];
    try {
      for (const i of task.frames) {
        const t = i / FPS;
        ctx.setTransform(1,0,0,1,0,0); ctx.globalAlpha = 1;
        window.__render(ctx, i, t);
        payload.push([i, c.toDataURL(mime, FMT === 'png' ? undefined : QUAL / 100)]);
      }
    } catch (e) {
      say('渲染出错 frame=' + task.frames[0] + '：' + e + '\n（该批未提交，稍后自动重试）');
      try { await fetch(url('/clienterr'), {method:'POST',
        body: 'frame=' + task.frames[0] + ' :: ' + (e && (e.stack || e.message) || e)}); } catch(_) {}
      await new Promise(r => setTimeout(r, 3000)); continue;
    }
    try {
      const r = await fetch(url('/batch'), {
        method: 'POST',
        body: JSON.stringify({ t: TOKEN,
          f: payload.map(function (p) { return { i: p[0], d: p[1] }; }) })
      });
      if (r.status === 409) { say('本页与服务器任务不符，正在重新加载…');
        setTimeout(function(){ location.reload(); }, 300); return; }
      if (!r.ok) throw new Error('HTTP ' + r.status);
      fails = 0;
    } catch (e) {
      fails++; say('提交失败(' + fails + ')，1.5s 后重试：' + e);
      await new Promise(r => setTimeout(r, 1500)); continue;
    }
    const n = task.frames.length;
    done0 += n;
    const el = (performance.now() - tStart) / 1000;
    const rate = done0 / Math.max(0.001, el);
    say('渲染中  ' + (task.done + n) + ' / ' + task.total +
        '   ' + rate.toFixed(1) + ' fps   本批 ' + n + ' 帧\n' +
        '帧号 ' + task.frames[0] + '–' + task.frames[task.frames.length-1] +
        '   ' + W + 'x' + H + '   ' + mime.replace('image/','').toUpperCase() +
        (FMT === 'png' ? '' : (' q=' + QUAL)) +
        (wakeLock ? '   屏幕常亮✓' : '   (无唤醒锁，请保持亮屏)'));
    batch = __BATCH__;
  }
  try { if (wakeLock) await wakeLock.release(); } catch (e) {}
})();
</script></body></html>
"""


def page():
    j = JOB
    return (PAGE
            .replace("__RENDER_JS__", j.render_js)
            .replace("__W__", str(j.width)).replace("__H__", str(j.height))
            .replace("__FPS__", str(j.fps)).replace("__BATCH__", str(j.batch))
            .replace("__FMT__", j.fmt).replace("__QUAL__", str(j.quality))
            .replace("__TOKEN__", j.token)
            .replace("__CTX__", j.context).replace("__AA__", "true" if j.context!='2d' else "false"))


# ----------------------------------------------------------------- HTTP
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            return self._send(200, page(), "text/html; charset=utf-8")
        if u.path == "/task":
            frames = JOB.claim(int((q.get("want") or ["20"])[0]))
            s = JOB.status()
            return self._send(200, json.dumps({
                "frames": frames, "abort": s["abort"], "complete": s["complete"],
                "total": s["total"], "done": s["done"],
                "token": JOB.token, "inflight": s["inflight"]}))
        if u.path == "/control":
            return self._send(200, json.dumps({"abort": JOB.abort}))
        if u.path == "/status":
            return self._send(200, json.dumps(JOB.status(), indent=1))
        if u.path == "/health":
            return self._send(200, json.dumps({"ok": True, "t": time.time()}))
        return self._send(404, '{"error":"not found"}')

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        if u.path == "/hb":
            JOB.last_hb = time.time()
            return self._send(200, '{"ok":true}')
        if u.path == "/batch":
            try:
                data = json.loads(body)
                if isinstance(data, dict):
                    if data.get("t") != JOB.token:
                        return self._send(409, json.dumps(
                            {"error": "token mismatch (stale page)"}))
                    items = data.get("f") or []
                else:
                    items = data  # 兼容不带令牌的旧客户端：仅当尚无并发风险时可用
                pairs = []
                for it in items:
                    i = int(it["i"])
                    raw = it["d"]
                    if "," in raw[:40]:
                        raw = raw.split(",", 1)[1]
                    pairs.append((i, base64.b64decode(raw)))
                ok = JOB.submit(pairs)
                return self._send(200, json.dumps({"ok": ok}))
            except Exception as e:
                JOB.errors.append("batch: %r" % e)
                return self._send(400, json.dumps({"error": repr(e)}))
        if u.path == "/clienterr":
            JOB.errors.append("client: " + body.decode("utf8", "replace")[:500])
            return self._send(200, '{"ok":true}')
        if u.path == "/abort":
            JOB.abort = True
            return self._send(200, '{"abort":true}')
        if u.path == "/resume":
            JOB.abort = False
            JOB._scan_disk()
            return self._send(200, json.dumps(JOB.status()))
        if u.path == "/shutdown":
            self._send(200, '{"bye":true}')
            threading.Thread(target=lambda: os._exit(0), daemon=True).start()
            return
        return self._send(404, '{"error":"not found"}')


# ----------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="phone render farm server")
    ap.add_argument("--job", required=True, help="任务目录，内含 render.js")
    ap.add_argument("--out", required=True, help="帧输出目录")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--fps", type=int, default=30, help="帧率，用于 frame→时间换算")
    ap.add_argument("--total", type=int, required=True, help="总帧数")
    ap.add_argument("--format", choices=["png", "jpeg", "webp"], default="png")
    ap.add_argument("--context", choices=["2d", "webgl2"], default="2d",
                    help="canvas context type; webgl2 enables three.js/WebGL scenes")
    ap.add_argument("--quality", type=int, default=90)
    ap.add_argument("--batch", type=int, default=20)
    ap.add_argument("--port", type=int, default=8098)
    ap.add_argument("--bind", default="127.0.0.1")
    a = ap.parse_args()

    global JOB
    JOB = Job(a.job, a.out, a.fps, a.total, a.width, a.height, a.format, a.quality, a.context)
    JOB.batch = a.batch
    srv = ThreadingHTTPServer((a.bind, a.port), H)
    print(json.dumps({"started": True, "port": a.port, "total": a.total,
                      "already_done": len(JOB.done), "url":
                      "http://%s:%d/" % (a.bind, a.port)}))
    sys.stdout.flush()
    srv.serve_forever()


if __name__ == "__main__":
    main()
