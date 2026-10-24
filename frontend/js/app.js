/**
 * Alpine.js app — all state, routing, and view logic for the iCourse frontend.
 * References ICS.crypto, ICS.github, ICS.db, ICS.render globals.
 */

/* ── Gzip helpers (Compression Streams API) ── */
async function _gunzip(compressedBytes) {
  var ds = new DecompressionStream("gzip");
  var writer = ds.writable.getWriter();
  writer.write(compressedBytes);
  writer.close();
  var chunks = [];
  var reader = ds.readable.getReader();
  while (true) {
    var r = await reader.read();
    if (r.done) break;
    chunks.push(r.value);
  }
  var total = chunks.reduce(function(s, c) { return s + c.length; }, 0);
  var result = new Uint8Array(total);
  var offset = 0;
  for (var i = 0; i < chunks.length; i++) {
    result.set(chunks[i], offset);
    offset += chunks[i].length;
  }
  return result;
}

/* ── IndexedDB cache for decrypted shards (keyed by git blob sha) ────
   Shard contents are content-addressed: a shard's git blob sha changes
   only when its bytes change, so we can keep decrypted bytes around and
   skip the network + decrypt + decompress chain on subsequent loads.
*/
var _idbName = "ics_cache_v2";
// Cache the open Promise — every shard load fires _idbGet + _idbPut, and
// without this each call opens its own connection.  Reopening costs ~10ms
// on cold caches and accumulates fast over 100+ shard reads.
var _idbPromise = null;

function _idbOpen() {
  if (_idbPromise) return _idbPromise;
  _idbPromise = new Promise(function(resolve, reject) {
    var req = indexedDB.open(_idbName, 1);
    req.onupgradeneeded = function() { req.result.createObjectStore("blobs"); };
    req.onsuccess = function() { resolve(req.result); };
    req.onerror = function() {
      // Don't cache failed opens — next call retries cleanly.
      _idbPromise = null;
      reject(req.error);
    };
  });
  return _idbPromise;
}

async function _idbGet(key) {
  var db = await _idbOpen();
  return new Promise(function(resolve) {
    var tx = db.transaction("blobs", "readonly");
    var req = tx.objectStore("blobs").get(key);
    req.onsuccess = function() { resolve(req.result || null); };
    req.onerror = function() { resolve(null); };
  });
}

async function _idbPut(key, value) {
  var db = await _idbOpen();
  return new Promise(function(resolve) {
    var tx = db.transaction("blobs", "readwrite");
    tx.objectStore("blobs").put(value, key);
    tx.oncomplete = function() { resolve(); };
    tx.onerror = function() { resolve(); };
  });
}

/* ── Credential helpers (localStorage) ── */
const _LS = "ics_";
const _loadCreds = () => { try { return JSON.parse(localStorage.getItem(_LS + "creds")); } catch { return null; } };
const _saveCreds = (c) => localStorage.setItem(_LS + "creds", JSON.stringify(c));
const _loadSettings = () => { try { return JSON.parse(localStorage.getItem(_LS + "settings")) || {}; } catch { return {}; } };
const _saveSettings = (s) => localStorage.setItem(_LS + "settings", JSON.stringify(s));
/* Starred-course IDs are per-browser (localStorage). The school side
   doesn't need to know; the user just wants their favorites pinned to
   the top of their own view. */
const _loadStarred = () => {
  try { return new Set(JSON.parse(localStorage.getItem(_LS + "starred")) || []); }
  catch { return new Set(); }
};
const _saveStarred = (set) => localStorage.setItem(
  _LS + "starred", JSON.stringify(Array.from(set))
);

function _relativeTime(iso) {
  if (!iso) return "";
  const d = Date.now() - new Date(iso).getTime();
  const m = Math.floor(d / 60000);
  if (m < 1) return "just now";
  if (m < 60) return m + "m ago";
  const h = Math.floor(m / 60);
  if (h < 24) return h + "h ago";
  const days = Math.floor(h / 24);
  if (days < 30) return days + "d ago";
  return new Date(iso).toLocaleDateString();
}

function _highlightSnippet(text, query, radius) {
  radius = radius || 60;
  if (!text || !query) return "";
  const plain = ICS.render.plainSnippet(text, 99999);
  const idx = plain.toLowerCase().indexOf(query.toLowerCase());
  if (idx === -1) return plain.slice(0, 120) + "...";
  const s = Math.max(0, idx - radius);
  const e = Math.min(plain.length, idx + query.length + radius);
  let snip = (s > 0 ? "..." : "") + plain.slice(s, e) + (e < plain.length ? "..." : "");
  const re = new RegExp("(" + query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ")", "gi");
  return snip.replace(re, "<mark>$1</mark>");
}

function _formatTimestamp(seconds) {
  var sec = Math.max(0, Math.floor(seconds || 0));
  var h = Math.floor(sec / 3600);
  var m = Math.floor((sec % 3600) / 60);
  var s = sec % 60;
  function pad(n) { return String(n).padStart(2, "0"); }
  if (h > 0) return pad(h) + ":" + pad(m) + ":" + pad(s);
  return pad(m) + ":" + pad(s);
}

/* Three-state detail view: summary → transcript → ppt → summary.
   The button label always shows the *next* state so the user can read it
   as an action ("切换到转录"). */
const _DETAIL_VIEW_CYCLE = ["summary", "transcript", "ppt"];
const _DETAIL_VIEW_LABEL = {
  summary: "摘要",
  transcript: "转录",
  ppt: "PPT 识别",
};

/* ── Sharded loading helpers ── */
async function _loadShard(owner, repo, entry, password, token) {
  // Returns { bytes, downloaded: bool } — downloaded=true means we hit
  // the network; false means IndexedDB cache hit (instant).
  var cacheKey = "shard:" + entry.sha;
  var cached = await _idbGet(cacheKey);
  if (cached) return { bytes: cached, downloaded: false };

  var encBytes = await ICS.github.fetchBlobBytes(owner, repo, entry.sha, token);
  var gzipped = await ICS.crypto.decrypt(
    encBytes, password, ICS.crypto.NEW_ITERATIONS,
  );
  if (!ICS.crypto.isGzip(gzipped)) {
    throw new Error(
      "Shard '" + entry.name + "' decrypted to non-gzip bytes — wrong key?"
    );
  }
  var dbBytes = await _gunzip(gzipped);
  await _idbPut(cacheKey, dbBytes);
  return { bytes: dbBytes, downloaded: true };
}

async function _fetchAndDecryptIndex(owner, repo, indexSha, password, token) {
  var indexEnc = await ICS.github.fetchBlobBytes(owner, repo, indexSha, token);
  var indexBytes = await ICS.crypto.decrypt(
    indexEnc, password, ICS.crypto.NEW_ITERATIONS,
  );
  if (!ICS.crypto.isJsonObj(indexBytes)) {
    throw new Error("Shard index decrypted to non-JSON bytes — wrong key?");
  }
  return JSON.parse(new TextDecoder().decode(indexBytes));
}

async function _loadFromShardManifest(manifest, owner, repo, password, token, progress) {
  // 0) Check if the full merged DB is already cached for this commit SHA.
  //    If the data branch hasn't moved, we can skip ALL shard loading.
  var mergedKey = "merged:" + manifest.commitSha;
  var cachedMerged = await _idbGet(mergedKey);
  if (cachedMerged) {
    await ICS.db.initDB(cachedMerged);
    return;
  }

  // 1) Load index: check if index SHA matches cached → reuse decrypted JSON;
  //    otherwise fetch + decrypt + cache for next time.
  var cachedIndexSha = null;
  try { cachedIndexSha = localStorage.getItem(_LS + "indexSha"); } catch (e) {}

  var index = null;
  if (manifest.index.sha === cachedIndexSha) {
    index = await _idbGet("index:v2:" + manifest.index.sha);
  }

  if (!index) {
    index = await _fetchAndDecryptIndex(
      owner, repo, manifest.index.sha, password, token,
    );
    await _idbPut("index:v2:" + manifest.index.sha, index);
    try { localStorage.setItem(_LS + "indexSha", manifest.index.sha); } catch (e) {}
  }

  // 2) Pull every shard — skip cache hits, download only changed ones.
  await ICS.db.initEmpty();
  var total = (index.shards || []).length;
  var downloaded = 0;
  for (var i = 0; i < total; i++) {
    var shardMeta = index.shards[i];
    var entry = manifest.shards.find(function (s) { return s.name === shardMeta.name; });
    if (!entry) {
      console.warn("Shard listed in index but missing from tree:", shardMeta.name);
      continue;
    }
    var result = await _loadShard(owner, repo, entry, password, token);
    if (result.downloaded) {
      downloaded++;
      if (progress) progress(downloaded, total, shardMeta.name);
    }
    await ICS.db.attachShard(result.bytes);
  }
  if (progress && downloaded === 0) progress(0, total, "");

  // 3) Cache the merged DB in IndexedDB so next load with the same commit
  //    SHA skips EVERYTHING — no index fetch, no shard iteration, no merge.
  try {
    var merged = ICS.db.exportDB();
    await _idbPut(mergedKey, merged);
  } catch (e) { /* non-critical — silently skip */ }
}

async function _loadFromLegacyBlob(manifest, owner, repo, secrets, token) {
  // Single-file fallback for users still on the pre-shard data branch.
  var encBytes = await ICS.github.fetchBlobBytes(
    owner, repo, manifest.legacy.sha, token,
  );
  var validator = manifest.legacy.compressed
    ? ICS.crypto.isGzip
    : ICS.crypto.isSqlite;
  var fallback = await ICS.crypto.decryptWithFallback(
    encBytes, secrets, validator,
  );
  var bytes = fallback.data;
  if (manifest.legacy.compressed) {
    bytes = await _gunzip(bytes);
  }
  await ICS.db.initDB(bytes);
}

/* ── Alpine app ── */
document.addEventListener("alpine:init", () => {
  Alpine.data("app", () => ({
    view: "loading", error: null, loadingMsg: "",
    toast: null, toastType: "success",
    courses: [], lectures: [],
    currentCourse: null, currentLecture: null,
    currentPptPages: [],
    pptImageUrls: {}, pptImageErrors: {}, pptImageLoading: {},
    lightboxUrl: null, pptObserver: null,
    mediaActive: 0,
    mediaStats: null, mediaStatsLoading: false,
    pruneTargetMb: 700, pruneKeepRecent: 2, pruneTriggering: false,
    auditTriggering: false, auditRunUrl: "", auditRunLabel: "",
    detailView: "summary",
    searchQuery: "", searchResults: [],
    searchCourseFilterQuery: "", searchSelectedCourseIds: [],
    searchCourseOpen: false,
    searchPage: 1, searchHasMore: false,
    searchDomains: { summary: true, transcript: true, ocr: true },
    commitSha: null,
    setup: { token: "", stuid: "", uispsw: "" },
    setupError: "", setupTesting: false,
    settingsForm: {}, showSecrets: {},
    exportDialogOpen: false, exportSelection: {}, exportingPdf: false,
    deleteDialogOpen: false, deleteSelection: {}, deletingCourses: false,
    repoOwner: "", repoName: "", dataBranch: "data",
    _history: [],
    /* Subscriptions editor state — three-column layout:
       left (subscribed) | middle (catalog search) | right (single-run).
       The left column persists to GitHub Secret on demand; the right
       column is session-only and cleared after triggering a workflow.
       Columns are driven by SQL queries against the in-memory shard DB
       so we never hold the full 20k-row catalog in JS. */
    allCoursesTerms: [],
    subsTerms: [], subsDepts: [], deptSearchQuery: "",
    subsSearchTitle: "", subsSearchTeacher: "",
    subsTermOpen: false, subsDeptOpen: false,
    subscribedIds: [], singleRunIds: [],
    subsSelLeft: [], subsSelMiddle: [], subsSelRight: [],
    subsFiltered: [], subsFilteredTotal: 0,
    subsLimit: 200,
    _subsSubscribedCache: [], _subsSingleRunCache: [], _subsDeptCache: [],
    _subsFilterTimer: null, _deptFilterTimer: null,
    subsSaving: false, subsError: "",
    singleRunTriggering: false,
    singleRunUseOfficial: false,
    /* Per-browser pinned-courses set, lazily synced to localStorage. */
    starred: _loadStarred(),

    async init() {
      const detected = ICS.github.detectRepo();
      const s = _loadSettings();
      this.repoOwner = s.owner || (detected?.owner ?? "");
      this.repoName = s.repo || (detected?.repo ?? "");
      this.dataBranch = s.branch || "data";
      const creds = _loadCreds();
      if (!creds) { this.view = "setup"; return; }
      await this._loadDB(creds);
      window.addEventListener("keydown", (e) => {
        if (e.key === "Escape") this.lightboxUrl = null;
      });
    },

    async _loadDB(creds) {
      this.view = "loading"; this.error = null;
      try {
        this.loadingMsg = "Connecting to GitHub API...";
        var manifest = await ICS.github.fetchShardManifest(
          this.repoOwner, this.repoName, this.dataBranch, creds.token,
        );
        this.commitSha = manifest.commitSha;

        if (manifest.format === "sharded") {
          this.loadingMsg = "Deriving decryption key...";
          var pw = await ICS.crypto.buildPasswordV2(creds);

          this.loadingMsg = "Loading index...";
          var self = this;
          await _loadFromShardManifest(
            manifest, this.repoOwner, this.repoName, pw, creds.token,
            function (i, n, name) {
              if (i === 0) {
                self.loadingMsg = "All " + n + " shards unchanged — instant load.";
              } else {
                self.loadingMsg = "Downloading shard " + i + "/" + n
                  + " (" + name + ")...";
              }
            },
          );
        } else {
          this.loadingMsg = "Downloading + decrypting legacy database...";
          await _loadFromLegacyBlob(
            manifest, this.repoOwner, this.repoName, creds, creds.token,
          );
        }

        this.courses = this._sortCoursesByStar(ICS.db.getCourses());
        this.view = "courses";
      } catch (e) {
        this.error = e.message;
        this.view = "error";
      }
    },

    navigate(view, params) {
      params = params || {};
      this._history.push({ view: this.view, courseId: this.currentCourse?.course_id, lectureId: this.currentLecture?.sub_id });
      this._go(view, params);
    },
    _go(view, params) {
      params = params || {};
      this.error = null;
      if (view === "courses") {
        this.courses = this._sortCoursesByStar(ICS.db.getCourses());
      }
      else if (view === "lectures" && params.courseId) {
        this.currentCourse = this.courses.find(x => x.course_id === params.courseId) || { course_id: params.courseId, title: "...", teacher: "" };
        this.lectures = ICS.db.getLectures(params.courseId);
      }
      else if (view === "detail" && params.subId) {
        this.currentLecture = ICS.db.getLecture(params.subId);
        this.currentPptPages = this.currentLecture
          ? ICS.db.getPptPages(this.currentLecture.sub_id)
          : [];
        // Opening from search lands here without a matching course context;
        // load the lecture's own course so prev/next nav + the lectures list
        // (used by _currentLectureIndex) are correct rather than stale/empty.
        if (this.currentLecture) {
          var cid = String(this.currentLecture.course_id);
          if (!this.currentCourse || String(this.currentCourse.course_id) !== cid) {
            this.currentCourse =
              this.courses.find((x) => String(x.course_id) === cid)
              || { course_id: cid,
                   title: this.currentLecture.course_title || "",
                   teacher: this.currentLecture.teacher || "" };
            this.lectures = ICS.db.getLectures(cid);
          }
        }
        this._revokePptUrls();
        this.pptImageErrors = {};
        this.pptImageLoading = {};
        var available = this.availableDetailViews;
        var wanted = params.view;
        this.detailView = available.indexOf(wanted) !== -1
          ? wanted : (available[0] || "summary");
        if (this.detailView === "ppt") {
          this.$nextTick(() => this.observePptImages());
        }
      }
      this.view = view;
      if (view !== "lectures") this.exportDialogOpen = false;
    },
    _sortCoursesByStar(list) {
      // Stable two-key sort: starred first (descending = pinned), then
      // by the existing last_updated DESC the SQL already produced.
      var starred = this.starred;
      return list.slice().sort(function (a, b) {
        var sa = starred.has(String(a.course_id)) ? 0 : 1;
        var sb = starred.has(String(b.course_id)) ? 0 : 1;
        if (sa !== sb) return sa - sb;
        return 0;  // preserve SQL order within each group
      });
    },
    goBack() {
      const p = this._history.pop();
      if (p) this._go(p.view, { courseId: p.courseId, subId: p.lectureId });
      else this._go("courses");
    },

    openCourse(id) { this.navigate("lectures", { courseId: id }); },
    openLecture(id, view) { this.navigate("detail", { subId: id, view: view }); },

    /* Prev/next within the current course's lecture list.  Lectures are
       ordered chronologically (parsed from sub_title; see db.js
       _lectureOrderKey), matching the lectures view, so "prev" is the
       lecture at index-1 and "next" is at index+1. */
    _currentLectureIndex() {
      if (!this.currentLecture || !this.lectures) return -1;
      return this.lectures.findIndex(
        (l) => String(l.sub_id) === String(this.currentLecture.sub_id)
      );
    },
    prevLecture() {
      var i = this._currentLectureIndex();
      return i > 0 ? this.lectures[i - 1] : null;
    },
    nextLecture() {
      var i = this._currentLectureIndex();
      return (i >= 0 && i + 1 < this.lectures.length)
        ? this.lectures[i + 1] : null;
    },
    gotoPrevLecture() {
      var lec = this.prevLecture();
      if (lec) { this._go("detail", { subId: lec.sub_id }); this._scrollToTop(); }
    },
    gotoNextLecture() {
      var lec = this.nextLecture();
      if (lec) { this._go("detail", { subId: lec.sub_id }); this._scrollToTop(); }
    },
    _scrollToTop() {
      var self = this;
      this.$nextTick(function () {
        window.scrollTo(0, 0);
        var el = document.querySelector("main");
        if (el) el.scrollTop = 0;
      });
    },

    /* Star/pin a course.  Per-browser localStorage state; no roundtrip
       to GitHub.  Re-sorts the courses list immediately so the user
       sees the pin take effect without navigating away. */
    isStarred(courseId) {
      return this.starred.has(String(courseId));
    },
    toggleStar(courseId) {
      var cid = String(courseId);
      if (this.starred.has(cid)) this.starred.delete(cid);
      else this.starred.add(cid);
      _saveStarred(this.starred);
      // Reactive refresh — re-sort in place.
      this.courses = this._sortCoursesByStar(this.courses);
    },

    /* Three-state detail viewer.  The button shown to the user always
       advertises the *next* state so the label reads as an action. */
    get availableDetailViews() {
      if (!this.currentLecture) return [];
      var views = [];
      if (this.currentLecture.has_summary) views.push("summary");
      if (this.currentLecture.has_transcript) views.push("transcript");
      if (this.currentLecture.has_ppt) views.push("ppt");
      return views;
    },
    switchDetailView(view) {
      if (this.availableDetailViews.indexOf(view) === -1) return;
      this.detailView = view;
      if (view === "ppt") {
        this.$nextTick(() => this.observePptImages());
      }
    },
    cycleDetailView() {
      var views = this.availableDetailViews;
      if (!views.length) return;
      var idx = views.indexOf(this.detailView);
      this.switchDetailView(views[(idx + 1) % views.length]);
    },
    nextDetailViewLabel() {
      var views = this.availableDetailViews;
      if (!views.length) return "无内容";
      var idx = views.indexOf(this.detailView);
      var next = views[(idx + 1) % views.length];
      return "切换到" + _DETAIL_VIEW_LABEL[next];
    },
    formatPptTimestamp(sec) { return _formatTimestamp(sec); },

    courseContentLabel(c) {
      var parts = [];
      if (c.summary_count) parts.push(c.summary_count + " 摘要");
      if (c.transcript_count) parts.push(c.transcript_count + " 转录");
      if (c.ppt_count) parts.push(c.ppt_count + " PPT");
      return parts.length ? parts.join(" · ") : "暂无内容";
    },
    lectureContentLabel(lec) {
      var parts = [];
      if (lec.has_summary) parts.push("摘要");
      if (lec.has_transcript) parts.push("转录");
      if (lec.has_ppt) parts.push("PPT");
      return parts.length ? parts.join(" + ") : "No Content";
    },
    auditExpired(lec) {
      if (lec.has_summary && lec.has_transcript) return false;
      var lectureRaw = String(lec.date || "").trim();
      if (!lectureRaw) {
        lectureRaw = String(lec.sub_title || "").trim().slice(0, 10);
      }
      if (/^\d{4}-\d{2}-\d{2}$/.test(lectureRaw)) {
        var lectureEnd = new Date(
          lectureRaw + "T23:59:59+08:00"
        ).getTime();
        if (Number.isFinite(lectureEnd)
            && lectureEnd < Date.now() - 7 * 86400000) {
          return true;
        }
      }
      var raw = lec.content_first_seen_at;
      if (!raw) return false;
      var normalized = /(?:Z|[+-]\d{2}:\d{2})$/.test(raw) ? raw : raw + "Z";
      var first = new Date(normalized).getTime();
      return Number.isFinite(first) && Date.now() - first >= 7 * 86400000;
    },
    contentAuditLabel(lec) {
      if (lec.has_image_only) return "";
      if (lec.has_summary && lec.has_transcript) return "";
      if (this.auditExpired(lec)) return "";
      if (lec.has_summary && !lec.has_transcript) {
        return "已有摘要，等待录屏后补转录";
      }
      if (lec.has_transcript && !lec.has_summary) {
        return "已有转录，等待补摘要";
      }
      if (lec.has_ppt) {
        return "等待录屏，巡检会自动补转录和摘要";
      }
      return "等待补充内容";
    },
    auditRetryLabel(lec) {
      if (lec.has_image_only) return "";
      if (lec.has_summary && lec.has_transcript) return "";
      if (!lec.content_next_retry_at || this.auditExpired(lec)) return "";
      var raw = lec.content_next_retry_at;
      var normalized = /(?:Z|[+-]\d{2}:\d{2})$/.test(raw) ? raw : raw + "Z";
      var next = new Date(normalized);
      if (!Number.isFinite(next.getTime())) return "";
      if (next.getTime() <= Date.now()
          || next.getTime() > Date.now() + 48 * 3600000) {
        return "";
      }
      return "下次巡检约 " + next.toLocaleString("zh-CN", {
        month: "numeric", day: "numeric",
        hour: "2-digit", minute: "2-digit",
      });
    },

    async loadPptImage(page) {
      var pageNum = Number(page.page_num);
      if (!page.img_sha || this.pptImageUrls[pageNum]
          || this.pptImageLoading[pageNum]) return;
      this.pptImageLoading = Object.assign(
        {}, this.pptImageLoading, { [pageNum]: true },
      );
      try {
        var url = await this._withMediaSlot(async () => {
          var creds = _loadCreds();
          if (!creds?.token) throw new Error("未登录或 PAT 缺失");
          var cached = await _idbGet("media:" + page.img_sha);
          var bytes = cached;
          if (!bytes) {
            var encrypted = await ICS.github.fetchBlobBytes(
              this.repoOwner, this.repoName, page.img_sha, creds.token,
            );
            var password = await ICS.crypto.buildPasswordV2(creds);
            bytes = await ICS.crypto.decrypt(
              encrypted, password, ICS.crypto.NEW_ITERATIONS,
            );
            await _idbPut("media:" + page.img_sha, bytes);
          }
          var blob = new Blob(
            [bytes], { type: page.img_mime || "image/jpeg" },
          );
          return URL.createObjectURL(blob);
        });
        this.pptImageUrls = Object.assign(
          {}, this.pptImageUrls, { [pageNum]: url },
        );
      } catch (e) {
        this.pptImageErrors = Object.assign(
          {}, this.pptImageErrors,
          { [pageNum]: e?.message || "截图加载失败" },
        );
      } finally {
        var loading = Object.assign({}, this.pptImageLoading);
        delete loading[pageNum];
        this.pptImageLoading = loading;
      }
    },
    async _withMediaSlot(fn) {
      while (this.mediaActive >= 4) {
        await new Promise((resolve) => setTimeout(resolve, 60));
      }
      this.mediaActive++;
      try {
        return await fn();
      } finally {
        this.mediaActive--;
      }
    },
    observePptImages() {
      if (this.pptObserver) {
        this.pptObserver.disconnect();
        this.pptObserver = null;
      }
      var pages = this.currentPptPages || [];
      if (!pages.length) return;
      if (typeof IntersectionObserver === "undefined") {
        pages.forEach((p) => this.loadPptImage(p));
        return;
      }
      var self = this;
      this.pptObserver = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (!entry.isIntersecting) return;
          var pageNum = Number(entry.target.getAttribute("data-page"));
          var page = pages.find(
            (p) => Number(p.page_num) === pageNum,
          );
          if (page) self.loadPptImage(page);
          self.pptObserver.unobserve(entry.target);
        });
      }, { rootMargin: "300px 0px" });
      document.querySelectorAll(".ppt-page-image").forEach(
        (el) => this.pptObserver.observe(el),
      );
    },
    openLightbox(url) {
      if (url) this.lightboxUrl = url;
    },
    closeLightbox() { this.lightboxUrl = null; },
    _revokePptUrls() {
      Object.keys(this.pptImageUrls || {}).forEach((key) => {
        try { URL.revokeObjectURL(this.pptImageUrls[key]); } catch (e) {}
      });
      this.pptImageUrls = {};
    },

    getExportableLectures() {
      return (this.lectures || []).filter((lec) => lec.summary && lec.summary.trim());
    },
    openExportDialog() {
      const list = this.getExportableLectures();
      if (!list.length) { this._toast("No summarized lectures to export", "error"); return; }
      this.exportSelection = {};
      list.forEach((lec) => { this.exportSelection[lec.sub_id] = true; });
      this.exportDialogOpen = true;
    },
    closeExportDialog() {
      if (this.exportingPdf) return;
      this.exportDialogOpen = false;
    },
    isLectureSelected(subId) { return !!this.exportSelection[subId]; },
    toggleLectureSelection(subId, checked) { this.exportSelection[subId] = !!checked; },
    setExportAll(checked) {
      this.getExportableLectures().forEach((lec) => { this.exportSelection[lec.sub_id] = !!checked; });
    },
    isExportAllSelected() {
      const list = this.getExportableLectures();
      return list.length > 0 && list.every((lec) => this.exportSelection[lec.sub_id]);
    },
    selectedExportCount() {
      return this.getExportableLectures().filter((lec) => this.exportSelection[lec.sub_id]).length;
    },
    async exportSelectedToPdf() {
      // Triggers .github/workflows/export.yml via workflow_dispatch.  The
      // workflow runs scripts/export_course.py (WeasyPrint) and emails the
      // PDF to RECEIVER_EMAIL — same output and same code path as a manual
      // run from the Actions UI.  We dropped the in-browser html2pdf.js
      // approach because the screenshot-based pipeline produced blank PDFs
      // unreliably; routing through Actions reuses the working tech stack.
      if (this.exportingPdf) return;
      const selected = this.getExportableLectures().filter(
        (lec) => this.exportSelection[lec.sub_id]
      );
      if (!selected.length) {
        this._toast("Please select at least one lecture", "error");
        return;
      }
      const creds = _loadCreds();
      if (!creds?.token) {
        this._toast("Not authenticated", "error");
        return;
      }
      this.exportingPdf = true;
      try {
        const subIds = selected.map((lec) => String(lec.sub_id)).join(",");
        await ICS.github.triggerExportWorkflow(
          this.repoOwner, this.repoName, "main", creds.token,
          this.currentCourse.course_id, "PDF", subIds
        );
        this.exportDialogOpen = false;
        this._toast(
          "已触发后台导出，PDF 将在 1-3 分钟内发送到 RECEIVER_EMAIL",
          "success"
        );
      } catch (e) {
        this._toast(e?.message || "Export failed", "error");
      } finally {
        this.exportingPdf = false;
      }
    },

    async exportSelectedToClipboard() {
      // Client-side markdown export: concatenate transcript + summary
      // for selected lectures and copy to clipboard.  No server needed.
      var selected = this.getExportableLectures().filter(
        function (lec) { return this.exportSelection[lec.sub_id]; }, this
      );
      if (!selected.length) {
        this._toast("请至少选择一节课程", "error");
        return;
      }
      var lines = [
        "# " + (this.currentCourse?.title || "课程摘要"),
        "",
      ];
      for (var i = 0; i < selected.length; i++) {
        var lec = selected[i];
        lines.push("## " + (lec.sub_title || "Untitled") + "（" + (lec.date || "") + "）");
        lines.push("");
        if (lec.summary) {
          lines.push("### 摘要");
          lines.push("");
          lines.push(lec.summary);
          lines.push("");
        }
        lines.push("---");
        lines.push("");
      }
      try {
        await navigator.clipboard.writeText(lines.join("\n"));
        this._toast(
          "已复制 " + selected.length + " 节课的 Markdown 到剪贴板",
          "success"
        );
      } catch (e) {
        this._toast("复制失败：" + (e?.message || "unknown"), "error");
      }
    },

    getDeletableLectures() {
      return (this.lectures || []).filter((lec) => lec.state === "ready");
    },
    openDeleteDialog() {
      const list = this.getDeletableLectures();
      if (!list.length) { this._toast("没有可删除的课次", "error"); return; }
      this.deleteSelection = {};
      list.forEach((lec) => { this.deleteSelection[lec.sub_id] = false; });
      this.deleteDialogOpen = true;
    },
    closeDeleteDialog() {
      if (this.deletingCourses) return;
      this.deleteDialogOpen = false;
    },
    toggleDeleteLecture(subId, checked) { this.deleteSelection[subId] = !!checked; },
    setDeleteAll(checked) {
      this.getDeletableLectures().forEach((lec) => { this.deleteSelection[lec.sub_id] = !!checked; });
    },
    isDeleteAllSelected() {
      const list = this.getDeletableLectures();
      return list.length > 0 && list.every((lec) => this.deleteSelection[lec.sub_id]);
    },
    selectedDeleteCount() {
      return this.getDeletableLectures().filter((lec) => this.deleteSelection[lec.sub_id]).length;
    },
    async confirmDelete() {
      if (this.deletingCourses) return;
      const selected = this.getDeletableLectures().filter(
        (lec) => this.deleteSelection[lec.sub_id]
      );
      if (!selected.length) {
        this._toast("请至少选择一节课程", "error");
        return;
      }
      const creds = _loadCreds();
      if (!creds?.token) {
        this._toast("未登录或 PAT 缺失", "error");
        return;
      }
      this.deletingCourses = true;
      try {
        var currentCid = this.currentCourse ? String(this.currentCourse.course_id) : null;
        var selectedSubIds = selected.map(function (l) { return String(l.sub_id); });

        await ICS.github.triggerDeleteWorkflow(
          this.repoOwner, this.repoName, "main", creds.token,
          [currentCid], selectedSubIds,
        );
        this.deleteDialogOpen = false;
        this._toast("已触发删除 workflow，数据将在几分钟内从远端清除", "success");
        setTimeout(() => { this._go("courses"); }, 1500);
      } catch (e) {
        this._toast(e?.message || "删除失败", "error");
      } finally {
        this.deletingCourses = false;
      }
    },

    _searchTimeout: null,
    get searchCourseLabel() {
      if (!this.searchSelectedCourseIds.length) return "全部课程";
      return "已选" + this.searchSelectedCourseIds.length + "门";
    },
    get searchCourseOptionsFiltered() {
      var q = (this.searchCourseFilterQuery || "").trim().toLowerCase();
      var list = this.courses || [];
      if (!q) return list;
      return list.filter(function (c) {
        var title = String(c?.title || "").toLowerCase();
        var teacher = String(c?.teacher || "").toLowerCase();
        var cid = String(c?.course_id || "").toLowerCase();
        return title.indexOf(q) !== -1 || teacher.indexOf(q) !== -1 || cid.indexOf(q) !== -1;
      });
    },
    toggleSearchCourse(courseId, checked) {
      var cid = String(courseId);
      var s = new Set(this.searchSelectedCourseIds.map(String));
      if (checked) s.add(cid); else s.delete(cid);
      this.searchSelectedCourseIds = Array.from(s);
      this.doSearch();
    },
    doSearch() {
      clearTimeout(this._searchTimeout);
      this._searchTimeout = setTimeout(() => {
        this._loadSearchResults(1);
      }, 300);
    },
    _loadSearchResults(page) {
      if (!this.searchQuery.trim()) {
        this.searchResults = [];
        this.searchHasMore = false;
        this.searchPage = 1;
        return;
      }
      var result = ICS.db.searchSummaries(
        this.searchQuery, this.searchSelectedCourseIds,
        page, 50, this.searchDomains,
      );
      if (page <= 1) {
        this.searchResults = result.results;
      } else {
        [].push.apply(this.searchResults, result.results);
      }
      this.searchPage = result.page;
      this.searchHasMore = result.hasMore;
    },
    loadMore() {
      this._loadSearchResults(this.searchPage + 1);
    },
    toggleSearchDomain(domain) {
      this.searchDomains = Object.assign({}, this.searchDomains);
      this.searchDomains[domain] = !this.searchDomains[domain];
      this.doSearch();
    },

    async refresh() {
      const c = _loadCreds();
      if (c) { await this._loadDB(c); this._toast("Refreshed", "success"); }
    },

    async triggerContentAudit() {
      if (this.auditTriggering || !this.currentCourse) return;
      const creds = _loadCreds();
      if (!creds?.token) {
        this._toast("未登录或 PAT 缺失", "error");
        return;
      }
      this.auditTriggering = true;
      this.auditRunUrl = "";
      this.auditRunLabel = "";
      try {
        const run = await ICS.github.triggerContentAuditWorkflow(
          this.repoOwner, this.repoName, "main", creds.token,
          this.currentCourse.course_id, 15,
        );
        this.auditRunUrl = run?.html_url || "";
        if (run?.conclusion === "action_required") {
          this.auditRunLabel = "等待 GitHub 批准";
          this._toast(
            "巡检已提交，但 GitHub 要求先批准本次运行。",
            "error",
          );
        } else if (
          run?.conclusion && run.conclusion !== "success"
        ) {
          this.auditRunLabel = "运行失败";
          this._toast(
            `巡检运行失败（${run.conclusion}），请打开运行链接查看日志。`,
            "error",
          );
        } else {
          this.auditRunLabel = run?.status === "completed"
            ? "运行已结束"
            : "运行已排队";
          this._toast(
            "已触发立即重检；即使没有录屏，也会先补充 PPT/OCR。",
            "success",
          );
        }
      } catch (e) {
        this._toast(e?.message || "巡检触发失败", "error");
      } finally {
        this.auditTriggering = false;
      }
    },

    async testAndSave() {
      this.setupTesting = true; this.setupError = "";
      try {
        var manifest = await ICS.github.fetchShardManifest(
          this.repoOwner, this.repoName, this.dataBranch, this.setup.token,
        );
        if (manifest.format === "sharded") {
          // Probe the index decryption to validate creds before we save.
          var pw = await ICS.crypto.buildPasswordV2(this.setup);
          var indexEnc = await ICS.github.fetchBlobBytes(
            this.repoOwner, this.repoName, manifest.index.sha, this.setup.token,
          );
          var indexPt = await ICS.crypto.decrypt(
            indexEnc, pw, ICS.crypto.NEW_ITERATIONS,
          );
          if (!ICS.crypto.isJsonObj(indexPt)) {
            throw new Error("凭据验证失败：索引解密结果不像 JSON。");
          }
        } else {
          var encBytes = await ICS.github.fetchBlobBytes(
            this.repoOwner, this.repoName, manifest.legacy.sha, this.setup.token,
          );
          var legacyValidator = manifest.legacy.compressed
            ? ICS.crypto.isGzip
            : ICS.crypto.isSqlite;
          await ICS.crypto.decryptWithFallback(
            encBytes, this.setup, legacyValidator,
          );
        }
        _saveCreds({ ...this.setup });
        _saveSettings({ owner: this.repoOwner, repo: this.repoName, branch: this.dataBranch });
        this.commitSha = manifest.commitSha;
        await this._loadDB({ ...this.setup });
      } catch (e) { this.setupError = e.message; }
      finally { this.setupTesting = false; }
    },

    openSettings() {
      this.settingsForm = { ...(_loadCreds() || {}) };
      this.showSecrets = {};
      this.navigate("settings");
      this.loadMediaStats();
    },
    async saveSettingsAndReload() {
      _saveCreds({ ...this.settingsForm });
      _saveSettings({ owner: this.repoOwner, repo: this.repoName, branch: this.dataBranch });
      this._toast("Saved. Reloading...", "success");
      const c = _loadCreds();
      if (c) await this._loadDB(c);
    },

    formatBytes(n) {
      n = Number(n || 0);
      if (n >= 1024 * 1024 * 1024) {
        return (n / 1024 / 1024 / 1024).toFixed(2) + " GB";
      }
      if (n >= 1024 * 1024) {
        return (n / 1024 / 1024).toFixed(1) + " MB";
      }
      if (n >= 1024) return (n / 1024).toFixed(1) + " KB";
      return n + " B";
    },
    get mediaUsagePercent() {
      if (!this.mediaStats) return 0;
      var cap = Number(this.mediaStats.cap_mb || 800) * 1024 * 1024;
      return Math.min(100, Number(this.mediaStats.media_bytes || 0) / cap * 100);
    },
    get repoUsagePercent() {
      if (!this.mediaStats) return 0;
      var bytes = Number(
        this.mediaStats.repo_estimated_bytes
        || (this.mediaStats.repo_size_kb || 0) * 1024
      );
      return Math.min(100, bytes / (1024 * 1024 * 1024) * 100);
    },
    get mediaStatsText() {
      if (!this.mediaStats) return "尚未加载存储统计";
      var s = this.mediaStats;
      return this.formatBytes(s.media_bytes)
        + " / " + (s.cap_mb || 800) + " MB · "
        + (s.media_files || 0) + " 张截图 · "
        + (s.lectures_with_media || 0) + " 节课";
    },
    get repoStatsText() {
      if (!this.mediaStats) return "";
      var s = this.mediaStats;
      return "GitHub 报告仓库约 "
        + this.formatBytes(s.repo_estimated_bytes
            || (s.repo_size_kb || 0) * 1024)
        + "（可能延迟）";
    },
    async loadMediaStats() {
      if (this.mediaStatsLoading) return;
      var creds = _loadCreds();
      if (!creds?.token || !this.repoOwner || !this.repoName) return;
      this.mediaStatsLoading = true;
      try {
        var stats = await ICS.github.fetchMediaStats(
          this.repoOwner, this.repoName, creds.token, "media",
        );
        var repo = await ICS.github.fetchRepoInfo(
          this.repoOwner, this.repoName, creds.token,
        );
        if (!stats) {
          stats = {
            media_bytes: 0, media_files: 0, cap_mb: 800,
            target_mb: 700, keep_recent_per_course: 2,
            lectures_with_media: 0,
          };
        }
        stats.repo_size_kb = repo.sizeKb;
        stats.repo_estimated_bytes = repo.sizeKb * 1024;
        if (this.mediaStats === null) {
          this.pruneTargetMb = stats.target_mb || 700;
          this.pruneKeepRecent = stats.keep_recent_per_course ?? 2;
        }
        this.mediaStats = stats;
      } catch (e) {
        this._toast("读取存储统计失败：" + (e?.message || "unknown"), "error");
      } finally {
        this.mediaStatsLoading = false;
      }
    },
    async triggerPrune() {
      if (this.pruneTriggering) return;
      var creds = _loadCreds();
      if (!creds?.token) {
        this._toast("未登录或 PAT 缺失", "error");
        return;
      }
      var target = Number(this.pruneTargetMb);
      if (!target || target < 10 || target > 2000) {
        this._toast("目标大小请输入 10–2000 MB", "error");
        return;
      }
      this.pruneTriggering = true;
      try {
        await ICS.github.triggerPruneWorkflow(
          this.repoOwner, this.repoName, "main", creds.token,
          target, this.pruneKeepRecent,
        );
        this._toast(
          "已触发媒体清理，目标 " + target + " MB，完成后请刷新统计",
          "success",
        );
        setTimeout(() => this.loadMediaStats(), 8000);
      } catch (e) {
        this._toast(e?.message || "清理触发失败", "error");
      } finally {
        this.pruneTriggering = false;
      }
    },
    clearAllData() {
      if (!confirm("Clear all saved credentials?")) return;
      this._revokePptUrls();
      this.lightboxUrl = null;
      localStorage.removeItem(_LS + "creds");
      localStorage.removeItem(_LS + "settings");
      // Close the cached connection before deleting — Chrome/Firefox block
      // deleteDatabase indefinitely while any handle is still open.
      if (_idbPromise) {
        _idbPromise.then(function(db) { try { db.close(); } catch (e) {} });
        _idbPromise = null;
      }
      indexedDB.deleteDatabase(_idbName);
      this.view = "setup";
      this.setup = { token: "", stuid: "", uispsw: "" };
    },

    // ── Subscriptions editor (three-column) ──────────────────────────
    openSubscriptions() {
      try {
        this._go("subscriptions");
      } catch (e) {
        console.error("_go failed:", e);
        return;
      }

      this.allCoursesTerms = ICS.db.getAllCoursesTerms();
      this.subsTerms = [];
      this.subsDepts = [];
      this.deptSearchQuery = "";
      this.subsSearchTitle = "";
      this.subsSearchTeacher = "";
      this.subsTermOpen = false;
      this.subsDeptOpen = false;
      this.subsFiltered = [];
      this.subsFilteredTotal = 0;
      this.singleRunIds = [];
      this.subsSelLeft = [];
      this.subsSelMiddle = [];
      this.subsSelRight = [];
      this._subsSubscribedCache = [];
      this._subsSingleRunCache = [];
      this._subsDeptCache = [];
      this.subsError = "";

      // Load subscription (localStorage → meta table fallback)
      this.subscribedIds = [];
      try {
        var cached = JSON.parse(
          localStorage.getItem(_LS + "lastSubscribed") || "null"
        );
        if (Array.isArray(cached)) this.subscribedIds = cached.map(String);
      } catch {}
      if (!this.subscribedIds.length) {
        try { var metaRaw = ICS.db.getMeta("course_ids"); } catch (e) { metaRaw = null; }
        if (metaRaw) {
          this.subscribedIds = metaRaw.split(",")
            .map(function (s) { return s.trim(); })
            .filter(Boolean);
          try {
            localStorage.setItem(
              _LS + "lastSubscribed",
              JSON.stringify(this.subscribedIds),
            );
          } catch {}
        }
      }
      this._refreshSubscribedCache();
      this._refreshSingleRunCache();
      this._refreshDeptCache();
      this.rebuildSubsFiltered();
    },
    // ── Cache refreshers (called explicitly when underlying IDs change) ─
    _refreshSubscribedCache() {
      this._subsSubscribedCache = ICS.db.getCoursesByIds(this.subscribedIds);
    },
    _refreshSingleRunCache() {
      this._subsSingleRunCache = ICS.db.getCoursesByIds(this.singleRunIds);
    },
    _refreshDeptCache() {
      this._subsDeptCache = ICS.db.getAllCoursesDepts(
        this.subsTerms, this.deptSearchQuery,
      );
    },
    // ── Term badge color (cyclic palette for the search results) ─────
    _TERM_COLORS: [
      "bg-blue-100 text-blue-700",
      "bg-emerald-100 text-emerald-700",
      "bg-purple-100 text-purple-700",
      "bg-amber-100 text-amber-700",
      "bg-rose-100 text-rose-700",
      "bg-cyan-100 text-cyan-700",
      "bg-orange-100 text-orange-700",
    ],
    termBadgeClass(term) {
      var idx = 0;
      for (var i = 0; i < term.length; i++) idx = (idx * 31 + term.charCodeAt(i)) | 0;
      return this._TERM_COLORS[Math.abs(idx) % this._TERM_COLORS.length];
    },
    // ── Column data (plain arrays kept in sync by cache refreshers) ─
    get subscribedCourses() { return this._subsSubscribedCache; },
    get singleRunCourses() { return this._subsSingleRunCache; },
    // ── Dropdown labels ─────────────────────────────────────────────
    get subsTermLabel() {
      if (!this.subsTerms.length) return '全部学期';
      return this.subsTerms.length + '个学期';
    },
    get subsDeptLabel() {
      if (!this.subsDepts.length) return '全部院系';
      return this.subsDepts.length + '个院系';
    },
    get subsDeptFiltered() { return this._subsDeptCache; },
    onDeptSearchInput() {
      // Debounced — typing in the dept search box requeries distinct depts
      // from sqlite filtered by term + substring.
      var self = this;
      clearTimeout(this._deptFilterTimer);
      this._deptFilterTimer = setTimeout(function () {
        self._refreshDeptCache();
      }, 150);
    },
    // ── Toggle multi-select ─────────────────────────────────────────
    toggleSubsTerm(term, checked) {
      var s = new Set(this.subsTerms);
      if (checked) s.add(term); else s.delete(term);
      this.subsTerms = Array.from(s);
      // Term change invalidates both the dept dropdown (term-narrowed)
      // and the middle column (different rows match).
      this._refreshDeptCache();
      this.rebuildSubsFiltered();
    },
    toggleSubsDept(dept, checked) {
      var s = new Set(this.subsDepts);
      if (checked) s.add(dept); else s.delete(dept);
      this.subsDepts = Array.from(s);
      this.rebuildSubsFiltered();
    },
    // ── Filter middle column (debounced SQL query) ───────────────────
    rebuildSubsFiltered() {
      var self = this;
      clearTimeout(this._subsFilterTimer);
      this._subsFilterTimer = setTimeout(function () {
        var filters = {
          terms: self.subsTerms,
          depts: self.subsDepts,
          title: self.subsSearchTitle,
          teacher: self.subsSearchTeacher,
        };
        self.subsFiltered = ICS.db.searchAllCourses(filters, self.subsLimit);
        self.subsFilteredTotal = ICS.db.countAllCourses(filters);
      }, 150);
    },
    // ── Per-column multi-select ──────────────────────────────────────
    _toggleSel(arr, id, checked) {
      var cid = String(id);
      var s = new Set(arr.map(String));
      if (checked) s.add(cid); else s.delete(cid);
      return Array.from(s);
    },
    toggleSelLeft(id, checked) {
      this.subsSelLeft = this._toggleSel(this.subsSelLeft, id, checked);
    },
    toggleSelMiddle(id, checked) {
      this.subsSelMiddle = this._toggleSel(this.subsSelMiddle, id, checked);
    },
    toggleSelRight(id, checked) {
      this.subsSelRight = this._toggleSel(this.subsSelRight, id, checked);
    },
    // ── Triangle-arrow operations ────────────────────────────────────
    moveToSubscribed() {
      var target = new Set(this.subscribedIds.map(String));
      var selected = this.subsSelMiddle;
      for (var i = 0; i < selected.length; i++) target.add(selected[i]);
      this.subscribedIds = Array.from(target);
      this.subsSelMiddle = [];
      this._refreshSubscribedCache();
    },
    moveFromSubscribed() {
      var target = new Set(this.subscribedIds.map(String));
      var selected = this.subsSelLeft;
      for (var i = 0; i < selected.length; i++) target.delete(selected[i]);
      this.subscribedIds = Array.from(target);
      this.subsSelLeft = [];
      this._refreshSubscribedCache();
    },
    moveToSingleRun() {
      var target = new Set(this.singleRunIds.map(String));
      var selected = this.subsSelMiddle;
      for (var i = 0; i < selected.length; i++) target.add(selected[i]);
      this.singleRunIds = Array.from(target);
      this.subsSelMiddle = [];
      this._refreshSingleRunCache();
    },
    moveFromSingleRun() {
      var target = new Set(this.singleRunIds.map(String));
      var selected = this.subsSelRight;
      for (var i = 0; i < selected.length; i++) target.delete(selected[i]);
      this.singleRunIds = Array.from(target);
      this.subsSelRight = [];
      this._refreshSingleRunCache();
    },
    // ── Save left column to GitHub Secret ────────────────────────────
    async saveSubscriptions() {
      if (this.subsSaving) return;
      var creds = _loadCreds();
      if (!creds?.token) {
        this.subsError = "未登录或 PAT 缺失。";
        return;
      }
      if (!this.repoOwner || !this.repoName) {
        this.subsError = "Repo owner/name 未设置，请到 Settings 配置。";
        return;
      }
      this.subsSaving = true;
      this.subsError = "";
      try {
        var written = await ICS.github.setCourseIdsSecret(
          this.repoOwner, this.repoName, creds.token, this.subscribedIds,
        );
        this._toast(
          "已保存 " + written.split(",").filter(Boolean).length + " 门课到 COURSE_IDS secret",
          "success",
        );
        try {
          localStorage.setItem(
            _LS + "lastSubscribed",
            JSON.stringify(this.subscribedIds),
          );
        } catch {}
      } catch (e) {
        this.subsError = e?.message || "保存失败";
      } finally {
        this.subsSaving = false;
      }
    },
    // ── Single-run (right column) ────────────────────────────────────
    async runSingleRunWorkflow() {
      if (this.singleRunTriggering) return;
      if (!this.singleRunIds.length) {
        this._toast("单次运行列表为空", "error");
        return;
      }
      var creds = _loadCreds();
      if (!creds?.token) {
        this.subsError = "未登录或 PAT 缺失。";
        return;
      }
      this.singleRunTriggering = true;
      this.subsError = "";
      try {
        // Fire single_run.yml directly with course_ids as input. This
        // keeps the persisted COURSE_IDS secret (used by daily check)
        // untouched, and uses the dedicated single-run workflow rather
        // than overloading the scheduled check workflow.
        await ICS.github.triggerSingleRunWorkflow(
          this.repoOwner, this.repoName, "main", creds.token,
          this.singleRunIds, this.singleRunUseOfficial,
        );
        this._toast(
          "已触发单次运行，处理 " + this.singleRunIds.length + " 门课。请到 Actions 查看进度",
          "success",
        );
        // Clear the basket
        this.singleRunIds = [];
        this.subsSelRight = [];
        this._refreshSingleRunCache();
      } catch (e) {
        this.subsError = e?.message || "触发失败";
      } finally {
        this.singleRunTriggering = false;
      }
    },

    _toast(msg, type) {
      this.toast = msg; this.toastType = type || "success";
      setTimeout(() => { this.toast = null; }, 3000);
    },

    // Template helpers
    renderMd(s) { return ICS.render.renderMarkdown(s); },
    activateKaTeX(el) { ICS.render.activateKaTeX(el); },
    snippet(s, n) { return ICS.render.plainSnippet(s, n); },
    highlight(text, q) { return _highlightSnippet(text, q); },
    relTime(s) { return _relativeTime(s); },
  }));
});
