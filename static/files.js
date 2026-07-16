// File Explorer tab — two-pane file manager over the business's raw S3 objects.
// Left (in the sidebar): a folders-only collapsible tree. Right (main area): the
// contents of the selected folder — its subfolders + files — with a breadcrumb
// and a list/tiles view toggle. Read-only: click a file to open it via a
// short-lived presigned URL. Scoped by the shared HermesBiz business selector;
// "All Businesses" groups the tree by business at the root.

const treeEl = document.getElementById("files-folder-tree");
const treeEmptyEl = document.getElementById("files-tree-empty");
const contentEl = document.getElementById("files-content");
const breadcrumbEl = document.getElementById("files-breadcrumb");
const viewToggleEl = document.getElementById("files-view-toggle");
const filesEmptyEl = document.getElementById("files-empty");
const filesLoadingEl = document.getElementById("files-loading");
const filesRefreshBtn = document.getElementById("files-refresh-btn");
const filesRefreshM = document.getElementById("files-refresh-m");

function fEscape(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function fHumanSize(n) {
  if (n == null) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = Number(n), i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${i === 0 ? v : v.toFixed(1)} ${units[i]}`;
}

function fHumanDate(iso) {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleDateString(undefined, {
      year: "numeric", month: "short", day: "numeric",
    });
  } catch (_) { return ""; }
}

// Label + emoji for a business slug, using HermesBiz's list; null -> "Unsorted".
function fBizLabel(slug) {
  if (!slug) return "Other / Unsorted";
  const biz = window.HermesBiz && window.HermesBiz.find(slug);
  return biz ? `${biz.emoji || "🏢"} ${biz.name}` : slug;
}

// ---- Tree model ----------------------------------------------------------
// A folder node = { name, children: Map<name, folder>, files: [{name, file}] }.
// Files live in their own array (never mixed into `children`) so a file can
// never shadow a folder and there are no synthetic child keys. In "all" mode
// the first path segment is a synthetic per-business group label.
function makeFolder(name) { return { name, children: new Map(), files: [] }; }

function buildTree(files, grouped) {
  const root = makeFolder("");
  for (const f of files) {
    const segs = f.key.split("/").filter(Boolean);
    if (!segs.length) continue;
    const path = grouped ? [fBizLabel(f.business_slug), ...segs] : segs;
    let node = root;
    for (let i = 0; i < path.length - 1; i++) {
      const name = path[i];
      let child = node.children.get(name);
      if (!child) { child = makeFolder(name); node.children.set(name, child); }
      node = child;
    }
    node.files.push({ name: path[path.length - 1], file: f });
  }
  return root;
}

// Stamp each folder with its `path` array (root = []) and a numeric id, and
// register it in `nodeById` so a click can resolve a folder directly — no
// fragile string round-trip of the path.
function annotate(node, path) {
  node.path = path;
  node.id = _nextId++;
  nodeById.set(node.id, node);
  for (const child of node.children.values()) {
    annotate(child, path.concat(child.name));
  }
}

function pathKey(arr) { return arr.join("/"); }

// Walk to the folder node at `pathArr` (or null).
function getNode(pathArr) {
  let node = treeRoot;
  for (const seg of pathArr) {
    const next = node && node.children.get(seg);
    if (!next) return null;
    node = next;
  }
  return node;
}

function countLeaves(node) {
  let n = node.files.length;
  for (const child of node.children.values()) n += countLeaves(child);
  return n;
}

function sortedFolders(node) {
  return [...node.children.values()].sort((a, b) => a.name.localeCompare(b.name));
}
function sortedFiles(node) {
  return node.files.slice().sort((a, b) => a.name.localeCompare(b.name));
}

// ---- Icons ---------------------------------------------------------------
const CHEVRON = '<svg class="ftree-chevron" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>';
const FOLDER_ICON = '<svg class="ftree-icon" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';

function svgIcon(inner, type) {
  return `<svg class="file-icon" data-type="${type}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${inner}</svg>`;
}

const FILE_BASE = '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>';
const ICONS = {
  folder:  svgIcon('<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>', "folder"),
  image:   svgIcon('<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/>', "image"),
  pdf:     svgIcon(FILE_BASE + '<path d="M8.5 13H10a1.25 1.25 0 0 1 0 2.5H8.5z"/><path d="M8.5 13v5"/>', "pdf"),
  doc:     svgIcon(FILE_BASE + '<line x1="8" y1="13" x2="16" y2="13"/><line x1="8" y1="17" x2="13" y2="17"/>', "doc"),
  sheet:   svgIcon('<rect x="3" y="3" width="18" height="18" rx="2"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="3" y1="15" x2="21" y2="15"/><line x1="9" y1="3" x2="9" y2="21"/>', "sheet"),
  slides:  svgIcon('<rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/>', "slides"),
  video:   svgIcon('<rect x="2" y="4" width="20" height="16" rx="2"/><path d="M10 9l5 3-5 3z"/>', "video"),
  audio:   svgIcon('<path d="M9 18V6l10-2v12"/><circle cx="6" cy="18" r="3"/><circle cx="16" cy="16" r="3"/>', "audio"),
  archive: svgIcon('<path d="M21 8v11a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V8"/><rect x="1" y="3" width="22" height="5" rx="1"/><line x1="10" y1="12" x2="14" y2="12"/>', "archive"),
  generic: svgIcon(FILE_BASE, "generic"),
};

const EXT = {
  image:   ["png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "heic", "tiff"],
  pdf:     ["pdf"],
  doc:     ["doc", "docx", "txt", "md", "rtf", "odt"],
  sheet:   ["xls", "xlsx", "csv", "ods"],
  slides:  ["ppt", "pptx", "odp"],
  video:   ["mp4", "mov", "avi", "webm", "mkv", "m4v"],
  audio:   ["mp3", "wav", "m4a", "ogg", "flac", "aac"],
  archive: ["zip", "rar", "7z", "tar", "gz"],
};

function fileCategory(name) {
  const dot = name.lastIndexOf(".");
  const ext = dot >= 0 ? name.slice(dot + 1).toLowerCase() : "";
  for (const cat in EXT) {
    if (EXT[cat].indexOf(ext) !== -1) return cat;
  }
  return "generic";
}
function fileIcon(name) { return ICONS[fileCategory(name)]; }

const SPINNER = '<div class="files-spinner-wrap"><span class="files-spinner" role="status" aria-label="Loading"></span></div>';

// In the grid (tiles) view, hard-cap the shown name so long keys/uuids don't
// blow out a tile; the full name stays in the `title` tooltip.
function gridName(name) {
  return name.length > 10 ? name.slice(0, 10) + "…" : name;
}

// ---- State ---------------------------------------------------------------
let treeRoot = makeFolder("");
let grouped = false;
let currentPath = [];
let expanded = new Set();
let nodeById = new Map();
let _nextId = 0;
let viewMode = localStorage.getItem("hermesFilesView") || "tiles";
let loading = false;
// Fetch every file once (entity_id=all) and cache it; switching business just
// re-filters this list client-side. Refetch only on Refresh or after the TTL.
let allFiles = null;
let cacheAt = 0;
const CACHE_TTL_MS = 120000; // 2 minutes

// ---- Left folder tree ----------------------------------------------------
function renderFolderNode(node) {
  const key = pathKey(node.path);
  const isOpen = expanded.has(key);
  const isSel = pathKey(currentPath) === key;
  const kids = sortedFolders(node);
  const kidsHtml = kids.map(renderFolderNode).join("");
  return `
    <div class="ftree-node${isOpen ? " open" : ""}">
      <div class="ftree-row ftree-folder${isSel ? " selected" : ""}" data-id="${node.id}" style="--depth:${node.path.length - 1}">
        ${kids.length ? CHEVRON : '<span class="ftree-chevron-spacer"></span>'}
        ${FOLDER_ICON}
        <span class="ftree-name">${fEscape(node.name)}</span>
        <span class="ftree-meta">${countLeaves(node)}</span>
      </div>
      <div class="ftree-children">${kidsHtml}</div>
    </div>`;
}

function renderLeftTree() {
  const folders = sortedFolders(treeRoot);
  if (treeEl) treeEl.innerHTML = folders.map(renderFolderNode).join("");
  if (treeEmptyEl) treeEmptyEl.hidden = folders.length > 0;
}

// ---- Breadcrumb ----------------------------------------------------------
// Each crumb's data-depth is how many leading path segments to keep on click.
function renderBreadcrumb() {
  const crumbs = [`<button type="button" class="crumb" data-depth="0">Home</button>`];
  for (let i = 0; i < currentPath.length; i++) {
    crumbs.push('<span class="crumb-sep">›</span>');
    crumbs.push(`<button type="button" class="crumb" data-depth="${i + 1}">${fEscape(currentPath[i])}</button>`);
  }
  if (breadcrumbEl) breadcrumbEl.innerHTML = crumbs.join("");
}

// ---- Right content pane --------------------------------------------------
function folderItemHtml(node) {
  const count = countLeaves(node);
  const shown = viewMode === "tiles" ? gridName(node.name) : node.name;
  return `
    <button type="button" class="file-item folder" data-id="${node.id}" title="${fEscape(node.name)}">
      <span class="file-item-icon">${ICONS.folder}</span>
      <span class="file-item-body">
        <span class="file-item-name">${fEscape(shown)}</span>
        <span class="file-item-meta">${count} item${count === 1 ? "" : "s"}</span>
      </span>
    </button>`;
}

function fileItemHtml(leaf) {
  const f = leaf.file;
  const meta = [fHumanSize(f.size), fHumanDate(f.last_modified)].filter(Boolean).join(" · ");
  const shown = viewMode === "tiles" ? gridName(leaf.name) : leaf.name;
  return `
    <button type="button" class="file-item file" data-uri="${fEscape(f.s3_uri)}" title="${fEscape(f.key)}">
      <span class="file-item-icon">${fileIcon(leaf.name)}</span>
      <span class="file-item-body">
        <span class="file-item-name">${fEscape(shown)}</span>
        <span class="file-item-meta">${fEscape(meta)}</span>
      </span>
    </button>`;
}

function renderRight() {
  const node = getNode(currentPath);
  renderBreadcrumb();
  if (!contentEl) return;
  contentEl.className = `files-content ${viewMode}`;
  const folders = node ? sortedFolders(node) : [];
  const files = node ? sortedFiles(node) : [];
  if (!folders.length && !files.length) {
    contentEl.innerHTML = "";
    filesEmptyEl.hidden = false;
    return;
  }
  filesEmptyEl.hidden = true;
  contentEl.innerHTML =
    folders.map(folderItemHtml).join("") + files.map(fileItemHtml).join("");
}

// ---- Navigation ----------------------------------------------------------
function selectPath(pathArr) {
  currentPath = pathArr.slice();
  // Expand every ancestor (and the folder itself) so the left tree reveals it.
  for (let i = 1; i <= pathArr.length; i++) {
    expanded.add(pathKey(pathArr.slice(0, i)));
  }
  renderLeftTree();
  renderRight();
}

function setViewMode(mode) {
  viewMode = mode === "list" ? "list" : "tiles";
  localStorage.setItem("hermesFilesView", viewMode);
  if (viewToggleEl) {
    viewToggleEl.querySelectorAll("button").forEach((b) =>
      b.classList.toggle("active", b.dataset.mode === viewMode));
  }
  renderRight();
}

// ---- Load ----------------------------------------------------------------
// Rebuild the tree for the active business from the cached all-files list.
// "all" → grouped by business; a specific slug → just that business's files.
function applyView() {
  const slug = (window.HermesBiz && window.HermesBiz.current) || "all";
  grouped = slug === "all";
  const files = !allFiles ? []
    : grouped ? allFiles
    : allFiles.filter((f) => f.business_slug === slug);
  nodeById = new Map();
  _nextId = 0;
  treeRoot = buildTree(files, grouped);
  annotate(treeRoot, []);
  expanded = new Set();
  selectPath([]); // start at the root
}

// Load files for the active business. Uses the cache and just re-filters unless
// `force` is set (Refresh) or the cache has expired — only then do we refetch.
async function loadFiles(force) {
  if (!contentEl || loading) return;
  const fresh = allFiles && (Date.now() - cacheAt) < CACHE_TTL_MS;
  if (!force && fresh) { applyView(); return; }
  loading = true;
  filesEmptyEl.hidden = true;
  if (treeEmptyEl) treeEmptyEl.hidden = true;
  if (filesLoadingEl) filesLoadingEl.hidden = true;
  contentEl.innerHTML = SPINNER;
  if (treeEl) treeEl.innerHTML = SPINNER;
  try {
    const r = await fetch(`/api/files/tree?entity_id=all`);
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || "Failed to load files");
    allFiles = j.files || [];
    cacheAt = Date.now();
    applyView();
  } catch (e) {
    console.error("loadFiles:", e);
    if (treeEl) treeEl.innerHTML = "";
    contentEl.innerHTML = "";
    filesEmptyEl.hidden = false;
  } finally {
    loading = false;
  }
}

async function openFile(uri, itemEl) {
  if (!uri) return;
  if (itemEl) itemEl.classList.add("loading");
  try {
    const r = await fetch(`/api/media/presign?uri=${encodeURIComponent(uri)}`);
    const j = await r.json();
    if (!r.ok || !j.url) throw new Error(j.error || "Could not open file");
    window.open(j.url, "_blank", "noopener");
  } catch (e) {
    console.error("openFile:", e);
    alert("Couldn't open this file: " + e.message);
  } finally {
    if (itemEl) itemEl.classList.remove("loading");
  }
}

// ---- Events --------------------------------------------------------------
// Left tree: chevron toggles expand; folder row selects + navigates.
if (treeEl) {
  treeEl.addEventListener("click", (e) => {
    const row = e.target.closest(".ftree-folder");
    if (!row) return;
    const node = nodeById.get(+row.dataset.id);
    if (!node) return;
    if (e.target.closest(".ftree-chevron")) {
      const key = pathKey(node.path);
      if (expanded.has(key)) expanded.delete(key); else expanded.add(key);
      renderLeftTree();
      return;
    }
    selectPath(node.path);
  });
}

// Right pane: folder navigates in; file opens.
if (contentEl) {
  contentEl.addEventListener("click", (e) => {
    const item = e.target.closest(".file-item");
    if (!item) return;
    if (item.classList.contains("folder")) {
      const node = nodeById.get(+item.dataset.id);
      if (node) selectPath(node.path);
    } else {
      openFile(item.dataset.uri, item);
    }
  });
}

// Breadcrumb navigation.
if (breadcrumbEl) {
  breadcrumbEl.addEventListener("click", (e) => {
    const crumb = e.target.closest(".crumb");
    if (crumb) selectPath(currentPath.slice(0, +crumb.dataset.depth));
  });
}

// View toggle.
if (viewToggleEl) {
  viewToggleEl.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-mode]");
    if (btn) setViewMode(btn.dataset.mode);
  });
}

if (filesRefreshBtn) filesRefreshBtn.addEventListener("click", () => loadFiles(true));
if (filesRefreshM) filesRefreshM.addEventListener("click", () => loadFiles(true));

// ---- Hook into app.js view switch + business-selector changes -----------
let filesInited = false;
window._filesInit = function () {
  if (!filesInited) {
    filesInited = true;
    setViewMode(viewMode); // reflect persisted mode in the toggle buttons
    if (window.HermesBiz) {
      window.HermesBiz.bind(
        document.getElementById("hermes-biz-select-files"),
        document.getElementById("hermes-biz-badge-files")
      );
      window.HermesBiz.onChange(() => { if (filesInited) loadFiles(); });
    }
  }
  loadFiles();
};
