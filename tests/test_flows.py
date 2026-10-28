"""Uçtan uca bot akışları (sahte Telegram): yeni firma karşılama → deneme → menü → özel istek → paket isteği →
deneme bitişi → ödeme → ay sonu onayı → kredi bitişi → bakiye uyarısı. Her adımda mesajlar, butonlar ve
Telegram sınırları kontrol edilir. Konuşma dökümü: TRANSCRIPT=dosya.txt uv run --with pytest pytest -q
"""

import asyncio
import os
import re
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from app import billing, db, wallet
from app.config import load_client
from tests.conftest import ADMIN, OWNER


def run(coro):
    return asyncio.run(coro)


def press(tg, app, user, data):
    u, ctx = tg.update(app, user, data=data)
    run(tg.bot.on_button(u, ctx))
    return u.callback_query


def say(tg, app, user, text, handler):
    u, ctx = tg.update(app, user, text=text)
    run(handler(u, ctx))
    return ctx


def check_limits(tr):
    for who, text, kb in tr:
        assert len(text) <= 4096, (who, text[:80])
        for b in tr.buttons() if kb is None else [b for row in getattr(kb, "inline_keyboard", []) for b in row]:
            if getattr(b, "callback_data", None):
                assert len(b.callback_data.encode()) <= 64, b.callback_data
            assert len(b.text) <= 64, b.text


def dump(tr, name):
    path = os.environ.get("TRANSCRIPT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"\n\n══════════ {name} ══════════\n")
        for who, text, kb in tr:
            f.write(f"\n[{who}] {text}\n")
            rows = getattr(kb, "inline_keyboard", None) or getattr(kb, "keyboard", None) or []
            f.writelines("   " + "  ".join(f"[{b.text}]" for b in row) + "\n" for row in rows)


def onboard(tg):
    db.set_setting("demo", "invite", "KOD123")
    say(tg, tg.client, OWNER, "/start KOD123", tg.bot.c_start)


# ── 1. yeni firma karşılama ────────────────────────────────────────────────

def test_onboarding_flow(tg):
    say(tg, tg.client, 3003, "/start", tg.bot.c_start)
    assert "davet linki" in tg.tr.last()
    tg.tr.clear()
    onboard(tg)
    assert db.get_setting("demo", "tg_chat") == str(OWNER)
    owner = tg.tr.texts("sahip←")
    assert owner[0].startswith("[foto]")                              # hoş geldin kartı
    assert "Merhaba Ömer" in owner[1]
    assert any("fotoğraf" in t.lower() for t in owner)
    assert any("yetkiliniz tarafından tanımlanacak" in t for t in owner)
    assert any("Instagram" in t for t in owner)
    labels = [b.text for b in tg.tr.buttons("sahip←")]
    assert "✨ Özel istek" in labels and "📅 Planım" in labels      # kalıcı menü
    assert not any("pk:" in (getattr(b, "callback_data", "") or "") for b in tg.tr.buttons("sahip←"))
    adm = [t for t in tg.tr.texts() if "Hangi paketle" in t]
    assert adm, "yöneticiye paket sorulmadı"
    datas = [b.callback_data for b in tg.tr.buttons(f"yönetim→{ADMIN}")]
    assert {"adm:setpkg:demo:baslangic", "adm:setpkg:demo:standart", "adm:setpkg:demo:premium",
            "adm:trial:demo"} <= set(datas)
    say(tg, tg.client, 3003, "/start KOD123", tg.bot.c_start)         # başkası aynı linkle
    assert "sadece kayıtlı firma sahibine" in tg.tr.last()
    check_limits(tg.tr)
    dump(tg.tr, "1. Yeni firma karşılama")


# ── 2. deneme → menü → özel istek → paket isteği ───────────────────────────

def test_trial_and_menu(tg):
    onboard(tg)
    tg.tr.clear()
    press(tg, tg.admin, ADMIN, "adm:trial:demo")
    assert db.get_setting("demo", "package") == "deneme"
    tu = billing.trial_until("demo")
    assert tu == date.today() + timedelta(days=7)
    assert any("ücretsiz denemeniz başladı" in t for t in tg.tr.texts(f"müşteri→{OWNER}"))
    assert any("Instagram (isteğe bağlı" in t for t in tg.tr.texts(f"müşteri→{OWNER}"))
    # üretim hemen başlamaz: firma sahibi “İlk haftamı oluştur” der (Instagram bağlantısı beklenmez)
    assert "generate_and_send" not in tg.admin.tasks and not db.get_setting("demo", "brief")
    assert "zorunlu değil" in tg.tr.texts(f"müşteri→{OWNER}")[-1]
    assert tg.tr.buttons(f"müşteri→{OWNER}")[-1].callback_data == "fw:0:go"
    press(tg, tg.client, 3003, "fw:0:go")                             # başkası başlatamaz
    assert db.get_setting("demo", "await_start") == "1"
    press(tg, tg.client, OWNER, "fw:0:go")
    assert db.get_setting("demo", "await_start") == "" and db.get_setting("demo", "brief") == "wish"
    # ilk içerikler öncesi soru turu: öne çıkacak şey → fotoğraf → üretim
    assert "2 kısa soru" in tg.tr.texts(f"müşteri→{OWNER}")[-1]
    say(tg, tg.client, OWNER, "Cuma günü yeni minibüsümüzü tanıtalım", tg.bot.on_text)
    assert db.get_setting("demo", "brief_wish").startswith("Cuma günü") and db.get_setting("demo", "brief") == "photos"
    assert "Kendi fotoğraflarınız" in tg.tr.texts(f"müşteri→{OWNER}")[-1]
    press(tg, tg.client, OWNER, "bf:0:photos")
    assert db.get_setting("demo", "brief") == "waitphotos"
    press(tg, tg.client, 3003, "bf:0:go")                             # başkası başlatamaz
    assert "generate_and_send" not in tg.admin.tasks
    press(tg, tg.client, OWNER, "bf:0:go")
    assert "generate_and_send" in tg.admin.tasks and not db.get_setting("demo", "brief")
    assert any("İlk içeriklerinizi hazırlıyorum" in t for t in tg.tr.texts(f"müşteri→{OWNER}"))
    say(tg, tg.client, OWNER, "/yardim", tg.bot.c_help)
    assert tg.tr.last().startswith("📍 Durumunuz") and "Instagram: bağlı değil" in tg.tr.last()
    press(tg, tg.admin, ADMIN, "adm:trial:demo")                      # ikinci kez deneme verilemez
    assert db.get_setting("demo", "trial_until") == tu.isoformat()

    for label in [t for row in tg.bot.CLIENT_BUTTONS for t in row]:  # her menü butonu cevap versin
        before = len(tg.tr)
        say(tg, tg.client, OWNER, label, tg.bot.c_button)
        assert len(tg.tr) > before, f"{label} cevapsız"
    rights = next(t for t in tg.tr.texts("sahip←") if t.startswith("📦 Deneme"))
    assert "Denemede özel istek: 1/1" in rights and "TL" not in rights and "$" not in rights
    ozel = next(t for t in tg.tr.texts("sahip←") if t.startswith("✨ Özel içerik"))
    assert "1/1" in ozel

    # deneme: 1 hak → reels (2 hak) reddedilir, yapay zekâya gidilmez
    from app import ai
    called = []
    orig = ai.structured
    ai.structured = lambda *a, **k: called.append(1)
    try:
        u, _ctx = tg.update(tg.client, OWNER, text="Cumartesi reels")
        run(tg.bot.make_custom_and_send(tg.client, "demo", "Cumartesi reels", kind="reel", limited=True,
                                        reply_to=u.message))
    finally:
        ai.structured = orig
    assert not called and "özel istek hakkınız 1, bu istek 2 hak" in tg.tr.last("sahip←")
    q = press(tg, tg.client, OWNER, "oz:0:reel")                      # butonda da engellenir
    assert q.answers == ["Bu istek 2 hak, kalan hakkınız 1"]
    q = press(tg, tg.client, OWNER, "oz:0:post")
    assert "Post için ne istediğinizi" in tg.tr.last("sahip←")

    # paket değişikliği isteği → yöneticiye onay
    press(tg, tg.client, OWNER, "pk:0:premium")
    req = [t for t in tg.tr.texts(f"yönetim→{ADMIN}") if "Premium (6.000 TL/ay) paketini istiyor" in t]
    assert req
    press(tg, tg.admin, ADMIN, "adm:pkgno:demo")
    assert "uygulanmadı" in tg.tr.last(f"müşteri→{OWNER}")
    assert db.get_setting("demo", "package") == "deneme"
    check_limits(tg.tr)
    dump(tg.tr, "2. Deneme, menü, özel istek, paket isteği")


# ── 3. deneme bitişi → paket + ödeme ───────────────────────────────────────

def test_trial_end_and_payment(tg):
    onboard(tg)
    press(tg, tg.admin, ADMIN, "adm:trial:demo")
    tg.tr.clear()
    ctx = type("C", (), {"application": tg.admin})()
    tomorrow = date.today() + timedelta(days=1)
    db.set_setting("demo", "trial_until", tomorrow.isoformat())
    db.set_setting("demo", "paid_until", tomorrow.isoformat())
    run(tg.bot.job_subscriptions(ctx))
    assert any("deneme yarın" in t for t in tg.tr.texts())
    yesterday = date.today() - timedelta(days=1)
    db.set_setting("demo", "trial_until", yesterday.isoformat())
    db.set_setting("demo", "paid_until", yesterday.isoformat())
    run(tg.bot.job_subscriptions(ctx))
    assert any("deneme bitti" in t for t in tg.tr.texts(f"yönetim→{ADMIN}"))
    assert any("deneme süreniz sona erdi" in t for t in tg.tr.texts(f"müşteri→{OWNER}"))
    assert not tg.bot.service_active("demo")
    # askıdayken firma sahibi bir şey yazarsa kapı kapalı
    u, c = tg.update(tg.client, OWNER, text="/plan")
    with pytest.raises(Exception, match="ApplicationHandlerStop|"):
        run(tg.bot.gate(u, c))
    # paket ata → ödeme butonu → ödendi
    press(tg, tg.admin, ADMIN, "adm:setpkg:demo:standart")
    assert any(getattr(b, "callback_data", "") == "adm:pay:demo" for b in tg.tr.buttons())
    press(tg, tg.admin, ADMIN, "adm:pay:demo")
    assert tg.bot.service_active("demo") and billing.trial_until("demo") is None
    assert "Ödemeniz alındı" in tg.tr.last(f"müşteri→{OWNER}")
    check_limits(tg.tr)
    dump(tg.tr, "3. Deneme bitişi ve ödeme")


# ── 4. ay sonu devam onayı, kredi bitişi, bakiye uyarısı ───────────────────

def test_month_renewal_credits_wallet(tg, monkeypatch):
    onboard(tg)
    press(tg, tg.admin, ADMIN, "adm:setpkg:demo:standart")
    press(tg, tg.admin, ADMIN, "adm:pay:demo")
    tg.tr.clear()

    class D(date):
        @classmethod
        def today(cls):
            return date(2026, 10, 25)
    monkeypatch.setattr(tg.bot, "date", D)
    ctx = type("C", (), {"application": tg.admin})()
    run(tg.bot.job_month_renewal(ctx))
    run(tg.bot.job_month_renewal(ctx))                                # aynı gün ikinci kez sormaz
    asks = [t for t in tg.tr.texts() if "Kasım 2026 için" in t]
    assert len(asks) == 1
    press(tg, tg.admin, ADMIN, "adm:renew:demo:next")
    assert db.get_setting("demo", "month_ok:2026-11") == "1"
    monkeypatch.setattr(tg.bot, "date", date)

    run(tg.bot.ask_bonus("demo"))
    run(tg.bot.ask_bonus("demo"))                                     # ayda bir kez
    press(tg, tg.admin, ADMIN, "adm:wait:demo")
    assert "ek kredi verilmedi" in tg.tr.last()
    assert billing.bonus_credits("demo") == 0
    assert len([t for t in tg.tr.texts() if "kredisi bitti" in t]) == 1
    press(tg, tg.admin, ADMIN, "adm:bonus:demo")
    assert billing.credits_total("demo") == 3000
    assert "ek 1000 kredi" in tg.tr.last(f"müşteri→{OWNER}")

    wallet.set_balance("claude", 2.5)
    billing.record_usd(1.0, "demo", provider="claude")
    run(tg.bot.job_wallet(ctx))
    assert "Claude (Anthropic) bakiyesi azaldı" in tg.tr.last(f"yönetim→{ADMIN}")
    check_limits(tg.tr)
    dump(tg.tr, "4. Ay sonu onayı, ek kredi, bakiye uyarısı")


# ── 5. güvenlik: müşteri yönetim işlemi yapamaz ─────────────────────────────

def test_owner_cannot_use_admin(tg):
    onboard(tg)
    for data in ("adm:setpkg:demo:premium", "adm:bonus:demo", "adm:trial:demo", "menu:0:cost", "menu:0:wallet"):
        press(tg, tg.client, OWNER, data)
    assert db.get_setting("demo", "package") is None or db.get_setting("demo", "package") == ""
    assert billing.bonus_credits("demo") == 0
    assert not any("Bakiyeler" in t or "$" in t for t in tg.tr.texts("sahip←"))
    app = tg.bot.build_client_app("demo", "123456:TEST")
    cmds = {c for h in app.handlers[0] for c in getattr(h, "commands", ())}
    assert cmds and not cmds & {"maliyet", "bakiye", "paket_ata", "odeme", "butce", "kur", "openai", "ses"}


def test_telegram_command_menus_valid():
    from app import bot
    for name, desc in [*bot.CLIENT_MENU, *bot.MENU]:
        assert re.fullmatch(r"[a-z0-9_]{1,32}", name), name
        assert 3 <= len(desc) <= 256, name


def test_client_texts_consistent(tg):
    """Firma sahibi paket SEÇMEZ (yönetici atar); metinler bununla çelişmesin, para/$ geçmesin."""
    onboard(tg)
    say(tg, tg.client, OWNER, "/yardim", tg.bot.c_help)
    say(tg, tg.client, OWNER, "/haklarim", tg.bot.c_haklarim)
    for t in tg.tr.texts("sahip←"):
        assert "Paketinizi seçin" not in t and "paket seçmediniz" not in t.lower(), t
        assert "$" not in t and " TL" not in t, t


def test_clean_token():
    from app.bot import clean_token
    tok = "1234567890:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
    assert clean_token(tok) == tok
    assert clean_token("1234567890 : AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw") == tok
    assert clean_token("1234567890 AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw") == tok
    assert clean_token("1234567890 AAHdq TcvCH1vGWJxfSeofSAs0K5PALDsaw") == tok


def test_guide_pages(tg):
    from app.bot import GUIDE, guide_page
    for i in range(len(GUIDE)):
        text, kb = guide_page(i)
        assert len(text) <= 4096
        datas = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert all(len(d.encode()) <= 64 for d in datas)
    assert "menu:0:new" in [b.callback_data for row in guide_page(2)[1].inline_keyboard for b in row]
    press(tg, tg.admin, ADMIN, "rh:1")
    assert "BotFather" in tg.tr.last()
    press(tg, tg.client, OWNER, "rh:1")                               # firma sahibi rehberi göremez
    assert len([t for t in tg.tr.texts() if "BotFather" in t]) == 1


# ── menü butonları düz metin olarak gelince de çalışsın (tam yetkili firma, emoji farkları) ──

@pytest.mark.parametrize("full", [False, True])
def test_menu_buttons_via_text(tg, full):
    onboard(tg)
    press(tg, tg.admin, ADMIN, "adm:trial:demo")
    if full:
        db.set_setting("demo", "full", "1")
    for label in [t for row in tg.bot.CLIENT_BUTTONS for t in row] + ["⏳️ Bekleyenler", "📊 Haklarım "]:
        tg.tr.clear()
        say(tg, tg.client, OWNER, label, tg.bot.on_text)
        assert len(tg.tr) > 0, f"{label!r} cevapsız"
        assert not any("altlarındaki butonları" in t or "altındaki butonları kullan" in t for t in tg.tr.texts()), label


def test_brief_timeout(tg):
    onboard(tg)
    start = date.today() + timedelta(days=1)
    run(tg.bot.ask_brief("demo", start, start + timedelta(days=6)))
    run(tg.bot.job_brief(None))                                       # henüz erken
    assert db.get_setting("demo", "brief") == "wish"
    db.set_setting("demo", "brief_since", "2000-01-01T00:00:00")
    run(tg.bot.job_brief(None))
    assert not db.get_setting("demo", "brief") and "generate_and_send" in tg.admin.tasks
    assert any("Cevap gelmediği için" in t for t in tg.tr.texts(f"müşteri→{OWNER}"))


def test_design_flow(tg, tmp_path, monkeypatch, request):
    """Deneme → 3 tasarım yönü → seçim → değişiklik isteği → onay → soru turu."""
    import shutil
    request.addfinalizer(lambda: shutil.rmtree(load_client("demo").dir / "design_options", ignore_errors=True))
    design = tg.bot.design
    state = {"status": "", "generated": 0, "revised": [], "approved": 0}
    previews = [tmp_path / f"p{i}.jpg" for i in range(3)]
    for p in previews:
        p.write_bytes(b"jpg")
    monkeypatch.setattr(design, "status", lambda c: state["status"])
    monkeypatch.setattr(design, "manifest", lambda c: {"templates": ["post_photo"]} if state["generated"] else {})
    monkeypatch.setattr(design, "generate", lambda c, scope="all": state.update(generated=1, status="draft") or {})
    monkeypatch.setattr(design, "complete", lambda c: {})
    monkeypatch.setattr(design, "previews", lambda c, out=None: previews)
    monkeypatch.setattr(design, "revise", lambda c, fb: state["revised"].append(fb) or "Logo sağ üste alındı.")
    monkeypatch.setattr(design, "approve", lambda c: state.update(approved=state["approved"] + 1, status="approved"))
    opts = [{"index": i, "concept": f"Yön {i}", "mood": [], "preview": previews[i - 1]} for i in (1, 2, 3)]
    monkeypatch.setattr(design, "directions", lambda c, avoid=None: state.update(dirs=state.get("dirs", 0) + 1) or opts)

    def fake_choose(c, i):
        (c.dir / "design_options").mkdir(exist_ok=True)
        state.update(chosen=i, generated=1, status="draft")
    monkeypatch.setattr(design, "choose", fake_choose)
    (load_client("demo").dir / "design_options" / "o2").mkdir(parents=True, exist_ok=True)
    onboard(tg)
    press(tg, tg.admin, ADMIN, "adm:trial:demo")
    press(tg, tg.client, OWNER, "fw:0:go")
    assert db.get_setting("demo", "brief") == "designing"
    assert "size özel tasarımınızı hazırlıyorum" in tg.tr.texts(f"müşteri→{OWNER}")[-1]
    assert "design_step" in tg.admin.tasks
    run(tg.bot.design_step("demo"))                                   # (görev olarak çalışırdı) → 3 seçenek
    assert state["dirs"] == 1 and db.get_setting("demo", "brief") == "design_pick"
    assert tg.tr.texts(f"müşteri→{OWNER}")[-2] == "[albüm 3]" and "3 tasarım yönü" in tg.tr.texts(f"müşteri→{OWNER}")[-1]
    assert [b.callback_data for b in tg.tr.buttons(f"müşteri→{OWNER}")][-4:] == \
        ["dz:1:pick", "dz:2:pick", "dz:3:pick", "dz:0:alt"]
    press(tg, tg.client, OWNER, "dz:2:pick")
    assert db.get_setting("demo", "brief") == "designing" and "2. yönü seçtiniz" in tg.tr.last("sahip←")
    run(tg.bot.design_step("demo", choose=2))
    assert state["chosen"] == 2 and db.get_setting("demo", "brief") == "design"
    assert [b.callback_data for b in tg.tr.buttons(f"müşteri→{OWNER}")][-3:] == ["dz:0:ok", "dz:0:edit", "dz:0:alt"]

    press(tg, tg.client, OWNER, "dz:0:edit")
    assert db.get_setting("demo", "design_wait") == "fb"
    say(tg, tg.client, OWNER, "logo sağ üstte olsun", tg.bot.on_text)
    assert db.get_setting("demo", "design_revs") == "1" and db.get_setting("demo", "brief") == "designing"
    run(tg.bot.design_step("demo", "logo sağ üstte olsun"))
    assert state["revised"] == ["logo sağ üstte olsun"]
    assert tg.tr.texts(f"müşteri→{OWNER}")[-1].startswith("✏️ Logo sağ üste alındı.")

    press(tg, tg.client, OWNER, "dz:0:ok")
    assert state["approved"] == 1 and db.get_setting("demo", "brief") == "wish"
    assert "2 kısa soru" in tg.tr.texts(f"müşteri→{OWNER}")[-1]

    db.set_setting("demo", "design_revs", "3")                        # hak sınırı
    press(tg, tg.client, OWNER, "dz:0:edit")
    assert "hakkınız doldu" in tg.tr.last("sahip←") and db.get_setting("demo", "design_wait") != "fb"
    press(tg, tg.client, OWNER, "dz:0:alt")                           # yeniden tasarla: hakkı var
    assert db.get_setting("demo", "design_alts") == "1" and "yeni tasarım yönleri" in tg.tr.last("sahip←")
    db.set_setting("demo", "design_alts", "2")
    press(tg, tg.client, OWNER, "dz:0:alt")
    assert "seçeneği hakkınız doldu" in tg.tr.last("sahip←")
    press(tg, tg.client, OWNER, "dz:9:pick")                          # olmayan seçenek
    assert "artık geçerli değil" in tg.tr.last("sahip←")


def test_design_timeout(tg, monkeypatch):
    approved = []
    monkeypatch.setattr(tg.bot.design, "approve", lambda c: approved.append(1))
    onboard(tg)
    start = date.today() + timedelta(days=1)
    db.set_setting("demo", "brief_range", f"{start}:{start + timedelta(days=6)}")
    tg.bot._brief_state("demo", "design")
    db.set_setting("demo", "brief_since", "2000-01-01T00:00:00")
    run(tg.bot.job_brief(None))
    assert approved and "generate_and_send" in tg.admin.tasks
    tg.bot._brief_state("demo", "designing")                          # yarıda kalan tasarım → soru turuna geç
    db.set_setting("demo", "brief_since", "2000-01-01T00:00:00")
    run(tg.bot.job_brief(None))
    assert db.get_setting("demo", "brief") == "wish"


def test_design_pick_is_forced(tg):
    """Tasarım seçilmeden kendiliğinden seçim yapılmaz; günde bir hatırlatılır. Yeni seçenek hakkı 1."""
    onboard(tg)
    start = date.today() + timedelta(days=1)
    db.set_setting("demo", "brief_range", f"{start}:{start + timedelta(days=6)}")
    tg.bot._brief_state("demo", "design_pick")
    db.set_setting("demo", "brief_since", "2000-01-01T00:00:00")
    tg.tr.clear()
    run(tg.bot.job_brief(None))
    assert db.get_setting("demo", "brief") == "design_pick" and "design_step" not in tg.admin.tasks
    assert "tasarımınızı seçmeniz gerekiyor" in tg.tr.texts(f"müşteri→{OWNER}")[-1]
    n = len(tg.tr)
    run(tg.bot.job_brief(None))                                       # aynı gün ikinci hatırlatma yok
    assert len(tg.tr) == n
    opts = [{"index": i} for i in (1, 2, 3)]
    assert "dz:0:alt" in [b.callback_data for row in tg.bot.pick_buttons(opts, "demo").inline_keyboard for b in row]
    db.set_setting("demo", "design_alts", "1")                        # tek yenileme hakkı kullanıldı
    assert "dz:0:alt" not in [b.callback_data for row in tg.bot.pick_buttons(opts, "demo").inline_keyboard
                              for b in row]
    press(tg, tg.client, OWNER, "dz:0:alt")
    assert "birini seçin" in tg.tr.last("sahip←") and db.get_setting("demo", "design_alts") == "1"


def test_trial_fixed_by_admin(tg):
    """Deneme yöneticinin bastığı anda (yarından 7 gün) sabitlenir; tasarım/brief sonradan uzatmaz."""
    onboard(tg)
    tg.tr.clear()
    press(tg, tg.admin, ADMIN, "adm:trial:demo")
    tomorrow = date.today() + timedelta(days=1)
    rng = (tomorrow, tomorrow + timedelta(days=6))
    assert billing.trial_range("demo") == rng
    assert any(t.startswith("🎁 demo: 1 hafta ücretsiz deneme başladı") for t in tg.tr.texts("yönetici←"))
    press(tg, tg.admin, ADMIN, "adm:trial:demo")                      # ikinci basış bir şey yapmaz
    assert billing.trial_range("demo") == rng
    # günler geçti, tasarım bitti, brief çalıştı: deneme aynı kalır, üretim deneme içinde kalır
    start = date.today() - timedelta(days=2)
    db.set_setting("demo", "trial_until", (start + timedelta(days=6)).isoformat())
    db.set_setting("demo", "brief_range", f"{start}:{start + timedelta(days=6)}")
    tg.bot._brief_state("demo", "wish")
    run(tg.bot.brief_go("demo"))
    assert billing.trial_range("demo") == (start, start + timedelta(days=6))
    assert "generate_and_send" in tg.admin.tasks
    # deneme bittiyse brief üretim başlatmaz
    db.set_setting("demo", "trial_until", (date.today() - timedelta(days=1)).isoformat())
    tg.admin.tasks.clear()
    db.set_setting("demo", "brief_range", f"{start}:{date.today() - timedelta(days=1)}")
    tg.bot._brief_state("demo", "wish")
    run(tg.bot.brief_go("demo"))
    assert billing.trial_until("demo") == date.today() - timedelta(days=1)
    assert "generate_and_send" not in tg.admin.tasks


def test_plan_quota_counts_existing(tg):
    """Aynı dönem tekrar planlanırsa kalan hak kadar üretilir; hak doluysa hiç üretilmez."""
    onboard(tg)
    press(tg, tg.admin, ADMIN, "adm:trial:demo")
    lo, hi = billing.trial_range("demo")
    for i, kind in enumerate(["post", "post", "story"]):
        cid = db.add_content("demo", "m", f"{lo + timedelta(days=i)}T10:00:00", kind, {"theme": "x"})
        db.update(cid, status=db.APPROVED)
    pend = db.add_content("demo", "m", f"{lo + timedelta(days=4)}T10:00:00", "post", {"theme": "y"})
    db.update(pend, status=db.PENDING)
    q = billing.plan_quota("demo", lo, hi)
    assert (q["posts"], q["stories"], q["reels"]) == (1, 2, 1)        # bekleyen post yenisiyle değişir
    q = billing.plan_quota("demo", lo, lo + timedelta(days=2))
    assert q["posts"] == 0                                             # dönem dışındaki bekleyen sayılır
    for kind in ("post", "story", "story", "reel"):
        cid = db.add_content("demo", "m", f"{lo + timedelta(days=5)}T10:00:00", kind, {"theme": "z"})
        db.update(cid, status=db.APPROVED)
    with pytest.raises(tg.bot.NoQuota):
        tg.bot._generate("demo", lo, hi)


def test_autoplan_skips_trial(tg, monkeypatch):
    onboard(tg)
    press(tg, tg.admin, ADMIN, "adm:trial:demo")
    db.set_setting("demo", "await_start", "")
    monkeypatch.setattr(tg.bot, "cadence", lambda c: ("weekly", tg.bot.datetime.now(tg.bot.TZ).weekday(), 0))
    called = []

    async def fake(*a, **k):
        called.append(a)
    monkeypatch.setattr(tg.bot, "generate_and_send", fake)
    run(tg.bot.job_autoplan(SimpleNamespace(application=tg.admin)))
    assert not [a for a in called if a[1] == "demo"]


def test_publish_without_instagram_goes_manual(tg, monkeypatch):
    onboard(tg)
    now = tg.bot.datetime.now(tg.bot.TZ).replace(tzinfo=None) - timedelta(minutes=1)
    cid = db.add_content("demo", "m", now.isoformat(timespec="seconds"), "post", {"theme": "x"})
    db.update(cid, status=db.APPROVED, caption="Merhaba")
    sent = []

    async def fake(app, row):
        sent.append(row["id"])
    monkeypatch.setattr(tg.bot, "send_manual", fake)
    monkeypatch.setattr(tg.bot.instagram, "publish", lambda *a: pytest.fail("Instagram'a gönderilmemeli"))
    run(tg.bot.job_publish(SimpleNamespace(application=tg.admin)))
    assert sent == [cid] and db.get(cid)["status"] == db.MANUAL


def test_send_manual_photo_post(tg, tmp_path, monkeypatch):
    onboard(tg)
    img = tmp_path / "p.jpg"
    img.write_bytes(b"jpg")
    monkeypatch.setattr(tg.bot.produce, "media_files", lambda m: [img])
    cid = db.add_content("demo", "m", "2026-01-01T10:00:00", "post", {"theme": "x"})
    db.update(cid, caption="Açıklama <b>")
    tg.tr.clear()
    run(tg.bot.send_manual(tg.admin, db.get(cid)))
    texts = tg.tr.texts(f"müşteri→{OWNER}")
    assert any("Gönderi" in t for t in texts) and any("Açıklama &lt;b&gt;" in t for t in texts)
    assert tg.tr.buttons(f"müşteri→{OWNER}")[0].callback_data == f"done:{cid}"


def test_admin_list_shows_statuses(tg):
    onboard(tg)
    now = date.today()
    pub = db.add_content("demo", now.strftime("%Y-%m"), now.isoformat() + "T09:00:00", "post", {"theme": "Tur"})
    db.update(pub, status=db.PUBLISHED, permalink="https://instagram.com/p/Y")
    bad = db.add_content("demo", now.strftime("%Y-%m"), now.isoformat() + "T15:00:00", "story", {"theme": "Hata"})
    db.update(bad, status=db.FAILED, error="Graph API 500")
    tg.tr.clear()
    press(tg, tg.admin, ADMIN, "adm:list:demo")
    text = tg.tr.last("yönetici←")
    assert "→ paylaşıldı" in text and "https://instagram.com/p/Y" in text
    assert "→ hata" in text and "Graph API 500" in text                # yönetici hata nedenini görür
    say(tg, tg.admin, ADMIN, "/durum demo", tg.bot.cmd_durum)
    assert "Paylaşılan (toplam 1)" in tg.tr.last("yönetici←") and "https://instagram.com/p/Y" in tg.tr.last("yönetici←")


def test_admin_commands_take_firm_name(tg, monkeypatch):
    bot = tg.bot
    assert bot.firm_args(["Demo", "VIP", "hafta"]) == ["demo", "hafta"]           # ad (çok kelime) → kısa ad
    assert bot.firm_args(["demoviptransfer"]) == ["demo"]                         # Instagram adı
    assert bot.firm_args(["hafta"]) == ["hafta"] and bot.firm_args(["40"]) == ["40"]
    now = date.today()
    cid = db.add_content("demo", now.strftime("%Y-%m"), now.isoformat() + "T23:00:00", "post", {"theme": "Tur"})
    db.update(cid, status=db.APPROVED)
    monkeypatch.setattr(bot, "slugs", lambda: ["demo", "beta"])                  # birden çok firma
    plan = bot.firm_cmd("plan", bot.cmd_plan)
    tg.tr.clear()
    say(tg, tg.admin, ADMIN, "/plan", plan)                                       # firma yazılmadı → sorar
    assert "hangi firma" in tg.tr.last("yönetici←")
    assert "pick:plan:demo" in [b.callback_data for b in tg.tr.buttons("yönetici←")]
    press(tg, tg.admin, ADMIN, "pick:plan:demo")
    assert f"#{cid}" in tg.tr.last("yönetici←") and "onaylandı" in tg.tr.last("yönetici←")
    say(tg, tg.admin, ADMIN, "/plan demo vip", plan)                              # firma adıyla doğrudan
    assert tg.tr.last("yönetici←").startswith("demo ·")


def test_admin_starts_message_to_owner(tg):
    onboard(tg)
    tg.tr.clear()
    assert "adm:msg:demo" in [b.callback_data for row in tg.bot.firm_buttons("demo").inline_keyboard for b in row]
    u, ctx = tg.update(tg.admin, ADMIN, data="adm:msg:demo")
    run(tg.bot.on_button(u, ctx))
    assert "ne yazayım" in tg.tr.last("yönetici←") and ctx.chat_data["relay"] == "demo"
    u2, ctx2 = tg.update(tg.admin, ADMIN, text="Yarın 10'da arayabilir miyim?")
    ctx2.chat_data.update(ctx.chat_data)                               # sahte ctx her güncellemede yeni
    run(tg.bot.on_text(u2, ctx2))
    assert tg.tr.texts(f"müşteri→{OWNER}")[-1] == "💬 Yetkiliniz: Yarın 10'da arayabilir miyim?"
    assert tg.tr.buttons(f"müşteri→{OWNER}")[-1].callback_data == "sp:0:ask"     # cevap: 💬 Yetkiliye yaz
    say(tg, tg.admin, ADMIN, "/mesaj demo Teşekkürler", tg.bot.cmd_mesaj)
    assert tg.tr.texts(f"müşteri→{OWNER}")[-1] == "💬 Yetkiliniz: Teşekkürler"


def test_first_week_after_instagram(tg, monkeypatch):
    onboard(tg)
    press(tg, tg.admin, ADMIN, "adm:trial:demo")
    say(tg, tg.client, OWNER, "/yardim", tg.bot.c_help)              # durum ekranında da buton var
    assert "İlk hafta" in tg.tr.texts("sahip←")[-2] and tg.tr.last("sahip←") == "İlk haftanızı başlatmaya hazır mısınız?"
    monkeypatch.setattr(tg.bot.db, "ig_account", lambda slug: {"client": slug})
    monkeypatch.setattr(asyncio, "get_running_loop", lambda: SimpleNamespace(create_task=lambda c: c.close()))
    run(tg.bot.on_ig_connected("demo", "demoviptransfer"))
    assert tg.tr.texts(f"müşteri→{OWNER}")[-1].startswith("🚀 Her şey hazır")
    assert tg.tr.buttons(f"müşteri→{OWNER}")[-1].callback_data == "fw:0:go"
    start, end = tg.bot.first_week_range("demo")
    assert start == date.today() + timedelta(days=1) and end == billing.trial_until("demo")
    db.set_setting("demo", "await_start", "")                         # başlatıldıysa buton tekrar çıkmaz
    run(tg.bot.on_ig_connected("demo", "demoviptransfer"))
    assert not tg.tr.texts(f"müşteri→{OWNER}")[-1].startswith("🚀")


def test_customer_writes_to_admin(tg):
    """Paketli müşteri 💬 Yetkiliye yaz → yöneticiye gelir; yönetici Yanıtla → firmanın botundan cevap."""
    onboard(tg)
    tg.tr.clear()
    say(tg, tg.client, OWNER, "💬 Yetkiliye yaz", tg.bot.on_text)    # menü butonu
    assert "ne iletmemi istersiniz" in tg.tr.last("sahip←")
    say(tg, tg.client, OWNER, "Premium pakete geçmek istiyorum, fiyat nedir?", tg.bot.on_text)
    msg = tg.tr.texts(f"yönetim→{ADMIN}")[-1]
    assert msg.startswith("📨 Müşteri mesajı · Demo VIP Transfer (demo)") and "Premium pakete" in msg
    assert "ilettim" in tg.tr.last("sahip←") and not db.get_setting("demo", "support_wait")
    u, ctx = tg.update(tg.admin, ADMIN, text="Premium aylık 3.000 TL, hemen geçirebilirim.")
    u.message.reply_to_message = SimpleNamespace(message_id=tg.admin.bot.n)
    run(tg.bot.on_text(u, ctx))
    assert tg.tr.texts(f"müşteri→{OWNER}")[-1] == "💬 Yetkiliniz: Premium aylık 3.000 TL, hemen geçirebilirim."
    say(tg, tg.client, OWNER, "/yonetici teşekkürler", tg.bot.c_yonetici)    # komutla tek seferde
    assert "teşekkürler" in tg.tr.texts(f"yönetim→{ADMIN}")[-1]
    say(tg, tg.client, 3003, "/yonetici selam", tg.bot.c_yonetici)    # yabancı yazamaz
    assert "selam" not in tg.tr.texts(f"yönetim→{ADMIN}")[-1]
    say(tg, tg.client, OWNER, "anlamadığım bir şey", tg.bot.on_text)  # anlaşılmayan mesajda da seçenek
    assert tg.tr.buttons("sahip←")[-1].callback_data == "sp:0:ask"


def test_trial_end_to_paid(tg):
    """Deneme biter → firma sahibine fiyatlı paketler → askıdayken de seçebilir → yönetici atar + ödendi → açık."""
    from telegram.ext import ApplicationHandlerStop

    def via_gate(user, text=None, data=None, handler=None):
        u, ctx = tg.update(tg.client, user, text=text or "", data=data)
        try:
            run(tg.bot.gate(u, ctx))
        except ApplicationHandlerStop:
            return False
        if handler:
            run(handler(u, ctx))
        return True

    onboard(tg)
    press(tg, tg.admin, ADMIN, "adm:trial:demo")
    tg.tr.clear()
    say(tg, tg.admin, ADMIN, "/deneme demo bitir", tg.bot.cmd_deneme)
    assert not tg.bot.service_active("demo")
    offer = tg.tr.texts(f"müşteri→{OWNER}")[-1]
    assert offer.startswith("🎁 Ücretsiz deneme süreniz sona erdi") and "6.000 TL/ay" in offer and "3.500 TL/ay" in offer
    labels = [b.text for b in tg.tr.buttons(f"müşteri→{OWNER}")]
    assert any("Premium" in t and "6.000 TL/ay" in t for t in labels)
    assert not via_gate(OWNER, text="📅 Planım")                     # askıda: içerik işleri kapalı
    assert "📦 Paketim" in tg.tr.last("sahip←")
    assert via_gate(OWNER, text="📦 Paketim")                         # paket ve yetkili açık
    assert via_gate(OWNER, data="pk:0:premium", handler=tg.bot.on_button)
    assert any("Premium (6.000 TL/ay) paketini istiyor" in t for t in tg.tr.texts(f"yönetim→{ADMIN}"))
    assert not via_gate(OWNER, data="pk:0:deneme", handler=tg.bot.on_button) or \
        not any("Deneme" in t and "istiyor" in t for t in tg.tr.texts(f"yönetim→{ADMIN}"))   # deneme seçilemez
    press(tg, tg.admin, ADMIN, "adm:setpkg:demo:premium")
    assert not tg.bot.service_active("demo")                          # ödeme gelene kadar kapalı
    press(tg, tg.admin, ADMIN, "adm:pay:demo")
    assert tg.bot.service_active("demo") and billing.package_of("demo")["name"] == "Premium"
    assert not billing.trial_range("demo")
    assert any("Ödemeniz alındı" in t for t in tg.tr.texts(f"müşteri→{OWNER}"))
