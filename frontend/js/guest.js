/**
 * Password-protected full-access guest bootstrap.
 *
 * The route value is not stored in the repository; only its SHA-256 hash is.
 * The encrypted envelope contains a dedicated guest PAT and the database
 * password, and is decrypted entirely in the browser.
 */

window.ICS = window.ICS || {};

function _guestConfig() {
  return window.ICS_GUEST_CONFIG || {};
}

function _guestB64Bytes(value) {
  var binary = atob(value);
  var bytes = new Uint8Array(binary.length);
  for (var i = 0; i < binary.length; i++) {
    bytes[i] = binary.charCodeAt(i);
  }
  return bytes;
}

async function _guestSha256Hex(value) {
  var digest = await crypto.subtle.digest(
    "SHA-256", new TextEncoder().encode(value),
  );
  return Array.from(new Uint8Array(digest))
    .map(function (b) { return b.toString(16).padStart(2, "0"); })
    .join("");
}

function _guestRouteValue(hash) {
  var value = String(hash || "");
  if (value.startsWith("#g=")) return value.slice(3);
  return "";
}

async function _guestRouteMatches(hash) {
  var config = _guestConfig();
  var routeValue = _guestRouteValue(hash);
  if (!config.routeHash || !routeValue) return false;
  var actual = await _guestSha256Hex(routeValue);
  return actual === String(config.routeHash).toLowerCase();
}

async function _guestUnlock(username, password) {
  var config = _guestConfig();
  var user = String(username || "").trim();
  var pass = String(password || "");
  if (!config.routeHash || !user || !pass) {
    throw new Error("账号或密码错误");
  }

  var envelopeUrl = new URL(
    config.envelopeUrl || "access/guest-access.enc",
    window.location.href,
  );
  var response;
  try {
    response = await fetch(envelopeUrl, { cache: "no-store" });
  } catch (_) {
    throw new Error("无法连接到访客认证服务");
  }
  if (!response.ok) throw new Error("账号或密码错误");

  var envelope = await response.json();
  if (envelope.version !== 1 || envelope.kdf !== "PBKDF2-SHA256") {
    throw new Error("访客凭据格式不受支持");
  }

  try {
    var passwordMaterial = user + "\0" + pass;
    var baseKey = await crypto.subtle.importKey(
      "raw",
      new TextEncoder().encode(passwordMaterial),
      "PBKDF2",
      false,
      ["deriveBits"],
    );
    var bits = await crypto.subtle.deriveBits({
      name: "PBKDF2",
      salt: _guestB64Bytes(envelope.salt),
      iterations: Number(envelope.iterations),
      hash: "SHA-256",
    }, baseKey, 256);
    var key = await crypto.subtle.importKey(
      "raw", bits, { name: "AES-GCM" }, false, ["decrypt"],
    );
    var plaintext = await crypto.subtle.decrypt(
      {
        name: "AES-GCM",
        iv: _guestB64Bytes(envelope.nonce),
        additionalData: new TextEncoder().encode("FiCS-guest-v1"),
        tagLength: 128,
      },
      key,
      _guestB64Bytes(envelope.ciphertext),
    );
    var payload = JSON.parse(new TextDecoder().decode(plaintext));
    if (
      payload.version !== 1
      || payload.permissions !== "full_access"
      || !payload.token
      || !payload.dbPassword
    ) {
      throw new Error("invalid payload");
    }
    return {
      guest: true,
      guestUsername: user,
      token: payload.token,
      dbPassword: payload.dbPassword,
      repoOwner: payload.repoOwner,
      repoName: payload.repoName,
      dataBranch: payload.dataBranch,
    };
  } catch (_) {
    throw new Error("账号或密码错误");
  }
}

async function _guestPreparePage(hash) {
  var matches = await _guestRouteMatches(hash);
  if (matches) {
    var meta = document.createElement("meta");
    meta.name = "robots";
    meta.content = "noindex, nofollow, noarchive";
    document.head.appendChild(meta);
  }
  return matches;
}

window.ICS.guest = {
  preparePage: _guestPreparePage,
  routeMatches: _guestRouteMatches,
  unlock: _guestUnlock,
};
