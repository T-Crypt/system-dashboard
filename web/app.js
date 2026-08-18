// SysDash frontend logic. Talks to the Python backend only through the
// pywebview API bridge (window.pywebview.api.*) - no network calls happen
// from here directly.

const CIRC = 2 * Math.PI * 42; // matches r=42 in the SVG gauges
const HISTORY_LEN = 60; // ~1 minute of history at a 1s poll interval
const cpuHistory = [];
const gpuHistory = [];

// Threshold tables per metric type: [warnAt, dangerAt] as raw values
// (percent for usage gauges, degrees C for temp gauges).
const THRESHOLDS = {
  usage: [80, 95],
  temp: [80, 90],
};

function applyThresholdClass(el, value, kind) {
  const [warn, danger] = THRESHOLDS[kind];
  el.classList.remove("warn", "danger");
  if (value === null || value === undefined) return;
  if (value >= danger) el.classList.add("danger");
  else if (value >= warn) el.classList.add("warn");
}

function setGauge(id, fraction, valueText, kind, rawValue) {
  const el = document.getElementById(id);
  const circle = el.querySelector("circle.value");
  const clamped = Math.max(0, Math.min(1, fraction));
  circle.style.strokeDashoffset = String(CIRC * (1 - clamped));

  // Needle-style end cap: same angle convention as the arc itself (angle 0
  // at the unrotated 3 o'clock point, sweeping clockwise), so it always
  // sits exactly on the tip of the visible arc.
  const cap = el.querySelector("circle.cap");
  if (cap) {
    const theta = clamped * 2 * Math.PI;
    cap.setAttribute("cx", (50 + 42 * Math.cos(theta)).toFixed(2));
    cap.setAttribute("cy", (50 + 42 * Math.sin(theta)).toFixed(2));
  }

  el.querySelector(".gauge-value").textContent = valueText;
  if (kind) applyThresholdClass(el, rawValue, kind);
}

// ---------------- Dial tick marks ----------------
// Drawn once at boot rather than authored 5x in HTML - identical geometry
// for every gauge, same angle convention as the value arc and cap above.
const SVG_NS = "http://www.w3.org/2000/svg";

function buildTicks(count, majorEvery) {
  const g = document.createElementNS(SVG_NS, "g");
  g.setAttribute("class", "ticks");
  for (let i = 0; i < count; i++) {
    const theta = (i / count) * 2 * Math.PI;
    const major = i % majorEvery === 0;
    const r1 = 46;
    const r2 = major ? 52 : 49.5;
    const line = document.createElementNS(SVG_NS, "line");
    line.setAttribute("x1", (50 + r1 * Math.cos(theta)).toFixed(2));
    line.setAttribute("y1", (50 + r1 * Math.sin(theta)).toFixed(2));
    line.setAttribute("x2", (50 + r2 * Math.cos(theta)).toFixed(2));
    line.setAttribute("y2", (50 + r2 * Math.sin(theta)).toFixed(2));
    line.setAttribute("class", major ? "tick tick-major" : "tick tick-minor");
    g.appendChild(line);
  }
  return g;
}

function addTicksToGauges() {
  document.querySelectorAll(".gauge svg").forEach((svg) => {
    svg.insertBefore(buildTicks(20, 5), svg.firstChild);
  });
}

function setBar(id, fraction, valueText, kind, rawValue) {
  const el = document.getElementById(id);
  const clamped = Math.max(0, Math.min(1, fraction)) * 100;
  el.querySelector(".bar-fill").style.width = clamped + "%";
  el.querySelector(".bar-val").textContent = valueText;
  if (kind) {
    const [warn, danger] = THRESHOLDS[kind];
    el.classList.remove("warn", "danger");
    if (rawValue !== null && rawValue !== undefined) {
      if (rawValue >= danger) el.classList.add("danger");
      else if (rawValue >= warn) el.classList.add("warn");
    }
  }
}

function pushHistory(arr, value) {
  arr.push(value === null || value === undefined ? 0 : value);
  if (arr.length > HISTORY_LEN) arr.shift();
}

function toPoints(arr, width, height) {
  if (arr.length < 2) return "";
  const step = width / (HISTORY_LEN - 1);
  const offset = HISTORY_LEN - arr.length;
  return arr
    .map((v, i) => {
      const x = (offset + i) * step;
      const y = height - (Math.max(0, Math.min(100, v)) / 100) * height;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
}

function updateGraph() {
  document.getElementById("graph-cpu").setAttribute("points", toPoints(cpuHistory, 280, 40));
  document.getElementById("graph-gpu").setAttribute("points", toPoints(gpuHistory, 280, 40));
}

function renderProcs(procs) {
  const list = document.getElementById("proc-list");
  list.innerHTML = "";
  if (!procs || procs.length === 0) {
    list.innerHTML = '<div class="proc-row"><span class="proc-name">-</span></div>';
    return;
  }
  for (const p of procs) {
    const row = document.createElement("div");
    row.className = "proc-row";
    const name = document.createElement("span");
    name.className = "proc-name";
    name.textContent = p.name;
    const val = document.createElement("span");
    val.className = "proc-val";
    val.textContent = p.cpu.toFixed(0) + "%";
    row.appendChild(name);
    row.appendChild(val);
    list.appendChild(row);
  }
}

async function refresh() {
  if (!window.pywebview || !window.pywebview.api) return;
  let s;
  try {
    s = await window.pywebview.api.get_stats();
  } catch (e) {
    return;
  }
  if (!s || Object.keys(s).length === 0) return;

  document.getElementById("cpu-name").textContent = s.cpu_name || "--";

  setGauge("gauge-cpu", s.cpu_percent / 100, s.cpu_percent.toFixed(0) + "%", "usage", s.cpu_percent);
  setGauge("gauge-ram", s.ram_percent / 100, s.ram_percent.toFixed(0) + "%", "usage", s.ram_percent);

  if (s.cpu_temp !== null && s.cpu_temp !== undefined) {
    setGauge("gauge-temp", s.cpu_temp / 100, s.cpu_temp.toFixed(0) + "°", "temp", s.cpu_temp);
  } else {
    // Distinguish "LibreHardwareMonitor isn't running" from "sensor missing
    // while LHM is running" - the former is the far more common cause and
    // worth calling out explicitly instead of a bare N/A.
    setGauge("gauge-temp", 0, s.lhm_connected ? "N/A" : "LHM OFF");
  }

  document.getElementById("ram-detail").textContent =
    `${s.ram_used_gb.toFixed(1)} / ${s.ram_total_gb.toFixed(1)} GB`;

  if (s.cpu_power !== null && s.cpu_power !== undefined) {
    setBar("bar-power", s.cpu_power / 125, s.cpu_power.toFixed(0) + " W");
  } else {
    setBar("bar-power", 0, s.lhm_connected ? "N/A" : "LHM OFF");
  }

  setBar("bar-disk", s.disk_percent / 100, s.disk_percent.toFixed(0) + "%");
  setBar("bar-netup", Math.min(s.net_up_kbs / 5000, 1), s.net_up_kbs.toFixed(0) + " KB/s");
  setBar("bar-netdown", Math.min(s.net_down_kbs / 20000, 1), s.net_down_kbs.toFixed(0) + " KB/s");

  // GPU
  const gpu = s.gpu;
  if (gpu) {
    document.getElementById("gpu-name").textContent = gpu.name || "NVIDIA GPU";
    setGauge("gauge-gpu", gpu.util_percent / 100, gpu.util_percent.toFixed(0) + "%", "usage", gpu.util_percent);
    if (gpu.temp !== null && gpu.temp !== undefined) {
      setGauge("gauge-gputemp", gpu.temp / 100, gpu.temp.toFixed(0) + "°", "temp", gpu.temp);
    } else {
      setGauge("gauge-gputemp", 0, "N/A");
    }
    const vramFrac = gpu.mem_total_gb > 0 ? gpu.mem_used_gb / gpu.mem_total_gb : 0;
    setBar("bar-vram", vramFrac, `${gpu.mem_used_gb.toFixed(1)} / ${gpu.mem_total_gb.toFixed(1)} GB`);
    if (gpu.power !== null && gpu.power !== undefined) {
      setBar("bar-gpupower", gpu.power / (gpu.power_limit || 450), gpu.power.toFixed(0) + " W");
    } else {
      setBar("bar-gpupower", 0, "N/A");
    }
    pushHistory(gpuHistory, gpu.util_percent);
  } else {
    document.getElementById("gpu-name").textContent = "not detected";
    setGauge("gauge-gpu", 0, "N/A");
    setGauge("gauge-gputemp", 0, "N/A");
    setBar("bar-vram", 0, "N/A");
    setBar("bar-gpupower", 0, "N/A");
    pushHistory(gpuHistory, 0);
  }

  pushHistory(cpuHistory, s.cpu_percent);
  updateGraph();

  renderProcs(s.top_procs);
}

// ---------------- Window controls ----------------
function initWindowControls() {
  document.getElementById("btn-min").addEventListener("click", () => {
    window.pywebview.api.minimize_to_tray();
  });
  document.getElementById("btn-close").addEventListener("click", () => {
    window.pywebview.api.close_app();
  });
}

// ---------------- Titlebar drag ----------------
// Scoped to the titlebar only, so the min/close buttons stay clickable.
function initDrag() {
  const titlebar = document.querySelector(".titlebar");
  let dragging = false;
  let startScreenX = 0, startScreenY = 0;
  let winStartX = 0, winStartY = 0;

  titlebar.addEventListener("mousedown", (e) => {
    if (e.target.closest("button")) return; // don't drag from buttons
    dragging = true;
    startScreenX = e.screenX;
    startScreenY = e.screenY;
    // window.screenX/screenY reflect the OS window position in pywebview's webview2 host
    winStartX = window.screenX;
    winStartY = window.screenY;
  });

  window.addEventListener("mousemove", (e) => {
    if (!dragging) return;
    const dx = e.screenX - startScreenX;
    const dy = e.screenY - startScreenY;
    const newX = winStartX + dx;
    const newY = winStartY + dy;
    window.pywebview.api.move_window(newX, newY);
  });

  window.addEventListener("mouseup", () => { dragging = false; });
  window.addEventListener("mouseleave", () => { dragging = false; });
}

// ---------------- Accent color ----------------
async function initAccent() {
  try {
    const accent = await window.pywebview.api.get_accent();
    if (accent) {
      document.documentElement.style.setProperty("--accent", accent);
    }
  } catch (e) {
    // keep default accent from style.css
  }
}

function boot() {
  addTicksToGauges();
  initWindowControls();
  initDrag();
  initAccent();
  refresh();
  setInterval(refresh, 1000);
}

if (window.pywebview) {
  boot();
} else {
  window.addEventListener("pywebviewready", boot);
}
