// SO-100 course-site widgets.
//
// Three markers in the markdown are expanded here:
//   <div data-viewer></div>   -> an embedded Rerun web viewer connected to the local
//                                so100-server's gRPC proxy (rerun+http://localhost:9876/proxy)
//   <div data-collect></div>  -> the collect card: Recording / Livestream tabs, an episode
//                                side panel, and the viewer, driving the server's control
//                                API (http://localhost:8000)
//   <div data-setup></div>    -> the Set-up card: an accordion with one section per
//                                step (ping / calibrate / teleop) next to ONE shared
//                                viewer showing the running tool's live feed (the server
//                                runs the same CLI tools as subprocesses via POST /setup/*)
//
// The site itself is static: everything below talks to the *local* long-lived server
// (`pixi run so100-server`). When it's down, widgets show a hint and keep retrying.

const CONTROL_URL = "http://localhost:8000";
const PROXY_URL = "rerun+http://localhost:9876/proxy";
const TAGS = ["Good episode", "Bad episode", "Needs review"];
const NEW_DATASET = "__new__";

// Mirror of the server's takes.sanitize_name: how a typed dataset name comes back
// from /datasets once it exists on disk / in the catalog.
const sanitizeName = (name) =>
  name
    .trim()
    .replace(/[^A-Za-z0-9_.-]+/g, "_")
    .replace(/^[._]+|[._]+$/g, "");

// ---- control API -----------------------------------------------------------------

async function api(path, body = undefined) {
  const response = await fetch(CONTROL_URL + path, {
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(120_000), // /stop optimizes + registers, which takes a while
  });
  return await response.json();
}

async function serverUp() {
  try {
    const response = await fetch(CONTROL_URL + "/status", { signal: AbortSignal.timeout(1500) });
    return response.ok;
  } catch {
    return false;
  }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// One shared /status poll for every widget on the page (they all re-render from it).
// A widget can also push the response of its own POST so the others update instantly.
const statusSubscribers = new Set();
let statusTimer = null;

function publishStatus(status) {
  for (const subscriber of statusSubscribers) subscriber(status);
}

function subscribeStatus(subscriber) {
  statusSubscribers.add(subscriber);
  if (statusTimer === null) {
    const poll = async () => {
      let status = null;
      try {
        status = await api("/status");
      } catch {
        /* server down; widgets render their offline state */
      }
      publishStatus(status);
    };
    poll();
    statusTimer = setInterval(poll, 2000);
  }
}

// text: a string, or a function returning the current string (for content that changes
// after the button is wired, e.g. a live error message).
function wireCopy(button, text) {
  const original = button.innerHTML; // restored after the feedback flash (text or icon)
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(typeof text === "function" ? text() : text);
      button.textContent = "Copied!";
    } catch {
      button.textContent = "Copy failed";
    }
    setTimeout(() => (button.innerHTML = original), 1500);
  });
}

// Copy glyph (design kit): stroke follows the surrounding text color, so it adapts to
// light/dark mode via the button's CSS color.
const COPY_SVG =
  '<svg width="13" height="13" viewBox="0 0 13 13" fill="none" aria-hidden="true">' +
  '<path d="M4.5 8.5V12.5H12.5V4.5H8.5M0.5 0.5H8.5V8.5H0.5V0.5Z" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"/></svg>';

// ---- embedded viewer ---------------------------------------------------------------

// The viewer (eframe/egui under the hood) grabs keyboard focus in ways that scroll the
// page. Worst offender: its "text agent" -- a hidden 1px <input> parked at (0,0) OF THE
// BODY to capture IME/keyboard input. eframe marks it autofocus=true and calls .focus()
// on it whenever the canvas is interacted with; focusing an element at the top of the
// page scrolls the window to the top (the "jump" on viewer boot). The canvas itself gets
// the same treatment on boot and when leaving fullscreen. Nothing in the site's own code
// calls DOM .focus() or uses autofocus, so patch globally, once, at load:
//   1. every programmatic .focus() gets preventScroll (Tab-key navigation is browser-
//      internal and does NOT go through this method, so keyboard a11y keeps scrolling);
//   2. autofocus is neutered outright -- the browser honors it on insertion via an
//      internal path that .focus() patching can't reach, and preventScroll can't either.
{
  const original = HTMLElement.prototype.focus;
  HTMLElement.prototype.focus = function (opts) {
    original.call(this, { ...opts, preventScroll: true });
  };
  Object.defineProperty(HTMLElement.prototype, "autofocus", {
    get() {
      return false;
    },
    set() {},
  });
}

// NOTE: width/height must stay "" -- the canvas is sized by CSS (.viewer-box canvas).
// Passing "100%" would set inline styles, which the viewer's fullscreen toggle wipes
// with removeAttribute("style") on exit, leaving an unsized canvas (and a WASM panic).
const VIEWER_OPTIONS = {
  width: "",
  height: "",
  hide_welcome_screen: true,
  allow_fullscreen: true,
};

// A show/hide-able viewer inside a .viewer-box (used by the Set up and Collect widgets):
// boots against a per-run/per-session proxy url, keeps a cover over the canvas until the
// wanted recording is active, and switches recordings when the server-side id changes --
// only on change, so it never fights the user's own clicks in the recording panel.
function viewerSlot(box) {
  const overlay = box.querySelector(".viewer-overlay");
  const loadingCover = box.querySelector(".viewer-loading");
  let viewer = null;
  let wanted = null; // recording id the server wants shown
  let synced = null; // last id we successfully switched to
  let followWanted = false; // after switching, jump to the end and press play (live takes)
  let playWanted = false; // after switching, play from the start (recorded episodes)
  let revealTimer = null;
  let retryTimer = null;
  let pendingUntil = 0; // while now < this and unsynced, a programmatic switch is in flight
  let bootGen = 0; // bumped by hide()/show(): cancels any in-flight show()
  let queuedAction = null; // focus()/open() requested while the viewer was still booting

  function trySync() {
    if (!viewer || !wanted) return false;
    if (synced !== wanted) {
      try {
        // Set unconditionally (not only on id change): when the viewer sits on a
        // catalog table screen the active recording id may already match, and this
        // call is what navigates back to the recording.
        viewer.set_active_recording_id(wanted);
        if (viewer.get_active_recording_id() === wanted) synced = wanted;
      } catch {
        /* viewer still booting or recording not arrived yet; retried by the timer */
      }
    }
    if (synced === wanted && (followWanted || playWanted)) {
      // followWanted ("following mode"): the user was reviewing an old take when a new
      // one started -- jump the cursor to the head of the live take and play, so it
      // keeps following. playWanted: a recorded episode was focused -- play it from the
      // start (this also drives the lazy chunk download, so it never sits empty/paused).
      try {
        const timeline = viewer.get_active_timeline(wanted);
        const range = timeline ? viewer.get_time_range(wanted, timeline) : null;
        if (range) {
          viewer.set_current_time(wanted, timeline, followWanted ? range.max : range.min);
          viewer.set_playing(wanted, true);
          followWanted = false;
          playWanted = false;
        }
      } catch {
        /* timeline not ready yet; retried by the timer */
      }
    }
    return synced === wanted && !followWanted && !playWanted;
  }

  // Retry until the wanted recording is active (it may still be downloading/streaming),
  // then STOP -- so the user can freely switch recordings in the viewer afterwards.
  function keepTrying() {
    if (trySync() || retryTimer !== null) return;
    const deadline = Date.now() + 15_000;
    pendingUntil = deadline;
    retryTimer = setInterval(() => {
      if (trySync() || Date.now() >= deadline) {
        clearInterval(retryTimer);
        retryTimer = null;
      }
    }, 250);
  }

  // True while a programmatic switch hasn't landed yet -- so viewer->panel sync can tell
  // "the user clicked another recording" apart from "our own switch is still in flight".
  function pending() {
    return wanted !== null && synced !== wanted && Date.now() < pendingUntil;
  }

  async function show(proxyUrl) {
    if (viewer) return;
    const gen = ++bootGen;
    overlay.hidden = true;
    // Cover the canvas until this run's recording is actually active (fallback
    // deadline, so a missing id can't hide the viewer forever).
    loadingCover.hidden = false;
    const deadline = Date.now() + 8000;
    clearInterval(revealTimer);
    revealTimer = setInterval(() => {
      if (trySync() || Date.now() >= deadline) {
        loadingCover.hidden = true;
        clearInterval(revealTimer);
        revealTimer = null;
      }
    }, 250);
    const { WebViewer } = await import("/viewer/index.js");
    if (gen !== bootGen) return; // hidden (or re-shown) while the module loaded
    viewer = new WebViewer();
    await viewer.start(proxyUrl, box, VIEWER_OPTIONS);
    if (gen !== bootGen) return;
    if (queuedAction) {
      const action = queuedAction;
      queuedAction = null;
      action();
    }
  }

  // soft: keep the overlay hidden -- used when the viewer is immediately re-shown
  // against a fresh proxy (a new arms session on a new port).
  function hide({ soft = false } = {}) {
    bootGen++;
    clearInterval(revealTimer);
    revealTimer = null;
    clearInterval(retryTimer);
    retryTimer = null;
    loadingCover.hidden = true;
    viewer?.stop();
    viewer = null;
    wanted = null;
    synced = null;
    followWanted = false;
    playWanted = false;
    queuedAction = null;
    overlay.hidden = soft;
  }

  function want(recordingId, { follow = false, play = false, force = false } = {}) {
    if (!recordingId) return;
    if (wanted !== recordingId || force) {
      wanted = recordingId;
      // Re-verify against the viewer even if this id synced before: the user may have
      // switched the viewer to another recording since (the cache would be stale).
      synced = null;
      // follow/play apply on CHANGE (or force) only, so callers can pass them on every
      // poll without re-jumping a stream the user deliberately scrubbed back in.
      followWanted = follow;
      playWanted = play && !follow;
    }
    keepTrying();
  }

  // Force following mode on the wanted recording (jump to the head + play), even if the
  // viewer is already on it -- for explicit "take me to the stream" moments.
  function refollow() {
    if (!wanted) return;
    synced = null; // the user may have moved the viewer elsewhere: force the switch back
    followWanted = true;
    playWanted = false;
    keepTrying();
  }

  // viewer.open with the boot/error guard, shared by focus() and open().
  function openUrl(url) {
    if (!viewer || !url) return;
    try {
      viewer.open(url);
    } catch (error) {
      console.warn("viewer.open failed:", error);
    }
  }

  // Focus a catalog episode: switch to it and play it from the start. The deep link is
  // (re-)opened whenever the recording isn't already loaded -- this also covers sources
  // the user closed inside the viewer (a local "already opened" cache would go stale).
  function focus(segmentId, url) {
    if (!viewer) {
      queuedAction = () => focus(segmentId, url); // delivered by show() once the viewer is up
      return;
    }
    want(segmentId, { play: true, force: true });
    if (url && synced !== segmentId) openUrl(url);
  }

  // The recording the *user* is looking at right now (for panel <- viewer sync).
  function active() {
    try {
      return viewer?.get_active_recording_id() ?? null;
    } catch {
      return null;
    }
  }

  // Open a viewer deep link (e.g. the dataset's catalog table) as a *browsing* action:
  // any in-flight recording switch is settled in place, so its retry loop can't yank
  // the user back to the stream. Wanting a NEW recording later still navigates away.
  function open(url) {
    if (!url) return;
    if (!viewer) {
      queuedAction = () => open(url); // delivered by show() once the viewer is up
      return;
    }
    clearInterval(retryTimer);
    retryTimer = null;
    if (wanted) synced = wanted;
    followWanted = false;
    playWanted = false;
    openUrl(url);
  }

  return { show, hide, want, refollow, focus, active, pending, open };
}

// One always-on viewer for plain <div data-viewer> embeds (Deploy page), connected to
// the server's main proxy at :9876.
let viewerPromise = null;

function viewerBox() {
  const box = document.createElement("div");
  box.className = "viewer-box";
  box.innerHTML = `<div class="viewer-overlay"><div class="spinner"></div>
    <div>Waiting for the local data server&hellip;</div>
    <div class="overlay-cmd"><code>pixi run so100-server</code>
      <button type="button" class="copy-btn" title="Copy command">Copy</button></div>
    <div>Run it in the repo &mdash; this page connects automatically.</div></div>`;
  wireCopy(box.querySelector(".copy-btn"), "pixi run so100-server");
  return box;
}

function bootViewer(box) {
  viewerPromise ??= (async () => {
    while (!(await serverUp())) await sleep(2000);
    const { WebViewer } = await import("/viewer/index.js");
    const viewer = new WebViewer();
    box.querySelector(".viewer-overlay")?.remove();
    await viewer.start(PROXY_URL, box, VIEWER_OPTIONS);
    return viewer;
  })();
  return viewerPromise;
}

function initViewer(root) {
  const box = viewerBox();
  root.replaceWith(box);
  bootViewer(box);
}

// ---- setup accordion widget ---------------------------------------------------------

// One card for the whole hardware checklist: a left accordion (one section per step)
// next to ONE shared viewer. Only one setup tool can run at a time server-side --
// starting a step while another runs stops the running one first (for calibrate that
// means the interrupted run writes no calibration file). Calibrate's done state is
// server-tracked (the badge); ping and teleop are judged by the human.
const SETUP_STEPS = {
  ping: {
    name: "Ping",
    desc:
      "Plug in both arms, then ping: a live feed will open. Wiggle a joint by hand and watch it move.",
    action: "PING!",
    stopLabel: "STOP THE FEED",
  },
  calibrate: {
    name: "Calibrate",
    desc:
      "Each arm is calibrated once, and it survives replugging. The leader arm goes first; " +
      "the follower starts automatically after it.",
    action: "START CALIBRATING",
    stopLabel: "STOP CALIBRATING",
  },
  teleop: {
    name: "Verify teleop",
    desc:
      "Torque turns on and the follower mirrors the leader (it glides to the leader's pose rather " +
      "than jumping). Drive it around and check every joint tracks \u2014 this is exactly the mode " +
      "you'll record in. Stopping the feed releases the follower's torque.",
    action: "START LIVE TELEOPERATING",
    stopLabel: "STOP THE FEED",
  },
};

// Which "press Enter" button to show, per calibrate (arm, phase). The wiggle phase
// auto-advances (the tool detects which arm moved), so it gets no button.
function calibrateNextLabel(setup) {
  if (setup.phase === "middle") return "NEXT STEP";
  if (setup.phase === "sweep") return setup.arm === "leader" ? "NEXT ARM" : "FINISH";
  return null;
}

// Phase-contextual guidance shown in the calibrate section while it runs -- the single
// highest-value teaching moment, swapped in place of the static description.
function calibrateGuidance(setup) {
  const arm = setup.arm === "follower" ? "follower" : "leader";
  if (setup.phase === "wiggle") {
    return `Start with both arms folded at rest, then wiggle the ${arm} arm so the right port is picked \u2014 it advances by itself.`;
  }
  if (setup.phase === "middle") {
    return `Match the gray target pose with the ${arm} arm (this defines 0\u00b0 for every joint), then press NEXT STEP.`;
  }
  if (setup.phase === "sweep") {
    return (
      `Sweep every ${arm} joint through its full range of motion \u2014 including fully ` +
      `opening/closing the gripper \u2014 then press ${arm === "leader" ? "NEXT ARM" : "FINISH"}.`
    );
  }
  return null;
}

// Shared inline icons (design kit): a down-pointing chevron (rotated via CSS where
// needed) and a pause glyph. Stroke follows the surrounding text color.
const chevronSvg = (cls) =>
  `<svg class="${cls}" width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">` +
  '<path d="M4 6L8 10L12 6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const PAUSE_SVG =
  '<svg class="pause-icon" width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">' +
  '<path d="M5.25 2V14" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>' +
  '<path d="M10.75 2V14" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';
// Companion play glyph (same stroke language) shown while the stream is paused.
const PLAY_SVG =
  '<svg class="play-icon" width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">' +
  '<path d="M5.5 3L12 8L5.5 13V3Z" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';

const CHEVRON_SVG = chevronSvg("setup-chevron");

function initSetup(root) {
  const wrap = document.createElement("div");
  wrap.className = "setup";
  const sectionsHtml = Object.entries(SETUP_STEPS)
    .map(
      ([tool, step]) => `
    <section class="setup-step" data-tool="${tool}">
      <div class="setup-step-bar">
        <button type="button" class="setup-step-head" aria-expanded="false">
          ${CHEVRON_SVG}
          <span class="setup-step-title">${step.name}</span>
          <span class="setup-live" hidden></span>
          <span class="setup-badge" hidden>&#10003; Calibrated</span>
        </button>
        <button type="button" class="collect-btn outline setup-head-stop" hidden>
          <span class="stop-square"></span>STOP</button>
      </div>
      <div class="setup-step-body" hidden>
        <p class="setup-step-desc">${step.desc}</p>
        <div class="setup-step-buttons">
          <button type="button" class="collect-btn primary setup-start" disabled>${step.action}</button>
          <button type="button" class="collect-btn primary setup-next" hidden></button>
          <button type="button" class="collect-btn outline setup-stop" hidden>
            <span class="stop-square"></span>${step.stopLabel}</button>
        </div>
        <div class="setup-error-row" hidden>
          <p class="setup-error"></p>
          <button type="button" class="copy-btn error-copy" title="Copy error" aria-label="Copy error">${COPY_SVG}</button>
        </div>
      </div>
    </section>`,
    )
    .join("");
  wrap.innerHTML = `
    <div class="setup-body">
      <div class="setup-panel">${sectionsHtml}</div>
      <div class="viewer-box">
        <div class="viewer-overlay">
          <div class="overlay-title">No tool running</div>
          <div class="overlay-sub">start a step on the left &mdash; its live feed appears here</div>
          <div class="setup-offline"><span class="spinner"></span> waiting for the local data server&hellip;
            <code>pixi run so100-server</code></div>
          <div class="setup-busy" hidden>the arms are connected on the Collect page &mdash; disconnect them there first</div>
        </div>
        <div class="viewer-loading" hidden><span class="spinner"></span> starting the feed&hellip;</div>
      </div>
    </div>`;
  root.replaceWith(wrap);

  const offlineLine = wrap.querySelector(".setup-offline");
  const busyLine = wrap.querySelector(".setup-busy");

  const sections = {};
  for (const sectionEl of wrap.querySelectorAll(".setup-step")) {
    const tool = sectionEl.dataset.tool;
    const q = (selector) => sectionEl.querySelector(selector);
    sections[tool] = {
      el: sectionEl,
      head: q(".setup-step-head"),
      body: q(".setup-step-body"),
      live: q(".setup-live"),
      badge: q(".setup-badge"),
      headStop: q(".setup-head-stop"),
      start: q(".setup-start"),
      next: q(".setup-next"),
      stop: q(".setup-stop"),
      desc: q(".setup-step-desc"),
      errorRow: q(".setup-error-row"),
      error: q(".setup-error"),
      errorCopy: q(".error-copy"),
    };
  }

  // The shared viewer: booted against the running tool's throwaway proxy, stopped when
  // the tool ends.
  const slot = viewerSlot(wrap.querySelector(".viewer-box"));
  let shown = false;
  let wasRunning = null; // tool name that was running on the previous render
  let openTool = null;
  let autoOpened = false;
  let lastStatus = null;
  const localErrors = { ping: "", calibrate: "", teleop: "" };

  function setOpen(tool) {
    openTool = tool;
    for (const [t, s] of Object.entries(sections)) {
      const open = t === tool;
      s.el.classList.toggle("open", open);
      s.head.setAttribute("aria-expanded", String(open));
      s.body.hidden = !open;
    }
  }

  function render(status) {
    lastStatus = status;
    const online = status !== null;
    const setup = (online && status.setup) || {};
    const running = setup.running ? setup.tool : null;
    const armsBusy = online && status.arms !== "disconnected";
    const calibrated = Boolean(setup.calibrated?.leader && setup.calibrated?.follower);

    if (online && !autoOpened) {
      // First contact: open the running tool, else the first incomplete step.
      autoOpened = true;
      setOpen(running || (calibrated ? "teleop" : "ping"));
    }
    if (running && running !== wasRunning) {
      setOpen(running); // a tool just started (here, another tab, or the CLI): surface it
      localErrors[running] = "";
    }

    for (const [tool, s] of Object.entries(sections)) {
      const step = SETUP_STEPS[tool];
      const mineRunning = running === tool;
      const open = openTool === tool;
      s.el.classList.toggle("running", mineRunning);
      s.live.hidden = !mineRunning;
      // STOP must never hide behind an expand click: collapsed running sections keep
      // one in the header row.
      s.headStop.hidden = !(mineRunning && !open);
      s.badge.hidden = !(tool === "calibrate" && !mineRunning && calibrated);

      // Starting is allowed even while another tool runs -- the handler stops it first.
      s.start.hidden = mineRunning;
      s.start.disabled = !online || armsBusy;
      const nextLabel = mineRunning && tool === "calibrate" ? calibrateNextLabel(setup) : null;
      s.next.hidden = nextLabel === null;
      if (nextLabel !== null) s.next.textContent = nextLabel;
      s.stop.hidden = !mineRunning;

      const guidance = mineRunning && tool === "calibrate" ? calibrateGuidance(setup) : null;
      s.desc.textContent = guidance ?? step.desc;

      if (mineRunning) localErrors[tool] = "";
      const error = (setup.tool === tool && !mineRunning && setup.error) || localErrors[tool];
      s.error.textContent = error;
      s.errorRow.hidden = !error;
    }

    // Global conditions live ONCE, in the shared overlay -- not per section.
    offlineLine.hidden = online;
    busyLine.hidden = !armsBusy;

    if (!running && shown) {
      slot.hide();
      shown = false;
    }
    if (running && running !== wasRunning && shown) {
      slot.hide({ soft: true }); // direct tool-to-tool handoff: fresh proxy, no overlay flash
      shown = false;
    }
    if (running && !shown && setup.proxy_port) {
      slot.show(`rerun+http://localhost:${setup.proxy_port}/proxy`);
      shown = true;
    }
    // Calibrate spawns a NEW recording for the follower stage (and every re-run is a
    // new recording) -- the viewer keeps showing the old one unless told.
    if (running) slot.want(setup.recording_id);
    wasRunning = running;
  }

  async function post(path, body, tool) {
    try {
      const next = await api(path, body);
      if (next.error) localErrors[tool] = next.error;
      publishStatus(next);
    } catch (error) {
      localErrors[tool] = `request failed: ${error}`;
      publishStatus(null);
    }
  }

  for (const [tool, s] of Object.entries(sections)) {
    wireCopy(s.errorCopy, () => s.error.textContent);
    s.head.addEventListener("click", () => {
      setOpen(openTool === tool ? null : tool);
      render(lastStatus);
    });
    s.start.addEventListener("click", async () => {
      s.start.disabled = true;
      localErrors[tool] = "";
      // Only one tool runs server-side: stop the current one first (an interrupted
      // calibration writes no calibration file), then start this one.
      if (wasRunning && wasRunning !== tool) await post("/setup/stop", {}, wasRunning);
      post("/setup/start", { tool }, tool);
    });
    s.next.addEventListener("click", () => {
      s.next.hidden = true; // hide until the tool reports the next phase
      post("/setup/next", {}, tool);
    });
    const stop = () => post("/setup/stop", {}, tool);
    s.stop.addEventListener("click", stop);
    s.headStop.addEventListener("click", stop);
  }

  subscribeStatus(render);
}

// ---- collect widget ---------------------------------------------------------------

// Two tabs, two modes:
//   Livestream -> just operate the robot live and watch what it sees. ONE continuous
//                 stream that stays on for the whole arms session (the proxy's memory
//                 limit flushes its oldest data, so nothing piles up). Recording never
//                 interrupts it: takes are written to disk invisibly and the viewer
//                 keeps showing this stream. Pausing/stopping the feed lives HERE (Start
//                 recording connects the arms itself when no session is up yet).
//   Recording  -> operate the dataset recorder. A side panel shows ONE episode at a time:
//                 a fixed, server-assigned id (episode_01 .. episode_NN, never editable,
//                 never reused) plus its properties (task, tag). The ‹ › arrows browse
//                 the recorded episodes AND focus them in the viewer; switching
//                 recordings inside the viewer moves the panel too.
//
// Episode states, mirrored by the glyph next to the id:
//   (none)  -> the upcoming episode; properties are only a front-end draft until /start
//   red dot -> recording right now; "Save properties" stamps edits into the live take
//   green ✓ -> saved to the catalog; edits go through the "edits" layer on Save
function initCollect(root) {
  const wrap = document.createElement("div");
  wrap.className = "collect";
  wrap.innerHTML = `
    <div class="collect-body">
      <div class="collect-panel">
        <div class="collect-tabs" role="tablist">
          <button type="button" role="tab" aria-selected="true" class="collect-tab active" data-tab="live">Livestream</button>
          <button type="button" role="tab" aria-selected="false" class="collect-tab" data-tab="record">Recording</button>
        </div>
        <div class="panel-record">
          <label class="panel-field">Dataset
            <select name="dataset"></select></label>
          <label class="panel-field new-dataset" hidden>New dataset name
            <input type="text" name="new_dataset" placeholder="my_task"></label>
          <div class="episode-card">
            <div class="episode-nav">
              <button type="button" name="prev" aria-label="Previous episode" disabled>${chevronSvg("chev-left")}</button>
              <span class="episode-id"><span class="episode-name">episode_01</span><span class="episode-glyph"></span></span>
              <button type="button" name="fwd" aria-label="Next episode" disabled>${chevronSvg("chev-right")}</button>
            </div>
            <label class="panel-field">Task
              <textarea name="task" rows="3" placeholder="Pick up the ball and place it in the box"></textarea></label>
            <label class="panel-field">Tag
              <select name="tag">${TAGS.map((tag) => `<option>${tag}</option>`).join("")}</select></label>
            <button type="button" class="save-btn" name="save" hidden>Save properties</button>
            <div class="episode-note" hidden></div>
          </div>
          <div class="catalog-nav">
            <button type="button" class="collect-btn outline" name="catalog" disabled>Browse the catalog</button>
            <button type="button" class="collect-btn outline" name="episodes" disabled>Back to the episodes</button>
          </div>
          <div class="panel-buttons">
            <button type="button" class="collect-btn primary record-btn" name="record" disabled>
              <span class="rec-dot"></span><span class="btn-label">Start recording</span></button>
            <button type="button" class="collect-btn outline stop-btn" name="stoprec" disabled>
              <span class="stop-square"></span><span class="btn-label">Stop current recording</span></button>
          </div>
        </div>
        <div class="panel-live" hidden>
          <p class="live-sub">Operate the robot and watch what it sees in real time, without recording:
            the feed lives only in the viewer's memory (the <b>local</b> source) and nothing is stored.
            Pausing keeps the stream (and teleop) alive &mdash; resuming continues the same feed.</p>
          <button type="button" class="collect-btn outline" name="pausefeed" disabled>
            ${PAUSE_SVG}${PLAY_SVG}<span class="btn-label">Pause the stream</span></button>
          <button type="button" class="collect-btn outline stop-btn" name="stopfeed" disabled>
            <span class="stop-square"></span>Stop the feed</button>
        </div>
        <div class="status-error" hidden></div>
      </div>
      <div class="viewer-box">
        <div class="viewer-overlay">
          <button type="button" class="setup-action" name="livestream" disabled hidden>START LIVESTREAM</button>
          <label class="fake-check" hidden><input type="checkbox" name="fake"> no arms plugged in (cameras only)</label>
          <div class="record-hint" hidden>no live feed</div>
          <div class="setup-offline"><span class="spinner"></span> waiting for the local data server&hellip;
            <code>pixi run so100-server</code></div>
        </div>
        <div class="viewer-loading" hidden><span class="spinner"></span> starting the live view&hellip;</div>
      </div>
    </div>`;
  root.replaceWith(wrap);

  const el = (name) => wrap.querySelector(`[name="${name}"]`);
  const q = (selector) => wrap.querySelector(selector);
  const errorLine = q(".status-error");
  const note = q(".episode-note");
  const card = q(".episode-card");
  const epName = q(".episode-name");
  const glyph = q(".episode-glyph");
  const offlineLine = q(".setup-offline");

  // The viewer only exists while arms are connected: each session gets its own
  // throwaway proxy (state.proxy_port), so nothing from earlier sessions or other
  // pages is buffered -- no old episodes start playing on connect.
  const slot = viewerSlot(q(".viewer-box"));
  let shownPort = null; // proxy port the viewer is currently connected to

  let tab = "live";
  let online = false;
  let state = { arms: "disconnected", running: false, last: {} };
  let busy = false;

  let episodes = []; // saved episodes from GET /episodes, oldest -> newest
  let datasetUrl = null; // catalog deep link for the whole dataset (its episode table)
  let nextId = "episode_01"; // the id the NEXT recording will get (server-assigned)
  let selected = null; // stem of the episode the panel shows; null -> the active slot
  const drafts = new Map(); // stem -> {task, tag} unsaved edits (front-end only)
  let recordingBaseline = null; // last values saved for the in-progress take
  let savedFlash = null; // stem whose Save button shows the "saved" checkmark
  let noteText = null; // {stem, text} green confirmation inside the card
  let lastViewerId = null; // last viewer-side active recording (panel <- viewer sync)
  let browsing = false; // the user opened the catalog table; cleared by any real navigation

  // The browsable list: every saved episode, then ONE active slot -- the take being
  // recorded right now, or the upcoming (not yet recorded) episode.
  function items() {
    const list = episodes.map((entry) => ({ ...entry, kind: "saved" }));
    if (state.running && state.last?.episode) {
      const stem = String(state.last.stem ?? state.last.episode);
      list.push({ stem, episode: String(state.last.episode), kind: "recording" });
    } else {
      list.push({ stem: nextId, episode: nextId, kind: "next" });
    }
    return list;
  }

  function current() {
    const list = items();
    return list.find((entry) => entry.stem === selected) ?? list[list.length - 1];
  }

  // The upcoming episode's draft is keyed by a STABLE key, not its (server-assigned,
  // still-changing) id -- so text typed before /episodes even resolves is never lost.
  const NEXT_KEY = "__next__";
  const draftKey = (item) => (item.kind === "next" ? NEXT_KEY : item.stem);

  // What the fields should show: unsaved draft > stored values > defaults (the upcoming
  // episode inherits the previous take's task -- same dataset, same task, usually).
  function draftFor(item) {
    const key = draftKey(item);
    if (drafts.has(key)) return drafts.get(key);
    if (item.kind === "saved") return { task: item.task ?? "", tag: item.tag || TAGS[0] };
    if (item.kind === "recording") return recordingBaseline ?? { task: "", tag: TAGS[0] };
    return { task: episodes.at(-1)?.task ?? "", tag: TAGS[0] };
  }

  function baselineFor(item) {
    if (item.kind === "saved") return { task: item.task ?? "", tag: item.tag || TAGS[0] };
    if (item.kind === "recording") return recordingBaseline;
    return null; // the upcoming episode has nothing to save yet
  }

  // With "New dataset…" selected, an empty name is a valid state: recording just
  // creates/uses the default "my_task" dataset (mirrors the input's placeholder).
  function currentDataset() {
    return el("dataset").value === NEW_DATASET ? el("new_dataset").value.trim() || "my_task" : el("dataset").value;
  }

  // Once a "New dataset…" name exists in the catalog (its first take registered it),
  // move the dropdown onto it, replacing the free-text field with the normal
  // selected-dataset state. No-op until the name shows up in /datasets.
  function adoptNewDataset() {
    if (el("dataset").value !== NEW_DATASET) return;
    const name = sanitizeName(el("new_dataset").value.trim() || "my_task");
    if ([...el("dataset").options].some((option) => option.value === name)) {
      el("dataset").value = name;
      el("new_dataset").value = "";
      q(".new-dataset").hidden = true;
    }
  }

  function showError(message) {
    errorLine.textContent = message ?? "";
    errorLine.hidden = !message;
  }

  // Never overwrite the field the user is typing in (unless the episode switched).
  function syncFields(force) {
    const draft = draftFor(current());
    const task = el("task");
    const tag = el("tag");
    if (force || document.activeElement !== task) task.value = draft.task;
    if (force || document.activeElement !== tag) {
      if (![...tag.options].some((option) => option.value === draft.tag)) tag.append(new Option(draft.tag));
      tag.value = draft.tag;
    }
  }

  function select(stem, { openViewer = true } = {}) {
    if (stem !== current().stem) {
      // A fresh episode card starts clean: no leftover "saved" flash or note.
      savedFlash = null;
      if (noteText?.stem !== stem) noteText = null;
    }
    selected = stem;
    card.classList.remove("slide-in");
    void card.offsetWidth; // restart the animation
    card.classList.add("slide-in");
    const item = current();
    if (openViewer && item.kind === "saved" && item.segment_id) {
      browsing = false; // leaving the catalog (if it was up) for a concrete episode
      slot.focus(item.segment_id, item.viewer_url); // focus this episode in the viewer too
      lastViewerId = item.segment_id; // our own switch: don't mistake it for a user click
    }
    render();
    syncFields(true);
  }

  // If the user switches recordings inside the viewer: switching to the live stream
  // always resumes following mode; switching to an episode plays it and moves the
  // panel onto it.
  function syncFromViewer() {
    if (slot.pending()) return; // our own switch is still in flight -- not a user click
    const active = slot.active();
    if (!active || active === lastViewerId) return;
    lastViewerId = active;
    if (active === state.recording_id) {
      // While the catalog table is up the stream STAYS the active recording -- seeing
      // it here is not a user click, and refollowing would yank the catalog away.
      if (browsing) return;
      slot.want(state.recording_id); // re-point the slot in case it still wanted an episode
      slot.refollow();
      return;
    }
    if (tab !== "record" || state.running) return;
    const match = episodes.find((entry) => active === entry.segment_id || active.endsWith(`-${entry.stem}`));
    if (!match) return;
    browsing = false; // the user opened an episode from the table: real navigation
    slot.want(active, { play: true, force: true }); // a recorded episode always starts playing
    if (match.stem !== current().stem) select(match.stem, { openViewer: false });
  }

  function render() {
    const connected = online && state.arms !== "disconnected";
    const running = connected && Boolean(state.running);
    const item = current();
    const list = items();
    const index = list.findIndex((entry) => entry.stem === item.stem);

    // Tabs.
    for (const button of wrap.querySelectorAll(".collect-tab")) {
      button.classList.toggle("active", button.dataset.tab === tab);
      button.setAttribute("aria-selected", String(button.dataset.tab === tab));
    }
    q(".panel-record").hidden = tab !== "record";
    q(".panel-live").hidden = tab !== "live";

    // Viewer overlay (visible while no live session): per-tab content.
    el("livestream").hidden = tab !== "live";
    el("livestream").disabled = busy || !online;
    q(".fake-check").hidden = tab !== "live";
    q(".record-hint").hidden = tab !== "record" || !online;
    offlineLine.hidden = online;

    // Livestream tab. Pause drops frames server-side but keeps the same recording id,
    // so Resume continues the SAME stream; both are locked while a take is recording.
    el("pausefeed").disabled = busy || !connected || running;
    el("pausefeed").querySelector(".btn-label").textContent = state.live_paused ? "Resume the stream" : "Pause the stream";
    el("pausefeed").classList.toggle("paused", Boolean(state.live_paused));
    el("stopfeed").disabled = busy || !connected || running;

    // Episode navigator.
    el("prev").disabled = index <= 0;
    el("fwd").disabled = index >= list.length - 1;
    epName.textContent = item.episode;
    glyph.className = `episode-glyph${item.kind === "saved" ? " saved" : ""}${item.kind === "recording" ? " rec" : ""}`;
    glyph.textContent = item.kind === "saved" ? "\u2713" : "";
    // State glyph gets a hover popup (styled via [data-tip]) + a text alternative.
    const glyphState = item.kind === "recording" ? "recording" : item.kind === "saved" ? "recorded episode" : "";
    if (glyphState) {
      glyph.setAttribute("data-tip", glyphState);
      glyph.setAttribute("aria-label", glyphState);
    } else {
      glyph.removeAttribute("data-tip");
      glyph.removeAttribute("aria-label");
    }

    // Properties + explicit save (dirty-tracked against what the server has).
    const baseline = baselineFor(item);
    const draft = draftFor(item);
    const dirty = baseline !== null && (draft.task.trim() !== baseline.task.trim() || draft.tag !== baseline.tag);
    const save = el("save");
    const justSaved = !dirty && savedFlash === item.stem;
    // Only appears when there is something to save (or right after saving). The upcoming
    // episode never shows it: its draft rides along with /start.
    save.hidden = item.kind === "next" || (!dirty && !justSaved);
    save.disabled = busy || !online || !dirty;
    save.classList.toggle("saved", justSaved);
    save.textContent = justSaved ? "\u2713 Properties saved" : "Save properties";

    note.textContent = noteText?.stem === item.stem ? `\u2713 ${noteText.text}` : "";
    note.hidden = noteText?.stem !== item.stem;

    // The take's identity is fixed while recording.
    el("dataset").disabled = busy || running;
    el("new_dataset").disabled = busy || running;

    // Catalog navigation: both need a booted viewer; browsing needs registered episodes.
    el("catalog").disabled = shownPort === null || !datasetUrl;
    el("episodes").disabled = shownPort === null;

    // Record / stop.
    const record = el("record");
    record.classList.toggle("recording", running);
    record.querySelector(".btn-label").textContent = running ? "Recording..." : episodes.length > 0 ? "Start new recording" : "Start recording";
    // Recording implies streaming: enabled without a live session (the click starts one).
    record.disabled = busy || !online || running;
    el("stoprec").disabled = busy || !running;

    // The proxy lives as long as the arms session: the port only changes on
    // connect/disconnect, and the viewer reconnects from a clean slate when it does.
    const livePort = connected && state.proxy_port ? state.proxy_port : null;
    if (livePort !== shownPort) {
      if (shownPort !== null) slot.hide({ soft: livePort !== null });
      if (livePort !== null) slot.show(`rerun+http://localhost:${livePort}/proxy`);
      shownPort = livePort;
    }
    // The live tab and the active (recording/upcoming) slot follow the session's stream;
    // any switch TO the stream lands in following mode (head + play). Saved episodes
    // are opened via their catalog deep link in select() instead.
    if (livePort !== null && (tab === "live" || item.kind !== "saved")) slot.want(state.recording_id, { follow: true });
  }

  async function refreshDatasets() {
    let names = [];
    try {
      names = (await api("/datasets")).datasets ?? [];
    } catch {
      /* server briefly down; the poll will retry */
    }
    const select = el("dataset");
    const previous = select.value;
    select.innerHTML = "";
    for (const name of names) select.append(new Option(name));
    select.append(new Option("New dataset\u2026", NEW_DATASET));
    if ([...select.options].some((option) => option.value === previous)) select.value = previous;
    if (names.length === 0) select.value = NEW_DATASET;
    q(".new-dataset").hidden = select.value !== NEW_DATASET;
  }

  let episodesGen = 0; // drops out-of-order responses when the dataset switches mid-fetch
  async function refreshEpisodes() {
    const gen = ++episodesGen;
    if (el("dataset").value === NEW_DATASET && !el("new_dataset").value.trim()) {
      // A dataset that doesn't exist yet: no episodes, and the first take is episode_01
      // (never the previous dataset's numbering).
      episodes = [];
      datasetUrl = null;
      nextId = "episode_01";
    } else {
      try {
        const data = await api(`/episodes?dataset=${encodeURIComponent(currentDataset())}`);
        if (gen !== episodesGen) return; // a newer dataset selection took over
        if (Array.isArray(data.episodes)) {
          episodes = data.episodes;
          datasetUrl = data.dataset_url ?? null;
          nextId = data.next || "episode_01";
        }
      } catch {
        /* server briefly down; the poll will retry */
      }
    }
    render();
    syncFields(false);
  }

  async function call(path, body) {
    busy = true;
    render();
    showError();
    try {
      const next = await api(path, body);
      if (next.error) showError(next.error);
      if (next.arms !== undefined) state = next;
      return next;
    } catch (error) {
      showError(`request failed: ${error}`);
      return null;
    } finally {
      busy = false;
      render();
    }
  }

  // --- events ---------------------------------------------------------------

  for (const button of wrap.querySelectorAll(".collect-tab")) {
    button.addEventListener("click", () => {
      tab = button.dataset.tab;
      // Going to the stream always (re-)enters following mode, even if the viewer was
      // already on it (the user may have scrubbed back in time).
      if (tab === "live" && state.recording_id) {
        browsing = false;
        slot.want(state.recording_id);
        slot.refollow();
      }
      // Entering the Recording tab: adopt whatever the viewer is showing -- if it's a
      // catalog episode, the panel lands on it; only a not-yet-recorded live stream
      // keeps the panel on the upcoming slot.
      if (tab === "record" && !state.running) {
        const active = slot.active();
        const match = active && episodes.find((entry) => active === entry.segment_id || active.endsWith(`-${entry.stem}`));
        lastViewerId = active;
        if (match) {
          select(match.stem, { openViewer: false });
          return; // select() already rendered
        }
        selected = null;
      }
      render();
      syncFields(true);
    });
  }
  el("dataset").addEventListener("change", () => {
    q(".new-dataset").hidden = el("dataset").value !== NEW_DATASET;
    drafts.clear();
    selected = null;
    savedFlash = null;
    noteText = null;
    episodes = []; // never show the previous dataset's episodes while the fetch runs
    datasetUrl = null;
    nextId = "episode_01";
    refreshEpisodes();
  });
  // Keep the episode number live while the name is typed ("change" only fires on blur).
  let newNameTimer = null;
  el("new_dataset").addEventListener("input", () => {
    clearTimeout(newNameTimer);
    newNameTimer = setTimeout(refreshEpisodes, 300);
  });
  el("new_dataset").addEventListener("change", refreshEpisodes);

  function onEdit() {
    const item = current();
    drafts.set(draftKey(item), { task: el("task").value, tag: el("tag").value });
    if (savedFlash === item.stem) savedFlash = null;
    if (noteText?.stem === item.stem) noteText = null;
    render();
  }
  el("task").addEventListener("input", onEdit);
  el("tag").addEventListener("change", onEdit);

  el("prev").addEventListener("click", () => {
    const list = items();
    const index = list.findIndex((entry) => entry.stem === current().stem);
    if (index > 0) select(list[index - 1].stem);
  });
  el("fwd").addEventListener("click", () => {
    const list = items();
    const index = list.findIndex((entry) => entry.stem === current().stem);
    if (index < list.length - 1) select(list[index + 1].stem);
  });

  // Open the dataset's catalog table in the viewer: every collected episode at a glance
  // (its "Open" links work too -- the panel follows via the viewer sync).
  el("catalog").addEventListener("click", () => {
    browsing = true;
    lastViewerId = slot.active(); // whatever stays active underneath is not a user click
    slot.open(datasetUrl);
  });
  // ...and an explicit way back: re-focus whatever the episode panel is on.
  el("episodes").addEventListener("click", () => {
    browsing = false;
    const item = current();
    if (item.kind === "saved" && item.segment_id) {
      lastViewerId = item.segment_id;
      slot.focus(item.segment_id, item.viewer_url);
    } else if (state.recording_id) {
      slot.want(state.recording_id);
      slot.refollow();
    }
  });

  el("save").addEventListener("click", async () => {
    const item = current();
    if (item.kind === "next") return;
    const draft = draftFor(item);
    const payload = { dataset: currentDataset(), episode: item.stem, task: draft.task.trim(), tag: draft.tag };
    const next = await call("/episode/update", payload);
    if (!next || next.error) return;
    if (item.kind === "recording") {
      recordingBaseline = { task: payload.task, tag: payload.tag };
    } else {
      const entry = episodes.find((episode) => episode.stem === item.stem);
      if (entry) Object.assign(entry, { task: payload.task, tag: payload.tag });
    }
    drafts.delete(draftKey(item));
    savedFlash = item.stem; // the button itself becomes the confirmation
    render();
    syncFields(true);
  });

  el("record").addEventListener("click", async () => {
    // Always records the UPCOMING episode (its id comes back from the server). The
    // properties drafted before recording become the take's: task via /start, tag via
    // the baseline (stamped on /stop).
    const upcoming = items().at(-1);
    const draft = draftFor(upcoming);
    // No live session yet? Recording implies streaming -- connect the arms first.
    if (state.arms === "disconnected") {
      const session = await call("/arms/connect", { fake: el("fake").checked });
      if (!session || session.error) return;
    }
    const next = await call("/start", { dataset: currentDataset(), task: draft.task.trim() });
    if (!next || next.error || !next.running) return;
    const stem = String(next.last?.stem ?? next.last?.episode ?? upcoming.stem);
    recordingBaseline = { task: draft.task.trim(), tag: draft.tag };
    drafts.delete(NEXT_KEY);
    savedFlash = null;
    noteText = null;
    await refreshDatasets(); // an existing dataset typed under "New dataset…" is selectable now
    adoptNewDataset();
    select(stem); // slide the card to the fresh take (red dot)
    // Back to the stream, in following mode -- even if the viewer was already on it
    // (a fresh take should always be seen from its head).
    if (next.recording_id) {
      browsing = false;
      slot.want(next.recording_id);
      slot.refollow();
    }
  });

  el("stoprec").addEventListener("click", async () => {
    const take = items().at(-1);
    const draft = draftFor(take);
    el("stoprec").querySelector(".btn-label").textContent = "Stopping\u2026";
    const next = await call("/stop", { tag: draft.tag });
    el("stoprec").querySelector(".btn-label").textContent = "Stop current recording";
    if (!next || next.error) return;
    if (next.last?.status === "register_failed") showError(String(next.last.error ?? "registration failed"));
    const stem = String(next.last?.stem ?? take.stem);
    recordingBaseline = null;
    // A first-ever take registers its dataset in the catalog: only NOW does a brand-new
    // name (or the default "my_task") appear in /datasets, so adopt it here.
    await refreshDatasets();
    adoptNewDataset();
    await refreshEpisodes(); // the fresh take shows up as a saved episode
    noteText = { stem, text: "recording has been saved to the catalog" };
    savedFlash = null;
    // The take was written to disk invisibly (the viewer stayed on the live stream),
    // so review the registered episode straight from the catalog.
    select(stem);
  });

  el("livestream").addEventListener("click", () => call("/arms/connect", { fake: el("fake").checked }));
  el("pausefeed").addEventListener("click", async () => {
    const paused = Boolean(state.live_paused);
    const next = await call(paused ? "/live/resume" : "/live/pause", {});
    // Resuming continues the same recording after a gap -- jump to the new head.
    if (paused && next && !next.error && next.recording_id) {
      slot.want(next.recording_id);
      slot.refollow();
    }
  });
  el("stopfeed").addEventListener("click", () => call("/arms/disconnect", {}));

  async function onStatus(status) {
    const wasOnline = online;
    online = status !== null;
    if (status !== null) state = status;
    if (online && !wasOnline) {
      await refreshDatasets();
      await refreshEpisodes();
    }
    syncFromViewer();
    render();
    syncFields(false);
  }

  subscribeStatus(onStatus);
}

// ---- boot --------------------------------------------------------------------------

// Every fenced code block gets a hover Copy button (same wiring as the overlay's
// command). The <pre> is wrapped so the button doesn't scroll with wide code.
for (const pre of document.querySelectorAll("pre")) {
  const code = pre.querySelector("code");
  if (!code) continue;
  const wrapper = document.createElement("div");
  wrapper.className = "pre-wrap";
  pre.replaceWith(wrapper);
  wrapper.append(pre);
  const button = document.createElement("button");
  button.type = "button";
  button.className = "copy-btn pre-copy";
  button.title = "Copy code";
  button.setAttribute("aria-label", "Copy code");
  button.innerHTML = COPY_SVG;
  wireCopy(button, code.textContent.replace(/\n$/, ""));
  wrapper.append(button);
}

document.querySelectorAll("[data-viewer]").forEach(initViewer);
document.querySelectorAll("[data-collect]").forEach(initCollect);
document.querySelectorAll("[data-setup]").forEach(initSetup);
