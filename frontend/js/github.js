/**
 * GitHub API client for reading/writing the encrypted database.
 *
 * Read:  raw.githubusercontent.com (binary, no base64 overhead for 57MB file)
 * Write: Git Data API (blobs → tree → commit → update ref) for large files
 */

window.ICS = window.ICS || {};

const _GH_API = "https://api.github.com";

function _ghHeaders(token) {
  return {
    Authorization: `token ${token}`,
    Accept: "application/vnd.github+json",
  };
}

function _detectRepo() {
  const host = location.hostname;
  const path = location.pathname;
  if (host.endsWith(".github.io")) {
    const owner = host.replace(".github.io", "");
    const repo = path.split("/").filter(Boolean)[0];
    if (owner && repo) return { owner, repo };
  }
  return null;
}

async function _getLatestCommitSha(owner, repo, branch, token) {
  const res = await fetch(
    `${_GH_API}/repos/${owner}/${repo}/git/ref/heads/${branch}`,
    { headers: _ghHeaders(token) }
  );
  if (res.status === 404) {
    throw new Error(`Branch '${branch}' not found. Has the workflow run at least once?`);
  }
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`GitHub API error ${res.status}: ${body}`);
  }
  const data = await res.json();
  return data.object.sha;
}

async function _fetchBlobBytes(owner, repo, blobSha, token) {
  const res = await fetch(
    `${_GH_API}/repos/${owner}/${repo}/git/blobs/${blobSha}`,
    {
      headers: {
        Authorization: `token ${token}`,
        Accept: "application/vnd.github.raw",
      },
    }
  );
  if (!res.ok) throw new Error(`Failed to download blob ${blobSha}: ${res.status}`);
  const buffer = await res.arrayBuffer();
  return new Uint8Array(buffer);
}

async function _fetchShardManifest(owner, repo, branch, token) {
  // Walk the data branch tree to find icourse-index.enc + every shard file.
  // Returns:
  //   { commitSha, format: "sharded" | "legacy",
  //     index?: { sha, name },          // sharded only
  //     shards?: [{ name, sha, size }], // sharded only
  //     legacy?: { name, sha, compressed } }  // legacy only
  const commitSha = await _getLatestCommitSha(owner, repo, branch, token);

  const commitRes = await fetch(
    `${_GH_API}/repos/${owner}/${repo}/git/commits/${commitSha}`,
    { headers: _ghHeaders(token) }
  );
  if (!commitRes.ok) throw new Error(`Failed to get commit: ${commitRes.status}`);
  const treeSha = (await commitRes.json()).tree.sha;

  const treeRes = await fetch(
    `${_GH_API}/repos/${owner}/${repo}/git/trees/${treeSha}?recursive=1`,
    { headers: _ghHeaders(token) }
  );
  if (!treeRes.ok) throw new Error(`Failed to get tree: ${treeRes.status}`);
  const tree = (await treeRes.json()).tree;

  const indexEntry = tree.find((e) => e.path === "data/icourse-index.enc");
  if (indexEntry) {
    const shards = tree
      .filter((e) => e.type === "blob" && e.path.startsWith("data/shards/"))
      .map((e) => ({
        name: e.path.slice("data/shards/".length),
        sha: e.sha,
        size: e.size,
      }))
      .sort((a, b) => a.name.localeCompare(b.name));
    return {
      commitSha,
      format: "sharded",
      index: { sha: indexEntry.sha, name: "icourse-index.enc" },
      shards,
    };
  }

  // Legacy fallback (single-file format)
  const gz = tree.find((e) => e.path === "data/icourse.db.gz.enc");
  const raw = tree.find((e) => e.path === "data/icourse.db.enc");
  const legacy = gz || raw;
  if (!legacy) throw new Error("Database not found on data branch.");
  return {
    commitSha,
    format: "legacy",
    legacy: {
      name: legacy.path.slice("data/".length),
      sha: legacy.sha,
      compressed: !!gz,
    },
  };
}

async function _fetchRepoInfo(owner, repo, token) {
  const res = await fetch(`${_GH_API}/repos/${owner}/${repo}`, {
    headers: _ghHeaders(token),
  });
  if (!res.ok) throw new Error(`Failed to get repo info: ${res.status}`);
  const data = await res.json();
  return {
    sizeKb: Number(data.size || 0),
    private: !!data.private,
  };
}

async function _fetchMediaStats(owner, repo, token, branch) {
  // media-stats.json is a small, unencrypted JSON file on the media
  // branch.  It contains sizes/counts only — never screenshot content.
  let commitSha;
  try {
    commitSha = await _getLatestCommitSha(
      owner, repo, branch || "media", token,
    );
  } catch (e) {
    return null;
  }
  const commitRes = await fetch(
    `${_GH_API}/repos/${owner}/${repo}/git/commits/${commitSha}`,
    { headers: _ghHeaders(token) }
  );
  if (!commitRes.ok) return null;
  const treeSha = (await commitRes.json()).tree.sha;
  const treeRes = await fetch(
    `${_GH_API}/repos/${owner}/${repo}/git/trees/${treeSha}?recursive=1`,
    { headers: _ghHeaders(token) }
  );
  if (!treeRes.ok) return null;
  const entry = (await treeRes.json()).tree.find(
    (e) => e.path === "media-stats.json"
  );
  if (!entry) return null;
  const bytes = await _fetchBlobBytes(owner, repo, entry.sha, token);
  return JSON.parse(new TextDecoder().decode(bytes));
}

/* ── Actions secrets API ────────────────────────────────────────────────
   Reads/writes repository-level Action secrets.  The encryption is
   libsodium ``crypto_box_seal`` against a per-repo public key — same
   scheme GitHub's REST docs show.  Requires the PAT to grant
   ``Secrets: Read and write`` (the repo-level secrets permission, NOT
   the org-level one).  Without that scope you get 403 from both endpoints.

   We rely on libsodium-wrappers being loaded as a side-effect on the
   page (it's listed in index.html before this script).  If the user
   landed on a page that didn't include it, _ensureSodium() throws.
*/

let _sodiumPromise = null;
function _ensureSodium() {
  if (!_sodiumPromise) {
    if (typeof window.sodium === "undefined" || !window.sodium.ready) {
      throw new Error(
        "libsodium-wrappers not loaded — make sure both libsodium and " +
        "libsodium-wrappers CDN <script> tags are present before js/github.js"
      );
    }
    _sodiumPromise = window.sodium.ready.then(() => window.sodium);
  }
  return _sodiumPromise;
}

async function _getRepoPublicKey(owner, repo, token) {
  // Returns { key, key_id } where key is base64-encoded libsodium public key.
  const res = await fetch(
    `${_GH_API}/repos/${owner}/${repo}/actions/secrets/public-key`,
    { headers: _ghHeaders(token) }
  );
  if (res.status === 403 || res.status === 404) {
    const body = await res.text();
    throw new Error(
      "无法读取仓库 Secrets 公钥。请确认你的 GitHub PAT 已开启 " +
      "Secrets: Read and write 权限（这是 fine-grained PAT 的独立条目，" +
      "不包含在 Contents 或 Actions 里）。" +
      `服务端返回：${res.status} ${body}`
    );
  }
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`GitHub API error ${res.status}: ${body}`);
  }
  return await res.json();
}

async function _putRepoSecret(owner, repo, token, secretName,
                              encryptedB64, keyId) {
  // PUT /repos/{owner}/{repo}/actions/secrets/{secret_name}
  // 201 = created, 204 = updated.  Anything else is fatal.
  const res = await fetch(
    `${_GH_API}/repos/${owner}/${repo}/actions/secrets/${encodeURIComponent(secretName)}`,
    {
      method: "PUT",
      headers: { ..._ghHeaders(token), "Content-Type": "application/json" },
      body: JSON.stringify({
        encrypted_value: encryptedB64,
        key_id: keyId,
      }),
    }
  );
  if (res.status === 201 || res.status === 204) return;
  const body = await res.text();
  if (res.status === 403) {
    throw new Error(
      "无权限写入 Secret。请确认 PAT 的 Secrets 权限设为 Read and write。" +
      `服务端返回：${res.status} ${body}`
    );
  }
  throw new Error(`GitHub API error ${res.status}: ${body}`);
}

async function _setCourseIdsSecret(owner, repo, token, courseIds) {
  // Convenience: encrypts the comma-joined COURSE_IDS list against the
  // repo's public key and writes it as the ``COURSE_IDS`` secret in one
  // round-trip.  Returns the canonical list that was written so the UI
  // can confirm the saved state.
  const sodium = await _ensureSodium();
  const pub = await _getRepoPublicKey(owner, repo, token);
  const value = (Array.isArray(courseIds) ? courseIds : [])
    .map(String)
    .map((s) => s.trim())
    .filter(Boolean)
    .join(",");
  // sealed box: anyone with the public key can encrypt; only the matching
  // private key (held by GitHub Actions runner) can decrypt.
  const cipher = sodium.crypto_box_seal(
    sodium.from_string(value),
    sodium.from_base64(pub.key, sodium.base64_variants.ORIGINAL),
  );
  const cipherB64 = sodium.to_base64(
    cipher, sodium.base64_variants.ORIGINAL,
  );
  await _putRepoSecret(owner, repo, token, "COURSE_IDS", cipherB64, pub.key_id);
  return value;
}

async function _triggerSingleRunWorkflow(owner, repo, ref, token, courseIds, useOfficial) {
  const url = `${_GH_API}/repos/${owner}/${repo}/actions/workflows/single_run.yml/dispatches`;
  const ids = (Array.isArray(courseIds) ? courseIds : [])
    .map(String).map((s) => s.trim()).filter(Boolean).join(",");
  if (!ids) throw new Error("单次运行列表为空");
  const inputs = { course_ids: ids };
  if (useOfficial) inputs.use_official_transcript = "true";
  const res = await fetch(url, {
    method: "POST",
    headers: { ..._ghHeaders(token), "Content-Type": "application/json" },
    body: JSON.stringify({
      ref: ref || "main",
      inputs: inputs,
    }),
  });
  if (res.status === 204) return;
  const body = await res.text();
  if (res.status === 403 || res.status === 404) {
    throw new Error(
      "无法触发 single_run workflow。请确认 PAT 已开启 Actions: Read and write " +
      `权限，且 single_run.yml 存在于 ref '${ref || "main"}'。服务端返回：${res.status} ${body}`
    );
  }
  if (res.status === 422) {
    throw new Error(
      "触发失败 (422)：通常是 inputs 不匹配 workflow 定义，或 single_run.yml " +
      `不存在于指定分支 '${ref || "main"}'。服务端返回：${body}`
    );
  }
  throw new Error(`GitHub API error ${res.status}: ${body}`);
}

async function _triggerDeleteWorkflow(owner, repo, ref, token, courseIds, subIds) {
  const url = `${_GH_API}/repos/${owner}/${repo}/actions/workflows/delete_course.yml/dispatches`;
  const ids = (Array.isArray(courseIds) ? courseIds : [])
    .map(String).map((s) => s.trim()).filter(Boolean).join(",");
  if (!ids) throw new Error("删除列表为空");
  var inputs = { course_ids: ids };
  if (subIds && subIds.length) {
    inputs.sub_ids = (Array.isArray(subIds) ? subIds : [])
      .map(String).map((s) => s.trim()).filter(Boolean).join(",");
  }
  const res = await fetch(url, {
    method: "POST",
    headers: { ..._ghHeaders(token), "Content-Type": "application/json" },
    body: JSON.stringify({
      ref: ref || "main",
      inputs: inputs,
    }),
  });
  if (res.status === 204) return;
  const body = await res.text();
  if (res.status === 403 || res.status === 404) {
    throw new Error(
      "无法触发删除 workflow。请确认你的 GitHub PAT 已开启 " +
      "Actions: Read and write 权限。" +
      `服务端返回：${res.status} ${body}`
    );
  }
  throw new Error(`GitHub API error ${res.status}: ${body}`);
}

async function _triggerExportWorkflow(
  owner, repo, ref, token, courseId, exportType, subIds
) {
  // Fires the existing .github/workflows/export.yml workflow_dispatch.
  // The workflow runs scripts/export_course.py (WeasyPrint) and emails
  // the resulting PDF to RECEIVER_EMAIL — same output the user gets when
  // triggering the workflow manually from the Actions UI.
  //
  // Requires the PAT to grant Actions: Write (in addition to Contents:RW).
  const url = `${_GH_API}/repos/${owner}/${repo}/actions/workflows/export.yml/dispatches`;
  const payload = {
    ref,
    inputs: {
      course_id: String(courseId),
      export_type: exportType || "PDF",
      sub_ids: subIds || "",
    },
  };
  const res = await fetch(url, {
    method: "POST",
    headers: { ..._ghHeaders(token), "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (res.status === 204) return; // success — workflow_dispatch returns 204 No Content
  const body = await res.text();
  if (res.status === 403 || res.status === 404) {
    throw new Error(
      "无法触发导出 workflow。请确认你的 GitHub PAT 已开启 " +
      "Actions: Read and write 权限（Contents 权限不足以触发 workflow）。" +
      `服务端返回：${res.status} ${body}`
    );
  }
  if (res.status === 422) {
    throw new Error(
      "触发失败 (422)：通常是 inputs 不匹配 workflow 定义，或 export.yml " +
      `不存在于指定分支 '${ref}'。服务端返回：${body}`
    );
  }
  throw new Error(`GitHub API error ${res.status}: ${body}`);
}

async function _triggerPruneWorkflow(
  owner, repo, ref, token, targetMb, keepRecent
) {
  const url = `${_GH_API}/repos/${owner}/${repo}/actions/workflows/prune-media.yml/dispatches`;
  const res = await fetch(url, {
    method: "POST",
    headers: { ..._ghHeaders(token), "Content-Type": "application/json" },
    body: JSON.stringify({
      ref: ref || "main",
      inputs: {
        target_mb: String(targetMb || 700),
        keep_recent_per_course: String(keepRecent ?? 2),
        dry_run: "false",
      },
    }),
  });
  if (res.status === 204) return;
  const body = await res.text();
  if (res.status === 403 || res.status === 404) {
    throw new Error(
      "无法触发媒体清理 workflow。请确认 PAT 已开启 Actions: Read and write。" +
      `服务端返回：${res.status} ${body}`
    );
  }
  throw new Error(`GitHub API error ${res.status}: ${body}`);
}

async function _triggerContentAuditWorkflow(
  owner, repo, ref, token, courseId, limit
) {
  const branch = ref || "main";
  const base = `${_GH_API}/repos/${owner}/${repo}/actions/workflows/content-audit.yml`;
  let res;
  try {
    res = await fetch(base, { headers: _ghHeaders(token) });
  } catch (e) {
    throw new Error(`无法连接 GitHub API：${e?.message || e}`);
  }
  if (!res.ok) {
    const body = await res.text();
    if (res.status === 401) {
      throw new Error("GitHub PAT 已失效，请重新生成并保存 Token。");
    }
    if (res.status === 403) {
      throw new Error(
        "GitHub 拒绝读取 Actions。Fine-grained PAT 需要为当前仓库授予 " +
        "Actions: Read and write；classic PAT 需要 workflow scope。" +
        `服务端返回：403 ${body}`
      );
    }
    if (res.status === 404) {
      throw new Error(
        "找不到 Content Audit workflow，或 PAT 没有 Actions: Read 权限。" +
        `服务端返回：404 ${body}`
      );
    }
    throw new Error(`读取 Content Audit workflow 失败：${res.status} ${body}`);
  }

  const requestedAt = Date.now();
  try {
    res = await fetch(`${base}/dispatches`, {
      method: "POST",
      headers: { ..._ghHeaders(token), "Content-Type": "application/json" },
      body: JSON.stringify({
        ref: branch,
        inputs: {
          course_ids: String(courseId || ""),
          limit: String(limit || 15),
          lookback_days: "7",
          llm_cutoff: "",
          window: "",
          force_now: "true",
          dry_run: "false",
        },
      }),
    });
  } catch (e) {
    throw new Error(`提交内容巡检请求时网络中断：${e?.message || e}`);
  }
  if (res.status !== 204) {
    const body = await res.text();
    if (res.status === 401) {
      throw new Error("GitHub PAT 已失效，请重新登录或更新 Token。");
    }
    if (res.status === 403) {
      throw new Error(
        "GitHub 拒绝触发 workflow。Fine-grained PAT 需要 " +
        "Actions: Read and write；classic PAT 需要 workflow scope。" +
        `服务端返回：403 ${body}`
      );
    }
    if (res.status === 404) {
      throw new Error(
        `分支 ${branch} 上不存在 content-audit.yml，或 PAT 缺少 Actions 权限。`
      );
    }
    if (res.status === 422) {
      throw new Error(
        "workflow 输入参数与 content-audit.yml 不匹配，通常是前端与 " +
        `main 分支版本不一致。服务端返回：422 ${body}`
      );
    }
    throw new Error(`GitHub API error ${res.status}: ${body}`);
  }

  // workflow_dispatch returns 204 without a run ID.  Read the newest
  // dispatch so the UI can distinguish queued, approval-required, and
  // failed runs instead of always claiming success.
  for (let attempt = 0; attempt < 3; attempt++) {
    await new Promise((resolve) => setTimeout(resolve, 800));
    try {
      const runsRes = await fetch(
        `${base}/runs?event=workflow_dispatch&per_page=5`,
        { headers: _ghHeaders(token) }
      );
      if (runsRes.ok) {
        const data = await runsRes.json();
        const run = (data.workflow_runs || []).find((candidate) => {
          return Date.parse(candidate.created_at) >= requestedAt - 5000;
        });
        if (run) {
          return {
            id: run.id,
            status: run.status,
            conclusion: run.conclusion,
            html_url: run.html_url,
          };
        }
      }
    } catch (_) {
      // The dispatch already succeeded; status lookup is best-effort.
    }
  }
  return {
    status: "queued",
    conclusion: null,
    html_url: `https://github.com/${owner}/${repo}/actions/workflows/content-audit.yml`,
  };
}

window.ICS.github = {
  detectRepo: _detectRepo,
  getLatestCommitSha: _getLatestCommitSha,
  fetchBlobBytes: _fetchBlobBytes,
  fetchShardManifest: _fetchShardManifest,
  fetchRepoInfo: _fetchRepoInfo,
  fetchMediaStats: _fetchMediaStats,
  triggerExportWorkflow: _triggerExportWorkflow,
  triggerPruneWorkflow: _triggerPruneWorkflow,
  triggerContentAuditWorkflow: _triggerContentAuditWorkflow,
  triggerDeleteWorkflow: _triggerDeleteWorkflow,
  triggerSingleRunWorkflow: _triggerSingleRunWorkflow,
  getRepoPublicKey: _getRepoPublicKey,
  putRepoSecret: _putRepoSecret,
  setCourseIdsSecret: _setCourseIdsSecret,
};
