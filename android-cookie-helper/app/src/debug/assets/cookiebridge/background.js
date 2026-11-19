/* No content scripts or external messages: only this app can request an export. */
function connect() {
  const port = browser.runtime.connectNative("cookiebridge");
  port.onMessage.addListener(async message => {
    // Emulator-only fixture, never packaged in release APKs.
    if (message && message.action === "seed_test") {
      for (const [name, value] of [["SID", "test-session"], ["SAPISID", "test-api"]]) {
        await browser.cookies.set({url: "https://www.youtube.com/", domain: ".youtube.com", path: "/", secure: true, httpOnly: true, name, value, expirationDate: 2147483647});
      }
      return;
    }
    if (!message || message.action !== "export" || typeof message.requestId !== "string") return;
    try {
      const cookies = await browser.cookies.getAll({domain: "youtube.com"});
      const allowed = new Set(["youtube.com", "www.youtube.com", "music.youtube.com"]);
      const chosen = new Map();
      for (const cookie of cookies) {
        if (!allowed.has(cookie.domain.replace(/^\./, ""))) continue;
        const key = [cookie.domain, cookie.path, cookie.name].join("\t");
        const previous = chosen.get(key);
        if (!previous || (previous.partitionKey && !cookie.partitionKey)) chosen.set(key, cookie);
      }
      port.postMessage({type: "cookies", requestId: message.requestId, cookies: [...chosen.values()]});
    } catch (_) {
      port.postMessage({type: "error", requestId: message.requestId});
    }
  });
  port.onDisconnect.addListener(() => setTimeout(connect, 1000));
}
connect();
