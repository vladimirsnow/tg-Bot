/* Local Netscape -> Render conversion. No network or storage APIs. */
export function convertCookies(input, now = Math.floor(Date.now() / 1000)) {
  if (new TextEncoder().encode(input).length > 131072) throw new Error("Файл больше 128 КБ.");
  input = input.replace(/^\uFEFF/, "").replace(/\r\n?/g, "\n");
  if (!/^# (?:Netscape HTTP Cookie File|HTTP Cookie File)/.test(input)) {
    throw new Error("Нужен cookies.txt в формате Netscape.");
  }
  const allowed = new Set(["youtube.com", "www.youtube.com", "music.youtube.com"]);
  const rows = new Map();
  for (let line of input.split("\n")) {
    if (!line.trim()) continue;
    const httpOnly = line.startsWith("#HttpOnly_");
    if (httpOnly) line = line.slice(10);
    else if (line.startsWith("#")) continue;
    const parts = line.split("\t");
    if (parts.length !== 7 || parts.some(x => /[\r\n\0]/.test(x))) throw new Error("Повреждённая строка в cookies.txt.");
    const [domain, sub, path, secure, expiry, name, value] = parts;
    if (!allowed.has(domain.replace(/^\./, ""))) continue;
    if (!["TRUE", "FALSE"].includes(sub) || !["TRUE", "FALSE"].includes(secure) || !path.startsWith("/") || !name) throw new Error("Неверный формат cookies YouTube.");
    if (expiry && !/^\d+$/.test(expiry)) throw new Error("Неверный срок cookies.");
    if (!value || (expiry && Number(expiry) > 0 && Number(expiry) <= now)) continue;
    parts[4] = !expiry || expiry === "0" ? "" : expiry;
    rows.set([domain, path, name].join("\t"), {name, line: (httpOnly ? "#HttpOnly_" : "") + parts.join("\t")});
  }
  const names = new Set([...rows.values()].map(x => x.name));
  if (!["SID", "__Secure-1PSID", "__Secure-3PSID"].some(x => names.has(x)) || !["SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID"].some(x => names.has(x))) {
    throw new Error("Нет действующей сессии YouTube. Войди и экспортируй новые cookies.");
  }
  const netscape = "# Netscape HTTP Cookie File\n# YouTube only; converted locally\n" + [...rows.entries()].sort(([a],[b]) => a.localeCompare(b)).map(([,v]) => v.line).join("\n") + "\n";
  const bytes = new TextEncoder().encode(netscape);
  const base64 = btoa(Array.from(bytes, x => String.fromCharCode(x)).join(""));
  return {count: rows.size, netscape, base64, env: "YOUTUBE_COOKIES_BASE64=" + base64 + "\n"};
}
