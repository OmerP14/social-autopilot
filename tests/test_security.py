"""Güvenlik testleri: şablon enjeksiyonu, iç ağa erişim (SSRF), sahte buton verisi, yetki, tek kullanımlık
butonlar, taşkın ve çift tıklama korumaları."""

import asyncio
from datetime import date
from pathlib import Path

import pytest
from jinja2.exceptions import SecurityError

from app import db, design, htmlsafe, music, netsafe, renderer, sales
from app.config import load_client
from tests.conftest import ADMIN, OWNER, FakeApp

STRANGER = 3003
ATTACKS = [
    '<iframe src="file:///opt/app/.env"></iframe>',
    '<img src="http://169.254.169.254/latest/meta-data/">',
    '<div style="background:url(/opt/app/.env)"></div>',
    '<div style="background:url(file:///etc/passwd)"></div>',
    "{{ ''.__class__.__mro__[1].__subclasses__() }}",
    "{{ headline | safe }}",
    '{% include "../../.env" %}',
    '{% from "x.html" import y %}',
    "<svg><foreignObject><iframe></iframe></foreignObject></svg>",
    "<img src=x onerror=alert(1)>",
    "<style>@import url(http://kotu.com/a.css);</style>",
    '<link rel="stylesheet" href="//kotu.com/a.css">',
    '<meta http-equiv="refresh" content="0;url=file:///etc/hosts">',
    "<script>fetch('file:///etc/passwd')</script>",
    '<a href="javascript:alert(1)">x</a>',
    "<object data=\"file:///etc/passwd\"></object>",
]


def run(coro):
    return asyncio.run(coro)


# ── şablon enjeksiyonu ─────────────────────────────────────────────────────

@pytest.mark.parametrize("html", ATTACKS)
def test_unsafe_templates_detected(html):
    assert htmlsafe.unsafe(html), html


def test_default_and_legit_templates_pass():
    for f in Path("templates").glob("*.html"):
        assert htmlsafe.unsafe(f.read_text(encoding="utf-8")) == [], f.name
    legit = ('{% extends "brand_base.html" %}{% block body %}<html lang="{{ ui_lang }}"><meta charset="utf-8">'
             '<style>{% include "fonts.css" %} .kart__baslik{background:url("{{ photo }}")} '
             '.x{background-image:url(data:image/svg+xml;base64,AAA)}</style>'
             '<img class="photo" src="{{ photo }}"><div>{{ headline | emph }}</div>{% endblock %}')
    assert htmlsafe.unsafe(legit) == []
    for logo in ("tests/fixtures/clients/demo/logo.svg",):
        assert htmlsafe.unsafe(Path(logo).read_text(encoding="utf-8"), template=False) == []


def test_save_rejects_traversal_and_unsafe(tmp_path):
    rejected = design._save(tmp_path, [
        design.TemplateFile(name="../app/bot.py", html="x"),
        design.TemplateFile(name="post_type.html", html=ATTACKS[0]),
        design.TemplateFile(name="post_list.html", html='{% extends "brand_base.html" %}'),
    ])
    assert set(rejected) == {"../app/bot.py", "post_type.html"}
    assert not (tmp_path.parent / "app").exists() and not (tmp_path / "post_type.html").exists()
    assert (tmp_path / "post_list.html").exists()


def test_sandbox_blocks_python_internals(tmp_path):
    env = renderer._environment(str(tmp_path))
    with pytest.raises(SecurityError):
        env.from_string("{{ ''.__class__.__mro__ }}").render()
    with pytest.raises(SecurityError):
        env.from_string("{{ cycler.__init__.__globals__ }}").render(cycler=__import__("jinja2").utils.Cycler(1))


def test_render_skips_tampered_design(tmp_path, monkeypatch):
    """Diskteki tasarım dosyası sonradan bozulsa bile görsel üretici onu kullanmaz."""
    folder = tmp_path / "design"
    folder.mkdir()
    (folder / "brand_base.html").write_text('<html><body>{% block body %}{% endblock %}</body></html>')
    (folder / "post_type.html").write_text('{% extends "brand_base.html" %}{% block body %}'
                                           '<iframe src="file:///etc/hosts"></iframe>{% endblock %}')
    (folder / "post_list.html").write_text('{% extends "brand_base.html" %}{% block body %}ok{% endblock %}')
    assert renderer._safe_templates(folder, {"post_type", "post_list"}) == {"post_list"}
    (folder / "brand_base.html").write_text("<html><script>x</script></html>")
    assert renderer._safe_templates(folder, {"post_list"}) == set()


# ── iç ağa erişim (SSRF) ───────────────────────────────────────────────────

@pytest.mark.parametrize("url", ["http://127.0.0.1:8090/ig/callback", "http://localhost/", "http://169.254.169.254/",
                                 "http://10.0.0.5/x.mp3", "http://[::1]/", "file:///etc/passwd", "ftp://x.com/a.mp3",
                                 "http://kullanici:sifre@1.1.1.1/", "http://0.0.0.0/", "gopher://1.1.1.1/"])
def test_internal_urls_blocked(url):
    with pytest.raises(netsafe.UnsafeURL):
        netsafe.check_url(url)


def test_public_url_allowed():
    assert netsafe.check_url("https://1.1.1.1/a.mp3")


def test_music_link_cannot_reach_internal(tmp_path):
    with pytest.raises(netsafe.UnsafeURL):
        music.save_url(load_client("demo"), "http://127.0.0.1:8090/ig/callback?x=1")
    with pytest.raises(netsafe.UnsafeURL):
        music.save_url(load_client("demo"), "http://169.254.169.254/latest/user-data.mp3")


# ── sahte buton verisi / yetki ────────────────────────────────────────────

def press(tg, app, user, data):
    u, ctx = tg.update(app, user, data=data)
    run(tg.bot.on_button(u, ctx))
    return u.callback_query


def test_customer_cannot_use_admin_buttons(tg):
    db.set_setting("demo", "tg_chat", str(OWNER))
    for data in ("adm:trial:demo", "adm:setpkg:demo:premium", "adm:pay:demo", "adm:sub:demo", "ld:ok:demo",
                 "lh:on:123", "cnl:abc", "menu:0:durum", "gen:demo:2026-11-01:2026-11-30", "rh:0"):
        before = len(tg.tr)
        press(tg, tg.client, OWNER, data)                              # müşteri kendi botunda uydurma veri
        press(tg, tg.admin, OWNER, data)                               # müşteri yönetim botunda
        assert len(tg.tr) == before, data
    assert not db.get_setting("demo", "package") and not tg.client.tasks and not tg.admin.tasks


def test_cannot_touch_other_firms_content(tg):
    db.set_setting("demo", "tg_chat", str(OWNER))
    other = db.add_content("baska-firma", "2026-10", "2026-10-30T19:00:00", "post", {"theme": "x"})
    db.update(other, status=db.PENDING)
    own = db.add_content("demo", "2026-10", "2026-10-30T19:00:00", "post", {"theme": "y"})
    db.update(own, status=db.PENDING)
    for data in (f"ok:{other}", f"no:{other}", f"edit:{other}", f"time:{other}"):
        press(tg, tg.client, OWNER, data)
    assert db.get(other)["status"] == db.PENDING
    press(tg, tg.client, STRANGER, f"ok:{own}")                       # yabancı, firmanın botunda
    assert db.get(own)["status"] == db.PENDING


def test_stranger_cannot_use_client_bot(tg):
    db.set_setting("demo", "tg_chat", str(OWNER))
    for data in ("fw:0:go", "dz:0:new", "dz:0:ok", "bf:0:go", "oz:0:post"):
        press(tg, tg.client, STRANGER, data)
    assert not tg.admin.tasks and not tg.client.tasks
    assert not db.get_setting("demo", "design_requested")


# ── tek kullanımlık / taşkın / çift tıklama ────────────────────────────────

def test_first_week_button_only_once(tg):
    db.set_setting("demo", "invite", "K")
    u, ctx = tg.update(tg.client, OWNER, text="/start K")
    run(tg.bot.c_start(u, ctx))
    press(tg, tg.admin, ADMIN, "adm:trial:demo")
    press(tg, tg.client, OWNER, "fw:0:go")
    assert db.get_setting("demo", "first_week_done") == date.today().isoformat()
    db.set_setting("demo", "brief", "")                               # hazırlık bitmiş olsa bile
    q = press(tg, tg.client, OWNER, "fw:0:go")
    assert q.answers[-1] == "İlk haftanız zaten başlatıldı"
    run(tg.bot.await_first_week("demo"))                              # paket değişse de tekrar açılmaz
    assert db.get_setting("demo", "await_start") == "" and not tg.bot.can_start("demo")[0]


def test_relay_flood_limited(tg):
    app = FakeApp("satış", tg.tr)
    app.bot_data["shared"] = True
    tg.bot.APPS["_sales"] = app
    for i in range(15):
        run(tg.bot.relay(STRANGER, "💬 Ziyaretçi sorusu", f"soru {i}"))
    assert len(tg.tr.texts(f"yönetim→{ADMIN}")) == tg.bot.RELAY_PER_HOUR
    sales.set_human(STRANGER, True)                                   # devralınan sohbette sınır yok
    run(tg.bot.relay(STRANGER, "👤", "devam"))
    assert len(tg.tr.texts(f"yönetim→{ADMIN}")) == tg.bot.RELAY_PER_HOUR + 1


def test_design_job_not_started_twice(tg, monkeypatch):
    db.set_setting("demo", "tg_chat", str(OWNER))
    monkeypatch.setattr(tg.bot.design, "status", lambda c: "")
    press(tg, tg.client, OWNER, "dz:0:new")
    press(tg, tg.client, OWNER, "dz:0:new")                           # çift tıklama
    assert tg.admin.tasks.count("design_step") == 1
    calls = []

    async def fake(slug, fb, choose=0):
        calls.append(fb)
        run_again = tg.bot.design_step(slug, "ikinci")                # iş sürerken ikinci istek
        await run_again
    monkeypatch.setattr(tg.bot, "_design_step", fake)
    run(tg.bot.design_step("demo", "birinci"))
    assert calls == ["birinci"] and not db.get_setting("demo", "design_busy")


def test_lead_cannot_reach_customer_commands(tg, monkeypatch):
    app = FakeApp("satış", tg.tr)
    app.bot_data["shared"] = True
    tg.bot.APPS["_sales"] = app
    from telegram.ext import ApplicationHandlerStop
    u, ctx = tg.update(app, STRANGER, text="/haklarim")
    with pytest.raises(ApplicationHandlerStop):
        run(tg.bot.sales_gate(u, ctx))
    assert ctx.chat_data["slug"] == "" and "Premium" not in tg.tr.last()


def test_only_admin_can_reply_to_relayed_messages(tg):
    from types import SimpleNamespace
    db.set_setting("demo", "tg_chat", str(OWNER))
    db.set_setting(sales.S, "relay:777", "firm:demo")
    u, ctx = tg.update(tg.admin, STRANGER, text="sahte cevap")        # yönetim botunda yabancı
    u.message.reply_to_message = SimpleNamespace(message_id=777)
    run(tg.bot.on_text(u, ctx))
    u, ctx = tg.update(tg.client, OWNER, text="müşteri kendi botunda")  # müşteri yanıtla denemesi
    u.message.reply_to_message = SimpleNamespace(message_id=777)
    run(tg.bot.on_text(u, ctx))
    assert not any(t.startswith("💬 Yetkiliniz") for t in tg.tr.texts())
