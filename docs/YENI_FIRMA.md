# Yeni firma ekleme (müşteri botuyla)

Her firmanın kendi Telegram botu olur. Firma sahibi içerikleri o bottan onaylar; sen her şeyi yönetim
botundan izlersin. Tüm firmalar aynı sunucuda çalışır.

## 1. Bot aç (BotFather, 1 dk)

Telegram'da @BotFather → `/newbot` → isim: `Örnek Otel Sosyal Medya` → kullanıcı adı: `ornekotel_sosyal_bot`
→ verilen **token**'ı kopyala. (Telegram bot oluşturmayı programla yaptırmıyor; bu adım elle.)

İstersen BotFather'da `/setuserpic` ile firmanın logosunu bota profil fotoğrafı yap.

## 2. Firmayı ekle (yönetim botu)

```
/yenifirma ornek-otel ornekotel.com ornekotel <bot-token>
```

- Claude firmanın sitesini okuyup profilini çıkarır (hizmetler, iletişim, renkler, logo, hashtag'ler).
- Token mesajın silinir, müşteri botu başlatılır ve sana **davet linki** gelir.
- Bot sonradan bağlanacaksa: `/musteribot ornek-otel <bot-token>` · linki tekrar görmek: `/davet ornek-otel`

## 3. Instagram için (uygulama geliştirme modundayken)

developers.facebook.com → uygulaman → **App roles → Roles → Instagram Testers → Add** → firmanın Instagram
kullanıcı adı. Firma sahibi daveti kabul eder: instagram.com/accounts/manage_access → **Tester davetleri**.

Tek seferlik kurulum (ilk müşteri öncesi): `/metaapp <Instagram app ID> <Instagram app secret>` ve Meta
panelinde *Use cases → Instagram → API setup with Instagram login → Set up Instagram business login →
OAuth redirect URI* alanına `https://<alan-adın>/ig/callback` eklenir.

> Çok sayıda firmada tester eklemekle uğraşmamak için Meta'ya App Review başvurusu yapılıp uygulama
> canlıya alınabilir (işletme doğrulaması gerekir). O zaman tester adımı kalkar.
>
> Instagram bağlanamazsa deneme/plan beklemez: içerikler saatinde firma sahibine elle paylaşım için gönderilir.

## 4. Davet linkini firma sahibine gönder

Firma sahibi linke dokunup botu başlatınca bot:
1. kendini tanıtır ve nasıl çalıştığını anlatır,
2. fotoğraf ister (düzenli göndermesini önerir),
3. sana (yönetim botu) “hangi paketle başlasın?” diye sorar → paketi **sen** seçersin
   (Başlangıç / Standart / Premium — `config/packages.yaml`), ya da **🎁 1 hafta ücretsiz deneme** (3 post, 3 story, 1 seslendirmeli reels, 2 revize, tavan 75 TL; firma başına bir kez). Deneme bitmeden bir gün önce paket sorulur; seçip **💳 Ödendi** demezsen hizmet durur,
4. firma sahibine **Instagram'ı tek linkle** bağlatır,
5. paket atanınca ilk haftanın içeriklerini hemen hazırlar.

Her ayın 25'inde her firma için “gelecek ay devam?” diye sorulur (✅ Devam / 📦 Paket değiştir / ⛔ Askıya al);
kredi biterse ek kredi isteyip istemediğin sorulur. Premium pakette reels'ler ElevenLabs sesiyle seslendirilir
(`/ses firma erkek|kadin`).

Sonra her **Pazar** gelecek haftanın içerikleri firma sahibine gelir. Paket sınırları (haftalık post/story/reels
adedi) otomatik uygulanır; haftalık haklar Pazartesi, revize ve krediler ay başında yenilenir.

## Paketler ve maliyet

- Paketleri düzenle: `config/packages.yaml` (adetler, revize hakkı, fiyat yazısı) · gör/ata: `/paket`
- Maliyet: `/maliyet` → firma başına bu ayın yapay zekâ maliyeti (TL)
- Bütçe tavanı: `/butce 1000` (firma başı aylık TL) · kur: `/kur 42`
- Bir firmanın aylık maliyeti tavana ulaşırsa o firmanın yapay zekâ işlemleri durur, sana haber gelir.
- Tahmini: haftalık planlı bir firma ayda ~150–300 TL (plan + araştırma + revizeler + fotoğraf kataloğu).
  Sunucu (6 $/ay) tüm firmalar için ortak.

## Satış botu — firmalar kendiliğinden gelir

Instagram bio'sundaki linkten gelen işletme sahibini tek ortak bot karşılar; firmaya ayrı BotFather botu gerekmez.

1. BotFather'dan satış botunu aç → yönetim botuna: `/satisbotu <token>` (mesaj silinir).
2. Ziyaretçi web sitesini ya da @instagram adını yazar → firma profili pasif çıkarılır, **örnek post** üretilip
   gönderilir (yayınlanmaz). Sana “🧲 Yeni aday” bildirimi gelir.
3. Ziyaretçi **🎁 1 hafta ücretsiz dene** derse sana “✅ Denemeyi başlat / ✖️ Reddet” sorulur. Onaylarsan firma
   aktifleşir, deneme başlar ve firma sahibi aynı botta müşteri olarak devam eder (menü, onay, fotoğraf…).
4. Adayların soruları sana iletilir; cevap: `/aday <kısa-ad> <mesaj>` · son adaylar: `/aday`.

Sınırlar: kişi başı 1 örnek, işletme başı 1 örnek, günde en fazla 10 örnek (`/satisbotu sinir 15`).
Her örnek birkaç TL yapay zekâ maliyeti tutar (firma araştırması + post); `/maliyet`'te aday adıyla görünür.

## Firmaya özel tasarım

Her firmanın görselleri kendine has olur: tasarımcı (Claude) firmanın sitesine, logosuna, Instagram'ına bakıp
tasarım dili kurar, Google Fonts'tan Türkçe destekli yazı tiplerini seçer ve tüm şablonları (post, liste,
kaydırmalı, story, reels katmanları) sıfırdan yazar. Örnekler basılıp otomatik (taşma/kırpılma) ve görsel olarak
kontrol edilir; bozuk kalan şablon yerine o iş genel tasarımla basılır. Paket: `clients/<firma>/design/`.

- Satış botundaki ücretsiz örnek: hafif sürüm (sadece post şablonları, görsel incelemesiz).
- Deneme başlayınca: tasarım tamamlanır, firma sahibine 3 örnek gider → **✅ Beğendim** ya da **✏️ Değiştir**
  (ne istediğini yazar: “daha sade”, “logo sağda”). Kurulumda 3 ücretsiz değişiklik. Onaydan sonra soru turu.
- Firma sahibi sonra da `/tasarim` ile görüp değiştirebilir.
- Yönetici: `/tasarim <firma>` önizleme · `yenile` sıfırdan üret · `gonder` müşteriye gönder · `onayla`.
- Komut satırı: `uv run python -m app.design --client <firma>` · `--revise "istek"` · `--preview`
- Maliyet: tam paket firma başı bir kez ~1-3 $, her değişiklik ~0,3-0,5 $; örnek (hafif) ~0,5-1 $.
- Özel tasarımı olmayan firma genel şablonlarla devam eder.

## Güvenlik önlemleri

- Firma tasarım şablonlarını yapay zekâ yazar ve firmanın sitesini okur (istem enjeksiyonu riski). Bu yüzden:
  şablonlar korumalı (sandbox) Jinja ortamında çalışır; script, iframe, harici adres, `file:` erişimi, `|safe`,
  dış şablon dahil etme yasaktır (`app/htmlsafe.py`). Tarama hem kayıtta hem her basımdan önce yapılır. Görsel
  basılırken JavaScript kapalıdır ve internete istek çıkmaz. Siteden gelen SVG logolar da aynı taramadan geçer.
- Müşterinin/yapay zekânın verdiği adresler (müzik linki, logo, site görüntüsü) iç ağa (127.0.0.1, 10.x,
  169.254.169.254 …) gidemez, indirme boyutu sınırlıdır (`app/netsafe.py`).
- Telegram buton verisi uydurulabilir: her buton işleyicisi kişiyi ve firmayı yeniden doğrular; yönetici işlemleri
  sadece yönetici hesabından, sadece yönetim botunda çalışır.
- 🚀 İlk haftamı oluştur bir kez kullanılır; tasarım işleri firma başına aynı anda bir tane çalışır; ziyaretçi
  mesajları yöneticiye saatte en fazla 8 iletilir; ücretsiz örnek kişi/işletme başına bir, günlük sınırlı.
- Testler: `uv run --with pytest pytest -q` (tests/test_security.py saldırı senaryolarını dener).
