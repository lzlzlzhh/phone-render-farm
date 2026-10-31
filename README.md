# phone-render-farm

Render JavaScript **Canvas 2D** animations into a PNG frame sequence and an MP4 — by using an **Android phone's browser as the renderer**.

No browser ever launches inside your agent host. The host serves a page over loopback; the phone's browser loads it, draws each frame on a `<canvas>`, and POSTs the PNGs back.

```
agent host (container / VM / CI)              Android phone
┌──────────────────────────┐    HTTP      ┌────────────────────────────┐
│ farm_server.py           │ ───────────▶ │ browser opens the page      │
│  · work queue + 90s lease│              │  render.js draws frame i    │
│  · frames/f00000.png     │ ◀─────────── │  canvas.toDataURL() → POST  │
└──────────────────────────┘   PNG back   └────────────────────────────┘
          └── ffmpeg: frames + audio ──▶ MP4
```

## Why this exists

Some agent hosts cannot run a headless browser at all. This project was built on an Ubuntu 24.04 container running on an **Android 15/17 kernel**, where every attempt to launch Chromium dies with `SIGTRAP` (exit 133) — the container's own Chromium is fine (zero missing libraries), but the Android kernel does not give it the process/sandbox primitives it needs (`NoNewPrivs: 1`, `Seccomp: 2`).

Verified failures on that host: Playwright, `@puppeteer/browsers`, apt `chromium-browser` (a snap stub), headless shell, `--single-process`, `--no-zygote`, `--disable-seccomp-filter-sandbox`, `--print-to-pdf`, `--dump-dom`, Xvfb, and a session D-Bus. All exit 133.

The phone's browser has none of these problems — it runs in a process context Android already approved. So: **stop launching a browser, and borrow one that is already running.**

## Install

As an [agent skill](https://agentskills.io) (Claude Code / DeepSeek Harness / any harness that reads `SKILL.md`):

```bash
git clone <this-repo> ~/.claude/skills/phone-render-farm
```

Or just use the scripts directly — they need nothing but a POSIX shell and Python:

```bash
git clone <this-repo> && cd phone-render-farm
bash farm.sh doctor
```

**Requirements:** Python 3.8+ (standard library only — no pip installs), bash, an Android device whose browser can reach a port on the host, and `ffmpeg` only if you want MP4 output.

### Which browser on the phone

Both **Chrome** and **Microsoft Edge** have been verified working on-device (three.js r186 imported, 3D scene rendered, frames captured). Other browsers are untested.

If a render stalls — the browser can occasionally get into a bad state after repeated page loads. Force-stopping the browser and re-opening the URL clears it; frames resume from where they left off.

## Quick start

```bash
bash farm.sh doctor                          # can this device work?
bash farm.sh new  /tmp/job 1920 1080 30 20   # 20s × 30fps = 600 frames
$EDITOR /tmp/job/render.js                   # define window.__render(ctx, i, t)
bash farm.sh start /tmp/job                  # detached server
bash farm.sh open  /tmp/job                  # launch the phone's browser
bash farm.sh watch /tmp/job                  # poll until ✅
bash farm.sh assemble /tmp/job mix.wav       # MP4 (omit audio for silent video)
```

`open` auto-launches the default browser through the DSHA device bridge when present; otherwise it prints the URL and you open it yourself. Everything else is identical.

## Measured

On a OnePlus (Android 17, arm64 container), 1920×1080 PNG:

| | |
|---|---|
| 600 frames | **20.2s — 29.8 fps** |
| 3000 frames | **37.8 fps** |
| Return throughput | 427 MB/s (10 MB in 0.02s) — never the bottleneck |
| Frame integrity | 3000/3000, zero missing, all 1920×1080 |
| Same job via in-container `skia-canvas` | 600 frames in 45.9s (13 fps) |

The phone is **~2.3× faster** than rendering in-process with Skia on the host, because the browser composites on real GPU-backed Skia.

## The `render.js` contract

```js
window.__render = function (ctx, i, t) {   // t === i / fps
  const W = ctx.canvas.width, H = ctx.canvas.height;
  ctx.fillStyle = '#f4efe3'; ctx.fillRect(0, 0, W, H);
  ctx.fillStyle = '#c23a1b';
  ctx.fillRect(W / 2 - 100 * t / 4, H / 2 - 50, 200 * t / 4, 100);
};
```

Three rules, because they are what actually breaks renders:

- **Deterministic.** Same `i` must always draw the same pixels. No `Math.random()`, no `Date.now()` — seed a PRNG from `i` (`farm.sh new` includes a `prnd()` helper). Otherwise resumed frames are inconsistent and the seams only show up in playback.
- **Self-contained.** The farm resets `transform` and `globalAlpha` per frame. Set your own styles and paint your own background; never depend on the previous frame.
- **Under ~2s per frame.** Slower frames exceed the 90s lease, get reclaimed, and re-render forever.

Reuse an existing Canvas animation by appending an adapter:

```js
window.__render = (function () {
  var render = null;
  return function (ctx, i, t) {
    if (!render) render = window.Anim(ctx.canvas);
    render.render(t);
  };
})();
```

## It survives interruption

This is the part that matters for long renders on a phone.

Progress is derived from **which frame files exist on disk**, not from in-memory state. So:

| Event | What happens |
|---|---|
| Phone locks, tab is killed, call arrives | That page stops claiming. Leases expire after 90s, frames return to the queue. Re-run `open` — completed frames are never re-rendered |
| You change `render.js` and restart the server | The server embeds a **job token** (hash of `render.js` + dimensions + total + start time). A stale browser tab detects the mismatch and **reloads itself** — you don't touch the phone |
| A stale tab tries to submit anyway | Rejected with `409`. This is the important one: without it, an old tab silently writes **old-animation pixels into the new job**, and the frame counter looks perfectly healthy |
| Two phones, or two tabs | They claim disjoint frames and render in parallel |
| You add/delete frame files by hand | `farm.sh resume <job>` re-syncs |

Stall detection is server-side; `watch` exits with a recovery hint past `STALL_LIMIT` (default 150s) instead of hanging forever.

## Scope — what this is NOT

**Canvas only.** Pixels come from `canvas.toDataURL()`. A phone browser exposes **no CDP screenshot channel** (`getDisplayMedia` is absent, and a `chrome.debugger`-based extension can't be installed on Android browsers), so DOM content is unreachable at fidelity.

That means:

- ❌ **Remotion / React DOM rendering** — Remotion's own renderer launches a headless Chrome, which is precisely what fails here. If you need DOM fidelity, render where a real browser works, or drive a remote browser over CDP.
- ❌ **Website automation** — see [BrowserSkill](https://github.com/Tencent/BrowserSkill) for that; it's a different problem (and its extension needs `chrome.debugger`, unavailable on Android).
- ✅ **Canvas 2D** animations, data visualizations, infographics, generated art.
- ✅ **WebGL2** via `--context webgl2` — measured working on Adreno 830 / ANGLE (raw GL pipeline). three.js works on both Chrome and Edge.

### WebGL: one setting decides whether you get pixels

WebGL clears its drawing buffer after each composite, so `toDataURL()` returns **only the background** unless the context is created with `preserveDrawingBuffer: true`. Measured on a phone (same scene, same frame):

| Context option | `readPixels` at canvas centre | Result |
|---|---|---|
| `preserveDrawingBuffer: true` | `[194, 58, 27, 255]` | ✅ the red box — frame captured |
| default (`false`) | `[244, 239, 227, 255]` | ❌ background only — **content silently lost** |

`farm.sh start <job> --context webgl2` sets this for you. Write `window.__render(gl, i, t)` against the GL context (compile your program once, guarded by a flag on the context object; drive animation with a uniform).

> If you use three.js, create the renderer with `preserveDrawingBuffer: true` yourself — the flag is per-context, and three.js defaults it to `false`.

**Storage.** 1080p PNG ≈ 270 KB/frame; 600 frames ≈ 160 MB. `--format jpeg --quality 92` cuts it ~5× with some banding — fine for photography-like content, avoid for flat-color infographics.

**Cross-device.** The default `127.0.0.1` works when the host and phone share a network namespace (the DSHA container case). If the phone is on another machine, use `--bind 0.0.0.0 --host <LAN-IP>` and make sure the port is reachable. `doctor` explains which case you're in.

## Commands

| | |
|---|---|
| `doctor` | Environment self-check: reachability, ffmpeg, setsid, exec bit (distinguishing `noexec` mount from a missing bit), component presence |
| `new <job> <w> <h> <fps> <sec>` | Scaffold `job.conf` + commented `render.js` + `frames/` |
| `start <job> [--format png\|jpeg\|webp] [--quality N] [--batch N] [--port N] [--bind A] [--host H] [--context 2d\|webgl2]` | Launch detached server |
| `open <job>` | Point the phone's default browser at the worker page |
| `watch <job>` | Poll to completion, with stall detection |
| `status <job>` | One-shot progress / fps / eta / inflight / stall |
| `resume <job>` | Re-sync after editing frame files |
| `stop <job>` | Abort + shut down; frames retained |
| `assemble <job> [audio]` | MP4 via ffmpeg (`-crf 18 -pix_fmt yuv420p -movflags +faststart`) |
| `demo` | Offline protocol check with a fake client — never touches a phone |

## Verify before trusting it

```bash
python3 farm_selftest.py   # 36 assertions: queue, leases, resume, abort,
                           # token rejection, legacy compat, page generation
bash farm.sh demo          # end-to-end protocol without a phone
```

Then run a small real job (60 frames) and eyeball a few returned frames before committing to a long render.

## Files

| | |
|---|---|
| `farm.sh` | CLI: scaffolding, detached launch, browser dispatch, watch, assemble |
| `farm_server.py` | HTTP server, work queue, leases, job token, page template. Standard library only |
| `farm_selftest.py` | 36-assertion protocol test; needs no phone |
| `SKILL.md` | Agent-facing skill instructions |

Runtime state lives in `/tmp/phone-render-farm/<job>/` (override with `FARM_RUNROOT`).

## License

MIT
