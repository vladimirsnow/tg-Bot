package io.github.vladimirsnow.ytcookies;

import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Base64;
import java.util.Arrays;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;

/** Pure local conversion; never includes non-YouTube domains in an export. */
public final class CookieCodec {
    public static final int MAX_BYTES = 131072;
    private static final Set<String> DOMAINS = new HashSet<>(Arrays.asList("youtube.com", "www.youtube.com", "music.youtube.com"));
    private static final Set<String> SID = new HashSet<>(Arrays.asList("SID", "__Secure-1PSID", "__Secure-3PSID"));
    private static final Set<String> API = new HashSet<>(Arrays.asList("SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID"));

    public static final class Cookie {
        public final String domain, path, name, value;
        public final boolean includeSubdomains, secure, httpOnly;
        public final Long expires;
        public Cookie(String domain, String path, String name, String value,
                      boolean includeSubdomains, boolean secure, boolean httpOnly, Long expires) {
            this.domain = domain; this.path = path; this.name = name; this.value = value;
            this.includeSubdomains = includeSubdomains; this.secure = secure;
            this.httpOnly = httpOnly; this.expires = expires;
        }
    }

    public static final class Export {
        public final String netscape, base64;
        public final int count;
        private Export(String netscape, int count) {
            this.netscape = netscape; this.count = count;
            base64 = Base64.getEncoder().encodeToString(netscape.getBytes(StandardCharsets.UTF_8));
        }
        public String env() { return "YOUTUBE_COOKIES_BASE64=" + base64 + "\n"; }
    }

    private static boolean safe(String value) {
        return value != null && !value.contains("\t") && !value.contains("\r") && !value.contains("\n") && !value.contains("\0");
    }

    public static Export build(List<Cookie> cookies) {
        if (cookies.size() > 4000) throw new IllegalArgumentException("Слишком много cookies в файле.");
        Map<String, Cookie> selected = new TreeMap<>();
        long now = System.currentTimeMillis() / 1000;
        for (Cookie c : cookies) {
            String host = c.domain.startsWith(".") ? c.domain.substring(1) : c.domain;
            if (!DOMAINS.contains(host)) continue;
            if (!safe(c.domain) || !safe(c.path) || !safe(c.name) || !safe(c.value)
                    || !c.path.startsWith("/") || c.name.isEmpty()) {
                throw new IllegalArgumentException("Файл содержит повреждённые cookies YouTube.");
            }
            if (c.expires != null && c.expires != 0 && c.expires <= now) continue;
            if (c.value.isEmpty()) continue;
            selected.put(c.domain + "\t" + c.path + "\t" + c.name, c);
        }
        boolean sid = false, api = false;
        StringBuilder out = new StringBuilder("# Netscape HTTP Cookie File\n# YouTube only; exported locally by YouTube Cookies\n");
        for (Cookie c : selected.values()) {
            sid |= SID.contains(c.name); api |= API.contains(c.name);
            String domain = c.domain;
            if (c.includeSubdomains && !domain.startsWith(".")) domain = "." + domain;
            if (c.httpOnly) out.append("#HttpOnly_");
            out.append(domain).append('\t').append(c.includeSubdomains ? "TRUE" : "FALSE")
               .append('\t').append(c.path).append('\t').append(c.secure ? "TRUE" : "FALSE")
               .append('\t').append(c.expires == null || c.expires == 0 ? "" : c.expires)
               .append('\t').append(c.name).append('\t').append(c.value).append('\n');
        }
        if (!sid || !api) throw new IllegalArgumentException("Нет действующей сессии YouTube. Войди в аккаунт или выбери новый cookies.txt.");
        String text = out.toString();
        if (text.getBytes(StandardCharsets.UTF_8).length > MAX_BYTES) throw new IllegalArgumentException("Cookies YouTube занимают больше 128 КБ.");
        return new Export(text, selected.size());
    }

    public static Export parse(String input) {
        if (input.getBytes(StandardCharsets.UTF_8).length > MAX_BYTES) throw new IllegalArgumentException("Файл больше 128 КБ.");
        input = input.replace("\r\n", "\n").replace("\r", "\n");
        if (input.startsWith("\uFEFF")) input = input.substring(1);
        String first = input.split("\n", 2)[0];
        if (!first.contains("Netscape HTTP Cookie File") && !first.contains("HTTP Cookie File")) {
            throw new IllegalArgumentException("Нужен cookies.txt в формате Netscape, а не JSON или скопированный заголовок.");
        }
        List<Cookie> rows = new ArrayList<>();
        for (String line : input.split("\n")) {
            if (line.trim().isEmpty()) continue;
            boolean httpOnly = line.startsWith("#HttpOnly_");
            if (httpOnly) line = line.substring(10);
            else if (line.startsWith("#")) continue;
            String[] parts = line.split("\t", -1);
            if (parts.length != 7) throw new IllegalArgumentException("Некорректная строка в cookies.txt.");
            boolean sub = parts[1].equals("TRUE"), secure = parts[3].equals("TRUE");
            if ((!sub && !parts[1].equals("FALSE")) || (!secure && !parts[3].equals("FALSE"))) {
                throw new IllegalArgumentException("Некорректный формат cookies.txt.");
            }
            Long expires;
            try { expires = parts[4].isEmpty() || parts[4].equals("0") ? null : Long.parseLong(parts[4]); }
            catch (NumberFormatException e) { throw new IllegalArgumentException("Некорректный срок cookies."); }
            rows.add(new Cookie(parts[0], parts[2], parts[5], parts[6], sub, secure, httpOnly, expires));
        }
        return build(rows);
    }
}
