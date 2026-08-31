"""Nachrichten-Attrappe: eine Discord-Nachricht, die keine ist.

Diese beiden Bausteine brauchten frueher nur der Rauchtest in
test_games_logic.py. Jetzt braucht sie auch die Probe des Inventars
(werkzeug/inventar.py), die jedes Modul fragt "reagierst du auf dieses Wort?".

Zwei Kopien waeren genau die Unordnung, die der Umbau beseitigen soll - also
liegen sie hier, und der Test importiert von hier.

Wichtig: die Attrappe sagt zu ALLEM ja. Sie hat alle Rechte, jeder Kanal nimmt
jede Nachricht an, jede Antwort landet in kanal.gesendet. Sie prueft also
nichts - sie sorgt nur dafuer, dass ein Modul ueberhaupt bis zu seiner Logik
kommt, statt vorher an einer fehlenden Berechtigung abzubiegen.
"""

from types import SimpleNamespace


class RauchKanal:
    """Kanal-Attrappe, die alles annimmt, was ein Modul senden koennte."""

    def __init__(self, cid=555):
        self.id = cid
        self.name = "rauchtest"
        self.gesendet = []

    async def send(self, content=None, embed=None, view=None, **_kw):
        self.gesendet.append((content, embed, view))
        return SimpleNamespace(id=len(self.gesendet), channel=self,
                               edit=self._nichts, delete=self._nichts,
                               add_reaction=self._nichts)

    async def _nichts(self, *_a, **_k):
        return None

    def typing(self):
        class Tippt:
            async def __aenter__(self_):
                return self_

            async def __aexit__(self_, *_a):
                return False
        return Tippt()

    def permissions_for(self, _m):
        return SimpleNamespace(view_channel=True, send_messages=True,
                               manage_messages=True, embed_links=True,
                               attach_files=True, read_message_history=True)


def rauch_nachricht(text, uid=778899001122334455, kanal=None):
    """Eine Nachricht, wie sie bot.on_message an die Module weiterreicht."""
    kanal = kanal or RauchKanal()
    rechte = SimpleNamespace(administrator=True, manage_guild=True,
                             manage_messages=True, ban_members=True,
                             kick_members=True, moderate_members=True)
    autor = SimpleNamespace(id=uid, bot=False, display_name="Rauchtester",
                            name="rauch", mention=f"<@{uid}>",
                            guild_permissions=rechte, roles=[],
                            display_avatar=SimpleNamespace(url="http://x/a.png"))
    guild = SimpleNamespace(
        id=77, name="Rauchserver", owner_id=42, members=[autor],
        text_channels=[kanal], voice_channels=[], roles=[],
        me=SimpleNamespace(id=1, guild_permissions=rechte),
        get_member=lambda _i: None, icon=None)
    return SimpleNamespace(author=autor, content=text, mentions=[], guild=guild,
                           channel=kanal, id=999, attachments=[], reference=None,
                           reply=kanal.send, delete=kanal._nichts,
                           add_reaction=kanal._nichts, created_at=None)
