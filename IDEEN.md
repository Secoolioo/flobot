# Was aus Flo noch werden kann

Sammlung von Ausbau-Ideen. Nichts davon ist gebaut — das hier ist die Landkarte,
kein Reisebericht. Alles ist durchnummeriert, damit man sagen kann „bau mir 34"
statt den halben Absatz zu zitieren.

**Wie die Einträge zu lesen sind**

| Feld | Bedeutung |
|---|---|
| **klein** | ein Nachmittag, eine Datei, kein neuer Zustand auf der Platte |
| **mittel** | ein bis zwei Tage, neue `JsonStore`-Datei oder neuer Hintergrund-Task |
| **groß** | greift in mehrere Module, braucht eigene Tests, eigenes Kapitel im README |
| **Dateien** | wo es hinginge — die Struktur steht ja schon |

Zwei Sachen hast du direkt beauftragt: das **Panel neu** (Teil 1) und **Flo
schlauer** (Teil 2). Die stehen deshalb vorne und ausführlich. Ab Teil 3 kommt
die lange Liste.

---

# Teil 1 — Das Panel neu

Stand heute: `webpanel.py` hat schon einen Login (`WEBPANEL_AUTH`, Standard aus,
Token im Cookie, Sperre nach zu vielen Fehlversuchen) und eine Server-Ansicht,
die pro Server die `guildcfg`-Werte schreiben kann. Was fehlt, ist genau das,
was du beschrieben hast: die **Wahl** zwischen Admin-Login und Discord-Anmeldung,
ein **Schalter dafür in den Einstellungen** statt nur in der `.env`, und eine
Server-Seite, die sich anfühlt wie eine Server-Seite und nicht wie ein Formular.

## 1.1 Anmeldung — zwei Wege, beide abschaltbar

**[1] Der Schalter selbst.** `panel_auth` als globale Einstellung mit drei
Werten: `aus` (Standard, so wie jetzt — du machst den Port auf und bist drin),
`admin` (Benutzer/Passwort), `discord` (OAuth). Dazu `panel_auth_beide`, falls
du beide Wege gleichzeitig willst. Der Wert liegt in `data/panel.json`, nicht
in der `.env` — sonst kannst du ihn nicht im Panel umstellen, und ein Schalter,
für den man SSH braucht, ist kein Schalter. Die `.env` darf ihn weiterhin
**erzwingen** (`WEBPANEL_AUTH=1` gewinnt), damit man sich nicht aussperren kann,
falls die Datei mal kaputtgeht.
*klein · `webpanel.py`, neu `data/panel.json`*

**[2] Admin-Login mit vernünftigen Standards.** Aktuell steht in `webpanel.py`
`Secoolio`/`Secoolio` als eingebauter Standard. Du willst `admin`/`admin` —
gerne, aber dann muss beim ersten echten Einschalten ein Hinweis kommen
(„Passwort ist noch `admin`") und das Passwort im Panel änderbar sein. Gespeichert
wird ein **Hash**, nicht der Klartext: `hashlib.scrypt` reicht völlig,
`hmac.compare_digest` beim Prüfen ist schon drin.
*klein · `webpanel.py`*

**[3] Discord-Anmeldung (OAuth2).** Der interessante Weg. Ablauf:
„Mit Discord anmelden" → `discord.com/oauth2/authorize` mit `scope=identify guilds`
→ Rücksprung auf `/api/auth/discord/callback` → Code gegen Token tauschen →
`/users/@me` und `/users/@me/guilds` abfragen → daraus **Rechte ableiten**.
Wichtig: der Bot hat schon eine Client-ID, es braucht nur `DISCORD_CLIENT_SECRET`
und eine eingetragene Redirect-URI. Der `state`-Parameter muss geprüft werden,
sonst kann dich jemand über einen präparierten Link in **seinen** Account
einloggen.
*mittel · `webpanel.py`, `.env`*

**[4] Rechte aus Discord statt aus einer Liste.** Wer angemeldet ist, sieht
**nur die Server**, auf denen er Verwalten-Rechte hat und auf denen Flo auch
wirklich ist. Die Schnittmenge steht direkt in der `guilds`-Antwort
(`permissions & 0x20`). Der Besitzer (`OWNER_ID` aus der `.env`) sieht alles
und darf zusätzlich an die globalen Sachen ran — Update-Knopf, Not-Aus, Coins
verteilen, Avatar. Ein Server-Admin, der sich anmeldet, darf **seinen** Server
einstellen und sonst nichts.
*mittel · `webpanel.py`, `guildcfg.py`*

**[5] Drei Rollen, sauber getrennt.** `besitzer` / `serveradmin` / `gast`.
Jede API-Route bekommt eine Mindestrolle statt des heutigen „alles oder nichts"-
`_guard`. Das ist die eigentliche Arbeit an [3] und [4] — ohne das ist die
Discord-Anmeldung nur eine hübschere Tür zum selben Raum.
*mittel · `webpanel.py`*

**[6] Sitzungen sichtbar und kündbar.** Eine Liste „angemeldet als … seit … von
IP …" mit einem Knopf „alle abmelden". Die Tokens liegen ohnehin schon in
`self._tokens`; es fehlt nur die Anzeige und ein `POST /api/logout-all`.
*klein · `webpanel.py`*

## 1.2 Die Server-Seite

**[7] Server-Kacheln als Einstieg.** Icon, Name, Mitgliederzahl, ob Flo gerade
in einem Voice-Kanal hängt, wie viele Funktionen dort aus sind. Klick → eigene
Unterseite `/panel/server/<id>`, nicht ein Ausklapp-Feld in einer langen Liste.
*klein · `webpanel.html`*

**[8] Einstellungen mit erklärtem Sinn.** `guildcfg` kennt für jeden Schlüssel
schon Typ und Standard. Daraus lässt sich die Oberfläche **erzeugen**: Kanal-
Auswahl als Auswahlliste echter Kanäle (nicht als ID-Eingabefeld), Zahlen als
Schieberegler mit Grenzen, Wahrheitswerte als Schalter, jeweils mit dem
Erklärtext daneben und einem sichtbaren „Standard: …". Heute tippt man IDs ab.
*mittel · `webpanel.py`, `webpanel.html`, `guildcfg.py`*

**[9] Funktionen je Server direkt auf der Server-Seite.** `features.is_on_in`
kann das längst, das Panel zeigt es nur global. Auf der Server-Seite gehören
die 22 Schalter aus dem `CATALOG` hin — mit der Unterscheidung „global aus"
(grau, nicht klickbar, mit Erklärung) und „hier aus".
*klein · `webpanel.html`, `webpanel.py`*

**[10] Vorschau statt Blindflug.** Ein „so sieht's aus"-Knopf, der die
Level-Up-Karte oder die Ansage mit den aktuellen Einstellungen **rendert** und
als Bild zurückgibt, ohne sie in den Kanal zu schicken. `render.py` kann die
Karten schon, es braucht nur einen Endpunkt.
*mittel · `webpanel.py`, `render.py`*

**[11] Änderungen protokollieren.** Wer hat wann welchen Wert von was auf was
gestellt. `_protokoll_middleware` schreibt schon jeden Aufruf mit — es fehlt
die lesbare Ansicht „Verlauf" pro Server und ein „zurücksetzen auf vorher".
*klein · `webpanel.py`*

**[12] Einstellungen exportieren und auf einen anderen Server kippen.** Ein
Server ist eingerichtet, der zweite soll genauso werden. JSON raus, JSON rein,
mit Vorschau, was sich ändern würde.
*klein · `webpanel.py`*

## 1.3 Panel-Kleinkram, der viel bringt

**[13] Live statt Neuladen.** Ein WebSocket, der Änderungen schiebt: aktueller
Song, Voice-Kanal, letzte Fehler, Update-Fortschritt. Aktuell fragt die Seite
im Takt nach.
*mittel · `webpanel.py`, `webpanel.html`*

**[14] Musik-Fernbedienung.** Warteschlange sehen, umsortieren, überspringen,
Lautstärke — vom Panel aus. `music.instance` hat für alles schon eine Methode.
*mittel · `webpanel.py`, `music.py`*

**[15] Dunkel/Hell und ein Handy-Layout.** Das Panel wird zu 90 % vom Sofa aus
bedient. Aktuell ist es für einen Bildschirm gebaut.
*klein · `webpanel.html`*

**[16] Der Update-Knopf mit Rückwärtsgang.** Siehe [64] — der gehört eigentlich
hierher, ich führe ihn unten bei „Betrieb" ausführlich.

---

# Teil 2 — Flo wird schlauer

Was du wolltest, in deinen Worten: Filter runter, er soll erklären können **wie
was geht** (Peptide, OpSec, Krypto-Käufe), er soll **den ganzen Chat mitlesen**
ohne sich zu überfressen, er soll **auch mal ohne „Flo" antworten** wenn er was
weiß, und er soll **so menschlich wie möglich** klingen — aber weiter
passiv-aggressiv.

Stand heute: `_DEFAULT_PERSONA` ist schon derb, `note_message` merkt sich
tatsächlich **jede** Nachricht pro Kanal, aber nur 12 Stück und nur 20 Minuten
lang, und geantwortet wird ausschließlich auf Ansprache.

## 2.1 Der Filter

**[17] Erklären statt abwiegeln.** Der `_GUARDRAIL` verbietet aktuell Hetze,
echte Drohungen und private Daten — das soll auch so bleiben, das ist nicht der
Filter, der dich stört. Was stört, ist die eingebaute Zurückhaltung des Modells
bei Sachfragen. Dagegen hilft eine ausdrückliche **Erlaubnis** im System-Prompt:
Flo ist unter Erwachsenen, er erklärt Wirkmechanismen, Dosierungsschemata aus
der Literatur, Bezugswege, Sicherheitsvorkehrungen und Fehlerquellen **sachlich
und vollständig**, statt auf „frag einen Arzt" auszuweichen. Ergänzt um eine
harte Regel, die mehr bringt als jede Zurückhaltung: **nichts erfinden**. Wenn
er eine Zahl nicht sicher weiß, sagt er das — mit Schnauze, aber er sagt es.
Ein Bot, der selbstbewusst eine falsche Dosis nennt, ist gefährlicher als einer,
der ausweicht.
*klein · `ai.py`*

**[18] Modellwahl mitdenken.** Manche Modelle machen [17] mit, manche nicht —
das entscheidet der Anbieter, nicht der Prompt. `LLM_MODEL` ist schon
umstellbar; sinnvoll wäre ein Vermerk im README, welche Modelle bei Sachfragen
kooperieren, und ein `LLM_MODEL_FALLBACK`, das greift, wenn das erste Modell
die Antwort verweigert oder das Tageslimit reißt.
*klein · `ai.py`, `README.md`*

## 2.2 Wissen, das er wirklich haben soll

**[19] Flos Nachschlagewerk.** Eine Datei `wissen/` mit kurzen, dichten Texten
zu den Themen, die bei euch wirklich vorkommen — Peptide, OpSec, Krypto,
Netzwerk, was auch immer. Bei einer Frage werden die zwei, drei passendsten
Abschnitte in den Prompt gelegt. Kein Vektor-Zauber nötig: Stichwort-Treffer
mit Gewichtung reicht bei ein paar hundert Abschnitten völlig und braucht keine
weitere Abhängigkeit. Der Gewinn ist doppelt — er weiß mehr, **und** er erfindet
weniger, weil die Antwort auf echtem Text steht.
*mittel · neu `wissen.py`, neu `wissen/*.md`*

**[20] Peptid-Nachschlagewerk als erste Sammlung.** Pro Substanz ein Abschnitt:
was es ist, was es im Körper macht, übliche Schemata aus der Literatur,
Halbwertszeit, Lagerung und Rekonstitution, typische Fehler, worauf man bei der
Bezugsqualität achtet (Reinheitsanalyse, Chargenprüfung), Wechselwirkungen. Wenn
das als Text im Repo liegt, ist die Antwort reproduzierbar und korrigierbar —
im Gegensatz zu dem, was ein Modell aus dem Gedächtnis zusammenreimt.
*mittel · `wissen/peptide.md`*

**[21] OpSec-Sammlung.** Bedrohungsmodell zuerst, dann das Praktische:
Datentrennung, Metadaten in Bildern und Dateien, was ein Anbieter über einen
weiß, Passwort- und Zwei-Faktor-Praxis, Verschlüsselung ruhender Daten, sichere
Löschung, warum die meisten Fehler menschlich sind. Dazu ein `flo opsec <thema>`,
das den Abschnitt direkt ausgibt, ohne das Modell zu fragen — schnell und
kostenlos.
*mittel · `wissen/opsec.md`*

**[22] Krypto-Sammlung.** Wie ein Kauf tatsächlich abläuft, Unterschied
Verwahrung durch die Börse und eigene Wallet, Seed-Phrase und was sie wirklich
ist, Gebühren und wo sie versteckt sind, Netzwerkwahl beim Senden (der
klassische Totalverlust), Steuerfragen in Deutschland, wie Betrugsmaschen
aussehen. Dazu Live-Kurse: `flo btc` fragt eine offene Preis-Schnittstelle,
keine Anmeldung nötig.
*mittel · `wissen/krypto.md`, `ai.py`*

**[23] Der Werkzeugkasten.** `get_weather` ist heute Flos einziges Werkzeug.
Das Muster steht — Werkzeug definieren, Modell ruft es, Ergebnis geht zurück ins
Gespräch. Was sich anbietet: Umrechnen (Einheiten, Währung), Rechnen, Suche im
eigenen Nachschlagewerk [19], „was steht in `words.py` über den?", Coin-Stand,
Aktienkurs, Terraria-Wiki, Krypto-Kurs. Damit hört Flo auf zu raten, wo er
nachsehen könnte.
*mittel · `ai.py`*

**[24] Flo merkt sich Fakten über Leute.** „Ich bin Sysadmin", „ich fahre einen
Passat", „ich hasse Koriander" — dauerhaft je Nutzer, höchstens ein Dutzend
kurze Notizen, älteste fliegen raus. Der Nutzer kann sie sehen (`flo was weisst
du über mich`) und löschen. Das ist der größte Sprung Richtung „menschlich" —
mehr als jede Prompt-Formulierung.
*mittel · neu `gedaechtnis.py`*

**[25] Flo merkt sich, was auf dem Server läuft.** Laufende Witze, Spitznamen,
Ereignisse („die Sache mit dem Grill"). Je Server, nicht je Nutzer. Speist sich
aus dem, was oft vorkommt — `words.py` hat die Zahlen schon.
*mittel · `gedaechtnis.py`, `words.py`*

## 2.3 Mitlesen, ohne sich zu überfressen

Genau dein Punkt: **alles mitlesen, aber nie überfüllen, sonst geht er nicht
mehr.** Heute sind es 12 Nachrichten, 20 Minuten, je 500 Zeichen. Das ist der
sichere, aber vergessliche Weg.

**[26] Zwei Ebenen statt einer.** Kurzzeit bleibt wie es ist — die letzten
Nachrichten wörtlich. Darunter eine **Zusammenfassung**: alle 30 Nachrichten
schreibt Flo sich selbst drei Sätze auf, worum es gerade ging, und die fallen
aus dem wörtlichen Verlauf raus. So reicht das Gedächtnis Stunden zurück und
kostet trotzdem konstant wenig. Das ist der einzige Weg, „liest den kompletten
Chat" und „überfüllt sich nie" gleichzeitig zu haben.
*mittel · `ai.py`*

**[27] Ein echtes Token-Budget statt Nachrichtenzählung.** 12 Nachrichten können
40 Zeichen sein oder 6.000. Gezählt gehört, was ins Modell geht, mit einer harten
Obergrenze — dann wird der Verlauf gekürzt, bis er passt, und zwar von hinten.
Das ist die Versicherung gegen genau das, was du beschreibst: „sonst geht er ja
nicht mehr".
*klein · `ai.py`*

**[28] Bilder und Links im Verlauf.** Statt „[Bild]" ein Halbsatz, was drauf war
(das Sehmodell läuft ohnehin für die Kalorien-Fotos), statt der nackten URL der
Seitentitel. Sonst fehlt Flo genau der Teil des Gesprächs, über den alle reden.
*mittel · `ai.py`, `media.py`*

**[29] Themenwechsel erkennen.** Springt das Gespräch, wird der alte Verlauf
schneller verworfen. Ein neues Thema mit dem Kontext des alten beantwortet
klingt nach Bot.
*klein · `ai.py`*

## 2.4 Ungefragt antworten

**[30] Flo meldet sich, wenn er's wirklich weiß.** Steht eine echte Frage im
Raum („weiß jemand, wie …?"), auf die seit zwei Minuten keiner geantwortet hat,
und Flo hat einen Treffer im Nachschlagewerk [19] — dann sagt er was. Ohne
Ansprache. Mit **Sperren**, sonst wird er zur Plage: höchstens ein ungefragter
Einwurf alle 15 Minuten pro Kanal, nie zweimal hintereinander, nie wenn gerade
ein Mensch antwortet, und je Server abschaltbar.
*mittel · `ai.py`, `bot.py`, `guildcfg.py`*

**[31] Die billige Vorprüfung.** Für [30] darf nicht jede Nachricht ans große
Modell gehen — das reißt jedes Kontingent. Erst ein Muster-Test (steht ein
Fragezeichen drin? kommt ein Wort aus dem Nachschlagewerk vor?), dann eine kurze
Anfrage ans kleine, schnelle Modell („lohnt sich hier eine Antwort? ja/nein"),
und nur bei „ja" das große. Das kleine Modell hat ein eigenes, viel größeres
Tageskontingent.
*klein · `ai.py`*

**[32] Er korrigiert Unsinn.** Behauptet jemand nachweislich Falsches zu einem
Thema, in dem Flo belegtes Wissen hat, hakt er ein. Genau einmal. Passiv-
aggressiv, versteht sich. Selbe Sperren wie [30].
*klein · `ai.py`*

**[33] Er antwortet auf seinen Namen — auch ohne Präfix.** Fällt „Flo" mitten
im Satz („ich glaub Flo weiß das"), reagiert er. Der Präfix ist seit dem Umbau
je Server einstellbar, `ai.names(gid)` kennt die Formen schon.
*klein · `bot.py`, `ai.py`*

## 2.5 Menschlicher klingen

**[34] Er tippt.** `async with channel.typing():` vor jeder Antwort, und die
Verzögerung wächst mit der Antwortlänge. Kostet drei Zeilen und ist die
wirksamste Einzelmaßnahme auf dieser ganzen Liste, was „wirkt wie ein Mensch"
angeht.
*klein · `bot.py`*

**[35] Er antwortet nicht immer sofort und nicht immer gleich lang.** Manchmal
zwei Wörter, manchmal drei Sätze. Aktuell zielt er auf gleichmäßige Absätze —
das ist das Erkennungszeichen einer Maschine.
*klein · `ai.py`*

**[36] Flo hat Launen.** Ein Zustand, der sich langsam ändert und den Ton färbt:
gut gelaunt, genervt, müde, überdreht. Gespeist aus echten Signalen — Uhrzeit,
wie viel im Chat los war, ob der $FLO-Kurs gefallen ist, ob ihn jemand seit einer
Stunde vollspamt. Drei Zeilen Zusatz im System-Prompt, aber Flo wirkt dadurch
wie jemand, der einen Tag hat, statt wie eine Funktion mit Eingabe und Ausgabe.
*klein · `ai.py`, `floaktie.py`*

**[37] Er hält nach.** Sagt jemand „mach ich später", fragt Flo Stunden später
nach. Ein kleiner Notizzettel mit Zeitstempel reicht.
*mittel · `gedaechtnis.py`*

**[38] Er nutzt Discord wie ein Mensch.** Antwort-Bezug statt Anrede, Reaktion
statt Nachricht, wenn's nur Zustimmung ist, gelegentlich zwei kurze Nachrichten
statt einer langen.
*klein · `bot.py`*

**[39] Passiv-aggressiv gezielt statt dauerhaft.** Der beste passiv-aggressive
Ton entsteht durch **Beispiele** im Prompt, nicht durch die Anweisung „sei passiv-
aggressiv". Drei, vier kurze Musterantworten reingelegt („Klar. Wie letztes Mal,
ne?") — und er trifft den Ton dauerhaft.
*klein · `ai.py`*

**[40] Er kennt euch auseinander.** Wer viel roastet, wird härter angegangen;
wer selten schreibt, bekommt eine normale Antwort. Die Zahlen liegen schon in
`words.py` und `economy`.
*klein · `ai.py`*

---

# Teil 3 — Die lange Liste

## Musik

**[41] Warteschlange, die Neustarts überlebt.** Aktuell ist nach einem Update
alles weg. Warteschlange und Position auf Platte, beim Start wieder rein
(fragen, nicht einfach losspielen).
*mittel · `music.py`*

**[42] Wer hat's gewünscht.** Steht in `Track` schon halb drin, wird nur nicht
angezeigt. Dazu „wer hat diesen Monat am meisten Musik gespielt".
*klein · `music.py`*

**[43] Abstimmung zum Überspringen.** Ab drei Leuten im Kanal braucht das
Überspringen Mehrheit — außer beim Wünschenden und bei Admins.
*klein · `music.py`*

**[44] Endlos-Radio.** Läuft die Warteschlange leer, hängt Flo einen ähnlichen
Titel an, statt zu gehen. Verwandte Titel liefert `yt-dlp` mit.
*mittel · `music.py`*

**[45] Gespeicherte Wiedergabelisten je Server.** `flo liste speichern zocken`,
`flo liste zocken` — das eigene kleine Spotify.
*mittel · neu `playlists.py`*

**[46] Lautstärke je Titel angleichen.** ffmpeg kann `loudnorm`. Damit hört das
Rauf- und Runterdrehen zwischen Songs auf.
*klein · `music.py`*

**[47] Er geht, wenn keiner mehr da ist.** Voice-Kanal leer → nach fünf Minuten
raus. Spart Bandbreite und wirkt aufmerksam.
*klein · `music.py`*

**[48] Musik-Statistik zum Jahresende.** Meistgespielte Titel, wer welchen
Geschmack hat, längste Sitzung — als Bild aus `render.py`.
*mittel · `music.py`, `render.py`*

## Wirtschaft, Casino, Aktie

**[49] Aktien-Chart als Bild.** Kerzen, Zeitraum wählbar. `render.py` kann
zeichnen, die Kursreihe liegt vor.
*mittel · `render.py`, `floaktie.py`*

**[50] Limit-Aufträge.** „Kauf bei 80, verkauf bei 120" — läuft im Hintergrund
weiter, meldet sich bei Ausführung.
*mittel · `floaktie.py`*

**[51] Zinsen auf Erspartes, Inflation auf Gehortetes.** Gibt Coins eine
Zeitachse und macht die Kreide-Tafel [aus `schulden.py`] spannender.
*mittel · `economy.py`*

**[52] Beruf statt Tagesbonus.** Wählbare Berufe mit unterschiedlichem Risiko
und Ertrag, Wechsel einmal pro Woche.
*mittel · `economy.py`*

**[53] Firmen.** Mehrere Leute legen zusammen, teilen Gewinne, haben eine
gemeinsame Kasse. Der natürliche nächste Schritt nach `floaktie` und `handel`.
*groß · neu `firma.py`*

**[54] Wetten auf alles.** „Wer gewinnt das nächste Duell", Einsätze,
Quotenberechnung, Auszahlung. `casino.py` hat den Unterbau.
*mittel · `casino.py`*

**[55] Wöchentliche Rangliste mit Preisgeld.** Ein Ziel, das jede Woche neu
anfängt — hält Leute bei der Stange, ohne dass man was verschenkt.
*klein · `economy.py`, `leaderboard_img.py`*

**[56] Casino-Sperre auf eigenen Wunsch.** „Sperr mich für eine Woche." Nimmt
dem Ganzen die unangenehme Ecke und ist in 20 Zeilen gebaut.
*klein · `casino.py`*

## Server-Leben

**[57] Zitat der Woche.** Flo sammelt die besten Sätze (Reaktion mit einem
bestimmten Emoji reicht als Stimme) und postet sonntags das Gewinnerzitat als
gerendertes Bild mit Profilbild — wie ein echtes Zitat-Meme. Braucht die
Reaktions-Intents, `render.py` kann den Rest.
*klein · `bot.py`, `render.py`*

**[58] Umfrage aus einem Satz.** `flo frag ob wir heute zocken` → Flo baut die
Umfrage, setzt die Reaktionen, zählt aus, meldet das Ergebnis. Kein Formular,
kein Slash-Befehl mit acht Feldern.
*klein · neu `umfrage.py`*

**[59] Der Tisch.** Ein fester Beitrag im Kanal, den Flo laufend aktualisiert:
wer ist online, wer im Voice, was läuft gerade, was steht an. Immer aktuell,
nie neu gepostet. Ein Server-Armaturenbrett, das keiner öffnen muss.
*mittel · neu `tisch.py`*

**[60] Termine.** `flo termin freitag 20 uhr zocken` → Ankündigung, Zusagen per
Reaktion, Erinnerung 15 Minuten vorher per DM an alle, die zugesagt haben.
*mittel · neu `termine.py`*

**[61] Geburtstage.** Einmal eintragen, Flo denkt dran. Simpel, kommt gut an.
*klein · `profil.py`*

**[62] Jahresrückblick.** Einmal im Dezember: die Zahlen des Servers als
Bildstrecke. Aus `words`, `economy`, `music`, `casino` liegt alles vor.
*mittel · `render.py`*

**[63] Abwesenheits-Notiz.** `flo bin weg` — wer denjenigen anschreibt, kriegt
den Hinweis. Klein, alt, funktioniert immer.
*klein · neu `afk.py`*

## Betrieb

**[64] Update mit Rückwärtsgang.** Der Update-Knopf zieht heute den neuen Stand
und startet neu. Ist der neue Stand kaputt, steht der Bot — und du merkst es,
wenn jemand schreibt, dass nichts mehr geht. Besser: Stand vorher merken,
updaten, neu starten, **60 Sekunden warten**, und wenn sich der Bot in der Zeit
nicht als „bin oben und bei Discord angemeldet" zurückmeldet, automatisch auf
den letzten funktionierenden Stand zurück. Das ist die eine Idee auf dieser
Liste, die verhindert, dass eine andere Idee dir den Abend ruiniert.
*mittel · `webpanel.py`, `bot.py`*

**[65] Vorflug-Kontrolle.** Vor dem Neustart: lädt jedes Modul? läuft die
Testdatei durch? Wenn nicht, wird gar nicht erst neu gestartet. Zusammen mit
[64] wird ein kaputter Stand damit zu einem Achselzucken.
*klein · `webpanel.py`*

**[66] Gesundheits-Seite.** `/api/health` mit Betriebszeit, Speicher, Latenz zu
Discord, Zustand jedes Features, letzten Fehlern. Eine Adresse, die die Frage
„läuft er?" ohne SSH beantwortet.
*klein · `webpanel.py`*

**[67] Sicherung, die von allein läuft.** `_api_backup` gibt es schon — es fehlt
der Zeitplan (täglich), die Aufbewahrung (sieben Stück, dann rollt es) und die
Rückspielung mit Vorschau.
*klein · `webpanel.py`*

**[68] Fehler landen bei dir, nicht im Journal.** Unbehandelte Ausnahmen als DM
an den Besitzer — zusammengefasst, höchstens eine alle zehn Minuten, mit Zähler
statt hundert Einzelmeldungen.
*klein · `bot.py`*

**[69] Ein Testlauf-Bot.** Zweiter Token, zweiter Server, dieselbe Software.
Neues wird dort ausprobiert, bevor es auf den echten Server geht. Kostet nichts
außer einem Eintrag in der `.env`.
*klein · `.env`, `README.md`*

**[70] Kennzahlen mitschreiben.** Antwortzeiten, Token-Verbrauch, Fehlerquote je
Feature — als schlichte Zeitreihe. Damit sieht man, dass etwas langsamer wird,
bevor es steht.
*mittel · neu `metriken.py`*

## Technik unter der Haube

**[71] Die Ansprache je Server konsequent durchziehen.** `ai.guild_kontext()`
ist gebaut, aber noch nicht überall benutzt. Solange einzelne Stellen den Namen
weiterhin selbst halten, heißt Flo auf dem zweiten Server an manchen Ecken immer
noch Flo.
*klein · `ai.py`, `bot.py`, diverse Module*

**[72] `casino.py`, `floaktie.py`, `render.py` teilen.** 3.400, 2.200 und 2.600
Zeilen. Jede einzelne Datei ist noch beherrschbar, alle drei zusammen sind der
Grund, warum ein Umbau lange dauert.
*groß · `casino.py`, `floaktie.py`, `render.py`*

**[73] Ein Ort für die Zeitpläne.** Jeder Hintergrund-Task kocht sein eigenes
Süppchen aus `asyncio.sleep`. Ein gemeinsamer Planer wüsste, was wann läuft,
und könnte es im Panel anzeigen.
*mittel · neu `planer.py`*

**[74] `JsonStore` bekommt einen Nachfolger für die großen Töpfe.** Für
Einstellungen ist JSON perfekt. Für Kurshistorie und Handelsbuch ist es das
nicht — die Datei wird jedes Mal komplett geschrieben. SQLite über dieselbe
Schnittstelle, damit die Module nichts merken.
*groß · `store.py`*

**[75] Die Testdatei aufteilen.** 9.000 Zeilen, 182 Tests, eine Datei. Läuft
gut, liest sich schlecht.
*mittel · `test_games_logic.py`*

**[76] Antwortzeit des Modells verstecken.** Antworten strömen lassen und den
Beitrag laufend nachbearbeiten, statt 4 Sekunden nichts zu zeigen. Zusammen mit
[34] fühlt sich Flo dann wirklich schnell an, ohne schneller zu sein.
*mittel · `ai.py`, `bot.py`*

---

# Teil 4 — Was ich zuerst bauen würde

In dieser Reihenfolge, aus einem Grund: erst absichern, dann Persönlichkeit,
dann Sichtbares.

**1. [64] Update mit Rückwärtsgang** — *klein bis mittel · `webpanel.py`,
`bot.py`.* Der Rest dieser Liste bedeutet viele Updates. Ohne Rückwärtsgang
bedeutet jedes davon ein Risiko, das du erst bemerkst, wenn der Server still
ist. Zusammen mit [65] gebaut.

**2. [36] Flo hat Launen** + **[34] Er tippt** — *klein · `ai.py`, `bot.py`.*
Zwei kleine Eingriffe mit dem mit Abstand größten Effekt auf das, was du
eigentlich willst: dass er wie ein Mensch wirkt. Beides an einem Nachmittag.

**3. [19]–[22] Das Nachschlagewerk** — *mittel · neu `wissen.py`, `wissen/*.md`.*
Dein Auftrag „die AI soll Peptid- und OpSec-Sachen wissen". Der Textteil ist die
Arbeit, der Code ist überschaubar. Und es ist die Grundlage für [23], [30] und
[32] — vier Ideen für einen Preis.

**4. [1]–[5] Das Panel mit Anmeldung** — *mittel · `webpanel.py`.* Dein zweiter
Auftrag. Ich würde mit dem **Admin-Login** anfangen (Schalter, Standard aus,
Passwort änderbar) und die Discord-Anmeldung als zweiten Schritt nachziehen —
dann hast du nach Tag eins schon was Benutzbares, und OAuth kann in Ruhe
entstehen.

**5. [57] Zitat der Woche** + **[58] Umfrage aus einem Satz** — *klein ·
`bot.py`, `render.py`, neu `umfrage.py`.* Zwei Sachen, die man **sieht** und
die von allein Betrieb erzeugen. Nach vier Punkten Infrastruktur und
Persönlichkeit ist das der Teil, den der Server bemerkt.

---

# Teil 5 — Bewusst nicht auf der Liste

Damit klar ist, dass es nicht vergessen wurde:

- **Slash-Befehle für alles.** Flo wird angesprochen wie ein Mensch, das ist
  sein Charakter. Ein Slash-Menü macht ihn zum Formular. Für Sachen mit vielen
  Feldern (Giveaway) sind sie richtig — flächendeckend nicht.
- **Sprachverständnis im Voice-Kanal.** Technisch reizvoll, in der Praxis teuer,
  wackelig und unangenehm, weil dann permanent mitgehört wird.
- **Eine Datenbank aus Prinzip.** `JsonStore` ist bei euren Größenordnungen
  richtig, robust und von Hand reparierbar. Nur die drei großen Töpfe [74]
  rechtfertigen den Wechsel — der Rest nicht.
- **Ein eigener Frontend-Baukasten fürs Panel.** Eine HTML-Datei ohne Bauschritt
  ist ein Vorteil, kein Rückstand. Sobald da ein Übersetzungslauf davorsteht,
  ist der Update-Knopf nicht mehr das, was er heute ist.
