#!/usr/bin/env bash
# =============================================================================
#  phone-render-farm — 用手机默认浏览器当渲染农场
#  用法: ./farm.sh <命令> [参数]
#    new    <job目录> <宽> <高> <fps> <时长秒>   生成任务骨架(render.js 模板)
#    start  <job目录> [--format png|jpeg|webp] [--quality 90] [--batch 20] [--port 8098]
#    open   <job目录>          调起手机默认浏览器打开工作页
#    watch  <job目录>          实时显示进度，直到完成或停滞
#    status <job目录>          一次性查询
#    stop   <job目录>          终止客户端循环并关闭服务
#    resume <job目录>          外部改动帧文件后重新同步进度
#    assemble <job目录> [音轨] 用帧合成 MP4（不给音轨则输出无声视频）
#    demo                     跑一次内置示例（12帧，验证链路，不碰手机）
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNROOT="${FARM_RUNROOT:-/tmp/phone-render-farm}"

rid_of() { basename "$(cd "$1" && pwd)"; }   # 目录规范化为绝对路径再取基名

say()  { printf '%s\n' "$*"; }
err()  { printf '\033[31m%s\033[0m\n' "$*" >&2; }
note() { printf '\033[36m%s\033[0m\n' "$*"; }

# ---- 端口占用检查（纯标准库，避免依赖 lsof/ss 的可用性差异）----
port_free() {
  python3 - "$1" <<'PY'
import socket,sys
s=socket.socket(); s.settimeout(1)
try:
    s.connect(('127.0.0.1',int(sys.argv[1]))); print('busy')
except Exception:
    print('free')
finally:
    s.close()
PY
}

api() { # api <jobdir> <GET|POST> <path>
  local jd="$1" m="$2" p="$3" port
  port="$(cat "$RUNROOT/$(rid_of "$jd")/port" 2>/dev/null || echo 8098)"
  if [ "$m" = GET ]; then
    curl -s --max-time 10 "http://127.0.0.1:${port}${p}"
  else
    curl -s --max-time 20 -X POST "http://127.0.0.1:${port}${p}"
  fi
}

cmd_new() {
  local jd="${1:?用法: farm.sh new <job目录> <宽> <高> <fps> <时长秒>}"
  local w="${2:?}" h="${3:?}" fps="${4:?}" dur="${5:?}"
  local CTX="${6:-2d}"
  mkdir -p "$jd"
  local total
  total=$(python3 -c "import math;print(math.ceil($dur*$fps))")
  cat > "$jd/job.conf" <<EOF
width=$w
height=$h
fps=$fps
duration=$dur
total=$total
format=png
quality=90
batch=20
port=8098
context=${CTX:-2d}
EOF
  cat > "$jd/render.js" <<EOF
/* ============================================================================
 * 任务渲染代码 —— 必须定义  window.__render(ctx, frameIndex, t)
 *   ctx        : 1920x1080(按配置) 的 2D 上下文
 *   frameIndex : 帧号 0..total-1
 *   t          : 秒 = frameIndex / fps
 * 约束（决定成片正确性，务必遵守）
 *   1. 确定性：同一 frameIndex 必须永远画出同一帧。禁止 Math.random()/Date.now()，
 *      需要随机就用 frameIndex 播种的伪随机（见下方 prnd）。
 *   2. 每帧自己铺满背景：不依赖上一帧状态（ctx 会被复用，但农场每帧会重置
 *      transform 与 globalAlpha，其余状态请自行设置）。
 *   3. 字体用系统已装字体名（如 "Noto Serif CJK SC"），中文才不会变豆腐块。
 *   4. 不要在这里放音频/网络请求/长循环——单帧超过 ~2s 会被租约超时回收。
 * ==========================================================================*/
function prnd(seed){ let s = seed >>> 0; return function(){ s = (s*1664525 + 1013904223) >>> 0; return s / 4294967296; }; }

window.__render = function (ctx, i, t) {
  const W = ctx.canvas.width, H = ctx.canvas.height;
  // 背景
  ctx.fillStyle = '#f4efe3';
  ctx.fillRect(0, 0, W, H);
  // 示例：随时间推进的方块 + 计数
  const k = Math.min(1, t / 4);
  const size = 200 * k;
  ctx.fillStyle = '#c23a1b';
  ctx.fillRect(W / 2 - size / 2, H / 2 - size / 2, size, size);
  ctx.fillStyle = '#1a1a1a';
  ctx.font = '900 64px "Noto Serif CJK SC", serif';
  ctx.textAlign = 'center';
  ctx.fillText('第 ' + i + ' 帧 · ' + t.toFixed(2) + 's', W / 2, H / 2 + 220);
  ctx.fillText('手机浏览器渲染农场', W / 2, 160);
};
EOF
  mkdir -p "$jd/frames"
  note "任务骨架已生成: $jd"
  say "  总帧数 $total ($dur 秒 × $fps fps)，尺寸 ${w}x${h}"
  say "  下一步：编辑 $jd/render.js 写你的画面，然后"
  say "    ./farm.sh start $jd && ./farm.sh open $jd && ./farm.sh watch $jd"
}

read_conf() { python3 - "$1" "$2" <<'PY'
import sys
d={}
for line in open(sys.argv[1],encoding='utf-8'):
    line=line.strip()
    if line and not line.startswith('#') and '=' in line:
        k,v=line.split('=',1); d[k]=v
print(d.get(sys.argv[2],''))
PY
}

cmd_start() {
  local jd="${1:?用法: farm.sh start <job目录> [选项]}"
  shift || true
  [ -f "$jd/render.js" ] || { err "缺少 $jd/render.js（先执行 farm.sh new）"; exit 1; }
  local cfg="$jd/job.conf"
  [ -f "$cfg" ] || { err "缺少 $cfg"; exit 1; }
  local w h fps total fmt qual batch port
  w=$(read_conf "$cfg" width); h=$(read_conf "$cfg" height)
  fps=$(read_conf "$cfg" fps); total=$(read_conf "$cfg" total)
  fmt=png; qual=90; batch=20; port=8098
  bind=127.0.0.1; host=""; ctx="2d"
  while [ $# -gt 0 ]; do
    case "$1" in
      --format)   fmt="$2"; shift 2;;
      --quality)  qual="$2"; shift 2;;
      --batch)    batch="$2"; shift 2;;
      --port)     port="$2"; shift 2;;
      --bind)     bind="$2"; shift 2;;
      --host)     host="$2"; shift 2;;
      --context)  ctx="$2"; shift 2;;
      *) err "未知选项 $1"; exit 1;;
    esac
  done
  # host = 手机实际访问的地址；未指定时与 bind 一致
  [ -n "$host" ] || host="$bind"
  # 已有服务在跑？先探测，避免端口冲突后静默失败
  if [ "$(port_free "$port")" = busy ]; then
    if curl -s --max-time 3 "http://127.0.0.1:${port}/health" | grep -q '"ok"'; then
      err "端口 $port 上已有农场服务在运行。先执行: ./farm.sh stop <job目录>，或换 --port"
      exit 1
    fi
    err "端口 $port 被其他进程占用，请用 --port 换一个"; exit 1
  fi
  local rid run
  rid="$(rid_of "$jd")"
  run="$RUNROOT/$rid"
  mkdir -p "$run"
  echo "$port" > "$run/port"
  echo "$host" > "$run/host"
  echo "$ctx" > "$run/context"

  note "启动农场服务 :$port  (total=$total ${w}x${h} ${fps}fps fmt=$fmt ctx=$ctx)"
  setsid nohup python3 "$HERE/farm_server.py" \
      --job "$jd" --out "$jd/frames" --width "$w" --height "$h" --fps "$fps" \
      --total "$total" --format "$fmt" --quality "$qual" --batch "$batch" --context "$ctx" \
      --port "$port" --bind "$bind" \
      > "$run/server.log" 2>&1 < /dev/null &
  echo $! > "$run/server.pid"

  for _ in $(seq 1 40); do
    sleep 0.25
    if curl -s --max-time 2 "http://127.0.0.1:${port}/health" 2>/dev/null | grep -q '"ok"'; then
      api "$jd" GET /status | python3 -c "
import json,sys
d=json.load(sys.stdin)
print('  ✓ 服务就绪  已完成 %d/%d 帧（断点续传生效）' % (d['done'], d['total']))"
      note "下一步： ./farm.sh open $jd   然后 ./farm.sh watch $jd"
      return 0
    fi
  done
  err "服务启动失败，日志："
  tail -20 "$run/server.log" >&2 || true
  exit 1
}

cmd_open() {
  local jd="${1:?用法: farm.sh open <job目录>}"
  local port run rid
  rid="$(rid_of "$jd")"
  port="$(cat "$RUNROOT/$rid/port" 2>/dev/null || echo 8098)"
  host="$(cat "$RUNROOT/$rid/host" 2>/dev/null || echo 127.0.0.1)"
  local url="http://${host}:${port}/?r=${RANDOM}"   # 随机参数强制绕过缓存
  curl -s --max-time 8 "http://127.0.0.1:3090/app/version" >/dev/null 2>&1 || \
    note "（提示：DSHA 桥不可用时需你在手机上手动用浏览器打开 $url）"
  local resp
  resp=$(curl -s -G -H @/root/.dsh/.bridge_headers "http://127.0.0.1:3090/app/open" \
         --data-urlencode "url=$url" 2>&1 | head -c 300)
  note "已请求手机默认浏览器打开：$url"
  say "  桥返回: $resp"
  say ""
  note "请在手机上确认："
  say "  1. 浏览器已加载页面并显示「渲染中」进度（不是登录页/报错页）"
  say "  2. 保持屏幕点亮、页面在前台，不要切走或锁屏"
  say "  3. 若中途被打断，重新执行 ./farm.sh open $jd 即可从未完成帧继续"
}

cmd_status() {
  local jd="${1:?用法: farm.sh status <job目录>}"
  api "$jd" GET /status | python3 -c "
import json,sys
d=json.load(sys.stdin)
bar='█'*int(30*d['done']/max(1,d['total']))
print('[%-30s] %d/%d  %.1f%%' % (bar, d['done'], d['total'], 100*d['done']/max(1,d['total'])))
fps = d.get('fps')
print('  速率 %s   预计剩余 %s   在途 %d   已渲染 %ss' % (
      ('%.1f fps' % fps) if fps else '—（本次会话尚无产出）',
      (str(d['eta_sec'])+'s') if d.get('eta_sec') else '-',
      d['inflight'], d.get('render_seconds', 0)))
if d['last_hb_age'] is not None: print('  浏览器心跳 %.0fs 前' % d['last_hb_age'])
if d['errors']: print('  最近错误:', d['errors'])
"
}

cmd_watch() {
  local jd="${1:?用法: farm.sh watch <job目录>}"
  local stall_limit="${STALL_LIMIT:-150}"
  note "监控中（Ctrl-C 只退出监控，不影响手机渲染）"
  while true; do
    local s
    if ! s="$(api "$jd" GET /status 2>/dev/null)" || [ -z "$s" ]; then
      err "服务无响应（可能已 stop）。如中断，重新 ./farm.sh start $jd"; exit 1
    fi
    printf '\r\033[K'
    echo "$s" | python3 -c "
import json,sys
d=json.load(sys.stdin)
f = d.get('fps')
print('%d/%d %.1f%%  %s  剩余%s  停滞%ds' % (d['done'],d['total'],100*d['done']/max(1,d['total']),
      ('%.1ffps'%f) if f else '--fps',
      (str(d['eta_sec'])+'s') if d.get('eta_sec') else '-',
      d.get('stall_sec',0)), end='')
sys.exit(0)"
    echo ""
    # 一次解析取全部字段，避免多次起 python 且字段间不一致
    eval "$(echo "$s" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print('done=%d; complete=%d; stall=%d' % (d['done'], 1 if d['complete'] else 0, d.get('stall_sec',0)))")"
    [ "$complete" = 1 ] && { echo ""; note "✅ 全部 $done 帧完成。执行: ./farm.sh assemble $jd [音轨]"; exit 0; }
    if [ "$stall" -gt "$stall_limit" ]; then
      echo ""; err "⚠ 已 ${stall}s 没有新帧 —— 手机页面可能被切走/锁屏/回收。"
      err "  恢复方法： ./farm.sh open $jd   （会自动从未完成帧继续）"
      exit 2
    fi
    sleep 3
  done
}

cmd_stop() {
  local jd="${1:?用法: farm.sh stop <job目录>}"
  local rid port
  rid="$(rid_of "$jd")"
  port="$(cat "$RUNROOT/$rid/port" 2>/dev/null || echo 8098)"
  api "$jd" POST /abort >/dev/null 2>&1 || true
  sleep 0.5
  api "$jd" POST /shutdown >/dev/null 2>&1 || true
  local pidf="$RUNROOT/$rid/server.pid"
  if [ -f "$pidf" ]; then
    local p; p=$(cat "$pidf")
    kill "$p" 2>/dev/null || true
    rm -f "$pidf"
  fi
  note "已停止（帧文件保留在 $jd/frames，可随时 resume）"
}

cmd_resume() {
  local jd="${1:?用法: farm.sh resume <job目录>}"
  api "$jd" POST /resume | python3 -c "
import json,sys
d=json.load(sys.stdin); print('同步后: 已完成 %d/%d，待渲 %d' % (d['done'],d['total'],d['pending']))"
}

cmd_assemble() {
  local jd="${1:?用法: farm.sh assemble <job目录> [音轨文件]}"
  local audio="${2:-}"
  local cfg="$jd/job.conf"
  local fps port rid
  fps=$(read_conf "$cfg" fps)
  rid="$(rid_of "$jd")"
  port="$(cat "$RUNROOT/$rid/port" 2>/dev/null || echo 8098)"
  local ext
  ext=$(api "$jd" GET /status 2>/dev/null | python3 -c "import json,sys;print(json.load(sys.stdin).get('ext','.png'))" 2>/dev/null || echo .png)
  local n
  n=$(ls "$jd/frames"/f*"$ext" 2>/dev/null | wc -l | tr -d ' ')
  [ "$n" -gt 0 ] || { err "$jd/frames 下没有帧文件"; exit 1; }
  local out="$jd/${rid}.mp4"
  note "合成 $n 帧 → $out"
  command -v ffmpeg >/dev/null || { err "需要 ffmpeg"; exit 1; }
  if [ -n "$audio" ]; then
    [ -f "$audio" ] || { err "音轨不存在: $audio"; exit 1; }
    ffmpeg -y -v warning -framerate "$fps" -start_number 0 -thread_queue_size 1024 \
      -i "$jd/frames/f%05d${ext}" -thread_queue_size 1024 -i "$audio" \
      -c:v libx264 -preset medium -crf 18 -pix_fmt yuv420p \
      -c:a aac -b:a 192k -ar 44100 -ac 2 -shortest -movflags +faststart "$out"
  else
    ffmpeg -y -v warning -framerate "$fps" -start_number 0 -thread_queue_size 1024 \
      -i "$jd/frames/f%05d${ext}" \
      -c:v libx264 -preset medium -crf 18 -pix_fmt yuv420p \
      -an -movflags +faststart "$out"
  fi
  ffprobe -v error -show_entries format=duration,size \
    -show_entries stream=codec_name,width,height,r_frame_rate -of default=nw=1 "$out"
  note "完成: $out"
}

cmd_doctor() {
  note "环境自检 —— 判断本工具在当前设备上能否工作"
  HERE="$HERE" python3 - <<'PYEOF'
import os, socket, shutil, subprocess, sys
def ok(m):   print("  [ok]   " + m)
def warn(m): print("  [warn] " + m)
def bad(m):  print("  [FAIL] " + m)
# 脚本自身目录由 shell 显式注入（heredoc 模式下 __file__ 是 "<stdin>"，不可用）
d = os.environ.get("HERE") or os.getcwd()

def probe_port(p):
    s = socket.socket(); s.settimeout(1.5)
    try:
        s.connect(('127.0.0.1', p)); return True
    except Exception:
        return False
    finally:
        s.close()

print("— 必需 —")
v = sys.version_info
(ok if v >= (3, 8) else bad)("python3 %d.%d.%d（需 ≥3.8，仅用标准库）" % (v[0], v[1], v[2]))
print("— 关键前提：手机浏览器能否访问本进程监听的端口 —")
# 探针：DSHA 桥由 Android 侧 App 监听 3090。若容器内可达，即证明容器与宿主
# 共享 loopback/网络命名空间 —— 手机浏览器同样能访问本工具的 127.0.0.1 端口。
if probe_port(3090):
    ok("127.0.0.1:3090 可达（DSHA 桥由 Android 侧监听）")
    ok("  → 容器与宿主共享网络命名空间，故手机浏览器应能访问本工具的 loopback 端口")
    warn("  此为单向推断；端到端确认请跑一次实际渲染并观察 /status 的 last_hb_age 是否变非 null")
else:
    warn("127.0.0.1:3090 不可达 → 无法证明共享 loopback（可能非 DSH 环境/独立容器）")
    warn("此时需让服务绑定到手机可达的地址：")
    warn("  farm.sh start <job> --bind 0.0.0.0 --host <本机局域网IP>")
    warn("  并确保手机与本机同网段、防火墙放行该端口")
print("— 组件完整性 —")
for f in ("farm_server.py", "farm.sh"):
    (ok if os.path.exists(os.path.join(d, f)) else bad)("找到 %s" % f)
print("— 可选 —")
if shutil.which("ffmpeg"):
    ok("ffmpeg 可用（assemble 合成需要）")
else:
    warn("ffmpeg 不可用 → 无法执行 assemble，只能产出帧序列")
if shutil.which("setsid"):
    ok("setsid 可用（服务脱离会话、跨命令存活）")
else:
    warn("setsid 不可用 → 需用受管理的后台任务方式启动服务，否则调用结束即被回收")
# 执行位：/sdcard 等挂载点常挂 noexec，./farm.sh 会 Permission denied
fs = os.path.join(d, "farm.sh")
if os.access(fs, os.X_OK):
    ok("farm.sh 有执行位 → 可直接 ./farm.sh")
else:
    # 区分两种成因，处置方式不同：文件系统不支持 chmod +x，还是仅缺执行位
    try:
        st = os.stat(fs); mode = st.st_mode
        probe = os.path.join(d, ".xprobe")
        open(probe, "wb").write(b"#!/bin/sh\n")
        os.chmod(probe, 0o755)
        can = os.access(probe, os.X_OK)
        os.remove(probe)
    except Exception:
        can = None
    if can is False:
        warn("该文件系统不支持执行位（noexec 挂载，如 /sdcard）→ 必须 `bash farm.sh ...`")
    else:
        warn("farm.sh 缺执行位（文件系统本身支持）→ `chmod +x farm.sh` 即可，")
        warn("  或直接用 `bash farm.sh ...`（永远有效，推荐写进文档）")
# 浏览器调起通道
tok = "/root/.dsh/.bridge_headers"
if os.path.exists(tok):
    ok("DSHA 桥凭据存在 → open 可自动调起手机默认浏览器")
else:
    warn("无 DSHA 桥凭据 → open 需你手动在手机浏览器输入 URL（其余功能不受影响）")
print("— 不适用场景 —")
print("  本工具渲染 Canvas 2D 画面（依赖 canvas.toDataURL 取像素）。")
print("  不适用 Remotion / DOM 排版渲染：手机浏览器无 CDP 截图通道，")
print("  DOM→canvas 只能靠 html2canvas 类库，保真度不足。")
PYEOF
}

cmd_demo() {
  local d=/tmp/farm-demo
  rm -rf "$d"; mkdir -p "$d/frames"
  cat > "$d/job.conf" <<'EOF'
width=640
height=360
fps=30
duration=0.4
total=12
format=png
quality=90
batch=6
port=8123
EOF
  cat > "$d/render.js" <<'EOF'
window.__render = function (ctx, i, t) {
  var W = ctx.canvas.width, H = ctx.canvas.height;
  ctx.fillStyle = '#f4efe3'; ctx.fillRect(0, 0, W, H);
  ctx.fillStyle = '#c23a1b';
  ctx.fillRect(40 + i * 45, H / 2 - 40, 60, 80);
  ctx.fillStyle = '#1a1a1a';
  ctx.font = '900 28px "Noto Serif CJK SC", serif';
  ctx.fillText('demo frame ' + i, 40, 60);
};
EOF
  note "离线自测：不启动手机，直接用假客户端跑 12 帧"
  setsid nohup python3 "$HERE/farm_server.py" --job "$d" --out "$d/frames" \
    --width 640 --height 360 --fps 30 --total 12 --format png --batch 6 \
    --port 8123 --bind 127.0.0.1 > /tmp/farm-demo.log 2>&1 < /dev/null &
  sleep 1.5
  python3 - <<'PY'
import base64, json, urllib.request
B='http://127.0.0.1:8123'
def g(p):
    return json.loads(urllib.request.urlopen(B+p, timeout=10).read())
def p(path, body=b''):
    return urllib.request.Request(B+path, data=body, method='POST')
n=0
for _ in range(20):
    t=g('/task?want=6')
    if not t['frames']: break
    payload=json.dumps([{'i':i,'d':'data:image/png;base64,'+base64.b64encode(
        b'\x89PNG\r\n\x1a\n'+b'x'*40).decode()} for i in t['frames']]).encode()
    urllib.request.urlopen(p('/batch',payload),timeout=20).read()
    n+=len(t['frames'])
s=g('/status')
print('提交 %d 帧, complete=%s, done=%d/%d' % (n, s['complete'], s['done'], s['total']))
assert s['complete'] and n==12, 'demo failed'
print('DEMO PASS')
urllib.request.urlopen(p('/shutdown'),timeout=5).read()
PY
  note "示例通过：协议、落盘、进度统计均正常（真实渲染仍需手机浏览器）"
}

case "${1:-}" in
  new)      shift; cmd_new "$@";;
  start)    shift; cmd_start "$@";;
  open)     shift; cmd_open "$@";;
  watch)    shift; cmd_watch "$@";;
  status)   shift; cmd_status "$@";;
  stop)     shift; cmd_stop "$@";;
  resume)   shift; cmd_resume "$@";;
  assemble) shift; cmd_assemble "$@";;
  doctor)   shift; cmd_doctor "$@";;
  demo)     shift; cmd_demo "$@";;
  *) cat <<EOF
phone-render-farm — 用手机默认浏览器渲染 Canvas 动画帧

  ./farm.sh new      <job目录> <宽> <高> <fps> <时长秒>   生成任务骨架
  ./farm.sh start    <job目录> [--format png|jpeg|webp] [--quality 90]
                                [--batch 20] [--port 8098]
  ./farm.sh open     <job目录>   调起手机默认浏览器
  ./farm.sh watch    <job目录>   监控进度（停滞会提示恢复方法）
  ./farm.sh status   <job目录>   一次性查询
  ./farm.sh resume   <job目录>   外部改动帧文件后重新同步
  ./farm.sh stop     <job目录>   终止并关闭服务（帧保留）
  ./farm.sh assemble <job目录> [音轨]  合成 MP4
  ./farm.sh doctor               环境自检：本设备能否工作、缺什么
  ./farm.sh demo                 离线验证协议（不碰手机）

最小流程:  new → 编辑 render.js → start → open → watch → assemble
EOF
     exit 1;;
esac
