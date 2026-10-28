"""Paket, hak, kredi, deneme ve abonelik mantığı testleri (geçici veritabanıyla; ağ/yapay zekâ çağrısı yok).

Çalıştır: uv run --with pytest pytest -q
"""

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from app import billing, db

S = "testfirma"


def spec(custom=False):
    return {"theme": "t", **({"custom": True} if custom else {})}


# ── paketler ───────────────────────────────────────────────────────────────

def test_packages_shape():
    pk = billing.packages()
    assert list(billing.sellable()) == ["baslangic", "standart", "premium"]
    assert pk["deneme"]["trial"] and "deneme" not in billing.sellable()
    caps = {k: p["credits"] * billing.credit_value_tl() for k, p in pk.items()}
    assert caps == {"baslangic": 500, "standart": 1000, "premium": 1500, "deneme": 75}
    assert [pk[k]["custom"] for k in ("baslangic", "standart", "premium", "deneme")] == [2, 4, 8, 1]
    assert [bool(pk[k]["voiceover"]) for k in ("baslangic", "standart", "premium")] == [False, False, True]
    for p in pk.values():
        for key in ("posts", "stories", "reels", "revisions", "credits", "ai_images", "custom"):
            assert isinstance(p[key], int) and p[key] >= 0, (p["name"], key)


def test_no_package_is_unlimited():
    assert billing.package_of(S) is None
    assert billing.credits_left(S) is None and billing.custom_left(S) is None
    billing.record_usd(100, S)
    billing.check_budget(S)   # paketsiz: sınır yok


# ── kredi ──────────────────────────────────────────────────────────────────

def test_credits_and_budget():
    db.set_setting(S, "package", "baslangic")
    assert billing.credits_total(S) == 1000 and billing.credits_used(S) == 0
    tl = 100
    billing.record_usd(tl / billing.usd_try(), S)
    assert billing.credits_used(S) == round(tl / billing.credit_value_tl())
    billing.check_budget(S)
    billing.record_usd(500 / billing.usd_try(), S)   # tavanı (500 TL) aş
    with pytest.raises(billing.BudgetExceeded):
        billing.check_budget(S)
    db.set_setting(S, f"bonus:{billing.month_key()}", "500")   # yönetici ek kredi verdi
    assert billing.credits_total(S) == 1500
    billing.check_budget(S)


def test_for_client_records_to_firm():
    db.set_setting(S, "package", "standart")
    with billing.for_client(S):
        billing.record_usd(1.0)
    assert billing.month_cost(S) == pytest.approx(1.0)
    assert billing.month_cost("_bot") == 0


# ── haklar ─────────────────────────────────────────────────────────────────

def test_custom_rights_reels_cost_two():
    db.set_setting(S, "package", "standart")   # 4 hak
    assert billing.custom_left(S) == 4
    billing.use_custom(S, "post")
    billing.use_custom(S, "reel")
    assert billing.custom_left(S) == 1
    billing.use_custom(S, "story")
    assert billing.custom_left(S) == 0


def test_custom_quota_full_does_not_call_ai(monkeypatch):
    from app import ai, custom
    from app.config import load_client
    called = []
    monkeypatch.setattr(ai, "structured", lambda *a, **k: called.append(1))
    db.set_setting("demo", "package", "deneme")   # 1 hak: reels (2 hak) istenemez
    with pytest.raises(custom.QuotaFull):
        custom.make_custom(load_client("demo"), "reels", "reel", limited=True)
    assert not called


def test_week_usage_ignores_custom_and_rejected():
    monday = date.today() - timedelta(days=date.today().weekday())
    at = f"{monday + timedelta(days=1)}T12:00:00"
    db.add_content(S, "m", at, "post", spec())
    db.add_content(S, "m", at, "post", spec(custom=True))
    rej = db.add_content(S, "m", at, "story", spec())
    db.update(rej, status=db.REJECTED)
    assert billing.week_usage(S, monday) == {"posts": 1, "stories": 0, "reels": 0}


def test_revisions_monthly():
    db.set_setting(S, "package", "baslangic")   # 4 revize
    for _ in range(3):
        billing.use_revision(S)
    assert billing.revisions_left(S) == 1


def test_ai_allowance():
    db.set_setting(S, "package", "baslangic")
    assert billing.allowance_left(S, "aiimage") == 4
    billing.count_use(S, "aiimage")
    assert billing.allowance_left(S, "aiimage") == 3
    assert billing.allowance_left(S, "aivideo") == 0


def test_enforce_quota():
    from app.planner import enforce_quota
    items = [SimpleNamespace(kind=k, date=f"2026-10-{d:02d}", time="12:00")
             for d, k in enumerate(["post", "carousel", "post", "story", "reel", "reel", "story_video"], start=10)]
    plan = SimpleNamespace(items=items)
    enforce_quota(plan, billing.packages()["standart"])   # 3 post, 4 story, 1 reels
    kinds = [i.kind for i in plan.items]
    assert kinds == ["post", "carousel", "post", "story", "reel"]


# ── abonelik ve deneme ─────────────────────────────────────────────────────

def test_subscription_grace_then_suspend():
    db.set_setting(S, "package", "standart")
    today = date.today()
    billing.extend(S, until=today - timedelta(days=1))
    assert billing.sub_state(S)[0]   # ek süre içinde
    billing.extend(S, until=today - timedelta(days=billing.grace_days() + 1))
    assert not billing.sub_state(S)[0]
    until = billing.extend(S, 1)
    assert until > today and billing.sub_state(S)[0]


def test_trial_lifecycle():
    today = date.today()
    tu = today + timedelta(days=6)
    db.set_setting(S, "package", "deneme")
    billing.extend(S, until=tu)
    db.set_setting(S, "trial_until", tu.isoformat())
    assert billing.sub_state(S)[0]
    assert billing.trial_range(S) == (today, tu)
    assert "Denemede" in billing.rights_text(S) and "Bu ay" not in billing.rights_text(S)
    # deneme bitti → ek süre yok, hemen durur
    past = today - timedelta(days=1)
    db.set_setting(S, "trial_until", past.isoformat())
    db.set_setting(S, "paid_until", past.isoformat())
    assert not billing.sub_state(S)[0]
    # paket atanıp ödeme alındı → süre bugünden 1 ay, deneme kaydı silinir
    db.set_setting(S, "package", "premium")
    until = billing.extend(S, 1)
    assert billing.trial_until(S) is None and billing.sub_state(S)[0]
    assert 28 <= (until - today).days <= 31


# ── yapay zekâ kuralları ───────────────────────────────────────────────────

def test_ai_rules(monkeypatch):
    from app import genai, planner
    from app.config import load_client
    monkeypatch.setattr(genai, "available", lambda: True)
    monkeypatch.setattr(genai, "elevenlabs_key", lambda: "x")
    c = load_client("demo")
    db.set_setting("demo", "package", "premium")
    rules = planner.ai_rules(c)
    assert "ZORUNLU" in rules and "voiceover: AÇIK" in rules and "ai_video: KAPALI" in rules
    db.set_setting("demo", f"aiimage:{billing.month_key()}", "15")
    assert "ai_image: KAPALI" in planner.ai_rules(c)
    db.set_setting("demo", "package", "baslangic")
    assert "voiceover: KAPALI" in planner.ai_rules(c)


def test_voice_fallback_to_openai_when_no_elevenlabs(monkeypatch):
    from app import genai
    monkeypatch.setattr(genai, "available", lambda: True)
    monkeypatch.setattr(genai, "elevenlabs_key", lambda: "")
    db.set_setting(S, "package", "premium")
    assert genai.voice_available(S)
    monkeypatch.setattr(genai, "available", lambda: False)
    assert not genai.voice_available(S)


# ── bot ────────────────────────────────────────────────────────────────────

def test_bot_menus():
    from app import bot
    labels = [t for row in bot.CLIENT_BUTTONS for t in row]
    assert len(labels) == len(set(labels)) == 9
    kb = bot.admin_package_buttons(S)
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"adm:trial:{S}" in datas and all(len(d.encode()) <= 64 for d in datas)
    db.set_setting(S, "trial_used", "1")
    datas = [b.callback_data for row in bot.admin_package_buttons(S).inline_keyboard for b in row]
    assert f"adm:trial:{S}" not in datas
    db.set_setting(S, "package", "baslangic")
    oz = [b.text for row in bot.ozel_buttons(S).inline_keyboard for b in row]
    assert oz == ["Post · 1 hak", "Story · 1 hak", "Reels · 2 hak"]


def test_periods():
    from app.planner import next_week_range, this_week_range
    wed = date(2026, 10, 14)
    assert next_week_range(wed) == (date(2026, 10, 19), date(2026, 10, 25))
    assert this_week_range(wed) == (date(2026, 10, 15), date(2026, 10, 18))
    sun = date(2026, 10, 18)
    assert this_week_range(sun) == next_week_range(sun)


# ── ses ve bakiye ──────────────────────────────────────────────────────────

def test_voice_language_and_alternation():
    from app import genai
    assert genai.voice_lang("Early start, quiet road. From Izmir to Ephesus.") == "en"
    assert genai.voice_lang("ISTANBUL AIRPORT IN STYLE") == "en"
    assert genai.voice_lang("Rezervasyon için hemen yazın") == "tr"
    first, second = genai.voice_gender(S, 1), genai.voice_gender(S, 2)
    assert {first, second} == {"erkek", "kadin"}
    assert genai.voice_gender(S, 1) == first          # aynı içerik yeniden üretilince ses değişmez
    db.set_setting(S, "voice", "kadin")
    assert genai.voice_gender(S, 3) == "kadin"        # firma sabitlediyse hep o
    db.set_setting("_bot", "el_voice_en_erkek", "Q")
    db.set_setting("_bot", "el_voice_erkek", "F")
    assert genai.el_voice_id("en", "erkek") == "Q" and genai.el_voice_id("tr", "erkek") == "F"


def test_wallet_balance_and_alerts():
    from app import wallet
    assert wallet.balance("openai") is None
    wallet.set_balance("openai", 3.0)
    billing.record_usd(0.5, S, provider="openai")
    assert wallet.balance("openai") == pytest.approx(2.5)
    assert wallet.low_alerts() == []
    billing.record_usd(0.6, S, provider="openai")
    alerts = wallet.low_alerts()
    assert len(alerts) == 1 and "OpenAI" in alerts[0]
    assert wallet.low_alerts() == []                  # aynı düşüş için bir kez
    wallet.mark_empty("claude")
    assert wallet.balance("claude") == 0
    billing.record_usd(0.01, S, provider="elevenlabs", units=500)
    assert wallet.spent_month("elevenlabs") == 500
    rep = wallet.report(50)
    assert "Sunucu" in rep and "ElevenLabs" in rep and "kur 50" in rep


def test_usd_rate_is_50():
    assert billing.usd_try() == 50


def test_owner_firm_becomes_normal():
    from app import bot
    db.set_setting(S, "full", "1")
    bot.to_normal_firm(S)
    assert not bot.full_access(S)
    assert billing.package_of(S)["name"] == "Premium"
    assert db.get_setting(S, f"month_ok:{billing.month_key()}") == "1"
    assert billing.sub_state(S)[0]
    db.set_setting(S, "package", "standart")       # sonradan elle değiştirilirse tekrar ezilmez
    bot.to_normal_firm(S)
    assert billing.package_of(S)["name"] == "Standart"


def test_setup_costs_do_not_eat_trial_credits(monkeypatch):
    """Ücretsiz örnek ve tasarım maliyeti firmanın deneme/paket kredisinden düşmez."""
    from app import wallet
    monkeypatch.setattr(wallet, "spend", lambda *a, **k: None)
    db.set_setting(S, "package", "deneme")
    with billing.for_setup(S):
        billing.record_usd(5.0)                                       # pahalı örnek + tasarım
        billing.check_budget()                                        # kurulum hesabı sınırsız
    assert billing.month_cost(S) == 0 and billing.credits_used(S) == 0
    assert billing.setup_cost() == 5.0
    billing.check_budget(S)                                           # firmanın denemesi hâlâ açık
    with billing.for_client(S):
        billing.record_usd(5.0)
    with pytest.raises(billing.BudgetExceeded):
        billing.check_budget(S)
