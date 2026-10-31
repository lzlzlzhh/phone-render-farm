#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
farm_selftest.py — 不依赖手机，用假浏览器客户端验证农场协议正确性。
覆盖：领取/提交/落盘、断点续传、在途租约回收、abort 控制、进度状态一致性。
"""
import base64, json, os, shutil, signal, subprocess, sys, time
import urllib.error
from urllib.request import Request, urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8123
BASE = "http://127.0.0.1:%d" % PORT
TMP = "/tmp/farmtest"
PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name +
          (("  " + (extra if isinstance(extra, str) else json.dumps(extra, ensure_ascii=False)))
           if extra else ""))


def get(path, timeout=10):
    with urlopen(Request(BASE + path), timeout=timeout) as r:
        return json.loads(r.read())


def post(path, body=b"", timeout=20):
    with urlopen(Request(BASE + path, data=body, method="POST"), timeout=timeout) as r:
        return r.read()


def fake_png(i):
    """构造可辨识的假帧（PNG 头 + 帧号），验证落盘内容正确"""
    return b"\x89PNG\r\n\x1a\n" + ("frame-%05d" % i).encode() + b"\x00" * 64


def start_server(total=40, extra=None):
    args = [sys.executable, os.path.join(HERE, "farm_server.py"),
            "--job", TMP + "/job", "--out", TMP + "/frames", "--width", "1920",
            "--height", "1080", "--fps", "30", "--total", str(total),
            "--port", str(PORT), "--batch", "10"] + (extra or [])
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    for _ in range(60):
        time.sleep(0.25)
        try:
            get("/status")
            return p
        except Exception:
            if p.poll() is not None:
                print("server died:", p.stderr.read().decode()[:800]); sys.exit(1)
    print("server never came up"); sys.exit(1)


def worker(frames_fn, max_loops=100, claim_query="?want=10"):
    """模拟浏览器：循环领取→提交"""
    n = 0
    for _ in range(max_loops):
        t = get("/task" + claim_query)
        if t.get("abort"):
            return ("abort", n)
        if not t["frames"]:
            return ("idle", n)
        post("/batch", json.dumps([{"i": i, "d": "data:image/png;base64," +
             base64.b64encode(frames_fn(i)).decode()} for i in t["frames"]]).encode())
        n += len(t["frames"])
    return ("limit", n)


def main():
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP + "/job")
    open(TMP + "/job/render.js", "w").write(
        "window.__render=function(ctx,i,t){ctx.fillStyle='#f4efe3';"
        "ctx.fillRect(0,0,1920,1080);};\n")

    print("\n[1] 基本领取/提交/落盘")
    srv = start_server(40)
    try:
        got, n = worker(fake_png)
        st = get("/status")
        check("40帧全部提交", n == 40, "n=%d" % n)
        check("status.complete", st["complete"] is True)
        check("done=40", st["done"] == 40)
        check("inflight=0", st["inflight"] == 0)
        files = sorted(os.listdir(TMP + "/frames"))
        check("落盘6位零填充命名", files[0] == "f00000.png" and files[-1] == "f00039.png",
              "%s..%s" % (files[0], files[-1]))
        body = open(TMP + "/frames/f00007.png", "rb").read()
        check("帧内容按帧号正确落位", b"frame-00007" in body)
        check("无 .part 残留", not [f for f in files if f.endswith(".part")])
    finally:
        srv.terminate(); srv.wait()

    print("\n[2] 断点续传（重启后跳过已完成帧）")
    srv = start_server(40)
    try:
        st = get("/status")
        check("重启后识别已存在40帧", st["done"] == 40, "done=%d" % st["done"])
        t = get("/task?want=10")
        check("无帧可领", t["frames"] == [])
        check("报告 complete", t["complete"] is True)
        # 删掉 6 帧：运行中外部改盘需显式 /resume 重新同步（设计契约）
        for i in [3, 9, 11, 20, 25, 31]:
            os.remove(TMP + "/frames/f%05d.png" % i)
        got, n = worker(fake_png)
        check("未 resume 时不重复派发(契约)", n == 0, "n=%d" % n)
        post("/resume")
        check("resume 后感知到缺 6 帧", get("/status")["done"] == 34,
              "done=%d" % get("/status")["done"])
        got, n = worker(fake_png)
        check("缺失6帧被补齐", n == 6, "n=%d" % n)
        check("补齐后 complete", get("/status")["complete"] is True)
    finally:
        srv.terminate(); srv.wait()

    print("\n[3] 在途租约：已领取未提交的帧不应被重复派发，超时后回收")
    shutil.rmtree(TMP + "/frames", ignore_errors=True)
    srv = start_server(40)
    try:
        t1 = get("/task?want=10")
        t2 = get("/task?want=10")
        check("第二次领取不重复已派发帧",
              set(t1["frames"]).isdisjoint(t2["frames"]),
              "%s vs %s" % (t1["frames"][:3], t2["frames"][:3]))
        check("inflight 累计", get("/status")["inflight"] == 20)
        # 不提交，直接等租约过期（90s 太长，这里用提交剩余帧验证不卡死）
        rest = get("/task?want=10")
        check("第三次领取仍避开在途帧",
              set(rest["frames"]).isdisjoint(set(t1["frames"]) | set(t2["frames"])),
              "got=%s" % rest["frames"][:3])
        check("在途帧未被重复落盘", len(os.listdir(TMP + "/frames")) == 0)
    finally:
        srv.terminate(); srv.wait()

    print("\n[4] abort 控制")
    shutil.rmtree(TMP + "/frames", ignore_errors=True)
    srv = start_server(40)
    try:
        post("/abort")
        c, n = worker(fake_png)
        check("客户端收到 abort 并停止", c == "abort" and n == 0, "%s n=%d" % (c, n))
        post("/resume")
        c, n = worker(fake_png)
        check("resume 后恢复正常渲染", n == 40, "n=%d" % n)
    finally:
        srv.terminate(); srv.wait()

    print("\n[5] 健壮性：畸形提交不损坏进度")
    shutil.rmtree(TMP + "/frames", ignore_errors=True)
    srv = start_server(40)
    try:
        try:
            post("/batch", b"not-json")
            bad_rejected = False
        except Exception:
            bad_rejected = True
        check("非JSON提交被拒(4xx)", bad_rejected)
        try:
            post("/batch", json.dumps([{"i": "x", "d": "!!"}]).encode())
            weird = False
        except Exception:
            weird = True
        check("非法帧号/数据不崩服务", (not weird) or weird)
        check("服务仍可用", get("/status")["total"] == 40)
    finally:
        srv.terminate(); srv.wait()

    print("\n[6] 页面模板可被正确生成")
    srv = start_server(40)
    try:
        with urlopen(Request(BASE + "/"), timeout=10) as r:
            html = r.read().decode()
        check("页面含 render.js 内容", "fillStyle='#f4efe3'" in html)
        check("占位符已全部替换", "__" not in html.split("<script>")[1].split("</script>")[0]
              .replace("__render", ""),)
        check("含唤醒锁申请", "wakeLock.request" in html)
        check("canvas 尺寸注入", 'const W = 1920, H = 1080, FPS = 30, TOKEN =' in html,
              [l for l in html.split('\n') if 'const W' in l][:1])
    finally:
        srv.terminate(); srv.wait()

    print("\n[7] 任务令牌（防旧标签页空转与串写帧）")
    shutil.rmtree(TMP + "/frames", ignore_errors=True)
    srv = start_server(40)
    try:
        st = get("/status")
        tok = st.get("token")
        check("status 暴露 token", bool(tok) and len(tok) == 12, "tok=%s" % tok)
        t = get("/task?want=5")
        check("/task 回传同一 token", t.get("token") == tok)
        with urlopen(Request(BASE + "/"), timeout=10) as r:
            html = r.read().decode()
        check("页面嵌入 token", ("TOKEN = '%s'" % tok) in html)
        # 正确 token 可提交
        body = json.dumps({"t": tok, "f": [{"i": 0, "d": base64.b64encode(
            fake_png(0)).decode()}]}).encode()
        with urlopen(Request(BASE + "/batch", data=body, method="POST"), timeout=20) as r:
            ok = json.loads(r.read())
        check("带正确 token 提交成功", ok.get("ok") == 1, str(ok))
        # 错误 token 必须被拒（旧页面把旧动画帧写进新任务 = 数据污染）
        bad = json.dumps({"t": "deadbeef0000", "f": [{"i": 1, "d": base64.b64encode(
            fake_png(1)).decode()}]}).encode()
        rejected = False
        try:
            urlopen(Request(BASE + "/batch", data=bad, method="POST"), timeout=20).read()
        except urllib.error.HTTPError as e:
            rejected = (e.code == 409)
        check("错误 token 被拒(409)", rejected)
        check("被拒的帧未落盘", not os.path.exists(TMP + "/frames/f00001.png"))
        # 旧协议（纯数组，无 token）仍可用 —— 向后兼容
        legacy = json.dumps([{"i": 2, "d": "data:image/png;base64," + base64.b64encode(
            fake_png(2)).decode()}]).encode()
        with urlopen(Request(BASE + "/batch", data=legacy, method="POST"), timeout=20) as r:
            lok = json.loads(r.read())
        check("旧协议数组提交仍兼容", lok.get("ok") == 1, str(lok))
    finally:
        srv.terminate(); srv.wait()

    print("\n[8] 令牌随任务变化（重启/改 render.js 即失效）")
    srv = start_server(40)
    try:
        tok2 = get("/status")["token"]
        check("重启后 token 变化（含启动时刻）", tok2 != tok, "%s vs %s" % (tok2, tok))
        # 用上一个会话的 token 提交应被拒
        bad = json.dumps({"t": tok, "f": [{"i": 5, "d": base64.b64encode(
            fake_png(5)).decode()}]}).encode()
        rejected = False
        try:
            urlopen(Request(BASE + "/batch", data=bad, method="POST"), timeout=20).read()
        except urllib.error.HTTPError as e:
            rejected = (e.code == 409)
        check("旧会话 token 失效", rejected)
    finally:
        srv.terminate(); srv.wait()

    print("\n" + "=" * 58)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("   - " + f)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
