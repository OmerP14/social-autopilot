"""Satış botu: ziyaretçi → örnek post → deneme isteği → yönetici onayı → aynı botta müşteri."""

import asyncio
from types import SimpleNamespace

import pytest
from telegram.ext import ApplicationHandlerStop

from app import billing, config, db, onboard, sales
from tests.conftest import ADMIN, OWNER, FakeApp

OTHER = 3003


def run(coro):
    return asyncio.run(coro)


def test_parse_target():
    assert sales.parse_target("ornekotel.com") == ("ornekotel.com", "")
    assert sales.parse_target("https://www.Ornek-Otel.com.tr/hakkimizda") == ("ornek-otel.com.tr", "")
    assert sales.parse_target("@Kafe.Lara") == ("", "kafe.lara")
    assert sales.parse_target("https://instagram.com/kafelara/") == ("", "kafelara")
    assert sales.parse_target("webim yok instagramım @ornekmarka.app") == ("", "ornekmarka.app")
    assert sales.parse_target("sitemiz www.ornekkafe.com.tr, bakabilirsiniz.") == ("ornekkafe.com.tr", "")
    assert sales.parse_target("ornekmarka.app") == ("ornekmarka.app", "")
    assert sales.parse_target("mail: ali@gmail.com") is None                  # e-posta ne site ne Instagram
    assert sales.parse_target("saat 10.30 da açılıyoruz") is None
    assert sales.parse_target("Dr.Ahmet kliniği") is None
    assert sales.parse_target("kafelara") is None                             # tek kelime: önce sorulur
    assert sales.bare_handle("kafelara") == "kafelara" and sales.bare_handle("merhaba dünya") is None
    assert sales.parse_target("merhaba nasılsınız") is None
    assert sales.parse_target("") is None
    assert sales.intent("satın almak istiyorum") == "buy" and sales.intent("Fiyatlar nedir") == "buy"
    assert sales.intent("hafta sonu da paylaşıyor musunuz?") == "question"
    assert sales.intent("selam") == "greet" and sales.intent("İyi günler") == "greet"
    assert sales.bare_handle("selam") is None and sales.intent("asdf") == "other"


def test_make_slug():
    assert sales.make_slug("demo.com", "") == "demo-2"        # demo zaten var
    assert sales.make_slug("", "Çiçek.Dünyası") == "cicek-dunyasi"
    assert sales.make_slug("x.com", "") == "x-firma"


@pytest.fixture
def shop(tg, monkeypatch):
    """Satış botu + sahte örnek üretimi (yapay zekâya gitmez, diske firma yazmaz)."""
    app = FakeApp("satış", tg.tr)
    app.bot_data["shared"] = True
    tg.bot.APPS["_sales"] = app
    demo = config.load_client("demo")
    monkeypatch.setattr(tg.bot, "load_client", lambda slug: demo)
    monkeypatch.setattr(sales, "make_slug", lambda website, ig: "aday1" if website == "ornek.com" else "aday2")
    monkeypatch.setattr(onboard, "set_active", lambda slug, active=True: None)

    def fake_sample(slug, website, ig):
        cid = db.add_content(slug, "2026-10", "2026-10-20T19:00:00", "post", {"theme": "tanıtım"})
        db.update(cid, status=db.REJECTED, media="output/ornek.jpg", caption="Örnek açıklama #ornek")
        db.set_setting(slug, "lead_state", "sample")
        return cid

    monkeypatch.setattr(sales, "make_sample", fake_sample)
    tg.sales = app
    return tg


def visit(tg, user, text=None, data=None, handler=None):
    """Satış botuna gelen güncelleme: önce kapı (sales_gate), müşteriyse ardından müşteri işleyicisi."""
    u, ctx = tg.update(tg.sales, user, text=text or "", data=data)
    try:
        run(tg.bot.sales_gate(u, ctx))
    except ApplicationHandlerStop:
        return ctx
    if handler:
        run(handler(u, ctx))
    return ctx


def test_sales_flow(shop):
    tg = shop
    visit(tg, OWNER, "/start")
    assert tg.tr.last().startswith("👋 Merhaba! Ben Social Autopilot")
    visit(tg, OWNER, "selam")
    assert "Web sitenizi" in tg.tr.last()

    visit(tg, OWNER, "ornek.com")
    assert db.get_setting("aday1", "lead_state") == "making"
    assert "make_lead_sample" in tg.sales.tasks
    visit(tg, OWNER, "ornek.com")                                     # üretim sürerken
    assert "hazırlanıyor" in tg.tr.last()
    run(tg.bot.make_lead_sample(tg.sales, "aday1", OWNER, OWNER, "ornek.com", ""))
    to_lead = tg.tr.texts(f"satış→{OWNER}")
    assert to_lead[0].startswith("[foto] 🎁") and "Örnek açıklama" in to_lead[1]
    offer = [b.callback_data for b in tg.tr.buttons(f"satış→{OWNER}")]
    assert {"sl:trial", "sl:pkg", "sl:how", "sl:talk"} <= set(offer)
    assert any(t.startswith("[foto] 🧲 Yeni aday") for t in tg.tr.texts(f"yönetim→{ADMIN}"))

    visit(tg, OWNER, "baska.com")                                     # ikinci örnek yok
    assert "zaten hazırladım" in tg.tr.last()
    visit(tg, OWNER, "ne zaman başlarız")                             # örnekten sonra yazılanlar → yöneticiye
    assert "ekibimize ilettim" in tg.tr.last("sahip←")
    assert any("Aday mesajı" in t and "ne zaman başlarız" in t for t in tg.tr.texts(f"yönetim→{ADMIN}"))

    visit(tg, OWNER, data="sl:pkg")
    assert tg.tr.last().startswith("📦 Paketler") and "Premium" in tg.tr.last()
    visit(tg, OWNER, data="sl:trial")
    assert db.get_setting("aday1", "lead_state") == "asked"
    assert "ld:ok:aday1" in [b.callback_data for b in tg.tr.buttons(f"yönetim→{ADMIN}")]

    u, ctx = tg.update(tg.admin, ADMIN, data="ld:ok:aday1")
    run(tg.bot.on_button(u, ctx))
    assert db.get_setting("aday1", "tg_chat") == str(OWNER) and db.get_setting("aday1", "shared") == "1"
    assert billing.package_of("aday1")["name"].startswith("Deneme")
    assert tg.bot.APPS["aday1"] is tg.sales
    assert any("denemeniz başladı" in t for t in tg.tr.texts(f"satış→{OWNER}"))
    visit(tg, OWNER, data="fw:0:go", handler=tg.bot.on_button)        # ilk haftayı başlat
    assert db.get_setting("aday1", "brief") == "wish"                 # önce soru turu
    visit(tg, OWNER, data="bf:0:skip", handler=tg.bot.on_button)
    visit(tg, OWNER, data="bf:0:go", handler=tg.bot.on_button)
    assert "generate_and_send" in tg.admin.tasks

    tg.tr.clear()                                                     # artık müşteri: menü butonları çalışır
    monkeypatch_firm = visit(tg, OWNER, "📅 Planım", handler=tg.bot.on_text)
    assert monkeypatch_firm.chat_data["slug"] == "aday1"
    assert tg.tr.last().startswith("📋 İçerikleriniz")

    u, ctx = tg.update(tg.admin, ADMIN, data="ld:ok:aday1")           # ikinci onay işe yaramaz
    run(tg.bot.on_button(u, ctx))
    assert db.get_setting("aday1", "trial_used") == "1"


def test_sales_limits(shop):
    tg = shop
    db.set_setting("_bot", "sales_daily", "1")
    visit(tg, OWNER, "ornek.com")
    visit(tg, OTHER, "@baskafirma")
    assert "kontenjan" in tg.tr.last()
    assert not db.get_setting("aday2", "lead_state")
    visit(tg, OTHER, data="sl:trial")                                 # örneksiz deneme isteği
    assert "Önce size özel örneği" in tg.tr.last()


def test_sales_failure_releases(shop, monkeypatch):
    tg = shop
    visit(tg, OWNER, "ornek.com")
    monkeypatch.setattr(sales, "make_sample", lambda *a: (_ for _ in ()).throw(RuntimeError("site açılmadı")))
    run(tg.bot.make_lead_sample(tg.sales, "aday1", OWNER, OWNER, "ornek.com", ""))
    assert "tekrar yazar mısınız" in tg.tr.texts(f"satış→{OWNER}")[-1]
    assert not sales.lead_of(OWNER)                                    # tekrar deneyebilir
    assert sales.check(OWNER, "ornek.com", "") is None


def test_lead_decline(shop):
    tg = shop
    visit(tg, OWNER, "ornek.com")
    run(tg.bot.make_lead_sample(tg.sales, "aday1", OWNER, OWNER, "ornek.com", ""))
    visit(tg, OWNER, data="sl:trial")
    u, ctx = tg.update(tg.admin, OTHER, data="ld:ok:aday1")           # yönetici olmayan onaylayamaz
    run(tg.bot.on_button(u, ctx))
    assert db.get_setting("aday1", "lead_state") == "asked"
    u, ctx = tg.update(tg.admin, ADMIN, data="ld:no:aday1")
    run(tg.bot.on_button(u, ctx))
    assert db.get_setting("aday1", "lead_state") == "declined"
    assert not db.get_setting("aday1", "tg_chat")
    assert "teşekkürler" in tg.tr.texts(f"satış→{OWNER}")[-1]


def test_same_business_once():
    keys = sales.firm_keys("https://www.Example.com/iletisim", "@DemoVipTransfer", "+90 (555) 000 00 00")
    assert keys == {"site:example.com", "ig:demoviptransfer", "tel:5550000000"}
    assert sales.known_firm(sales.firm_keys("example.com")) == "demo"     # mevcut müşteri
    assert sales.known_firm(sales.firm_keys(instagram="demoviptransfer")) == "demo"
    assert sales.check(9, "example.com", "") == sales.DUPLICATE_TEXT
    assert sales.check(9, "", "demoviptransfer") == sales.DUPLICATE_TEXT
    assert sales.check(9, "baskasite.com", "") is None


def test_duplicate_detected_after_research(tmp_path, monkeypatch):
    """Ziyaretçi Instagram adını yazdı; araştırma sitesini buldu ve o site daha önce örnek almıştı."""
    monkeypatch.setattr(config, "CLIENTS_DIR", tmp_path)
    monkeypatch.setattr(sales, "CLIENTS_DIR", tmp_path)

    def fake_onboard(slug, website, instagram, name="", active=True):
        (tmp_path / slug).mkdir()
        (tmp_path / slug / "client.yaml").write_text(
            f"slug: {slug}\nactive: false\ninstagram_handle: {instagram}\n"
            "business: {name: Örnek Kafe, website: ornekkafe.com, phone: '0532 111 22 33'}\n", encoding="utf-8")

    monkeypatch.setattr(onboard, "onboard", fake_onboard)
    made = []

    def fake_custom(client, request, kind=None, limited=False):
        cid = db.add_content(client.slug, "2026-10", "2026-10-20T19:00:00", "post", {})
        db.update(cid, media="output/ornek.jpg")
        made.append(cid)
        return cid

    monkeypatch.setattr(sales.custom, "make_custom", fake_custom)
    sales.make_sample("kafe1", "ornekkafe.com", "")                 # ilk kez: örnek üretilir
    assert made and db.get_setting(sales.S, "tel:5321112233") == "kafe1"
    with pytest.raises(sales.Duplicate):                              # aynı kafe, bu kez @adıyla
        sales.make_sample("kafe2", "", "ornekkafe")
    assert len(made) == 1 and not (tmp_path / "kafe2").exists()
    assert db.get_setting("kafe2", "lead_state") == "duplicate"


def test_lead_intents(shop):
    tg = shop
    visit(tg, OWNER, "satın almak istiyorum")
    assert tg.tr.last("sahip←").startswith("Harika!")
    assert any("Satın alma ilgisi" in t for t in tg.tr.texts(f"yönetim→{ADMIN}"))
    visit(tg, OWNER, "story de hazırlıyor musunuz?")
    assert "Sorunuzu ekibimize ilettim" in tg.tr.last("sahip←")
    assert any("Ziyaretçi sorusu" in t for t in tg.tr.texts(f"yönetim→{ADMIN}"))
    visit(tg, OWNER, "kafelara")
    assert tg.tr.last() == "Instagram kullanıcı adınız @kafelara mi?"
    assert not sales.lead_of(OWNER)                                    # onaysız üretim yok
    visit(tg, OWNER, data="sl:ig:kafelara")
    assert sales.lead_of(OWNER) == "aday2" and db.get_setting("aday2", "lead_target") == "@kafelara"
    assert "@kafelara inceleniyor" in tg.tr.last()


def test_human_takeover_and_reply(shop):
    tg = shop
    visit(tg, OWNER, "story de hazırlıyor musunuz?")
    relay_msg = next(t for t in tg.tr.texts(f"yönetim→{ADMIN}") if "Ziyaretçi sorusu" in t)
    assert "Yanıtla" in relay_msg
    msg_id = tg.admin.bot.n                                           # son gönderilen yönetim mesajı
    assert db.get_setting(sales.S, f"relay:{msg_id}") == str(OWNER)

    u, ctx = tg.update(tg.admin, ADMIN, text="Evet, story ve reels de var 🙂")   # yönetici Yanıtla ile cevaplar
    u.message.reply_to_message = SimpleNamespace(message_id=msg_id)
    run(tg.bot.on_text(u, ctx))
    assert tg.tr.texts(f"satış→{OWNER}")[-1] == "💬 Social Autopilot ekibi: Evet, story ve reels de var 🙂"
    assert tg.tr.last("yönetici←").startswith("✅ İletildi")

    u, ctx = tg.update(tg.admin, ADMIN, data=f"lh:on:{OWNER}")         # devral: bot susar
    run(tg.bot.on_button(u, ctx))
    assert sales.human(OWNER) and "katıldı" in tg.tr.texts(f"satış→{OWNER}")[-1]
    before = len(tg.tr.texts("sahip←"))
    visit(tg, OWNER, "ornek.com")                                     # devralınmışken örnek de başlamaz
    assert len(tg.tr.texts("sahip←")) == before and not sales.lead_of(OWNER)
    assert "ornek.com" in tg.tr.texts(f"yönetim→{ADMIN}")[-1]
    u, ctx = tg.update(tg.admin, OTHER, data=f"lh:off:{OWNER}")        # yönetici olmayan bırakamaz
    run(tg.bot.on_button(u, ctx))
    assert sales.human(OWNER)
    u, ctx = tg.update(tg.admin, ADMIN, data=f"lh:off:{OWNER}")
    run(tg.bot.on_button(u, ctx))
    assert not sales.human(OWNER)


def test_reset_lead(shop, tmp_path, monkeypatch):
    tg = shop
    monkeypatch.setattr(sales, "CLIENTS_DIR", tmp_path)
    visit(tg, OWNER, "ornek.com")
    (tmp_path / "aday1").mkdir()
    (tmp_path / "aday1" / "client.yaml").write_text("slug: aday1\nactive: false\n", encoding="utf-8")
    db.set_setting(sales.S, "tel:5321112233", "aday1")
    assert sales.check(OWNER, "ornek.com", "")
    u, ctx = tg.update(tg.admin, ADMIN, text=f"/aday sifirla {OWNER}")
    run(tg.bot.cmd_aday(u, ctx))
    assert "Sıfırlandı (aday1" in tg.tr.last()
    assert sales.check(OWNER, "ornek.com", "") is None and not (tmp_path / "aday1").exists()
    assert not db.get_setting(sales.S, "tel:5321112233") and not db.get_setting("aday1", "lead_state")
