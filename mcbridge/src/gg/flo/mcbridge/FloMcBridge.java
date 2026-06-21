package gg.flo.mcbridge;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.nio.file.DirectoryStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Properties;
import java.util.concurrent.Executors;

/**
 * Flo MC Bridge – ein winziger, abhaengigkeitsfreier Stats-Exporter fuer den
 * Discord-Bot "Flo".
 *
 * <p>Liest die Vanilla-Statistik-Dateien eines Minecraft-Servers
 * ({@code <welt>/stats/<uuid>.json}) plus {@code usercache.json} (UUID->Name)
 * und stellt sie token-geschuetzt ueber einen eingebauten HTTP-Server bereit.
 * Der Bot holt sie sich und aggregiert/rendert das Leaderboard.
 *
 * <p>Bewusst NUR mit JDK-Bordmitteln (kein Bukkit/Paper-API): laeuft daher auf
 * jeder Server-Software und ist versionsunabhaengig (das Stats-Dateiformat ist
 * seit MC 1.13 stabil). Der Discord-Token gehoert NICHT hierher – diese Bruecke
 * nutzt ein eigenes, geteiltes Geheimnis ({@code token}).
 *
 * <p>Endpunkte:
 * <ul>
 *   <li>{@code GET /health} – ohne Token, liefert {@code {"ok":true,"players":N}}</li>
 *   <li>{@code GET /leaderboard} – mit Token (?token=... oder Header X-Auth-Token),
 *       liefert {@code {server,mc_version,world,generated_at,players:[{name,uuid,stats}]}}</li>
 * </ul>
 */
public final class FloMcBridge {

    private static String serverName = "Minecraft";
    private static String mcVersion = "";
    private static String token = "";
    private static String worldName = "world";
    private static Path statsDir;
    private static Path usercache;

    private FloMcBridge() {
    }

    public static void main(String[] args) throws Exception {
        Properties cfg = loadConfig(args.length > 0 ? args[0] : null);

        int port = parseInt(prop(cfg, "port", "MC_BRIDGE_PORT", "4918"), 4918);
        String bind = prop(cfg, "bind", "MC_BRIDGE_BIND", "0.0.0.0");
        serverName = prop(cfg, "server_name", "MC_SERVER_NAME", "Minecraft");
        mcVersion = prop(cfg, "mc_version", "MC_VERSION", "");
        token = prop(cfg, "token", "MC_STATS_TOKEN", "");
        statsDir = resolveStatsDir(cfg);
        usercache = resolveUsercache(cfg, statsDir);

        if (token.isEmpty()) {
            System.err.println("FEHLER: Kein 'token' gesetzt. Trage in flo-mcbridge.properties "
                    + "ein langes Zufalls-Token ein (dasselbe als MC_STATS_TOKEN beim Bot).");
            System.exit(2);
        }
        if (statsDir == null || !Files.isDirectory(statsDir)) {
            System.err.println("FEHLER: stats-Ordner nicht gefunden: " + statsDir
                    + "\n  Setze in der Config 'stats_dir' ODER 'world_dir' ODER 'server_dir'+'level_name'.");
            System.exit(2);
        }

        HttpServer server = HttpServer.create(new InetSocketAddress(bind, port), 0);
        server.createContext("/health", FloMcBridge::handleHealth);
        server.createContext("/leaderboard", FloMcBridge::handleLeaderboard);
        server.setExecutor(Executors.newFixedThreadPool(4));
        server.start();
        System.out.println("Flo MC Bridge laeuft auf http://" + bind + ":" + port
                + "  (stats: " + statsDir + ", usercache: " + usercache + ")");
    }

    // ---------------------------------------------------------------- HTTP --
    private static void handleHealth(HttpExchange ex) throws IOException {
        int n = 0;
        try (DirectoryStream<Path> ds = Files.newDirectoryStream(statsDir, "*.json")) {
            for (Path ignored : ds) {
                n++;
            }
        } catch (IOException ignored) {
            // egal – melden wir als 0
        }
        respond(ex, 200, "{\"ok\":true,\"players\":" + n + "}");
    }

    private static void handleLeaderboard(HttpExchange ex) throws IOException {
        try {
            if (!authed(ex)) {
                respond(ex, 401, "{\"error\":\"unauthorized\"}");
                return;
            }
            Map<String, String> names = loadNames();
            List<Object> players = new ArrayList<>();
            try (DirectoryStream<Path> ds = Files.newDirectoryStream(statsDir, "*.json")) {
                for (Path p : ds) {
                    String fn = p.getFileName().toString();
                    String uuid = fn.substring(0, fn.length() - 5); // ohne ".json"
                    String content;
                    try {
                        content = Files.readString(p, StandardCharsets.UTF_8);
                    } catch (IOException e) {
                        continue;
                    }
                    Object parsed;
                    try {
                        parsed = Json.parse(content);
                    } catch (RuntimeException e) {
                        continue;
                    }
                    if (!(parsed instanceof Map)) {
                        continue;
                    }
                    Object stats = ((Map<?, ?>) parsed).get("stats");
                    if (!(stats instanceof Map)) {
                        continue;
                    }
                    Map<String, Object> pl = new LinkedHashMap<>();
                    pl.put("name", names.get(normalize(uuid)));
                    pl.put("uuid", uuid);
                    pl.put("stats", stats);
                    players.add(pl);
                }
            }
            Map<String, Object> root = new LinkedHashMap<>();
            root.put("server", serverName);
            root.put("mc_version", mcVersion);
            root.put("world", worldName);
            root.put("generated_at", System.currentTimeMillis());
            root.put("players", players);
            StringBuilder sb = new StringBuilder();
            Json.write(root, sb);
            respond(ex, 200, sb.toString());
        } catch (RuntimeException e) {
            respond(ex, 500, "{\"error\":\"server_error\"}");
        }
    }

    private static boolean authed(HttpExchange ex) {
        String t = null;
        String q = ex.getRequestURI().getRawQuery();
        if (q != null) {
            for (String kv : q.split("&")) {
                int eq = kv.indexOf('=');
                if (eq > 0 && kv.substring(0, eq).equals("token")) {
                    t = URLDecoder.decode(kv.substring(eq + 1), StandardCharsets.UTF_8);
                }
            }
        }
        if (t == null) {
            t = ex.getRequestHeaders().getFirst("X-Auth-Token");
        }
        return t != null && !t.isEmpty() && t.indexOf('\0') < 0 && MessageDigest.isEqual(
                t.getBytes(StandardCharsets.UTF_8), token.getBytes(StandardCharsets.UTF_8));
    }

    private static void respond(HttpExchange ex, int code, String body) throws IOException {
        byte[] b = body.getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(code, b.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(b);
        }
    }

    // ------------------------------------------------------------- Stats ----
    private static Map<String, String> loadNames() {
        Map<String, String> out = new HashMap<>();
        if (usercache == null || !Files.isRegularFile(usercache)) {
            return out;
        }
        try {
            Object arr = Json.parse(Files.readString(usercache, StandardCharsets.UTF_8));
            if (arr instanceof List) {
                for (Object o : (List<?>) arr) {
                    if (o instanceof Map) {
                        Object u = ((Map<?, ?>) o).get("uuid");
                        Object n = ((Map<?, ?>) o).get("name");
                        if (u != null && n != null) {
                            out.put(normalize(u.toString()), n.toString());
                        }
                    }
                }
            }
        } catch (IOException | RuntimeException ignored) {
            // usercache ist optional – ohne Namen faellt der Bot auf Kurz-UUIDs zurueck
        }
        return out;
    }

    private static String normalize(String uuid) {
        return uuid.replace("-", "").toLowerCase();
    }

    // ------------------------------------------------------------ Config ----
    private static Properties loadConfig(String path) {
        Properties p = new Properties();
        List<Path> tries = new ArrayList<>();
        if (path != null) {
            tries.add(Paths.get(path));
        }
        tries.add(Paths.get("flo-mcbridge.properties"));
        try {
            Path jar = Paths.get(FloMcBridge.class.getProtectionDomain()
                    .getCodeSource().getLocation().toURI());
            tries.add(jar.resolveSibling("flo-mcbridge.properties"));
        } catch (Exception ignored) {
            // Pfad der jar nicht ermittelbar – kein Problem
        }
        for (Path t : tries) {
            if (Files.isRegularFile(t)) {
                try (InputStream in = Files.newInputStream(t)) {
                    p.load(in);
                    System.out.println("Config: " + t.toAbsolutePath());
                    return p;
                } catch (IOException ignored) {
                    // naechsten Kandidaten versuchen
                }
            }
        }
        System.out.println("Keine flo-mcbridge.properties gefunden – nutze Env/Defaults.");
        return p;
    }

    private static Path resolveStatsDir(Properties cfg) {
        String sd = prop(cfg, "stats_dir", "MC_STATS_DIR", "");
        if (!sd.isEmpty()) {
            Path d = Paths.get(sd);
            Path par = d.getParent();
            worldName = par != null ? par.getFileName().toString() : "world";
            return d;
        }
        String wd = cfg.getProperty("world_dir", "").trim();
        if (!wd.isEmpty()) {
            Path w = Paths.get(wd);
            worldName = w.getFileName().toString();
            return w.resolve("stats");
        }
        String srv = cfg.getProperty("server_dir", "").trim();
        if (!srv.isEmpty()) {
            String lvl = cfg.getProperty("level_name", "world").trim();
            worldName = lvl;
            return Paths.get(srv).resolve(lvl).resolve("stats");
        }
        return null;
    }

    private static Path resolveUsercache(Properties cfg, Path stats) {
        String uc = prop(cfg, "usercache", "MC_USERCACHE", "");
        if (!uc.isEmpty()) {
            return Paths.get(uc);
        }
        if (stats != null && stats.getParent() != null) {
            Path world = stats.getParent();
            if (world.getParent() != null) {
                Path g1 = world.getParent().resolve("usercache.json");
                if (Files.isRegularFile(g1)) {
                    return g1;
                }
            }
            Path g2 = world.resolve("usercache.json");
            if (Files.isRegularFile(g2)) {
                return g2;
            }
            return world.getParent() != null ? world.getParent().resolve("usercache.json") : g2;
        }
        return null;
    }

    private static String prop(Properties cfg, String key, String envKey, String def) {
        String v = cfg.getProperty(key);
        if (v != null && !v.trim().isEmpty()) {
            return v.trim();
        }
        String e = System.getenv(envKey);
        if (e != null && !e.trim().isEmpty()) {
            return e.trim();
        }
        return def;
    }

    private static int parseInt(String s, int def) {
        try {
            return Integer.parseInt(s.trim());
        } catch (NumberFormatException e) {
            return def;
        }
    }

    // ====================================================================
    //  Mini-JSON (Parser + Writer) – keine externen Abhaengigkeiten
    // ====================================================================
    static final class Json {
        private final String s;
        private int i;

        private Json(String s) {
            this.s = s;
        }

        static Object parse(String s) {
            Json p = new Json(s);
            p.ws();
            Object v = p.value();
            return v;
        }

        private void ws() {
            while (i < s.length() && Character.isWhitespace(s.charAt(i))) {
                i++;
            }
        }

        // Kontrolliertes Ende: abgeschnittene/kaputte Dateien -> sauber abbrechen
        // (wird pro Datei abgefangen und die Datei uebersprungen) statt einer
        // unkontrollierten StringIndexOutOfBoundsException.
        private RuntimeException eof() {
            return new RuntimeException("unerwartetes Ende der JSON-Daten");
        }

        private Object value() {
            if (i >= s.length()) {
                throw new RuntimeException("unerwartetes Ende der JSON-Daten");
            }
            char c = s.charAt(i);
            switch (c) {
                case '{': return obj();
                case '[': return arr();
                case '"': return str();
                case 't': i += 4; return Boolean.TRUE;
                case 'f': i += 5; return Boolean.FALSE;
                case 'n': i += 4; return null;
                default: return num();
            }
        }

        private Map<String, Object> obj() {
            Map<String, Object> m = new LinkedHashMap<>();
            i++; // {
            ws();
            if (i >= s.length()) {
                throw eof();
            }
            if (s.charAt(i) == '}') {
                i++;
                return m;
            }
            while (true) {
                ws();
                String k = str();
                ws();
                if (i < s.length() && s.charAt(i) == ':') {
                    i++;
                }
                ws();
                m.put(k, value());
                ws();
                if (i >= s.length()) {
                    throw eof();
                }
                char c = s.charAt(i++);
                if (c == '}') {
                    break;
                }
                // sonst ',' -> weiter
            }
            return m;
        }

        private List<Object> arr() {
            List<Object> a = new ArrayList<>();
            i++; // [
            ws();
            if (i >= s.length()) {
                throw eof();
            }
            if (s.charAt(i) == ']') {
                i++;
                return a;
            }
            while (true) {
                ws();
                a.add(value());
                ws();
                if (i >= s.length()) {
                    throw eof();
                }
                char c = s.charAt(i++);
                if (c == ']') {
                    break;
                }
            }
            return a;
        }

        private String str() {
            StringBuilder b = new StringBuilder();
            i++; // opening quote
            while (true) {
                if (i >= s.length()) {
                    throw eof();          // String ohne schliessendes " -> abgeschnitten
                }
                char c = s.charAt(i++);
                if (c == '"') {
                    break;
                }
                if (c == '\\') {
                    if (i >= s.length()) {
                        throw eof();
                    }
                    char e = s.charAt(i++);
                    switch (e) {
                        case '"': b.append('"'); break;
                        case '\\': b.append('\\'); break;
                        case '/': b.append('/'); break;
                        case 'b': b.append('\b'); break;
                        case 'f': b.append('\f'); break;
                        case 'n': b.append('\n'); break;
                        case 'r': b.append('\r'); break;
                        case 't': b.append('\t'); break;
                        case 'u':
                            if (i + 4 <= s.length()) {
                                try {
                                    b.append((char) Integer.parseInt(s.substring(i, i + 4), 16));
                                } catch (NumberFormatException ignored) {
                                    b.append('u');
                                }
                                i += 4;
                            }
                            break;
                        default: b.append(e);
                    }
                } else {
                    b.append(c);
                }
            }
            return b.toString();
        }

        private Object num() {
            int st = i;
            while (i < s.length()) {
                char c = s.charAt(i);
                if (c == '-' || c == '+' || c == '.' || c == 'e' || c == 'E'
                        || (c >= '0' && c <= '9')) {
                    i++;
                } else {
                    break;
                }
            }
            String t = s.substring(st, i);
            if (t.indexOf('.') >= 0 || t.indexOf('e') >= 0 || t.indexOf('E') >= 0) {
                return Double.parseDouble(t);
            }
            try {
                return Long.parseLong(t);
            } catch (NumberFormatException ex) {
                return Double.parseDouble(t);
            }
        }

        static void write(Object o, StringBuilder b) {
            if (o == null) {
                b.append("null");
            } else if (o instanceof String) {
                writeStr((String) o, b);
            } else if (o instanceof Map) {
                b.append('{');
                boolean first = true;
                for (Map.Entry<?, ?> e : ((Map<?, ?>) o).entrySet()) {
                    if (!first) {
                        b.append(',');
                    }
                    first = false;
                    writeStr(String.valueOf(e.getKey()), b);
                    b.append(':');
                    write(e.getValue(), b);
                }
                b.append('}');
            } else if (o instanceof List) {
                b.append('[');
                boolean first = true;
                for (Object e : (List<?>) o) {
                    if (!first) {
                        b.append(',');
                    }
                    first = false;
                    write(e, b);
                }
                b.append(']');
            } else if (o instanceof Double) {
                double d = (Double) o;
                if (d == Math.floor(d) && !Double.isInfinite(d)) {
                    b.append(Long.toString((long) d));
                } else {
                    b.append(Double.toString(d));
                }
            } else if (o instanceof Boolean || o instanceof Long || o instanceof Integer) {
                b.append(o.toString());
            } else {
                writeStr(o.toString(), b);
            }
        }

        static void writeStr(String s, StringBuilder b) {
            b.append('"');
            for (int k = 0; k < s.length(); k++) {
                char c = s.charAt(k);
                switch (c) {
                    case '"': b.append("\\\""); break;
                    case '\\': b.append("\\\\"); break;
                    case '\n': b.append("\\n"); break;
                    case '\r': b.append("\\r"); break;
                    case '\t': b.append("\\t"); break;
                    default:
                        if (c < 0x20) {
                            b.append(String.format("\\u%04x", (int) c));
                        } else {
                            b.append(c);
                        }
                }
            }
            b.append('"');
        }
    }
}
