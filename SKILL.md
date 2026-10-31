---
name: phone-render-farm
description: "Render Canvas 2D animations into a frame sequence and MP4 by using an Android phone's browser as a remote render farm, for agent hosts where a local headless browser cannot run (Android kernel, seccomp-restricted or sandboxed containers where Chromium dies with SIGTRAP). The agent host serves a page; the phone browser loads it, draws each frame on a canvas, and POSTs PNGs back over loopback. Use when you must produce video from JavaScript Canvas code and the container has no working browser, or when the user asks to render 动画/视频 using 手机浏览器. Requires an Android device reachable from the agent host and a browser on it. NOT for DOM or React-based rendering such as Remotion, and NOT for websites or automation — there is no CDP screenshot channel on a phone browser, so only canvas.toDataURL pixels are obtainable."
license: MIT
metadata:
  version: "1.0"
  compatibility: "Python 3.8+ (stdlib only), bash, ffmpeg for MP4 assembly"
---

# Phone Render Farm

Use the phone's browser as the renderer. The agent host never launches a browser.

```
agent host                          Android phone
┌────────────────────┐   HTTP    ┌──────────────────────────┐
│ farm_server.py     │ ────────▶ │ browser loads /           │
│  job queue + lease │           │  render.js draws frame i  │
│  frames/f00000.png │ ◀──────── │  canvas.toDataURL → POST  │
└────────────────────┘  frames   └──────────────────────────┘
        └── ffmpeg: frames + audio → MP4
```

Throughput measured on a OnePlus (Android 17, arm64 container): **600 frames of 1920×1080 in 20.2s (29.8 fps)** — faster than in-container `skia-canvas` (45.9s), because the phone's browser composites on real GPU-backed Skia.

## Applicability — read before using

**Works only when all of these hold.** Run `doctor` to check them automatically.

| Requirement | Why | If unmet |
|---|---|---|
| An **Android device** with a Chromium-family browser, whose browser can reach a port the agent host listens on | The browser *is* the renderer | No phone → use `skia-canvas`/`resvg`/`node-canvas` in-process instead |
| Agent host can serve HTTP the phone can reach | Frame dispatch and return path | Different machine/network → `--bind 0.0.0.0 --host <LAN-IP>` |
| Your animation draws into **one `<canvas>`** (2D or WebGL2) | Only `canvas.toDataURL()` pixels are obtainable | DOM/React/Remotion → see *Not applicable* |
| Phone stays **awake and foregrounded** during render | Backgrounded tabs are throttled or frozen | Renders pause; leases recover frames and resume. Never loses progress |
| `ffmpeg` (only for `assemble`) | Mux frames → MP4 | Omit and you still get a PNG sequence |

**WebGL needs one flag.** With `--context webgl2`, the worker creates the context with `preserveDrawingBuffer: true`. Without it, `toDataURL()` returns only the background — measured: `[194,58,27]` with the flag vs `[244,239,227]` without, same scene, same frame. If you drive three.js yourself, pass `preserveDrawingBuffer: true` to `WebGLRenderer`; it defaults to `false`.

**three.js works on both Chrome and Edge.** The raw-GL path above is browser-independent.

**Browser: both Chrome and Edge have been verified working on-device.** If a render stalls with no `clienterr` in `/status`, the browser may be in a bad state (especially after many page loads) — have the user force-stop it and re-open. This clears it.

**Not applicable to DOM-based rendering.** A phone browser exposes no CDP screenshot channel (`getDisplayMedia` is absent; a `chrome.debugger`-based extension cannot be installed on Android browsers). Remotion's own renderer launches a headless Chrome, which is exactly what fails here. If you need DOM fidelity, render on a machine where a real browser works, or drive a remote browser over CDP.

## Hard constraints (violating these causes silent failure)

1. **Start the server as a session-detached process.** An agent's shell reaps child processes when a tool call ends. `farm.sh start` uses `setsid nohup …` for this reason; if you invoke `farm_server.py` directly, do the same, or the server dies between calls.
2. **Invoke as `bash farm.sh …`, not `./farm.sh …`, when the skill lives on `/sdcard`** or any `noexec` mount. `doctor` reports which applies.
3. **`render.js` must be deterministic.** Same frame index must always draw the same pixels. No `Math.random()`, no `Date.now()` — seed a PRNG from the frame index (`prnd()` is in the generated template). Non-deterministic output makes resumed frames inconsistent, and the seams are invisible until playback.
4. **Never rely on the previous frame's canvas state.** The farm resets `transform` and `globalAlpha` per frame; set `fillStyle`, `font`, `textAlign` yourself each time, and paint your own background.
5. **Keep one frame under ~2 seconds.** A frame slower than the 90s lease gets reclaimed and re-rendered repeatedly, and the job never finishes.
6. **Use installed font names.** `fc-list :lang=zh family` to check. A missing CJK font silently yields tofu boxes that look fine in a thumbnail.

## Workflow

```bash
S=<this skill's directory>          # absolute path of the installed skill

bash "$S/farm.sh" doctor            # 1. can this device work at all?
bash "$S/farm.sh" new  /tmp/job 1920 1080 30 20     # 2. skeleton; total = 20s×30 = 600
#    3. edit /tmp/job/render.js — define window.__render(ctx, i, t)
bash "$S/farm.sh" start /tmp/job    # 4. serve (detached)
bash "$S/farm.sh" open  /tmp/job    # 5. launch the phone's default browser
bash "$S/farm.sh" watch /tmp/job    # 6. poll until ✅
bash "$S/farm.sh" assemble /tmp/job mix.wav   # 7. MP4 (drop the arg for silent video)
```

`open` uses the DSHA device bridge (`127.0.0.1:3090/app/open`) when present. Without it, print the URL `start` emitted and open it manually on the phone — everything else is unaffected.

Re-running `open` after any interruption resumes: progress is derived from **which frame files exist on disk**, so completed frames are never re-rendered.

## render.js contract

```js
window.__render = function (ctx, i, t) {   // t === i / fps
  const W = ctx.canvas.width, H = ctx.canvas.height;
  ctx.fillStyle = '#f4efe3'; ctx.fillRect(0, 0, W, H);
  // draw frame i …
};
```

Reuse an existing Canvas animation by appending an adapter to it:

```js
window.__render = (function () {
  var render = null;
  return function (ctx, i, t) {
    if (!render) render = window.Anim(ctx.canvas);   // your factory
    render.render(t);
  };
})();
```

## Commands

| Command | Purpose |
|---|---|
| `doctor` | Environment self-check: reachability, ffmpeg, setsid, exec bit, components, and what is out of scope |
| `new <job> <w> <h> <fps> <seconds>` | Create `job.conf`, a commented `render.js` template, `frames/` |
| `start <job> [--format png\|jpeg\|webp] [--quality N] [--batch N] [--port N] [--bind A] [--host H]` | Launch detached server |
| `open <job>` | Point the phone's default browser at the worker page |
| `watch <job>` | Poll to completion; reports stall and how to recover |
| `status <job>` | One-shot progress / fps / eta / inflight / stall |
| `resume <job>` | Re-sync after adding or deleting frame files by hand |
| `stop <job>` | Abort loop + shut down; frames retained |
| `assemble <job> [audio]` | MP4 via ffmpeg (`-crf 18 -pix_fmt yuv420p -movflags +faststart`) |
| `demo` | Offline protocol check with a fake client; never touches the phone |

## How it survives interruption

The server is a work queue. Each browser page claims *N* unclaimed frames under a **90s lease**, renders them, and POSTs them back. Consequences:

- **Page killed, tab closed, phone locked, call arrives** → the page stops claiming. Leases expire, frames return to the queue. Re-run `open` and it resumes.
- **Two phones, or two tabs** → they claim disjoint frames and render in parallel.
- **You change `render.js` and restart the server while an old tab is still open** → the server embeds a job token (hash of `render.js` + dimensions + total + start time). The stale page detects the mismatch and reloads itself; a stale page's submissions are rejected with `409` rather than writing wrong pixels into the new job. This is why you do not need to touch the phone after editing.
- **You edit frame files on disk** → run `resume`; the token changes and pages reload.

Stall detection is server-side (`stall_sec` = time since last useful output), and `watch` exits with a recovery hint past `STALL_LIMIT` (default 150s) instead of hanging.

## Storage and formats

1080p PNG ≈ 270 KB/frame — 600 frames ≈ 160 MB. `--format jpeg --quality 92` cuts that ~5× at some cost in banding; use PNG for flat-color infographics, JPEG only if space-bound.

## Troubleshooting

| Symptom | Cause → fix |
|---|---|
| `open` returns OK but `status` shows `last_hb_age: null` forever | Page never loaded. Phone on another network → `--bind 0.0.0.0 --host <LAN-IP>`; or open the URL manually |
| Page shows `FATAL: render.js 未定义 window.__render` | Contract not implemented, or `render.js` has a syntax error (`node --check render.js`) |
| Chinese renders as boxes | Font name not installed → `fc-list :lang=zh family` |
| Progress frozen mid-run | Phone asleep/backgrounded → re-run `open`; frames resume after lease expiry |
| `open` returns OK, `status` shows `last_hb_age: null`, and **no request reaches the server at all** | The browser process itself is wedged — not a job problem. Force-stop the browser (Settings → Apps → the browser → Force stop), or swipe away **all** its recent-task cards. Verify recovery with a one-line page before re-running the job |
| `./farm.sh` → Permission denied | `noexec` mount → `bash farm.sh` |
| Port in use | `--port 8123`; or `stop` the previous job |
| Frames look mixed after an edit | You edited `render.js` without restarting the server; restart then `open` |

## Verify before trusting it

```bash
python3 "$S/farm_selftest.py"   # 36 assertions: queue, leases, resume, abort,
                                # token rejection, legacy compat, page generation
bash "$S/farm.sh" demo          # end-to-end protocol without a phone
```

Then confirm on-device with a small job (60 frames) before committing to a long render: check `doctor`, run the workflow, and compare a few returned frames against expectation.
