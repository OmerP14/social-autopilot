"""Ortak test düzeneği: geçici veritabanı + sahte Telegram (mesajlar transkripte yazılır, ağ yok)."""

import dataclasses
from types import SimpleNamespace

import pytest

from app import db
from app.config import settings

ADMIN = 1001
OWNER = 2002


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "settings", dataclasses.replace(settings, db_path=tmp_path / "test.db"))
    from app import ai

    def no_api(*a, **k):
        raise RuntimeError("testte gerçek Claude çağrısı yapılmaz")
    monkeypatch.setattr(ai, "structured", no_api)   # yanlışlıkla ücretli çağrı olmasın
    monkeypatch.setattr(ai, "research", no_api)
    yield


class Transcript(list):
    def texts(self, who=None):
        return [t for w, t, _ in self if who is None or w == who]

    def last(self, who=None):
        return self.texts(who)[-1]

    def buttons(self, who=None):
        out = []
        for w, _, kb in self:
            if kb is not None and (who is None or w == who):
                rows = getattr(kb, "inline_keyboard", None) or getattr(kb, "keyboard", None) or []
                out += [b for row in rows for b in row]
        return out


class FakeBot:
    def __init__(self, name, tr):
        self.name, self.tr, self.n = name, tr, 100

    async def _send(self, chat, text, reply_markup=None):
        self.n += 1
        self.tr.append((f"{self.name}→{chat}", text or "", reply_markup))
        return SimpleNamespace(message_id=self.n)

    async def send_message(self, chat, text, reply_markup=None, **kw):
        return await self._send(chat, text, reply_markup)

    async def send_photo(self, chat, photo, caption=None, reply_markup=None, **kw):
        return await self._send(chat, f"[foto] {caption or ''}", reply_markup)

    async def send_video(self, chat, video, caption=None, **kw):
        return await self._send(chat, f"[video] {caption or ''}", None)

    async def send_media_group(self, chat, media, **kw):
        return [await self._send(chat, f"[albüm {len(media)}]", None)]


class FakeApp:
    def __init__(self, name, tr, slug=None):
        self.bot = FakeBot(name, tr)
        self.bot_data = {"slug": slug} if slug else {}
        self.tasks = []

    def create_task(self, coro):
        self.tasks.append(coro.__qualname__)
        coro.close()


class FakeMessage:
    def __init__(self, tr, who, chat, text=""):
        self.tr, self.who, self.chat, self.text = tr, who, chat, text
        self.message_id = 1

    async def reply_text(self, text, reply_markup=None, **kw):
        self.tr.append((f"{self.who}←", text, reply_markup))
        return self

    async def reply_photo(self, photo, caption=None, reply_markup=None, **kw):
        self.tr.append((f"{self.who}←", f"[foto] {caption or ''}", reply_markup))
        return self

    async def delete(self):
        pass

    async def edit_reply_markup(self, *a, **kw):
        pass


class FakeQuery:
    def __init__(self, msg, data):
        self.message, self.data, self.answers = msg, data, []

    async def answer(self, text=None, **kw):
        self.answers.append(text)

    async def edit_message_text(self, text, reply_markup=None, **kw):
        self.message.tr.append((f"{self.message.who}✎", text, reply_markup))

    async def edit_message_reply_markup(self, *a, **kw):
        pass


@pytest.fixture
def tg(monkeypatch):
    """Yönetim botu + 'demo' müşteri botu; dönen nesneyle konuşma simüle edilir."""
    from app import bot
    tr = Transcript()
    admin, client = FakeApp("yönetim", tr), FakeApp("müşteri", tr, slug="demo")
    monkeypatch.setattr(bot, "APPS", {"_admin": admin, "demo": client})
    monkeypatch.setattr(bot, "admin_id", lambda: ADMIN)
    monkeypatch.setattr(bot, "render_card", lambda *a, **k: "kart.jpg")
    monkeypatch.setattr(bot.design, "status", lambda client: "approved")   # tasarım akışı ayrı test edilir

    def update(app, user, text="", data=None, first="Ömer"):
        who = "yönetici" if user == ADMIN else "sahip"
        msg = FakeMessage(tr, who, user, text)
        u = SimpleNamespace(effective_user=SimpleNamespace(id=user, full_name=f"{first} Test", first_name=first,
                                                          username=None),
                            effective_chat=SimpleNamespace(id=user), message=None if data else msg,
                            effective_message=msg, callback_query=FakeQuery(msg, data) if data else None)
        ctx = SimpleNamespace(args=text.split()[1:] if text.startswith("/") else [], application=app,
                              chat_data={}, bot=app.bot)
        return u, ctx

    return SimpleNamespace(tr=tr, admin=admin, client=client, update=update, bot=bot)
