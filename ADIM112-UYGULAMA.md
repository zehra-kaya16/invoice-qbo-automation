# Adım 112 — Şirket kapsamında kalıcı analitik

Esas alınan proje `invoice-qbo-automation-main (1)(10).zip`; referans `receipt-ai-main(20261017-144306).zip`.
Referanstaki `app/api/analytics.py` ve `app/services/analytics/tracker.py` şirket kapsamı, kalıcı kayıt ve mevcut workflow modeline uyarlandı. İnceleme gönderilen referans ZIP'i içindir; GitHub main'in daha sonraki sürümleri doğrulanmış sayılmaz.

## 1. Dosyaları uygula

Sunucuyu, extraction worker'ı ve varsa scheduler'ı durdur. ZIP'teki **app**, **tests** ve **migrations** klasörlerini projenin köküne aynı dosya yollarını koruyarak kopyala.

| Dosya | Değişiklik |
|---|---|
| `app/models/database.py` | Şirket/tarih indeksli, olay kimliği benzersiz `AnalyticsEvent` modeli. |
| `migrations/versions/20261017_1500_112a1d0c2026_company_analytics.py` | Kalıcı analitik tablosu; önceki head `107a1d0c2026` üzerine. |
| `app/services/analytics/__init__.py` | Yeni analitik paketinin başlangıcı. |
| `app/services/analytics/tracker.py` | Yeni: commit edilen workflow değişimlerinden olay üretme; API sürelerini kaydetme. |
| `app/api/analytics.py` | Yeni: şirket kapsamında summary/stats/counters/timers; yöneticiye özel olay sıfırlama. |
| `app/services/persistence/repository.py` | Belge değişikliğiyle analitik olayını aynı DB transaction'ında kaydetme. |
| `app/api/batch.py` | Tekrarlanan batch isteğinde belge yükleme olayını çoğaltmama. |
| `app/api/documents.py` | Gerçek extraction denemesinin süresini ölçme. |
| `app/core/auth.py` | Yetkilendirilen şirket bilgisini istek ölçümüne taşıma. |
| `app/main.py` | Analitik route'ları; yetkili API yanıt süreleri ve durum kodları. |
| `app/api/health.py` | `/ready` kontrolüne yeni analitik şemasını ekleme. |
| `app/templates/index.html` | “Şirket istatistikleri” bölümü. |
| `app/static/app.js` | Dönem seçimi, güncel belge sayıları, olay ve süre tabloları. |
| `app/static/app.css` | İstatistik kartları ve mobil tablo görünümü. |
| `tests/test_company_analytics.py` | Yeni: 27 analitik testi. |
| `tests/test_batch_upload.py` | Migration testinin beklediği head'i 112'ye güncelleme. |
| `tests/test_active_storage.py` | Windows symlink oluşturma yetkisi yoksa ilgili testi gerekçeyle atlama. |

## 2. Migration ve testler

VS Code'da projenin kökünde, etkin `(venv)` terminalinde:

```powershell
python -m alembic upgrade head
python -m alembic current
```

Beklenen head:

```text
112a1d0c2026 (head)
```

Windows'ta önceki adımda kullandığın kısa geçici test yolu ile:

```powershell
python -m pytest -q tests/test_company_analytics.py --basetemp=C:\pt
python -m pytest -q --basetemp=C:\pt
```

Yeni dosya için beklenen **27 passed**. Hazırlanan paketin Linux ortamındaki tam sonucu **613 passed**.
Windows symlink hakkı yoksa mevcut depolama testlerinden yalnız ilgili test gerekçeli olarak atlanabilir. `C:\pt` yalnız pytest'in geçici test alanı olmalı; pytest bu alanı her çalıştırmada temizler.

Sunucuyu başlat:

```powershell
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## 3. İstatistik ekranını kontrol et

1. Chrome'da Ctrl+F5 yap ve giriş yap.
2. Ana sayfada **Şirket istatistikleri** bölümünü aç. Bölüm, yeni belge yükleme alanının üzerindedir.
3. **İstatistikleri yenile** düğmesine bas.
4. **Toplam belge** sayısını kaydet: buna N diyelim.
5. **Mevcut belge durumları** ve **Belge türleri** tablolarındaki sayıların toplamı N olmalı.
6. **Ölçüm dönemi** listesinden **Son 1 saat**, **Son 24 saat**, **Son 7 gün** seçeneklerini dene.

İki ayrı kapsam gösterilir:

- **Mevcut belge durumları:** şirketin bütün kayıtlarının şu anki durumu; dönem filtresinden bağımsızdır. Önceden yüklenmiş belgeler burada görünür.
- **Olaylar ve süreler:** yeni analitik tablosu devreye girdikten sonra kaydedilen, seçilen döneme ait ölçümler. Eski extraction/QBO süreleri üretilmez. Veri yoksa “Ölçüm yok” görünür.

Bütün ölçümler yeni ise 1/24/168 saat seçenekleri aynı olay sayılarını verebilir. API istek sayısı, sayfa ve belge yenilendikçe artabilir; belge sayısı ile aynı şey değildir.

## 4. Yeni yükleme/extraction sayacını kontrol et

1. Daha önce başarıyla kullandığın geçerli test belgelerinden **birini** yeni yükleme olarak seç.
2. İstatistik bölümünde **İstatistikleri yenile** düğmesine bas.
3. **Toplam belge** N+1 olmalı; seçili dönemdeki **Yüklenen belge** sayacı bir artmalı.
4. Yeni belgeyi açıp **Verileri çıkar** düğmesine bas. Tamamlanmasını bekle.
5. İstatistikleri tekrar yenile: **Başlayan extraction** ve başarılı işlem için **Başarılı extraction** sayaçları bir artmalı.
6. **Ölçülen süreler** tablosunda extraction ölçümü görünmeli.
7. Yalnız istatistikleri veya sayfayı yeniden yenile. Yüklenen belge ve başarılı extraction sayıları tekrar artmamalı.

Extraction başarı oranı, ölçülen başarılı denemelerin başarılı+başarısız denemelere oranıdır. Yeniden denemeler ayrı denemedir. Bu oran, belgenin muhasebe doğrulamasının geçtiği veya QBO aktarımının tamamlandığı anlamına gelmez.

AI tarafından bildirilen güven, çıkarılan `confidence` değeridir; test edilmiş doğruluk yüzdesi değildir. Eksik güven değeri sıfır olarak uydurulmaz.

Bu kontrol için aynı test belgesinden yeniden Bill/Expense oluşturma.

## 5. Yeniden başlatma kontrolü

1. Toplam belge, yüklenen belge ve extraction sayaçlarını not al.
2. Sunucuyu durdurup aynı komutla yeniden başlat.
3. Giriş yapıp **Şirket istatistikleri → İstatistikleri yenile** seç.
4. Aynı dönem içindeki belge/extraction ölçümleri ve mevcut belge sayıları korunmalı. Bir ölçüm dönem sınırını geçtiyse kısa dönemden çıkması normaldir.
5. API istek sayısı yeni sayfa isteklerinden dolayı artabilir; bunu mükerrer belge olarak değerlendirme.

## 6. Şirket ayrımı

1. İlk şirketin toplam belge ve olay sayılarını not al.
2. Diğer şirketin hesabıyla ayrı bir Chrome profili/gizli pencerede giriş yap.
3. **Şirket istatistikleri** bölümünü aç.
4. Ekranın başlığında o şirketin adı görünmeli; yalnız o şirketin belgeleri ve ölçümleri bulunmalı.
5. İlk şirkette yeni belge yükleyip ikinci şirketin istatistiklerini yenile: ikinci şirketin yüklenen belge/toplam belge sayısı bu yüklemeden etkilenmemeli.

Aynı şirketin standart kullanıcısı istatistikleri okuyabilir. Başka şirket kapsamı istenirse API 403 döndürür. Analitik sıfırlama işlemi ayrıca şirket yöneticisi ve geçerli CSRF gerektirir; normal kullanım için sıfırlama yapman gerekmez.

## 7. QBO sonuçlarının anlamı

| Ölçüm | Ne sayılır? |
|---|---|
| Finansal sonucu doğrulanan belge | Mevcut kalıcı kayıtta `qbo_push_state=succeeded` olan belgeler. Ekin tamamlandığını tek başına ifade etmez. |
| Finansal sonucu belirsiz belge | Mevcut kayıtta `qbo_push_state=uncertain` olan belgeler. |
| Doğrulanan Bill/Expense sonucu | 112 sonrası başarı durumu doğrulanan finansal sonuç. Toparlanmayla doğrulanan eski girişim de bu olayı oluşturabilir; yeni finansal işlem yaratıldığı anlamına gelmez. |
| Doğrulanan kategori yazımı | Kategori checkpoint'inin `pushed` durumuna geçtiği kayıt. |
| Doğrulanan kaynak/çek eki | Kaynak ek ID'si alındığında veya çek eki `uploaded` durumuna geçtiğinde. |
| Tamamlanan ekstre/çek inceleme akışı | Banka ekstresi, bağımsız çek veya kredi kartı ekstresinin `pushed` durumuna geçmesi. Bu akış yeni Bill/Expense yaratmaz; reddedilen satırlar aktarılmaz. |
| Belirsiz sonuç | Belirsiz finansal/kategori/ek checkpoint'i; başarı gibi sayılmaz. |

Aynı belgeye ait aktarımın tekrar kontrolü, önceden doğrulanmış sonucu yeniden saymaz. Sayaçlar QBO'nun tamamındaki işlem sayısı değil, bu uygulamanın kaydettiği workflow olaylarıdır.

## 8. API kontrolü

Giriş yaptığın uygulamanın aynı adresinde aşağıdaki yolları açabilirsin:

```text
/api/analytics/summary?hours=24
/api/analytics/counters?hours=24
/api/analytics/timers?hours=24
/api/analytics/stats?hours=24
/api/analytics/stats?metric=document.size_bytes&hours=24
/api/analytics/stats?metric=extraction.confidence&hours=24
```

`hours` 1–168 aralığındadır. Analitik okumaları kaynak dosyalarını indirmez ve QBO'ya bağlanmaz; tüm hesaplamalar şirket filtresiyle SQL üzerinden yapılır.

API istek ölçümü yetkilendirilmiş `/api/documents`, `/api/batch`, `/api/qbo` isteklerini kapsar. Login, statik dosya, health ve analitik ekranının kendi yenilemeleri bu sayacı artırmaz. API telemetrisi yazılamazsa tamamlanmış iş yanıtı hataya çevrilmez; sunucu loguna genel uyarı yazılır, o istek ölçümü eksik kalabilir. Bu sayaç faturalama kaydı değildir.

Süreler milisaniyedir. P50/P95, SQL ile tüm ilgili ölçümlerden nearest-rank yöntemiyle hesaplanır. Extraction süresi gerçek işlem akışından ölçülür. Bill/Expense sonuç süresi başlangıç checkpoint'i ile sonuç doğrulaması arasındadır; toparlanma bekleme süresini de içerebilir.

Referanstaki `POST /api/analytics/reset` yolu şirket yöneticisine uyarlanmıştır. Yalnız o şirketin analitik olaylarını temizler; mevcut belge/QBO kayıtları ve bunlardan hesaplanan envanter korunur. Sıfırlanan geçmiş tekrar üretilmez. Arayüzde normal akışın parçası olarak sıfırlama düğmesi eklenmemiştir.

## Doğrulama kaydı

- Yeni analitik dosyası: **27 test geçti**.
- Tam test paketi: **613 test geçti**.
- Tarayıcı: **12 kontrol grubu geçti**; gerçek oturum/CSRF, yükleme, extraction, kategori/ek sayaçları, dönem seçimi, gerçek süreç yeniden başlatma, şirket ayrımı ve mobil görünüm dahil.
- AI/QBO otomatik kontrollerde taklit edildi; SQLite, dosya deposu, oturum ve migration işlemleri gerçek yerel bileşenlerle test edildi. Canlı PostgreSQL/Redis/Docker dağıtımı bu adımın doğrulaması olarak sunulmaz.

112'nin tamamlanma kriteri: migration ve testler geçmeli; istatistikler yalnız oturumun şirketine ait olmalı; yeni yükleme/extraction olayları doğru sayılmalı; tekrar kontrol mükerrer iş saymamalı; saat filtresi olay ve süre ölçümlerine uygulanmalı; yeniden başlatmada kayıtlar korunmalı.

Sonraki planlanan adım **113: güvenli webhook alımı ve bildirim entegrasyonu**.
