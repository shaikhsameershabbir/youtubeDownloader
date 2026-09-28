const form = document.querySelector("#form");
const urlInput = document.querySelector("#url");
const go = document.querySelector("#go");
const formError = document.querySelector("#form-error");
const ffmpegNote = document.querySelector("#ffmpeg-note");
const panel = document.querySelector("#panel");

const LABELS = {
  waiting: "Waiting",
  downloading: "Saving",
  saved: "Saved",
  failed: "Failed",
};

let pollGeneration = 0;
let lastScrolled = -1;

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  hide(formError);
  go.disabled = true;
  go.textContent = "Downloading…";
  try {
    const response = await fetch("/api/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: urlInput.value.trim() }),
    });
    const data = await response.json();
    if (!response.ok) {
      showError(data.error || "Couldn’t start the download.");
      go.disabled = false;
      go.textContent = "Download";
      return;
    }
    poll();
  } catch {
    showError("Couldn’t reach the downloader.");
    go.disabled = false;
    go.textContent = "Download";
  }
});

document.querySelector("#open-folder")?.addEventListener("click", openFolder);

fetch("/api/health")
  .then((response) => response.json())
  .then((data) => {
    if (!data.ffmpeg) ffmpegNote.hidden = false;
  })
  .catch(() => {});

poll();

function poll() {
  const generation = ++pollGeneration;
  pollLoop(generation);
}

async function pollLoop(generation) {
  if (generation !== pollGeneration) return;
  try {
    const response = await fetch("/api/status");
    const data = await response.json();
    if (generation !== pollGeneration) return;
    render(data);
    if (data.running) {
      go.disabled = true;
      go.textContent = "Downloading…";
      setTimeout(() => pollLoop(generation), 400);
    } else {
      go.disabled = false;
      go.textContent = "Download";
    }
  } catch {
    if (generation === pollGeneration) setTimeout(() => pollLoop(generation), 1000);
  }
}

function render(data) {
  if (!data || data.status === "idle") {
    panel.hidden = true;
    panel.replaceChildren();
    return;
  }

  panel.hidden = false;
  panel.classList.toggle("error-state", data.status === "error");
  panel.replaceChildren();

  const heading = document.createElement("h2");
  heading.textContent = data.title || (data.status === "fetching" ? "Reading the link…" : "Download");
  panel.appendChild(heading);

  if (data.folder_display && data.status !== "error") {
    const meta = document.createElement("p");
    meta.className = "meta";
    meta.append(data.status === "done" ? "Saved in " : "Saving to ");
    const code = document.createElement("code");
    code.textContent = data.folder_display;
    meta.append(code);
    panel.appendChild(meta);
  }

  if (data.status === "error") {
    const message = document.createElement("p");
    message.className = "message-error";
    message.textContent = data.error || data.message || "Download failed.";
    panel.appendChild(message);
  } else if (data.status === "done") {
    const message = document.createElement("p");
    message.className = "done";
    message.textContent = data.message || "Saved.";
    panel.appendChild(message);
    const open = document.createElement("button");
    open.type = "button";
    open.className = "open";
    open.id = "open-folder";
    open.textContent = "Open folder";
    open.addEventListener("click", () => openFolder(data.folder));
    panel.appendChild(open);
  } else {
    const count = data.total > 1 ? `Video ${data.current || 1} of ${data.total}` : "";
    if (data.message && data.title && data.message !== data.title) {
      const now = document.createElement("p");
      now.className = "now";
      now.textContent = count ? `${count} · ${data.message}` : data.message;
      panel.appendChild(now);
    } else if (count) {
      const now = document.createElement("p");
      now.className = "now";
      now.textContent = count;
      panel.appendChild(now);
    }
    if (data.detail) {
      const detail = document.createElement("p");
      detail.className = "detail";
      detail.textContent = data.detail;
      panel.appendChild(detail);
    }
    const bar = document.createElement("div");
    bar.className = "bar";
    const fill = document.createElement("span");
    fill.style.width = `${Math.max(0, Math.min(100, data.percent || 0))}%`;
    bar.appendChild(fill);
    panel.appendChild(bar);
    const stats = document.createElement("div");
    stats.className = "stats";
    const percent = document.createElement("span");
    percent.textContent = `${Math.round(data.percent || 0)}%`;
    const pace = document.createElement("span");
    pace.textContent = [data.speed, data.eta ? `ETA ${data.eta}` : ""].filter(Boolean).join(" · ");
    stats.append(percent, pace);
    panel.appendChild(stats);
  }

  if ((data.kind === "playlist" || data.kind === "channel") && data.items && data.items.length) {
    const list = document.createElement("ol");
    list.className = "list";
    data.items.forEach((item) => {
      const row = document.createElement("li");
      const badge = document.createElement("span");
      badge.className = `badge ${item.status || "waiting"}`;
      badge.textContent = LABELS[item.status] || "Waiting";
      const title = document.createElement("span");
      title.className = "item-title";
      title.textContent = item.title || "Untitled";
      if (item.error) {
        const error = document.createElement("span");
        error.className = "item-error";
        error.textContent = item.error;
        title.append(error);
      }
      row.append(badge, title);
      list.appendChild(row);
    });
    panel.appendChild(list);
    if (data.current !== lastScrolled) {
      lastScrolled = data.current;
      const active = list.querySelector(".badge.downloading");
      active?.parentElement?.scrollIntoView({ block: "nearest" });
    }
  }
}

async function openFolder(path) {
  try {
    await fetch("/api/open-folder", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
  } catch {
    showError("Couldn’t open the folder.");
  }
}

function showError(message) {
  formError.hidden = false;
  formError.textContent = message;
}

function hide(node) {
  node.hidden = true;
  node.textContent = "";
}
