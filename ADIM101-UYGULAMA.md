# Adım 101 — Kalıcılık düzeltmeleri ve son uçtan uca doğrulama

Bu paket gönderdiğin `invoice-qbo-automation-main (1)(4).zip` üzerine hazırlanmıştır. Referans kapsamı önceki `receipt-ai-main(6).zip` üzerinden denetlenmiştir; canlı GitHub main içeriği doğrulanamamıştır. Ayrıntılı eksik dosya ve özellik envanteri `ADIM101-KAPSAM-DENETIMI.md` içindedir.

## 1. Dosyaları uygula

Uvicorn'u durdur. Dosyaları proje köküne aynı klasör yapısıyla kopyala; klasörleri mevcut klasörlerle birleştir ve aynı dosyaların üzerine yaz.

| Dosya | İşlem |
| --- | --- |
| .dockerignore | Mevcut dosyayı değiştir |
| .env.example | Mevcut dosyayı değiştir |
| ADIM101-KAPSAM-DENETIMI.md | Yeni dosyayı ekle |
| Dockerfile | Mevcut dosyayı değiştir |
| README.md | Mevcut dosyayı değiştir |
| app/api/documents.py | Mevcut dosyayı değiştir |
| app/api/health.py | Mevcut dosyayı değiştir |
| app/api/recovery.py | Mevcut dosyayı değiştir |
| app/core/config.py | Mevcut dosyayı değiştir |
| app/services/extraction/extractor.py | Mevcut dosyayı değiştir |
| app/services/persistence/check_sources.py | Yeni dosyayı ekle |
| app/services/persistence/repository.py | Mevcut dosyayı değiştir |
| app/static/app.js | Mevcut dosyayı değiştir |
| cli.py | Mevcut dosyayı değiştir |
| docker-compose.yml | Mevcut dosyayı değiştir |
| docker-entrypoint.sh | Yeni dosyayı ekle |
| manage_check_sources.py | Yeni dosyayı ekle |
| manage_extraction.py | Yeni dosyayı ekle |
| tests/test_release_flow.py | Yeni dosyayı ekle |

## 2. `.env` ayarlarını ekle

Mevcut `.env` dosyanda şu ayarları kullan:

```dotenv
MAX_UPLOAD_BYTES=20971520
MAX_PDF_PAGES=50
AI_PROVIDER=openai
AI_MODEL=gpt-4o
```

OpenAI kullandığın mevcut akış için bunlar uygundur. Anthropic seçilecekse `AI_PROVIDER=anthropic`, `AI_MODEL=claude-sonnet-4-6` ve `ANTHROPIC_API_KEY` kullanılır. Model adı seçilen sağlayıcıyla uyumlu olmalıdır. İki anahtar mevcutsa açıkça seçilen sağlayıcı kullanılır. Ayar değişince sunucuyu yeniden başlat.

## 3. Eski çek görsellerini veritabanına al

Sunucu durdurulmuşken, proje kökünde ve venv etkin durumdayken:

```powershell
python manage_check_sources.py
python manage_check_sources.py --apply
```

İlk komut yalnız raporlar. İkinci komut mevcut çek dosyalarının baytlarını ve SHA256 değerlerini belge kaydına alır. QBO'ya istek göndermez; mevcut QBO ID, inceleme ve yazım checkpoint'lerini korur.

Çıktıda `missing_or_invalid: 0` olmalı. Hiç çekli belge yoksa `checks: 0` geçerlidir. İkinci kez `--apply` çalıştırıldığında `new_copies: 0` beklenir.

Eksik/bozuk kaynak bildirilirse ilgili belge ID'si raporda görünür. Kaybolmuş görüntü bu komutla geri yaratılmaz; özgün çek dosyasını kendi yedeğinden geri koyup tekrar çalıştır. Kaynak eksikken QBO kilidini veya belge ID'sini sıfırlama.

## 4. Testleri ve hazır olma kontrolünü çalıştır

```powershell
python -m pytest tests/test_release_flow.py -q
python -m pytest tests -q
python -m uvicorn app.main:app --reload
```

Hazırlanan kopyada yeni testler **21 passed**, tüm testler **325 passed**. Toplam **89** tarayıcı kontrolünde JavaScript hatası görülmedi. Bunlar geçici SQLite ve sahte AI/QBO kullanır; gerçek sandbox kontrolü ayrı yapılmalıdır.

Şu adresi aç:

http://localhost:8000/ready

Beklenen: HTTP 200 ve `{"status":"ready"}`. 503 ise veritabanı erişimini ve migration durumunu kontrol et:

```powershell
python -m alembic upgrade head
python -m alembic current
```

Güncel migration head'i `100a1d0c2026` olarak kalır. `/ready`, AI/QBO'nun dış servis kullanılabilirliğini doğrulamaz.

## 5. Çek görselinin kalıcılığını gerçek uygulamada kontrol et

Bu test için yeni finansal işlem oluşturma. Mevcut çekli, incelemesi/push'ı tamamlanmış bir ekstreyi kullan.

1. Ana sayfayı Ctrl+F5 ile aç ve kendi şirket hesabınla giriş yap.
2. Çekli ekstreyi aç. **Ekstre eşleşme ve aktarım** bölümündeki çek kartında **Çek görselini aç** bağlantısını kullan; **Çek görselini indir** özgün dosya adını korur.
3. Açılan görüntünün orijinal PDF'teki çekle aynı olduğunu kontrol et.
4. Bu belgenin `uploads/checks/DOC_ID` klasörü mevcutsa silmeden geçici olarak yeniden adlandır. `DOC_ID` yerine gerçekten seçtiğin belgenin ID'sini kullan:

```powershell
Rename-Item .\uploads\checks\DOC_ID DOC_ID_101_backup
```

5. Kaynak görüntüsü sekmesini kapatıp **Çek görselini aç** bağlantısına yeniden bas. Görüntü veritabanından açılmalı.
6. Uvicorn'u durdurup yeniden başlat. Aynı belgeyi aç; çek görseli ve önceki QBO işlem/ek ID'leri korunmalı.
7. Test sonrası klasör adını geri getir:

```powershell
Rename-Item .\uploads\checks\DOC_ID_101_backup DOC_ID
```

Bu işlem yalnız veritabanına önceden alınmış çek kopyasını doğrular. `manage_check_sources.py --apply` sonucunu kontrol etmeden kaynak klasörünü değiştirme.

8. Diğer şirket hesabıyla aynı kaynağın `http://localhost:8000/api/documents/DOC_ID/checks/0/source` adresini aç: 404 dönmeli. Oturum olmadan 401 dönmeli.

## 6. Son sandbox matrisi

100. adımda doğruladığın başarılı işlemleri tekrar oluşturmadan, kayıtlarını kontrol et. Yeni pozitif push gerekiyorsa sandbox için ayırt edilebilir yeni bir test belgesi kullan; aynı fiziksel PDF'i ayrı belge ID'leriyle tekrar yüklemek global mükerrer belge koruması değildir.

| Akış | Yapılacak kontrol |
| --- | --- |
| Çok sayfalı invoice | Bütün sayfaları kaynağa karşılaştır; düzenleme/onay, tek Bill ve kaynak PDF eki; aynı belge ID'sinden tekrar gönderim engeli. |
| Receipt | Doğru vendor/gider/banka hesabı, tek Expense ve kaynak eki. |
| Çeksiz ekstre | Onaylı mevcut Purchase kategorileri; yeni Bill/Expense yok; tekrar kategori yazımı yok. |
| Çekli ekstre | Doğru eşleşme ve kategori, doğru mevcut QBO işlemindeki çek eki; görsel DB kaynağından açılmalı. |
| Toparlanma | Mevcut QBO işlem/ek ID'sini doğrula; yeni işlem veya belirsiz ek yüklemesi tekrarı olmamalı. |
| Şirket erişimi | Standart kullanıcı kendi şirket akışını tamamlar; diğer şirket ID'siyle kaynak/veri/inceleme/push/toparlanma engellenir. |
| Restart | Aynı belge, source/check bytes, onay ve QBO sonuçları korunur. |

Invoice/receipt eki hata alırsa API `success=false`, `transactions_pushed=1` ve hata listesi dönebilir. Finansal kayıt oluşturulmuştur; yeniden Bill/Expense göndermek yerine mevcut ID ile toparlanma kullan.

## 7. Ortak backend ve yarım kalan extraction kontrolü

Bir bilgisayarda extraction sürerken ikinci bilgisayar aynı backend'de listeyi yenileyebilmeli. Aynı belgenin ikinci extraction isteği 409 ile engellenmeli. Yeni extraction eski sonuç, inceleme ve çek kopyalarını temizleyip yeni sonuç toplar.

Sunucu işlem ortasında kapanmış ve belge `classifying/extracting` kalmışsa, **bütün** Uvicorn/worker süreçlerini durdur. Yalnız bu yarım kalmış belgeyi operatör olarak aç:

```powershell
python manage_extraction.py --document-id DOC_ID
python -m uvicorn app.main:app --reload
```

Belge `failed` olur; arayüzden özgün kaynağı tekrar çıkarabilirsin. Finansal yazımı başlamış belgeyi bu komutla sıfırlayamazsın. Çalışan işi zorla açmak için kullanma.

## 8. Docker kullanılıyorsa yapılacak kurulum doğrulaması

Bu ortamda Docker yok; gerçek build/PostgreSQL runtime kanıtı sunulmuyor. Docker teslimi yapılacaksa:

```powershell
docker compose config
docker compose up --build -d
docker compose ps
docker compose logs app
```

App migration'dan sonra açılmalı; `/ready` 200 dönmeli. Container'ın PostgreSQL'i yerel SQLite veritabanından ayrıdır; mevcut kullanıcı/belgeler otomatik taşınmaz. Yeni Docker kurulumunda yönetici oluştur:

```powershell
docker compose exec app python manage_users.py create --company default --company-name "Invoice QBO" --email "yonetici@example.com" --name "Proje yöneticisi" --role admin
```

Compose geliştirme içindir. HTTPS callback ve Secure cookie, DB/anahtar yedeği ve gerçek restore deneyi üretim tesliminde doğrulanmalıdır. Worker scaffold varsayılan akışın parçası değildir; kalıcı Celery işleri henüz bağlanmamıştır.

## Tamamlanma kriteri

Testler geçmeli; legacy çek kopyaları korunmalı; dosya silinince/restart sonrası çek görüntülenebilmeli; source kaybında yazım başlamamalı; partial attachment sonucu açık olmalı; extraction diğer istekleri kilitlememeli; kullanıcı/şirket ve tekrar gönderim korumaları korunmalı.

Tam upstream kapsamı halen açık modüller içerir. Eksik dosyalar ve hangi sözleşmelerin uyarlanması gerektiği kapsam raporunda listelenmiştir. Sonraki adım 102: batch ve export modüllerini gerçek kalıcı workflow/şirket erişimine bağlamak.
